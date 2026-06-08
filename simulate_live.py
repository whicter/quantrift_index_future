"""
simulate_live.py — 多周期分仓叠加实盘模拟

仓位分层（各周期独立入场/出场，美元盈亏累加）：
  1H 信号：3 MNQ（基础仓，$6/点）
  4H 信号：加 4 MNQ（累计 7 MNQ，$8/点）
  1D 信号：加 5 MNQ（累计 12 MNQ，$10/点）

MNQ = $2/点，contract_size=2
统一回测区间：2024-03-01 ~ 2026-06-07

用法：
  python simulate_live.py
  python simulate_live.py --start 2025-01-01   # 只看近期
"""

import argparse
import warnings
from copy import deepcopy
from pathlib import Path

import yaml
import numpy as np
import pandas as pd
from backtesting import Backtest

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent

# ── 各周期的"净增"合约数（非累计）──────────────────────────────────
# 1H = 3 MNQ（基础），4H = +4（累计7），1D = +5（累计12）
TF_ADDON = {"1h": 3, "4h": 4, "1d": 5}
TF_CUMUL = {"1h": 3, "4h": 7, "1d": 12}   # 仅用于展示
MNQ_MULT = 2   # MNQ: $2/点


# ══════════════════════════════════════════════════════════════════
# 单周期回测 → 返回交易明细
# ══════════════════════════════════════════════════════════════════

