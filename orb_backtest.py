"""
ORB（Opening Range Breakout）回测 — NQ 1H
开盘区间：每日 9:00 ET bar 的 High/Low
入场：10:00 bar open 突破区间 → 顺向入场
出场：TP = entry ± range×tp_mult | SL = 区间另一侧 | 超时（max_bars）
过滤（可选）：ADX < adx_thresh（只在震荡日交易）

MNQ $2/点，手数固定 1 手（结果以点数和美元呈现）
"""

import pandas as pd
import numpy as np
from pathlib import Path
import itertools

NQ_FILE = Path("/Users/congrenhan/Documents/quantrift_index_future/data/NQF_1h_2024-03-01_2026-06-24.csv")
MNQ_MULT = 2


# ── ADX 计算 ────────────────────────────────────────────────────────────
def compute_adx(df: pd.DataFrame, length: int = 14) -> pd.Series:
    h, l, c = df["High"], df["Low"], df["Close"]
    plus_dm  = (h.diff().clip(lower=0)).where(h.diff() > (-l.diff()).clip(lower=0), 0)
    minus_dm = ((-l.diff()).clip(lower=0)).where((-l.diff()).clip(lower=0) > h.diff().clip(lower=0), 0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr    = tr.ewm(alpha=1/length, min_periods=length).mean()
    pdi    = 100 * plus_dm.ewm(alpha=1/length, min_periods=length).mean() / atr
    mdi    = 100 * minus_dm.ewm(alpha=1/length, min_periods=length).mean() / atr
    dx     = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan))
    adx    = dx.ewm(alpha=1/length, min_periods=length).mean()
    return adx


# ── 数据加载 ─────────────────────────────────────────────────────────────
def load_data() -> pd.DataFrame:
    df = pd.read_csv(NQ_FILE, parse_dates=["Date"]).set_index("Date")
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"数据: {len(df)} bars  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


# ── ORB 回测 ─────────────────────────────────────────────────────────────
def run_orb(df: pd.DataFrame,
            tp_mult: float = 1.0,
            max_bars: int = 5,
            adx_thresh: float = 999.0,   # 999 = 不过滤
            adx_len: int = 14) -> pd.DataFrame:

    adx = compute_adx(df, adx_len)

    # 按日分组，取每天的 bar 列表
    df2 = df.copy()
    df2["adx"] = adx
    df2["date"] = df2.index.date

    trades = []

    for day, group in df2.groupby("date"):
        group = group.sort_index()
        hours = group.index.hour.tolist()

        # 需要有 9:00 bar 和至少一个后续 bar
        if 9 not in hours:
            continue
        orb_idx = group.index[group.index.hour == 9][0]
        orb_pos = group.index.get_loc(orb_idx)
        if orb_pos + 1 >= len(group):
            continue

        orb_high = group.loc[orb_idx, "High"]
        orb_low  = group.loc[orb_idx, "Low"]
        orb_range = orb_high - orb_low
        if orb_range <= 0:
            continue

        # ADX 过滤（用 9:00 bar 收盘时的 ADX）
        adx_val = group.loc[orb_idx, "adx"]
        if adx_val > adx_thresh:
            continue

        # 取 10:00 bar open 作为入场判断
        entry_bar_idx = group.index[orb_pos + 1]
        entry_open = group.loc[entry_bar_idx, "Open"]

        if entry_open > orb_high:
            direction = +1   # 突破上轨 → 做多
        elif entry_open < orb_low:
            direction = -1   # 突破下轨 → 做空
        else:
            continue         # 未突破，跳过

        entry_price = entry_open
        tp_price    = entry_price + direction * orb_range * tp_mult
        sl_price    = orb_low if direction > 0 else orb_high   # SL = 区间另一侧

        # 逐 bar 检查出场
        exit_price  = None
        exit_reason = None
        bars_held   = 0

        remaining = group.iloc[orb_pos + 2:]   # 从下一根 bar 开始检查
        for _, bar in remaining.iterrows():
            bars_held += 1

            # 检查 TP/SL（用 bar 的 High/Low 模拟盘中触达）
            if direction > 0:
                if bar["Low"] <= sl_price:
                    exit_price, exit_reason = sl_price, "sl"
                    break
                if bar["High"] >= tp_price:
                    exit_price, exit_reason = tp_price, "tp"
                    break
            else:
                if bar["High"] >= sl_price:
                    exit_price, exit_reason = sl_price, "sl"
                    break
                if bar["Low"] <= tp_price:
                    exit_price, exit_reason = tp_price, "tp"
                    break

            if bars_held >= max_bars:
                exit_price, exit_reason = bar["Close"], "timeout"
                break

        if exit_price is None:
            # 当日剩余 bar 不足
            last_bar = remaining.iloc[-1] if not remaining.empty else group.iloc[-1]
            exit_price, exit_reason = last_bar["Close"], "eod"
            bars_held = len(remaining)

        pnl_pts = direction * (exit_price - entry_price)
        pnl_usd = pnl_pts * MNQ_MULT

        trades.append({
            "date":       pd.Timestamp(day),
            "direction":  direction,
            "entry":      entry_price,
            "tp":         tp_price,
            "sl":         sl_price,
            "exit":       exit_price,
            "exit_reason": exit_reason,
            "bars":       bars_held,
            "orb_range":  orb_range,
            "adx":        adx_val,
            "pnl_pts":    pnl_pts,
            "pnl_usd":    pnl_usd,
        })

    return pd.DataFrame(trades)


