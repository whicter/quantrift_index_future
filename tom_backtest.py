"""
Turn-of-Month (TOM) 回测 — MES/MNQ 日线
规则：每月倒数第 5 个交易日收盘买入，月初第 3 个交易日收盘卖出
持有约 8 个交易日，每年约 12 笔
"""

import pandas as pd
import numpy as np
from pathlib import Path

BASE = Path("/Users/congrenhan/Documents/quantrift_index_future/data")
FILES = {
    "NQ": (BASE / "NQF_1d_2020-01-01_2026-06-07.csv", 2),   # MNQ $2/pt
    "ES": (BASE / "ESF_1d_2020-01-01_2026-06-08.csv", 5),   # MES $5/pt
}


def load(symbol: str):
    path, mult = FILES[symbol]
    df = pd.read_csv(path, parse_dates=["Date"]).set_index("Date")
    return df[["Open", "High", "Low", "Close"]].dropna(), mult


def run_tom(df: pd.DataFrame, mult: float,
            entry_offset: int = -5,   # 倒数第几个交易日入场（-5 = 倒数第5）
            exit_offset:  int = 3,    # 月初第几个交易日出场
            ) -> pd.DataFrame:
    """
    每月标记"倒数第 entry_offset 个交易日"和"下月第 exit_offset 个交易日"
    在收盘价入场/出场
    """
    df2 = df.copy()
    df2["month"] = df2.index.to_period("M")

    # 对每个月，找倒数第 |entry_offset| 个交易日 → 入场日
    entry_days = set()
    exit_days  = set()

    for period, grp in df2.groupby("month"):
        days = grp.index.tolist()
        # 入场：倒数第 |entry_offset| 个（entry_offset=-5 → index -5）
        if len(days) >= abs(entry_offset):
            entry_days.add(days[entry_offset])
        # 出场：下月第 exit_offset 个（index exit_offset-1）
        if len(days) >= exit_offset:
            exit_days.add(days[exit_offset - 1])

    trades = []
    holding = False
    entry_price = 0.0
    entry_date  = None

    for date, row in df2.iterrows():
        if holding:
            if date in exit_days:
                pnl = (row["Close"] - entry_price) * mult
                trades.append({
                    "entry_date": entry_date,
                    "exit_date":  date,
                    "entry":      entry_price,
                    "exit":       row["Close"],
                    "bars":       (df2.index.get_loc(date) - df2.index.get_loc(entry_date)),
                    "pnl":        pnl,
                })
                holding = False
        else:
            if date in entry_days:
                holding      = True
                entry_price  = row["Close"]
                entry_date   = date

    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame, label: str = ""):
    if trades.empty:
        print(f"  {label}: 无交易")
        return
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
    sharpe = avg / std * np.sqrt(252 / trades["bars"].mean()) if std > 0 else 0
    per_yr = n / ((trades["exit_date"].max() - trades["entry_date"].min()).days / 365)

    print(f"\n  {label}")
    print(f"    n={n}  WR={wr:.1f}%  Total=${tot:,.0f}  PF={pf:.2f}  "
          f"MaxDD=${mdd:,.0f}  Sharpe={sharpe:.3f}  /年≈{per_yr:.0f}笔")
    print(f"    avg_bars={trades['bars'].mean():.1f}  avg_pnl=${avg:,.0f}/笔")

    # 逐年
    trades2 = trades.copy()
    trades2["year"] = trades2["entry_date"].dt.year
    yearly = trades2.groupby("year")["pnl"].agg(["sum", "count"])
    profit_yr = (yearly["sum"] > 0).sum()
    print(f"    逐年 ({profit_yr}/{len(yearly)} 年盈利):")
    for yr, row in yearly.iterrows():
        sign = "✅" if row["sum"] > 0 else "❌"
        print(f"      {sign} {yr}: {row['count']:.0f}笔  ${row['sum']:+,.0f}")


def main():
    print("=" * 60)
    print("  Turn-of-Month (TOM) 回测")
    print("=" * 60)

    for symbol in ["NQ", "ES"]:
        df, mult = load(symbol)
        contract = "MNQ" if symbol == "NQ" else "MES"
        print(f"\n{'─'*60}")
        print(f"  {symbol} ({contract}, ${mult}/pt)")
        print(f"  数据: {df.index[0].date()} ~ {df.index[-1].date()}")
        print(f"{'─'*60}")

        # 标准 TOM：倒数第5 → 月初第3
        tr = run_tom(df, mult, entry_offset=-5, exit_offset=3)
        stats(tr, "标准 TOM（倒数第5 → 月初第3）")

        # IS / OOS
        split = int(len(tr) * 0.5)
        if split > 0:
            is_tr  = tr.iloc[:split]
            oos_tr = tr.iloc[split:]
            print(f"\n  IS/OOS split:")
            stats(is_tr,  f"IS  ({is_tr['entry_date'].iloc[0].date()} ~ {is_tr['entry_date'].iloc[-1].date()})")
            stats(oos_tr, f"OOS ({oos_tr['entry_date'].iloc[0].date()} ~ {oos_tr['entry_date'].iloc[-1].date()})")

        # 参数变体
        print(f"\n  参数变体（entry_offset / exit_offset）")
        for eo, xo in [(-4, 2), (-5, 3), (-6, 4), (-3, 1)]:
            tr2 = run_tom(df, mult, entry_offset=eo, exit_offset=xo)
            if not tr2.empty:
                wr  = (tr2["pnl"] > 0).sum() / len(tr2) * 100
                tot = tr2["pnl"].sum()
                avg = tr2["pnl"].mean()
                std = tr2["pnl"].std()
                sharpe = avg / std * np.sqrt(252 / tr2["bars"].mean()) if std > 0 else 0
                print(f"    entry={eo:+d} exit=+{xo}: n={len(tr2)}  WR={wr:.1f}%  "
                      f"Total=${tot:,.0f}  Sharpe={sharpe:.3f}")


if __name__ == "__main__":
    main()