def run_tf(tf_name: str, tf_params: dict, n_contracts: int,
           start_override: str = None) -> dict:
    from indicators import compute_signals
    from strategy   import ConfluenceStrategy
    from backtest_runner import load_data

    params = deepcopy(tf_params)
    if start_override:
        params["start"] = start_override
    params["n_contracts"]  = n_contracts
    params["contract_size"] = MNQ_MULT

    print(f"\n  ── {tf_name.upper()} ({n_contracts} MNQ, "
          f"${n_contracts * MNQ_MULT}/点) ──────────────────────────")
    print(f"     区间: {params['start']} ~ {params.get('end', '')}")

    df = load_data(params)
    df = compute_signals(df, params)

    # ── 配置策略参数 ───────────────────────────────────────────────
    ConfluenceStrategy.min_score           = int(params["min_score"])
    ConfluenceStrategy.adx_threshold       = float(params.get("adx_threshold", 20.0))
    ConfluenceStrategy.use_adx             = bool(params.get("use_adx", True))
    ConfluenceStrategy.vol_mult            = float(params.get("vol_mult", 1.0))
    ConfluenceStrategy.use_vol             = bool(params.get("use_vol", True))
    ConfluenceStrategy.allow_short         = bool(params.get("allow_short", True))
    ConfluenceStrategy.reversal_score      = int(params.get("reversal_score", 2))
    ConfluenceStrategy.allow_reversal_flip = bool(params.get("allow_reversal_flip", True))
    ConfluenceStrategy.conflict_threshold  = int(params.get("conflict_threshold", 2))
    ConfluenceStrategy.use_bbmc_dir        = bool(params.get("use_bbmc_dir", True))
    ConfluenceStrategy.use_squeeze_mr      = bool(params.get("use_squeeze_mr", False))
    ConfluenceStrategy.rsi_mr_ob           = float(params.get("rsi_mr_ob", 65.0))
    ConfluenceStrategy.rsi_mr_os           = float(params.get("rsi_mr_os", 35.0))
    ConfluenceStrategy.use_atr_exit        = bool(params.get("use_atr_exit", False))
    ConfluenceStrategy.atr_sl_mult         = float(params.get("atr_sl_mult", 1.5))
    ConfluenceStrategy.use_trend_filter    = bool(params.get("use_trend_filter", False))
    ConfluenceStrategy.n_contracts         = n_contracts
    ConfluenceStrategy.contract_size       = MNQ_MULT
    ConfluenceStrategy.use_staged_tp       = bool(params.get("use_staged_tp", True))
    ConfluenceStrategy.atr_tp1_mult        = float(params.get("atr_tp1_mult", 1.0))
    ConfluenceStrategy.atr_tp2_mult        = float(params.get("atr_tp2_mult", 2.0))
    ConfluenceStrategy.tp1_portion         = float(params.get("tp1_portion", 0.34))

    bt = Backtest(
        df, ConfluenceStrategy,
        cash            = 500_000,
        commission      = float(params.get("commission", 0.00002)),
        margin          = float(params.get("margin", 0.05)),
        exclusive_orders= True,
    )
    stats = bt.run()

    trades_df = stats._trades
    if trades_df is None or len(trades_df) == 0:
        print(f"     无交易")
        return _empty_result(tf_name, n_contracts)

    # ── 逐笔计算美元盈亏 ──────────────────────────────────────────
    # backtesting.py: size = n_contracts × contract_size
    # P&L = direction × (exit - entry) × |size|
    trades_list = []
    for _, t in trades_df.iterrows():
        size      = float(t.get("Size", 0))
        direction = 1 if size > 0 else -1
        entry     = float(t.get("EntryPrice", 0))
        exit_     = float(t.get("ExitPrice", 0))
        pts       = direction * (exit_ - entry)
        usd       = pts * abs(size)            # size已含 contract_size
        entry_t   = t.get("EntryTime")
        exit_t    = t.get("ExitTime")
        trades_list.append({
            "entry_time": str(entry_t),
            "exit_time":  str(exit_t),
            "dir":        "LONG" if direction > 0 else "SHORT",
            "entry":      round(entry, 2),
            "exit":       round(exit_, 2),
            "pts":        round(pts, 2),
            "usd":        round(usd, 2),
        })

    total_usd = sum(t["usd"] for t in trades_list)
    wins      = [t for t in trades_list if t["usd"] > 0]
    losses    = [t for t in trades_list if t["usd"] <= 0]
    n_t       = len(trades_list)

    # ── 年度拆解 ──────────────────────────────────────────────────
    yearly = {}
    for tr in trades_list:
        yr = str(tr["exit_time"])[:4]
        if yr not in yearly:
            yearly[yr] = {"trades": 0, "wins": 0, "usd": 0.0}
        yearly[yr]["trades"] += 1
        yearly[yr]["usd"]    += tr["usd"]
        if tr["usd"] > 0:
            yearly[yr]["wins"] += 1

    # ── 打印 ──────────────────────────────────────────────────────
    wr    = stats.get("Win Rate [%]") or 0
    ret   = stats.get("Return [%]")   or 0
    dd    = stats.get("Max. Drawdown [%]") or 0
    sharpe = stats.get("Sharpe Ratio") or 0
    pf    = stats.get("Profit Factor") or 0

    print(f"     笔数: {n_t}  |  胜率: {wr:.1f}%  |  "
          f"收益: {ret:.1f}%  |  DD: {dd:.1f}%  |  PF: {pf:.2f}")
    print(f"     美元盈亏: ${total_usd:>10,.0f}  "
          f"(均笔 ${total_usd/n_t:,.0f}  |  "
          f"胜 {len(wins)} 笔 / 负 {len(losses)} 笔)")

    return {
        "tf":          tf_name,
        "n_contracts": n_contracts,
        "cumulative":  TF_CUMUL[tf_name],
        "n_trades":    n_t,
        "win_rate":    round(wr, 1),
        "return_pct":  round(ret, 2),
        "max_dd":      round(dd, 2),
        "sharpe":      round(float(sharpe), 3) if sharpe == sharpe else 0,
        "pf":          round(float(pf), 3) if pf == pf else 0,
        "total_usd":   round(total_usd, 2),
        "avg_usd":     round(total_usd / n_t, 2) if n_t else 0,
        "wins":        len(wins),
        "losses":      len(losses),
        "yearly":      yearly,
        "trades":      trades_list,
    }


def _empty_result(tf_name, n_contracts):
    return {"tf": tf_name, "n_contracts": n_contracts,
            "cumulative": TF_CUMUL[tf_name],
            "n_trades": 0, "total_usd": 0, "yearly": {}, "trades": []}


# ══════════════════════════════════════════════════════════════════
# 汇总打印
# ══════════════════════════════════════════════════════════════════