# ── 统计 ──────────────────────────────────────────────────────────────────
def stats(trades: pd.DataFrame, label: str = "") -> dict:
    if trades.empty or len(trades) < 5:
        return {"n": 0, "wr": 0, "total": 0, "pf": 0, "maxdd": 0, "sharpe": 0}

    n    = len(trades)
    wins = (trades["pnl_usd"] > 0).sum()
    wr   = wins / n * 100
    tot  = trades["pnl_usd"].sum()
    gw   = trades.loc[trades["pnl_usd"] > 0, "pnl_usd"].sum()
    gl   = abs(trades.loc[trades["pnl_usd"] < 0, "pnl_usd"].sum())
    pf   = gw / gl if gl > 0 else 999.0
    cum  = trades["pnl_usd"].cumsum()
    mdd  = (cum - cum.cummax()).min()
    avg  = trades["pnl_usd"].mean()
    std  = trades["pnl_usd"].std()
    avg_b = trades["bars"].mean()
    sharpe = avg / std * np.sqrt(252) if std > 0 else 0   # 日度 Sharpe
    exit_c = trades["exit_reason"].value_counts().to_dict()

    months = (trades["date"].max() - trades["date"].min()).days / 30
    per_mo = n / months if months > 0 else 0

    if label:
        print(f"\n  {label}: n={n}  WR={wr:.1f}%  Total=${tot:,.0f}  PF={pf:.2f}  "
              f"MaxDD=${mdd:,.0f}  Sharpe={sharpe:.3f}  /月={per_mo:.1f}")
        print(f"    出场: {exit_c}  均入场方向: long={( trades['direction']>0).sum()} short={(trades['direction']<0).sum()}")

    return {"n": n, "wr": wr, "total": tot, "pf": pf, "maxdd": mdd,
            "sharpe": sharpe, "per_month": per_mo}


