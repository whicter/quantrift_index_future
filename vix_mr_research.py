"""
vix_mr_research.py — VIX 均值回归研究脚本

分析 VIX 在不同区间（20-30 / 30-40 / >40）触发时，ES 未来 N 天的表现。
研究问题：VIX 30-40 zone 做多 ES 是否有统计优势？

用法：
  python vix_mr_research.py
  python vix_mr_research.py --es-csv data/ESF_1d_2020-01-01_2026-06-08.csv
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).parent


# ══════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════

def load_vix(path: Path) -> pd.Series:
    df = pd.read_csv(path, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df['Close'].rename('VIX')


def load_es(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    date_col = next(c for c in df.columns if 'date' in c or 'time' in c)
    df = df.rename(columns={date_col: 'Date', 'open': 'Open', 'high': 'High',
                              'low': 'Low', 'close': 'Close', 'volume': 'Volume'})
    df['Date'] = pd.to_datetime(df['Date']).dt.normalize()
    df = df.set_index('Date').sort_index()
    return df[['Open', 'High', 'Low', 'Close']]


# ══════════════════════════════════════════════════════════
# VIX zone 分析
# ══════════════════════════════════════════════════════════

def analyze_vix_zone(vix: pd.Series, es: pd.DataFrame,
                     vix_lo: float, vix_hi: float,
                     hold_days: list[int] = [1, 3, 5, 10, 20],
                     label: str = "") -> pd.DataFrame:
    """
    找出 VIX 从 <vix_lo 首次突破到 [vix_lo, vix_hi] 的信号日，
    统计之后 hold_days 天的 ES 收益分布。
    """
    # 标记 VIX 是否在目标区间
    in_zone = (vix >= vix_lo) & (vix < vix_hi)

    # 找到进入该区间的第一天（前一天不在区间，当天在区间）
    entries = in_zone & (~in_zone.shift(1).fillna(False))
    entry_dates = vix.index[entries]

    if len(entry_dates) == 0:
        print(f"  [{label}] 无信号")
        return pd.DataFrame()

    records = []
    for d in entry_dates:
        row = {'signal_date': d, 'vix': round(vix[d], 1)}
        # 计算不同持有期的 ES 收益
        try:
            entry_price = es.loc[d, 'Close'] if d in es.index else None
        except KeyError:
            continue
        if entry_price is None or pd.isna(entry_price):
            continue

        for h in hold_days:
            future_dates = es.index[es.index > d]
            if len(future_dates) >= h:
                exit_price = es.loc[future_dates[h - 1], 'Close']
                ret = (exit_price - entry_price) / entry_price * 100
                row[f'ret_{h}d'] = round(ret, 2)
            else:
                row[f'ret_{h}d'] = np.nan
        records.append(row)

    return pd.DataFrame(records)


def print_zone_stats(df: pd.DataFrame, label: str, hold_days: list[int]):
    if df.empty:
        return
    print(f"\n{'═'*62}")
    print(f"  {label}  (N={len(df)} 次进场)")
    print(f"{'═'*62}")
    print(f"  {'持有期':<8} {'均收益%':>8} {'胜率%':>8} {'最大':>8} {'最小':>8} {'中位数':>8}")
    print(f"  {'─'*56}")
    for h in hold_days:
        col = f'ret_{h}d'
        if col not in df.columns:
            continue
        s = df[col].dropna()
        if len(s) == 0:
            continue
        print(f"  {h}天{'':<5} "
              f"{s.mean():>7.2f}% "
              f"{(s > 0).mean() * 100:>7.1f}% "
              f"{s.max():>7.2f}% "
              f"{s.min():>7.2f}% "
              f"{s.median():>7.2f}%")

    # 年度明细
    if 'signal_date' in df.columns:
        df = df.copy()
        df['year'] = pd.to_datetime(df['signal_date']).dt.year
        print(f"\n  年度明细（5天收益）:")
        for yr, grp in df.groupby('year'):
            s = grp['ret_5d'].dropna()
            if len(s) == 0:
                continue
            print(f"    {yr}: {len(s)}次  均{s.mean():.1f}%  胜率{(s>0).mean()*100:.0f}%")


# ══════════════════════════════════════════════════════════
# VIX 持续时间分析
# ══════════════════════════════════════════════════════════

def analyze_duration(vix: pd.Series, threshold: float, label: str):
    """分析 VIX 超过 threshold 后持续几天。"""
    above = vix >= threshold
    # 找到每次进入的起始点
    starts = above & (~above.shift(1).fillna(False))
    durations = []
    for d in vix.index[starts]:
        count = 0
        idx = vix.index.get_loc(d)
        while idx < len(vix) and vix.iloc[idx] >= threshold:
            count += 1
            idx += 1
        durations.append(count)

    if not durations:
        return
    d_arr = np.array(durations)
    print(f"\n  VIX>{threshold:.0f} 持续时长（交易日）: "
          f"均值={d_arr.mean():.1f}  中位={np.median(d_arr):.0f}  "
          f"最短={d_arr.min()}  最长={d_arr.max()}  共{len(d_arr)}次")


# ══════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--vix-csv',
                        default=str(BASE_DIR / 'data' / 'VIX_1d.csv'))
    parser.add_argument('--es-csv',
                        default=str(BASE_DIR / 'data' / 'ESF_1d_2020-01-01_2026-06-08.csv'))
    args = parser.parse_args()

    # 加载数据
    vix_path = Path(args.vix_csv)
    if not vix_path.exists():
        candidates = sorted((BASE_DIR / 'data').glob('VIX_1d*.csv'))
        vix_path = candidates[-1] if candidates else vix_path
    print(f"VIX: {vix_path.name}")

    es_path = Path(args.es_csv)
    if not es_path.exists():
        candidates = sorted((BASE_DIR / 'data').glob('ESF_1d*.csv'))
        es_path = candidates[-1] if candidates else es_path
    print(f"ES:  {es_path.name}")

    vix = load_vix(vix_path)
    es  = load_es(es_path)

    # 对齐日期
    common = vix.index.intersection(es.index)
    vix = vix[common]
    es  = es.loc[common]
    print(f"共同数据: {common[0].date()} ~ {common[-1].date()}  ({len(common)} 天)\n")

    hold_days = [1, 3, 5, 10, 20]

    # ── VIX 基础统计 ──────────────────────────────────────
    print("═" * 62)
    print("  VIX 区间分布（按交易日数）")
    print("═" * 62)
    bins = [(0, 15), (15, 20), (20, 25), (25, 30), (30, 35), (35, 40), (40, 50), (50, 999)]
    for lo, hi in bins:
        n = ((vix >= lo) & (vix < hi)).sum()
        pct = n / len(vix) * 100
        print(f"  VIX {lo:>3}-{hi:<4}: {n:>5} 天  {pct:>5.1f}%")

    # ── 持续时间分析 ──────────────────────────────────────
    print(f"\n  ── VIX 高位持续时间 ─────────────────────────────")
    for thr in [25, 30, 35, 40]:
        analyze_duration(vix, thr, f"VIX>{thr}")

    # ── 各 VIX zone 的 ES 收益 ────────────────────────────
    zones = [
        (20, 25, "VIX 20-25（轻度压力）"),
        (25, 30, "VIX 25-30（中度压力）"),
        (30, 35, "VIX 30-35（高度压力 MR目标下区）"),
        (35, 40, "VIX 35-40（高度压力 MR目标上区）"),
        (30, 40, "VIX 30-40（合并：MR目标区间）"),
        (40, 999, "VIX >40（极端恐慌，现有策略覆盖）"),
    ]

    all_results = {}
    for lo, hi, label in zones:
        df = analyze_vix_zone(vix, es, lo, hi, hold_days, label)
        all_results[label] = df
        print_zone_stats(df, label, hold_days)

    # ── 核心对比：VIX 30-40 vs VIX >40 ──────────────────
    print(f"\n{'═'*62}")
    print("  核心结论：VIX 30-40 是否值得加策略？")
    print(f"{'═'*62}")
    df_mr  = all_results.get("VIX 30-40（合并：MR目标区间）", pd.DataFrame())
    df_ext = all_results.get("VIX >40（极端恐慌，现有策略覆盖）", pd.DataFrame())

    for label, df in [("VIX 30-40", df_mr), ("VIX  >40", df_ext)]:
        if df.empty:
            continue
        s5 = df['ret_5d'].dropna()
        s10 = df['ret_10d'].dropna()
        print(f"\n  {label}: N={len(df)}")
        print(f"    5天:  均收益 {s5.mean():+.2f}%  胜率 {(s5>0).mean()*100:.0f}%")
        print(f"    10天: 均收益 {s10.mean():+.2f}%  胜率 {(s10>0).mean()*100:.0f}%")

    print(f"\n  判断标准（需满足以下之一才值得实现）：")
    print(f"    VIX 30-40 的 5天胜率 ≥ 60%")
    print(f"    VIX 30-40 的 10天均收益 ≥ +2%")
    print(f"    相比 VIX<20 基准有统计显著提升")

    # ── 基准：VIX 正常区间 ────────────────────────────────
    low_vix = (vix < 20)
    es_daily_ret = es['Close'].pct_change() * 100
    normal_5d = []
    for d in es.index[low_vix]:
        future = es.index[es.index > d]
        if len(future) >= 5:
            ret = (es.loc[future[4], 'Close'] - es.loc[d, 'Close']) / es.loc[d, 'Close'] * 100
            normal_5d.append(ret)
    normal_5d = pd.Series(normal_5d)
    print(f"\n  基准（VIX<20时买入ES，5天）: "
          f"均{normal_5d.mean():+.2f}%  胜率{(normal_5d>0).mean()*100:.0f}%")

    print(f"\n研究完成。根据以上数据决定是否实现 VIX 30-40 MR 策略。\n")


if __name__ == '__main__':
    main()
