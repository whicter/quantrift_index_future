"""
ES RTH Gap Reversion 回测 — MES 1H
RTH Gap = 当日 9:00 bar 开盘 vs 前日 15:00 bar 收盘（前日 RTH close）
策略：fade the gap（gap up→short MES，gap down→long MES）
TP = 填满 gap（回到前日 close），SL = gap * sl_mult，超时平仓
MES $5/pt，固定 1 手
"""

import pandas as pd
import numpy as np
from pathlib import Path

ES_FILE = Path("/Users/congrenhan/Documents/quantrift_index_future/data/ESF_1h_2024-06-10_2026-06-08.csv")
MES_MULT = 5


def load_data() -> pd.DataFrame:
    df = pd.read_csv(ES_FILE, parse_dates=["Date"]).set_index("Date")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"数据: {len(df)} bars  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def run_backtest(df: pd.DataFrame,
                 gap_thresh: float = 5.0,    # 最小 gap 阈值（ES points）
                 sl_mult: float = 3.0,       # SL = gap * sl_mult
                 max_bars: int = 8,          # 最大持仓 bars（1H）
                 vix_max: float = 999.0,     # 可选：VIX 过滤（999=不过滤）
                 ) -> pd.DataFrame:

    # 构建快速查找
    bars_idx = {t: i for i, t in enumerate(df.index)}
    arr = df[["Open", "High", "Low", "Close"]].values

    # 前日 RTH close = 前日 15:00 bar 的 Close（15:00-16:00 bar，收盘约 16:00）
    closes_15 = df[df.index.hour == 15]["Close"]

    # 当日 9:00 bar = RTH 前夕（9:00-10:00），用其 Open 作为入场价
    opens_9 = df[df.index.hour == 9]

    trades = []

    for entry_time, row in opens_9.iterrows():
        # 找前日 15:00 close（时间在 entry_time 之前，距离不超过 30 小时）
        prev = closes_15[closes_15.index < entry_time]
        if len(prev) == 0:
            continue
        prev_close = prev.iloc[-1]
        prev_close_time = prev.index[-1]
        hours_diff = (entry_time - prev_close_time).total_seconds() / 3600
        if hours_diff > 30:   # 跳过周末/假日
            continue

        gap = row["Open"] - prev_close
        if abs(gap) < gap_thresh:
            continue

        direction = -1 if gap > 0 else 1    # gap up→short，gap down→long
        entry_price = row["Open"]
        tp_price = prev_close                # TP = gap 填满
        sl_price = entry_price - direction * abs(gap) * sl_mult

        i_start = bars_idx.get(entry_time)
        if i_start is None:
            continue

        exit_price = None
        exit_reason = None
        bars_held = 0

        for j in range(i_start, min(i_start + max_bars, len(arr))):
            hi, lo = arr[j, 1], arr[j, 2]
            bars_held += 1

            if direction == 1:   # long：SL below，TP above
                if lo <= sl_price:
                    exit_price, exit_reason = sl_price, "sl"
                    break
                if hi >= tp_price:
                    exit_price, exit_reason = tp_price, "tp"
                    break
            else:                # short：SL above，TP below
                if hi >= sl_price:
                    exit_price, exit_reason = sl_price, "sl"
                    break
                if lo <= tp_price:
                    exit_price, exit_reason = tp_price, "tp"
                    break

            if bars_held >= max_bars:
                exit_price = arr[j, 3]   # close
                exit_reason = "timeout"
                break

        if exit_price is None:
            exit_price = arr[min(i_start + max_bars - 1, len(arr) - 1), 3]
            exit_reason = "timeout"

        pnl_pts = direction * (exit_price - entry_price)
        pnl_usd = pnl_pts * MES_MULT

        trades.append({
            "date":       entry_time.date(),
            "entry_time": entry_time,
            "direction":  direction,
            "gap_pts":    gap,
            "entry":      entry_price,
            "tp":         tp_price,
            "sl":         sl_price,
            "exit":       exit_price,
            "exit_reason": exit_reason,
            "bars":       bars_held,
            "pnl_pts":    pnl_pts,
            "pnl_usd":    pnl_usd,
        })

    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, label: str = "") -> dict:
    if trades.empty or len(trades) < 3:
        if label:
            print(f"  {label}: 笔数不足")
        return {"n": 0, "wr": 0, "total": 0, "pf": 0, "maxdd": 0, "sharpe": 0}

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

    # monthly Sharpe（更稳定）
    monthly = trades.set_index("entry_time")["pnl_usd"].resample("ME").sum()
    m_sharpe = monthly.mean() / monthly.std() * np.sqrt(12) if monthly.std() > 0 else 0

    months = (trades["entry_time"].max() - trades["entry_time"].min()).days / 30
    per_mo = n / months if months > 0 else 0

    tp_rate = (trades["exit_reason"] == "tp").mean() * 100

    if label:
        print(f"  {label}")
        print(f"    n={n}  WR={wr:.1f}%  Total=${tot:,.0f}  PF={pf:.2f}  "
              f"MaxDD=${mdd:,.0f}  Sharpe(日)={sharpe:.3f}  Sharpe(月)={m_sharpe:.3f}  /月={per_mo:.1f}")
        print(f"    出场: {exit_c}  TP填满率={tp_rate:.1f}%")

    return {"n": n, "wr": wr, "total": tot, "pf": pf,
            "maxdd": mdd, "sharpe": sharpe, "m_sharpe": m_sharpe,
            "per_month": per_mo, "tp_rate": tp_rate}