# ── 主函数 ────────────────────────────────────────────────────────────────
def main():
    df = load_data()

    # ── 1. 基准：无过滤，全样本 ──────────────────────────────────────────
    print("\n" + "="*70)
    print("  第一轮：无 ADX 过滤，参数网格")
    print("="*70)

    tp_mults  = [0.5, 1.0, 1.5, 2.0]
    max_bars_list = [3, 4, 5, 6]

    results = []
    for tp_mult, max_bars in itertools.product(tp_mults, max_bars_list):
        tr = run_orb(df, tp_mult=tp_mult, max_bars=max_bars)
        s = stats(tr)
        split = int(len(tr) * 0.5)
        is_s  = stats(tr.iloc[:split])
        oos_s = stats(tr.iloc[split:])
        results.append({
            "tp_mult": tp_mult, "max_bars": max_bars, "adx_thresh": 999,
            **s, "is_sharpe": is_s["sharpe"], "oos_sharpe": oos_s["sharpe"],
            "is_wr": is_s["wr"], "oos_wr": oos_s["wr"],
        })

    res = pd.DataFrame(results).sort_values("sharpe", ascending=False)
    print(f"\n  {'tp_mult':>8} {'max_b':>5} | {'n':>4} {'WR%':>5} {'Total$':>8} {'PF':>5} {'MaxDD$':>8} {'Sharpe':>7} | {'IS_Sh':>6} {'OOS_Sh':>7}")
    print(f"  {'-'*80}")
    for _, r in res.iterrows():
        print(f"  {r['tp_mult']:8.1f} {int(r['max_bars']):5d} | {r['n']:4.0f} {r['wr']:5.1f} "
              f"{r['total']:8,.0f} {r['pf']:5.2f} {r['maxdd']:8,.0f} {r['sharpe']:7.3f} | "
              f"{r['is_sharpe']:6.3f} {r['oos_sharpe']:7.3f}")

    # ── 2. ADX 过滤版本（盘整日专用）───────────────────────────────────
    print("\n" + "="*70)
    print("  第二轮：ADX < 25 过滤（盘整日）")
    print("="*70)

    results2 = []
    for tp_mult, max_bars in itertools.product(tp_mults, max_bars_list):
        tr = run_orb(df, tp_mult=tp_mult, max_bars=max_bars, adx_thresh=25.0)
        s = stats(tr)
        split = int(len(tr) * 0.5)
        is_s  = stats(tr.iloc[:split])
        oos_s = stats(tr.iloc[split:])
        results2.append({
            "tp_mult": tp_mult, "max_bars": max_bars, "adx_thresh": 25,
            **s, "is_sharpe": is_s["sharpe"], "oos_sharpe": oos_s["sharpe"],
            "is_wr": is_s["wr"], "oos_wr": oos_s["wr"],
        })

    res2 = pd.DataFrame(results2).sort_values("sharpe", ascending=False)
    print(f"\n  ADX<25 过滤后，每日信号数量约减少多少？")
    all_tr = run_orb(df, tp_mult=1.0, max_bars=5)
    adx_tr = run_orb(df, tp_mult=1.0, max_bars=5, adx_thresh=25.0)
    print(f"  无过滤: {len(all_tr)} 笔  ADX<25: {len(adx_tr)} 笔  "
          f"（过滤掉 {len(all_tr)-len(adx_tr)} 笔，{(len(all_tr)-len(adx_tr))/len(all_tr)*100:.0f}%）")

    print(f"\n  {'tp_mult':>8} {'max_b':>5} | {'n':>4} {'WR%':>5} {'Total$':>8} {'PF':>5} {'MaxDD$':>8} {'Sharpe':>7} | {'IS_Sh':>6} {'OOS_Sh':>7}")
    print(f"  {'-'*80}")
    for _, r in res2.iterrows():
        print(f"  {r['tp_mult']:8.1f} {int(r['max_bars']):5d} | {r['n']:4.0f} {r['wr']:5.1f} "
              f"{r['total']:8,.0f} {r['pf']:5.2f} {r['maxdd']:8,.0f} {r['sharpe']:7.3f} | "
              f"{r['is_sharpe']:6.3f} {r['oos_sharpe']:7.3f}")

    # ── 3. 最优参数逐月分析 ──────────────────────────────────────────────
    best = res.iloc[0]
    print(f"\n" + "="*70)
    print(f"  最优参数逐月分析: tp_mult={best['tp_mult']}  max_bars={int(best['max_bars'])}")
    print("="*70)
    tr_best = run_orb(df, tp_mult=best["tp_mult"], max_bars=int(best["max_bars"]))
    if not tr_best.empty:
        tr_best["month"] = tr_best["date"].dt.to_period("M")
        monthly = tr_best.groupby("month")["pnl_usd"].agg(["sum","count"])
        profit_mo = (monthly["sum"] > 0).sum()
        print(f"  {profit_mo}/{len(monthly)} 月盈利")
        for idx, row in monthly.iterrows():
            sign = "✅" if row["sum"] > 0 else "❌"
            print(f"    {sign} {idx}: {row['count']:.0f}笔  ${row['sum']:+,.0f}")


if __name__ == "__main__":
    main()
