"""
Spread 参数敏感性分析
测试 z_entry / window / max_bars / z_exit 组合
"""

import pandas as pd
import numpy as np
from pathlib import Path
import itertools

BASE = Path("/Users/congrenhan/Documents/quantrift_index_future")

NQ_FILE = BASE / "data" / "NQF_1h_2024-06-02_2026-06-25.csv"
ES_FILE = BASE / "data" / "ESF_1h_2024-07-01_2026-07-01.csv"

MNQ_MULT = 2
MES_MULT = 5
MES_N    = 2
Z_STOP   = 3.5   # 固定止损


def load_data():
    nq = pd.read_csv(NQ_FILE, parse_dates=["Date"]).set_index("Date")
    es = pd.read_csv(ES_FILE, parse_dates=["Date"]).set_index("Date")
    df = nq[["Close"]].rename(columns={"Close": "NQ"}).join(
         es[["Close"]].rename(columns={"Close": "ES"}), how="inner")
    df = df.dropna()
    print(f"公共数据: {len(df)} bars  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def compute_signals(df: pd.DataFrame, window: int) -> pd.DataFrame:
    df = df.copy()
    df["r_NQ"]  = df["NQ"].pct_change()
    df["r_ES"]  = df["ES"].pct_change()
    df["spread"] = df["r_NQ"] - df["r_ES"]
    df["mu"]  = df["spread"].rolling(window).mean()
    df["sig"] = df["spread"].rolling(window).std()
    df["z"]   = (df["spread"] - df["mu"]) / df["sig"].replace(0, np.nan)
    return df.dropna()


def run_backtest(df, z_entry, z_exit, max_bars):
    trades = []
    in_trade = False
    direction = 0
    entry_idx = 0
    entry_nq = entry_es = 0.0

    nq = df["NQ"].values
    es = df["ES"].values
    z  = df["z"].values

    for i in range(len(df)):
        if not in_trade:
            if z[i] > z_entry:
                direction, in_trade, entry_idx = -1, True, i
                entry_nq, entry_es = nq[i], es[i]
            elif z[i] < -z_entry:
                direction, in_trade, entry_idx = +1, True, i
                entry_nq, entry_es = nq[i], es[i]
        else:
            bars_held = i - entry_idx
            exit_reason = None
            if abs(z[i]) < z_exit:
                exit_reason = "mr"
            elif abs(z[i]) > Z_STOP:
                exit_reason = "stop"
            elif bars_held >= max_bars:
                exit_reason = "timeout"

            if exit_reason:
                mnq_pnl = direction * (nq[i] - entry_nq) * MNQ_MULT
                mes_pnl = -direction * MES_N * (es[i] - entry_es) * MES_MULT
                trades.append({
                    "pnl": mnq_pnl + mes_pnl,
                    "bars": bars_held,
                    "exit": exit_reason,
                    "entry_time": df.index[entry_idx],
                })
                in_trade = False

    if not trades:
        return pd.DataFrame(trades)
    return pd.DataFrame(trades)


def stats(trades: pd.DataFrame) -> dict:
    if trades.empty or len(trades) < 5:
        return {"n": 0, "wr": 0, "total": 0, "pf": 0, "maxdd": 0, "sharpe": 0, "per_month": 0}

    n       = len(trades)
    wins    = (trades["pnl"] > 0).sum()
    wr      = wins / n * 100
    total   = trades["pnl"].sum()
    gw      = trades.loc[trades["pnl"] > 0, "pnl"].sum()
    gl      = abs(trades.loc[trades["pnl"] < 0, "pnl"].sum())
    pf      = gw / gl if gl > 0 else 999.0
    cumsum  = trades["pnl"].cumsum()
    maxdd   = (cumsum - cumsum.cummax()).min()
    avg     = trades["pnl"].mean()
    std     = trades["pnl"].std()
    avg_b   = trades["bars"].mean()
    sharpe  = avg / std * np.sqrt(8760 / avg_b) if std > 0 and avg_b > 0 else 0

    # 月均笔数
    months = (trades["entry_time"].max() - trades["entry_time"].min()).days / 30
    per_month = n / months if months > 0 else 0

    return {"n": n, "wr": wr, "total": total, "pf": pf,
            "maxdd": maxdd, "sharpe": sharpe, "per_month": per_month}


def run_is_oos(df_sig, z_entry, z_exit, max_bars, split=0.5):
    split_idx = int(len(df_sig) * split)
    is_df  = df_sig.iloc[:split_idx]
    oos_df = df_sig.iloc[split_idx:]
    full   = run_backtest(df_sig, z_entry, z_exit, max_bars)
    is_t   = run_backtest(is_df,  z_entry, z_exit, max_bars)
    oos_t  = run_backtest(oos_df, z_entry, z_exit, max_bars)
    return stats(full), stats(is_t), stats(oos_t)


def main():
    df_raw = load_data()

    # 参数网格
    z_entries = [1.6, 1.8, 2.0]
    windows   = [120, 180, 240]
    max_bars_list = [6, 8, 10]
    z_exits   = [0.3, 0.5]

    results = []

    total_combos = len(z_entries) * len(windows) * len(max_bars_list) * len(z_exits)
    print(f"\n共 {total_combos} 组参数，正在计算...\n")

    for window in windows:
        df_sig = compute_signals(df_raw, window)
        for z_entry, z_exit, max_bars in itertools.product(z_entries, z_exits, max_bars_list):
            full, is_s, oos_s = run_is_oos(df_sig, z_entry, z_exit, max_bars)
            results.append({
                "z_entry": z_entry, "window": window,
                "max_bars": max_bars, "z_exit": z_exit,
                "n": full["n"], "wr": full["wr"], "total": full["total"],
                "pf": full["pf"], "maxdd": full["maxdd"],
                "sharpe": full["sharpe"], "per_month": full["per_month"],
                "is_sharpe": is_s["sharpe"], "oos_sharpe": oos_s["sharpe"],
                "is_wr": is_s["wr"], "oos_wr": oos_s["wr"],
                "is_total": is_s["total"], "oos_total": oos_s["total"],
            })

    res = pd.DataFrame(results).sort_values("sharpe", ascending=False)

    # 打印 Top 20
    print(f"\n{'='*110}")
    print(f"  Spread 参数敏感性分析  （按 Sharpe 降序，z_stop=3.5 固定）")
    print(f"{'='*110}")
    print(f"  {'z_entry':>7} {'window':>6} {'max_b':>5} {'z_exit':>6} | "
          f"{'n':>4} {'WR%':>5} {'Total$':>8} {'PF':>5} {'MaxDD$':>8} {'Sharpe':>7} | "
          f"{'IS_Sh':>7} {'OOS_Sh':>7} | {'IS_WR':>6} {'OOS_WR':>6} | '/mo':>5")
    print(f"  {'-'*108}")

    for _, r in res.head(30).iterrows():
        baseline = "◀BASE" if r["z_entry"]==2.0 and r["window"]==240 and r["max_bars"]==8 and r["z_exit"]==0.5 else ""
        print(f"  {r['z_entry']:7.1f} {int(r['window']):6d} {int(r['max_bars']):5d} {r['z_exit']:6.1f} | "
              f"{r['n']:4.0f} {r['wr']:5.1f} {r['total']:8,.0f} {r['pf']:5.2f} {r['maxdd']:8,.0f} {r['sharpe']:7.3f} | "
              f"{r['is_sharpe']:7.3f} {r['oos_sharpe']:7.3f} | "
              f"{r['is_wr']:6.1f} {r['oos_wr']:6.1f} | {r['per_month']:4.1f} {baseline}")

    print(f"\n{'='*110}")

    # 基准行
    base = res[(res["z_entry"]==2.0)&(res["window"]==240)&(res["max_bars"]==8)&(res["z_exit"]==0.5)]
    if not base.empty:
        b = base.iloc[0]
        print(f"\n【基准（当前参数）】: z_entry=2.0 window=240 max_bars=8 z_exit=0.5")
        print(f"  笔数={b['n']:.0f}  WR={b['wr']:.1f}%  Total=${b['total']:,.0f}  "
              f"PF={b['pf']:.2f}  MaxDD=${b['maxdd']:,.0f}  Sharpe={b['sharpe']:.3f}")
        print(f"  IS Sharpe={b['is_sharpe']:.3f}  OOS Sharpe={b['oos_sharpe']:.3f}")

    # 按 z_entry 分组汇总
    print(f"\n{'='*60}")
    print("  按 z_entry 分组（各组 Top Sharpe）")
    print(f"{'='*60}")
    for ze in z_entries:
        sub = res[res["z_entry"]==ze].iloc[0]
        print(f"  z_entry={ze}: best Sharpe={sub['sharpe']:.3f}  n={sub['n']:.0f}  "
              f"WR={sub['wr']:.1f}%  IS={sub['is_sharpe']:.3f}  OOS={sub['oos_sharpe']:.3f}  "
              f"window={sub['window']:.0f}  max_bars={sub['max_bars']:.0f}  z_exit={sub['z_exit']:.1f}")

    print(f"\n{'='*60}")
    print("  按 window 分组（各组 Top Sharpe）")
    print(f"{'='*60}")
    for w in windows:
        sub = res[res["window"]==w].iloc[0]
        print(f"  window={w}: best Sharpe={sub['sharpe']:.3f}  n={sub['n']:.0f}  "
              f"WR={sub['wr']:.1f}%  IS={sub['is_sharpe']:.3f}  OOS={sub['oos_sharpe']:.3f}  "
              f"z_entry={sub['z_entry']:.1f}  max_bars={sub['max_bars']:.0f}  z_exit={sub['z_exit']:.1f}")

    # 保存 CSV
    out = Path("/tmp/spread_sensitivity_results.csv")
    res.to_csv(out, index=False, float_format="%.3f")
    print(f"\n完整结果已保存: {out}")


if __name__ == "__main__":
    main()
