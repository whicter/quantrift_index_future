"""
VWAP 日内均值回归回测 — NQ 15min
策略：日内 VWAP 偏离 dev_mult × ATR → 反向入场，回归 VWAP 止盈
只在 RTH（09:30-15:45 ET）交易
VWAP 从每日 17:00 ET session 起重算
MNQ 1手，$2/点
"""

import pandas as pd
import numpy as np
from pathlib import Path
import itertools

NQ_FILE  = Path("/Users/congrenhan/Documents/quantrift_index_future/data/NQF_15min_2024-06-23_2026-06-30.csv")
MNQ_MULT = 2
ET_OFFSET = 0   # 数据已是 ET


def load_data() -> pd.DataFrame:
    df = pd.read_csv(NQ_FILE, parse_dates=["Date"]).set_index("Date")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    # 去掉 16:00-17:00 维护窗口（小时==16）
    df = df[df.index.hour != 16]
    print(f"数据: {len(df)} bars  ({df.index[0]} ~ {df.index[-1]})")
    return df


def add_features(df: pd.DataFrame, atr_len: int = 20) -> pd.DataFrame:
    df = df.copy()

    # ATR（15min）
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/atr_len, min_periods=atr_len).mean()

    # session 标识：每天从 17:00 ET 开始新 session
    # session_date = 下一个交易日的日期（17:00 归属明天）
    def session_date(ts):
        if ts.hour >= 17:
            return (ts + pd.Timedelta(days=1)).date()
        else:
            return ts.date()

    df["session"] = df.index.map(session_date)

    # VWAP（从每个 session 起点累积）
    df["tp"] = (df["High"] + df["Low"] + df["Close"]) / 3
    df["vol_tp"] = df["tp"] * df["Volume"]

    vwap_list = []
    for sess, grp in df.groupby("session"):
        cum_vol_tp = grp["vol_tp"].cumsum()
        cum_vol    = grp["Volume"].cumsum()
        vwap_list.append(cum_vol_tp / cum_vol.replace(0, np.nan))
    df["vwap"] = pd.concat(vwap_list)

    # RTH 标记（09:30-15:45）
    h = df.index.hour
    m = df.index.minute
    df["is_rth"] = ((h == 9) & (m >= 30)) | ((h >= 10) & (h <= 14)) | ((h == 15) & (m <= 45))

    return df


def run_backtest(df: pd.DataFrame,
                 dev_mult: float = 2.0,    # 偏离阈值（ATR 倍数）
                 exit_mult: float = 0.3,   # 接近 VWAP 止盈（ATR 倍数）
                 stop_mult: float = 3.0,   # 止损（ATR 倍数）
                 max_bars: int = 12,       # 最大持仓 bars（15min）
                 daily_limit: int = 1,     # 每日最多入场次数
                 ) -> pd.DataFrame:

    trades = []
    in_trade = False
    direction = 0
    entry_price = entry_vwap = 0.0
    entry_atr = 0.0
    entry_idx = 0
    bars_held = 0

    today_date = None
    today_count = 0

    rows = df[["Close", "High", "Low", "atr", "vwap", "is_rth", "session"]].itertuples()

    for row in rows:
        ts = row.Index
        close  = row.Close
        high   = row.High
        low    = row.Low
        atr    = row.atr
        vwap   = row.vwap
        is_rth = row.is_rth

        if pd.isna(atr) or pd.isna(vwap) or atr <= 0:
            continue

        # 每日计数重置
        d = ts.date()
        if d != today_date:
            today_date  = d
            today_count = 0

        # ── 持仓检查出场 ──
        if in_trade:
            bars_held += 1
            dev_from_vwap = close - vwap

            # TP：价格回到 VWAP ± exit_mult×ATR
            tp_hit = abs(dev_from_vwap) < exit_mult * entry_atr
            # SL：从入场扩大到 stop_mult×ATR
            if direction > 0:
                sl_hit = low <= entry_price - stop_mult * entry_atr
            else:
                sl_hit = high >= entry_price + stop_mult * entry_atr
            # 超时或 RTH 结束
            time_hit = bars_held >= max_bars or (not is_rth)

            exit_price = None
            exit_reason = None
            if sl_hit:
                exit_price  = entry_price - direction * stop_mult * entry_atr
                exit_reason = "sl"
            elif tp_hit:
                exit_price  = close
                exit_reason = "tp"
            elif time_hit:
                exit_price  = close
                exit_reason = "timeout" if bars_held >= max_bars else "eod"

            if exit_price is not None:
                pnl_pts = direction * (exit_price - entry_price)
                trades.append({
                    "entry_time":  df.index[entry_idx],
                    "exit_time":   ts,
                    "direction":   direction,
                    "entry":       entry_price,
                    "exit":        exit_price,
                    "exit_reason": exit_reason,
                    "bars":        bars_held,
                    "entry_atr":   entry_atr,
                    "pnl_pts":     pnl_pts,
                    "pnl_usd":     pnl_pts * MNQ_MULT,
                })
                in_trade   = False
                bars_held  = 0
            continue

        # ── 空仓：寻找入场 ──
        if not is_rth:
            continue
        if today_count >= daily_limit:
            continue

        dev = close - vwap
        if dev > dev_mult * atr:
            # 价格过高于 VWAP → 做空
            direction   = -1
        elif dev < -dev_mult * atr:
            # 价格过低于 VWAP → 做多
            direction   = +1
        else:
            continue

        in_trade    = True
        entry_price = close
        entry_vwap  = vwap
        entry_atr   = atr
        entry_idx   = df.index.get_loc(ts)
        bars_held   = 0
        today_count += 1

    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, label: str = "") -> dict:
    if trades.empty or len(trades) < 5:
        if label:
            print(f"  {label}: 笔数不足")
        return {"n": 0, "wr": 0, "total": 0, "pf": 0, "maxdd": 0, "sharpe": 0, "per_month": 0}

    n   = len(trades)
    wr  = (trades["pnl_usd"] > 0).sum() / n * 100
    tot = trades["pnl_usd"].sum()
    gw  = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].sum()
    gl  = abs(trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].sum())
    pf  = gw / gl if gl > 0 else 999.0
    cum = trades["pnl_usd"].cumsum()
    mdd = (cum - cum.cummax()).min()
    avg = trades["pnl_usd"].mean()
    std = trades["pnl_usd"].std()
    sharpe = avg / std * np.sqrt(252) if std > 0 else 0
    exit_c = trades["exit_reason"].value_counts().to_dict()
    months = (trades["entry_time"].max() - trades["entry_time"].min()).days / 30
    per_mo = n / months if months > 0 else 0

    if label:
        print(f"  {label}: n={n}  WR={wr:.1f}%  Total=${tot:,.0f}  PF={pf:.2f}  "
              f"MaxDD=${mdd:,.0f}  Sharpe={sharpe:.3f}  /月={per_mo:.1f}")
        print(f"    出场: {exit_c}")

    return {"n": n, "wr": wr, "total": tot, "pf": pf,
            "maxdd": mdd, "sharpe": sharpe, "per_month": per_mo}


