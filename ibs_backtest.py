"""
IBS (Internal Bar Strength) 回测 — NQ & ES 日线
IBS = (Close - Low) / (High - Low)

Long:  IBS < entry_thresh → 次日开盘买入，IBS > exit_thresh 或超时平仓
Short: IBS > (1 - entry_thresh) → 次日开盘卖空，IBS < (1 - exit_thresh) 或超时平仓
       Regime filter：价格 < 200MA（可选）或 VIX > vix_thresh（可选）

MNQ/MES $2/$5 per point，固定 1 手
"""

import pandas as pd
import numpy as np
from pathlib import Path
import itertools

BASE = Path("/Users/congrenhan/Documents/quantrift_index_future/data")

FILES = {
    "NQ": BASE / "NQF_1d_2020-01-01_2026-06-07.csv",
    "ES": BASE / "ESF_1d_2020-01-01_2026-06-08.csv",
}
VIX_FILE = BASE / "VIX_1d_2019-01-01_2026-06-09.csv"
MULT = {"NQ": 2, "ES": 5}


def load_data(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(FILES[symbol], parse_dates=["Date"]).set_index("Date")
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df["ibs"]  = (df["Close"] - df["Low"]) / (df["High"] - df["Low"]).replace(0, np.nan)
    df["ma200"] = df["Close"].rolling(200).mean()
    df["ma50"]  = df["Close"].rolling(50).mean()
    return df


def load_vix() -> pd.Series:
    df = pd.read_csv(VIX_FILE, parse_dates=["Date"]).set_index("Date")
    col = "Close" if "Close" in df.columns else df.columns[0]
    return df[col].rename("vix")


def run_backtest(df: pd.DataFrame, vix: pd.Series,
                 entry_thresh: float = 0.20,   # IBS 入场阈值
                 exit_thresh:  float = 0.80,   # IBS 出场阈值（long 出，short 进）
                 max_bars: int = 10,
                 side: str = "long",           # "long" / "short" / "both"
                 regime: str = "none",          # "none" / "ma200" / "vix20" / "ma200_or_vix20"
                 mult: float = 2.0) -> pd.DataFrame:

    df2 = df.copy()
    df2["vix"] = vix.reindex(df2.index).ffill()

    trades = []
    in_trade = False
    direction = 0
    entry_price = 0.0
    entry_idx = 0
    bars_held = 0

    idx_arr  = df2.index
    open_arr = df2["Open"].values
    ibs_arr  = df2["ibs"].values
    ma200_arr = df2["ma200"].values
    vix_arr  = df2["vix"].values

    for i in range(1, len(df2)):   # 从 i=1 开始，用 i-1 的信号在 i 的 open 入场
        if in_trade:
            bars_held += 1
            exit_reason = None
            ibs_now = ibs_arr[i]

            if direction == 1:
                if not np.isnan(ibs_now) and ibs_now > exit_thresh:
                    exit_reason = "ibs_exit"
            else:
                if not np.isnan(ibs_now) and ibs_now < (1 - exit_thresh):
                    exit_reason = "ibs_exit"

            if bars_held >= max_bars:
                exit_reason = "timeout"

            if exit_reason:
                exit_price = open_arr[i]
                pnl = direction * (exit_price - entry_price) * mult
                trades.append({
                    "entry_time":  idx_arr[entry_idx],
                    "exit_time":   idx_arr[i],
                    "direction":   direction,
                    "entry":       entry_price,
                    "exit":        exit_price,
                    "exit_reason": exit_reason,
                    "bars":        bars_held,
                    "pnl":         pnl,
                })
                in_trade  = False
                bars_held = 0
            continue

        # 用前一日 IBS 生成信号，在今日 open 入场
        ibs_prev  = ibs_arr[i - 1]
        ma200_prev = ma200_arr[i - 1]
        vix_prev  = vix_arr[i - 1]
        if np.isnan(ibs_prev) or np.isnan(ma200_prev):
            continue

        long_signal  = (side in ("long",  "both")) and ibs_prev < entry_thresh
        short_signal = (side in ("short", "both")) and ibs_prev > (1 - entry_thresh)

        # Regime filter for short
        if short_signal:
            price_prev = df2["Close"].iloc[i - 1]
            regime_ok = True
            if "ma200" in regime:
                regime_ok = regime_ok and (price_prev < ma200_prev)
            if "vix20" in regime:
                regime_ok = regime_ok and (not np.isnan(vix_prev) and vix_prev > 20)
            if not regime_ok:
                short_signal = False

        if long_signal:
            direction   = 1
        elif short_signal:
            direction   = -1
        else:
            continue

        in_trade    = True
        entry_price = open_arr[i]
        entry_idx   = i
        bars_held   = 0

    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, label: str = "") -> dict:
    if trades.empty or len(trades) < 3:
        if label:
            print(f"  {label}: 笔数不足")
        return {"n": 0, "wr": 0, "total": 0, "pf": 0, "maxdd": 0, "sharpe": 0}

    n   = len(trades)
    wr  = (trades["pnl"] > 0).sum() / n * 100
    tot = trades["pnl"].sum()
    gw  = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    gl  = abs(trades.loc[trades["pnl"] < 0, "pnl"].sum())
    pf  = gw / gl if gl > 0 else 999.0
    cum = trades["pnl"].cumsum()
    mdd = (cum - cum.cummax()).min()
    avg = trades["pnl"].mean()
    std = trades["pnl"].std()
    sharpe = avg / std * np.sqrt(252) if std > 0 else 0
    exit_c = trades["exit_reason"].value_counts().to_dict()
    months = (trades["entry_time"].max() - trades["entry_time"].min()).days / 30
    per_mo = n / months if months > 0 else 0

    if label:
        print(f"  {label}")
        print(f"    n={n}  WR={wr:.1f}%  Total=${tot:,.0f}  PF={pf:.2f}  "
              f"MaxDD=${mdd:,.0f}  Sharpe={sharpe:.3f}  /月={per_mo:.1f}")
        print(f"    出场: {exit_c}")

    return {"n": n, "wr": wr, "total": tot, "pf": pf,
            "maxdd": mdd, "sharpe": sharpe, "per_month": per_mo}