def print_summary(results: list, start: str, end: str):
    print("\n")
    print("╔" + "═" * 78 + "╗")
    print("║  NQ 多周期分仓实盘模拟  （MNQ = $2/点）" + " " * 36 + "║")
    print(f"║  区间: {start} ~ {end}" + " " * (69 - len(start) - len(end)) + "║")
    print("╠" + "═" * 78 + "╣")
    print("║  周期   净增   累计    笔数   胜率     美元盈亏     均笔     夏普   最大DD ║")
    print("╠" + "─" * 78 + "╣")

    total_usd = 0
    for r in results:
        if r["n_trades"] == 0:
            print(f"║  {r['tf']:<5}   +{r['n_contracts']:>2}    {r['cumulative']:>2}     —     —        —            —        —       —     ║")
            continue
        total_usd += r["total_usd"]
        sign = "+" if r["total_usd"] >= 0 else ""
        print(f"║  {r['tf']:<5}   +{r['n_contracts']:>2}    {r['cumulative']:>2}     "
              f"{r['n_trades']:>4}  {r['win_rate']:>5.1f}%  "
              f"{sign}${abs(r['total_usd']):>9,.0f}  "
              f"${r['avg_usd']:>7,.0f}  "
              f"{r['sharpe']:>5.3f}  "
              f"{r['max_dd']:>6.1f}%  ║")

    print("╠" + "═" * 78 + "╣")
    sign = "+" if total_usd >= 0 else ""
    print(f"║  合计                           "
          f"                  {sign}${abs(total_usd):>9,.0f}"
          + " " * 27 + "║")
    print("╚" + "═" * 78 + "╝")

    # ── 年度拆解 ──────────────────────────────────────────────────
    # 合并所有周期的年度盈亏
    combined_yearly: dict = {}
    for r in results:
        for yr, d in r.get("yearly", {}).items():
            if yr not in combined_yearly:
                combined_yearly[yr] = {"usd": 0.0, "trades": 0}
            combined_yearly[yr]["usd"]    += d["usd"]
            combined_yearly[yr]["trades"] += d["trades"]

    if combined_yearly:
        print("\n  ── 合并年度盈亏（所有周期叠加）─────────────────────────────")
        print(f"  {'年份':<6} {'总美元盈亏':>14}  {'累计':>14}")
        cum = 0.0
        for yr in sorted(combined_yearly.keys()):
            d   = combined_yearly[yr]
            cum += d["usd"]
            sign = "+" if d["usd"] >= 0 else ""
            print(f"  {yr:<6}  {sign}${d['usd']:>12,.0f}    ${cum:>12,.0f}")

    # ── 各周期年度明细 ─────────────────────────────────────────────
    for r in results:
        if not r.get("yearly"):
            continue
        print(f"\n  ── {r['tf'].upper()} 年度（{r['n_contracts']} MNQ）"
              f"────────────────────────────────────")
        print(f"  {'年份':<6} {'笔数':>5} {'胜率':>7} {'美元盈亏':>14}")
        for yr in sorted(r["yearly"].keys()):
            d    = r["yearly"][yr]
            wr_y = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            sign = "+" if d["usd"] >= 0 else ""
            print(f"  {yr:<6} {d['trades']:>5} {wr_y:>6.0f}%  {sign}${d['usd']:>12,.0f}")


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--start",  default=None, help="统一起始日期（默认 1H 起始）")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    symbol      = config.get("symbol", "NQ=F")
    allow_short = config.get("allow_short", True)
    tfs         = config.get("timeframes", {})

    # 统一起始日（默认用 1H 的 start）
    default_start = tfs.get("1h", {}).get("start", "2024-03-01")
    start_override = args.start or default_start
    end = tfs.get("1h", {}).get("end", "2026-06-07")

    print("═" * 60)
    print(f"  NQ 分仓实盘模拟  |  {symbol}")
    print(f"  统一区间: {start_override} ~ {end}")
    print(f"  仓位: 1H×3 MNQ + 4H×4 MNQ + 1D×5 MNQ = 最大 12 MNQ")
    print("═" * 60)

    results = []
    for tf_name in ["1h", "4h", "1d"]:
        if tf_name not in tfs:
            continue
        params = tfs[tf_name].copy()
        params.setdefault("symbol",      symbol)
        params.setdefault("allow_short", allow_short)
        n = TF_ADDON[tf_name]
        try:
            r = run_tf(tf_name, params, n, start_override=start_override)
        except Exception as e:
            import traceback
            print(f"  [ERROR] {tf_name}: {e}")
            traceback.print_exc()
            r = _empty_result(tf_name, n)
        results.append(r)

    print_summary(results, start_override, end)


if __name__ == "__main__":
    main()