def main():
    df = load_data()

    # ── 1. 参数网格 ─────────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print("  ES RTH Gap Reversion  参数网格（gap_thresh × sl_mult × max_bars）")
    print("=" * 80)

    import itertools
    thresholds  = [3.0, 5.0, 8.0, 12.0, 20.0]
    sl_mults    = [2.0, 3.0, 5.0]
    max_bars_l  = [4, 6, 8, 12]

    results = []
    for thresh, sl, mb in itertools.product(thresholds, sl_mults, max_bars_l):
        tr = run_backtest(df, gap_thresh=thresh, sl_mult=sl, max_bars=mb)
        s  = stats(tr)
        split = int(len(tr) * 0.5)
        is_s  = stats(tr.iloc[:split])
        oos_s = stats(tr.iloc[split:])
        results.append({
            "thresh": thresh, "sl_mult": sl, "max_bars": mb,
            **s,
            "is_sh": is_s["sharpe"], "oos_sh": oos_s["sharpe"],
            "is_wr": is_s["wr"],    "oos_wr": oos_s["wr"],
        })

    res = pd.DataFrame(results).sort_values("sharpe", ascending=False)

    print(f"\n  {'thresh':>6} {'sl_m':>5} {'mb':>3} | "
          f"{'n':>4} {'WR%':>5} {'Total$':>8} {'PF':>5} {'MaxDD$':>8} "
          f"{'Sharpe':>7} {'M_Sh':>6} | {'IS_Sh':>6} {'OOS_Sh':>7} | {'TP%':>5}")
    print(f"  {'-'*85}")
    for _, r in res.head(25).iterrows():
        print(f"  {r['thresh']:6.1f} {r['sl_mult']:5.1f} {int(r['max_bars']):3d} | "
              f"{r['n']:4.0f} {r['wr']:5.1f} {r['total']:8,.0f} {r['pf']:5.2f} "
              f"{r['maxdd']:8,.0f} {r['sharpe']:7.3f} {r['m_sharpe']:6.3f} | "
              f"{r['is_sh']:6.3f} {r['oos_sh']:7.3f} | {r['tp_rate']:5.1f}")

    # ── 2. 最优参数详细分析 ─────────────────────────────────────────────────
    best = res.iloc[0]
    print(f"\n{'='*70}")
    print(f"  最优参数: thresh={best['thresh']}pts  sl={best['sl_mult']}×gap  max_bars={int(best['max_bars'])}")
    print("=" * 70)
    tr_best = run_backtest(df, gap_thresh=best["thresh"], sl_mult=best["sl_mult"],
                           max_bars=int(best["max_bars"]))

    # IS / OOS
    split = int(len(tr_best) * 0.5)
    stats(tr_best.iloc[:split],
          label=f"IS  ({tr_best['entry_time'].iloc[0].date()} ~ {tr_best['entry_time'].iloc[split-1].date()})")
    stats(tr_best.iloc[split:],
          label=f"OOS ({tr_best['entry_time'].iloc[split].date()} ~ {tr_best['entry_time'].iloc[-1].date()})")

    # 逐月
    if not tr_best.empty:
        tr_best2 = tr_best.copy()
        tr_best2["month"] = pd.to_datetime(tr_best2["entry_time"]).dt.to_period("M")
        monthly = tr_best2.groupby("month")["pnl_usd"].agg(["sum", "count"])
        profit_mo = (monthly["sum"] > 0).sum()
        print(f"\n  逐月 ({profit_mo}/{len(monthly)} 月盈利):")
        for idx, row in monthly.iterrows():
            sign = "✅" if row["sum"] > 0 else "❌"
            print(f"    {sign} {idx}: {row['count']:.0f}笔  ${row['sum']:+,.0f}")

    # ── 3. 按 gap 大小分档 ──────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("  Gap 分档统计（sl=3.0×gap, max_bars=8, 无 thresh 过滤）")
    print("=" * 70)
    tr_all = run_backtest(df, gap_thresh=0.0, sl_mult=3.0, max_bars=8)
    if not tr_all.empty:
        tr_all["gap_abs"] = tr_all["gap_pts"].abs()
        bins = [0, 3, 5, 8, 12, 20, 999]
        labels = ["<3", "3-5", "5-8", "8-12", "12-20", ">20"]
        tr_all["gap_bin"] = pd.cut(tr_all["gap_abs"], bins=bins, labels=labels)
        g = tr_all.groupby("gap_bin", observed=True).agg(
            n=("pnl_usd", "count"),
            wr=("pnl_usd", lambda x: (x > 0).mean() * 100),
            total=("pnl_usd", "sum"),
            avg=("pnl_usd", "mean"),
            tp_rate=("exit_reason", lambda x: (x == "tp").mean() * 100),
        )
        print(f"  {'Gap':>6} {'n':>4} {'WR%':>6} {'Total$':>8} {'Avg$':>7} {'TP填%':>6}")
        for idx, row in g.iterrows():
            print(f"  {idx:>6} {row['n']:4.0f} {row['wr']:6.1f} {row['total']:8,.0f} {row['avg']:7.0f} {row['tp_rate']:6.1f}")


if __name__ == "__main__":
    main()