def monthly_table(trades: pd.DataFrame):
    if trades.empty:
        return
    trades = trades.copy()
    trades["month"] = trades["entry_time"].dt.to_period("M")
    m = trades.groupby("month")["pnl"].agg(["sum", "count"])
    profit_mo = (m["sum"] > 0).sum()
    print(f"    逐月 ({profit_mo}/{len(m)} 月盈利):")
    for idx, row in m.iterrows():
        sign = "✅" if row["sum"] > 0 else "❌"
        print(f"      {sign} {idx}: {row['count']:.0f}笔  ${row['sum']:+,.0f}")


def main():
    vix = load_vix()

    for symbol in ["NQ", "ES"]:
        df = load_data(symbol)
        mult = MULT[symbol]
        contract = "MNQ" if symbol == "NQ" else "MES"
        print(f"\n{'='*70}")
        print(f"  {symbol} ({contract})  数据: {df.index[0].date()} ~ {df.index[-1].date()}")
        print(f"{'='*70}")

        # ── 1. Long-only 参数网格 ─────────────────────────────────────────
        print(f"\n【Long-only】参数网格")
        results_long = []
        for et, mb in itertools.product([0.15, 0.20, 0.25], [5, 10, 15]):
            tr = run_backtest(df, vix, entry_thresh=et, exit_thresh=1-et,
                              max_bars=mb, side="long", mult=mult)
            s = stats(tr)
            split = int(len(tr) * 0.5)
            is_s  = stats(tr.iloc[:split])
            oos_s = stats(tr.iloc[split:])
            results_long.append({**s, "entry": et, "max_bars": mb,
                                  "is_sh": is_s["sharpe"], "oos_sh": oos_s["sharpe"]})

        res_l = pd.DataFrame(results_long).sort_values("sharpe", ascending=False)
        print(f"  {'entry':>5} {'max_b':>5} | {'n':>4} {'WR%':>5} {'Total$':>8} "
              f"{'PF':>5} {'MaxDD$':>8} {'Sharpe':>7} | {'IS':>6} {'OOS':>6}")
        print(f"  {'-'*70}")
        for _, r in res_l.iterrows():
            print(f"  {r['entry']:5.2f} {int(r['max_bars']):5d} | {r['n']:4.0f} {r['wr']:5.1f} "
                  f"{r['total']:8,.0f} {r['pf']:5.2f} {r['maxdd']:8,.0f} {r['sharpe']:7.3f} | "
                  f"{r['is_sh']:6.3f} {r['oos_sh']:6.3f}")

        # ── 2. Short-only 各 regime ──────────────────────────────────────
        print(f"\n【Short-only】不同 regime filter（entry=0.20, max_bars=10）")
        for regime, label in [("none", "无过滤"), ("ma200", "价格<200MA"),
                               ("vix20", "VIX>20"), ("ma200_or_vix20", "200MA OR VIX>20")]:
            tr = run_backtest(df, vix, entry_thresh=0.20, exit_thresh=0.80,
                              max_bars=10, side="short", regime=regime, mult=mult)
            stats(tr, label=label)

        # ── 3. Both（多空双向）最优参数 ──────────────────────────────────
        print(f"\n【Long+Short 双向】entry=0.20, max_bars=10, regime=vix20")
        tr_both = run_backtest(df, vix, entry_thresh=0.20, exit_thresh=0.80,
                               max_bars=10, side="both", regime="vix20", mult=mult)
        stats(tr_both, label="Long+Short(VIX>20 for short)")
        monthly_table(tr_both)

        # ── 4. IS/OOS ────────────────────────────────────────────────────
        best_et = float(res_l.iloc[0]["entry"])
        best_mb = int(res_l.iloc[0]["max_bars"])
        tr_best = run_backtest(df, vix, entry_thresh=best_et, exit_thresh=1-best_et,
                               max_bars=best_mb, side="long", mult=mult)
        split = int(len(tr_best) * 0.5)
        print(f"\n【Long 最优参数 IS/OOS】entry={best_et}, max_bars={best_mb}")
        stats(tr_best.iloc[:split],  label=f"IS  ({tr_best['entry_time'].iloc[0].date()} ~ {tr_best['entry_time'].iloc[split-1].date()})")
        stats(tr_best.iloc[split:],  label=f"OOS ({tr_best['entry_time'].iloc[split].date()} ~ {tr_best['entry_time'].iloc[-1].date()})")


if __name__ == "__main__":
    main()
