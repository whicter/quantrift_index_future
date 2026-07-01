"""
NQ/ES 收益率价差回测
spread = r_NQ - r_ES  (1H return)
持仓: 1 MNQ ($2/pt) + 2 MES ($5/pt)
"""

import pandas as pd
import numpy as np
from pathlib import Path
import argparse

BASE = Path(__file__).parent.parent

# ── 参数 ──────────────────────────────────────────────────────────────
Z_ENTRY  = 2.0
Z_EXIT   = 0.5
Z_STOP   = 3.5
WINDOW   = 240      # 滚动窗口 bars
MAX_BARS = 8        # 最大持仓 bars
MNQ_MULT = 2        # $2/pt
MES_MULT = 5        # $5/pt
MES_N    = 2        # 2手 MES 对冲 1手 MNQ

def load_data(nq_file: str, es_file: str) -> pd.DataFrame:
    nq = pd.read_csv(nq_file, parse_dates=["Date"]).set_index("Date")
    es = pd.read_csv(es_file, parse_dates=["Date"]).set_index("Date")
    df = nq[["Close"]].rename(columns={"Close": "NQ"}).join(
         es[["Close"]].rename(columns={"Close": "ES"}), how="inner")
    df = df.dropna()
    print(f"  公共数据: {len(df)} bars  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df

def compute_signals(df: pd.DataFrame, window: int) -> pd.DataFrame:
    df = df.copy()
    df["r_NQ"] = df["NQ"].pct_change()
    df["r_ES"] = df["ES"].pct_change()
    df["spread"] = df["r_NQ"] - df["r_ES"]
    df["mu"]  = df["spread"].rolling(window).mean()
    df["sig"] = df["spread"].rolling(window).std()
    df["z"]   = (df["spread"] - df["mu"]) / df["sig"].replace(0, np.nan)
    return df.dropna()

def run_backtest(df: pd.DataFrame, z_entry=Z_ENTRY, z_exit=Z_EXIT,
                 z_stop=Z_STOP, max_bars=MAX_BARS):
    trades = []
    in_trade = False
    direction = 0   # +1 = long spread (long NQ short ES), -1 = short spread
    entry_idx = 0
    entry_nq = entry_es = 0.0

    nq = df["NQ"].values
    es = df["ES"].values
    z  = df["z"].values

    for i in range(len(df)):
        if not in_trade:
            if z[i] > z_entry:
                direction = -1  # spread高 → 做空spread → short NQ, long ES
                in_trade = True
                entry_idx = i
                entry_nq = nq[i]
                entry_es = es[i]
            elif z[i] < -z_entry:
                direction = +1  # spread低 → 做多spread → long NQ, short ES
                in_trade = True
                entry_idx = i
                entry_nq = nq[i]
                entry_es = es[i]
        else:
            bars_held = i - entry_idx
            exit_reason = None
            if abs(z[i]) < z_exit:
                exit_reason = "mean_revert"
            elif abs(z[i]) > z_stop:
                exit_reason = "stop"
            elif bars_held >= max_bars:
                exit_reason = "timeout"

            if exit_reason:
                mnq_pnl = direction * (nq[i] - entry_nq) * MNQ_MULT
                mes_pnl = -direction * MES_N * (es[i] - entry_es) * MES_MULT
                total_pnl = mnq_pnl + mes_pnl
                trades.append({
                    "entry_time": df.index[entry_idx],
                    "exit_time": df.index[i],
                    "direction": direction,
                    "bars": bars_held,
                    "mnq_pnl": mnq_pnl,
                    "mes_pnl": mes_pnl,
                    "pnl": total_pnl,
                    "exit": exit_reason,
                    "z_entry": z[entry_idx],
                    "z_exit": z[i],
                })
                in_trade = False

    return pd.DataFrame(trades)

def print_stats(trades: pd.DataFrame, label: str = "全样本"):
    if trades.empty:
        print(f"  {label}: 0 笔")
        return
    n = len(trades)
    wins = (trades["pnl"] > 0).sum()
    wr = wins / n * 100
    total = trades["pnl"].sum()
    gross_win = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    gross_loss = abs(trades.loc[trades["pnl"] < 0, "pnl"].sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    cumsum = trades["pnl"].cumsum()
    peak = cumsum.cummax()
    maxdd = (cumsum - peak).min()
    avg_pnl = trades["pnl"].mean()
    std_pnl = trades["pnl"].std()
    sharpe = avg_pnl / std_pnl * np.sqrt(8760 / trades["bars"].mean()) if std_pnl > 0 else 0
    exit_counts = trades["exit"].value_counts()

    print(f"\n{'='*50}")
    print(f"  {label}")
    print(f"{'='*50}")
    print(f"  笔数: {n}  WR: {wr:.1f}%  总PnL: ${total:,.0f}")
    print(f"  PF: {pf:.2f}  MaxDD: ${maxdd:,.0f}  Sharpe≈{sharpe:.3f}")
    print(f"  均值: ${avg_pnl:.0f}/笔  avg bars: {trades['bars'].mean():.1f}")
    print(f"  出场方式: {dict(exit_counts)}")

def print_monthly(trades: pd.DataFrame):
    if trades.empty:
        return
    trades = trades.copy()
    trades["month"] = trades["entry_time"].dt.to_period("M")
    m = trades.groupby("month")["pnl"].agg(["sum", "count"])
    m.columns = ["pnl", "n"]
    profit_months = (m["pnl"] > 0).sum()
    print(f"\n  逐月 ({profit_months}/{len(m)} 月盈利):")
    for idx, row in m.iterrows():
        sign = "✅" if row["pnl"] > 0 else "❌"
        print(f"    {sign} {idx}: {row['n']:.0f}笔  ${row['pnl']:+,.0f}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nq-file", default=str(BASE / "data" / "NQF_1h_2024-06-02_2026-06-08.csv"))
    parser.add_argument("--es-file", default=str(BASE / "data" / "ESF_1h_2024-06-10_2026-06-08.csv"))
    parser.add_argument("--z-entry", type=float, default=Z_ENTRY)
    parser.add_argument("--z-exit",  type=float, default=Z_EXIT)
    parser.add_argument("--z-stop",  type=float, default=Z_STOP)
    parser.add_argument("--window",  type=int,   default=WINDOW)
    parser.add_argument("--max-bars",type=int,   default=MAX_BARS)
    parser.add_argument("--oos-split", type=float, default=0.5)
    parser.add_argument("--monthly", action="store_true")
    args = parser.parse_args()

    print(f"\n参数: z_entry={args.z_entry} z_exit={args.z_exit} z_stop={args.z_stop} "
          f"window={args.window} max_bars={args.max_bars}")

    df = load_data(args.nq_file, args.es_file)
    df = compute_signals(df, args.window)

    # 全样本
    trades = run_backtest(df, args.z_entry, args.z_exit, args.z_stop, args.max_bars)
    print_stats(trades, "全样本")
    if args.monthly:
        print_monthly(trades)

    # IS/OOS
    split = int(len(df) * args.oos_split)
    is_df  = df.iloc[:split]
    oos_df = df.iloc[split:]
    is_trades  = run_backtest(is_df,  args.z_entry, args.z_exit, args.z_stop, args.max_bars)
    oos_trades = run_backtest(oos_df, args.z_entry, args.z_exit, args.z_stop, args.max_bars)
    print_stats(is_trades,  f"IS  ({is_df.index[0].date()} ~ {is_df.index[-1].date()})")
    print_stats(oos_trades, f"OOS ({oos_df.index[0].date()} ~ {oos_df.index[-1].date()})")

if __name__ == "__main__":
    main()