def main():
    df_raw = load_data()
    df = add_features(df_raw)

    # ── 参数网格 ──────────────────────────────────────────────────────────
    dev_mults  = [1.5, 2.0, 2.5]
    exit_mults = [0.2, 0.3, 0.5]
    stop_mults = [2.5, 3.0, 4.0]
    max_bars_list = [8, 12, 16]

    print("\n" + "="*100)
    print("  VWAP 日内均值回归  参数网格（dev_mult × exit_mult × stop_mult × max_bars）")
    print("="*100)

    results = []
    combos = list(itertools.product(dev_mults, exit_mults, stop_mults, max_bars_list))
    print(f"  共 {len(combos)} 组参数\n")

    for dev_m, exit_m, stop_m, max_b in combos:
        tr = run_backtest(df, dev_mult=dev_m, exit_mult=exit_m,
                          stop_mult=stop_m, max_bars=max_b)
        s = stats(tr)
        split = int(len(tr) * 0.5)
        is_s  = stats(tr.iloc[:split]) if split > 5 else {"sharpe": 0, "wr": 0}
        oos_s = stats(tr.iloc[split:]) if (len(tr)-split) > 5 else {"sharpe": 0, "wr": 0}
        results.append({
            "dev_mult": dev_m, "exit_mult": exit_m,
            "stop_mult": stop_m, "max_bars": max_b,
            **s,
            "is_sharpe": is_s["sharpe"], "oos_sharpe": oos_s["sharpe"],
            "is_wr": is_s["wr"], "oos_wr": oos_s["wr"],
        })

    res = pd.DataFrame(results).sort_values("sharpe", ascending=False)

    print(f"  {'dev_m':>5} {'exit_m':>6} {'stop_m':>6} {'max_b':>5} | "
          f"{'n':>4} {'WR%':>5} {'Total$':>8} {'PF':>5} {'MaxDD$':>8} {'Sharpe':>7} | "
          f"{'IS_Sh':>6} {'OOS_Sh':>7} | {'/月':>4}")
    print(f"  {'-'*95}")
    for _, r in res.head(20).iterrows():
        print(f"  {r['dev_mult']:5.1f} {r['exit_mult']:6.1f} {r['stop_mult']:6.1f} {int(r['max_bars']):5d} | "
              f"{r['n']:4.0f} {r['wr']:5.1f} {r['total']:8,.0f} {r['pf']:5.2f} "
              f"{r['maxdd']:8,.0f} {r['sharpe']:7.3f} | "
              f"{r['is_sharpe']:6.3f} {r['oos_sharpe']:7.3f} | {r['per_month']:4.1f}")

    # ── 最优参数逐月 ─────────────────────────────────────────────────────
    best = res.iloc[0]
    print(f"\n{'='*70}")
    print(f"  最优参数逐月: dev={best['dev_mult']} exit={best['exit_mult']} "
          f"stop={best['stop_mult']} max_bars={int(best['max_bars'])}")
    print("="*70)
    tr_best = run_backtest(df, dev_mult=best["dev_mult"], exit_mult=best["exit_mult"],
                           stop_mult=best["stop_mult"], max_bars=int(best["max_bars"]))
    if not tr_best.empty:
        tr_best["month"] = tr_best["entry_time"].dt.to_period("M")
        monthly = tr_best.groupby("month")["pnl_usd"].agg(["sum", "count"])
        profit_mo = (monthly["sum"] > 0).sum()
        print(f"  {profit_mo}/{len(monthly)} 月盈利")
        for idx, row in monthly.iterrows():
            sign = "✅" if row["sum"] > 0 else "❌"
            print(f"    {sign} {idx}: {row['count']:.0f}笔  ${row['sum']:+,.0f}")


if __name__ == "__main__":
    main()
