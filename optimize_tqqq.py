"""
optimize_tqqq.py — 对 TQQQ 1H 做网格搜索，找最优参数组合。

用法：
  python optimize_tqqq.py                  # 快速搜索（默认）
  python optimize_tqqq.py --mode full      # 完整搜索（慢）
  python optimize_tqqq.py --metric Sharpe Ratio
  python optimize_tqqq.py --start 2023-01-01  # 只用近期数据优化
"""

import argparse
import itertools
import warnings
from copy import deepcopy
from pathlib import Path

import yaml
import pandas as pd
from backtesting import Backtest

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent

# ══════════════════════════════════════════════════════════
# TQQQ 专属搜索空间
# TQQQ 波动约 QQQ 的 3 倍，止损/止盈倍数需要更宽松
# ══════════════════════════════════════════════════════════

SEARCH_SPACE = {
    "quick": {
        "ut_key":        [2.0, 3.0, 4.0, 5.0, 6.0],
        "ssl_len":       [20, 30, 40, 54],
        "min_score":     [3, 4, 5],
        "adx_threshold": [15.0, 20.0, 25.0],
        "use_adx":       [True, False],
    },
    "full": {
        "ut_key":        [1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0],
        "ssl_len":       [15, 20, 30, 40, 54],
        "min_score":     [3, 4, 5],
        "adx_threshold": [15.0, 20.0, 25.0, 30.0],
        "use_adx":       [True, False],
        "atr_tp1_mult":  [0.8, 1.0, 1.5],
        "atr_tp2_mult":  [2.0, 2.5, 3.0],
    },
}


def safe(v):
    import numpy as np
    if isinstance(v, float) and (v != v or abs(v) == float("inf")):
        return None
    if isinstance(v, pd.Timestamp):
        return str(v)
    return v


def run_single(base_params: dict, override: dict) -> dict | None:
    from indicators import compute_signals
    from strategy import ConfluenceStrategy
    from backtest_runner import load_data

    params = deepcopy(base_params)
    params.update(override)

    try:
        df = load_data(params)
        df = compute_signals(df, params)
    except Exception:
        return None

    ConfluenceStrategy.min_score            = int(params["min_score"])
    ConfluenceStrategy.adx_threshold        = float(params.get("adx_threshold", 20.0))
    ConfluenceStrategy.use_adx              = bool(params.get("use_adx", True))
    ConfluenceStrategy.vol_mult             = float(params.get("vol_mult", 1.0))
    ConfluenceStrategy.use_vol              = bool(params.get("use_vol", True))
    ConfluenceStrategy.allow_short          = bool(params.get("allow_short", False))
    ConfluenceStrategy.reversal_score       = int(params.get("reversal_score", 2))
    ConfluenceStrategy.allow_reversal_flip  = bool(params.get("allow_reversal_flip", False))
    ConfluenceStrategy.conflict_threshold   = int(params.get("conflict_threshold", 2))
    ConfluenceStrategy.use_bbmc_dir        = bool(params.get("use_bbmc_dir", True))
    ConfluenceStrategy.use_squeeze_mr      = bool(params.get("use_squeeze_mr", False))
    ConfluenceStrategy.rsi_mr_ob           = float(params.get("rsi_mr_ob", 65.0))
    ConfluenceStrategy.rsi_mr_os           = float(params.get("rsi_mr_os", 35.0))
    ConfluenceStrategy.use_atr_exit        = bool(params.get("use_atr_exit", False))
    ConfluenceStrategy.atr_sl_mult         = float(params.get("atr_sl_mult", 1.5))
    ConfluenceStrategy.use_trend_filter    = bool(params.get("use_trend_filter", False))
    ConfluenceStrategy.n_contracts         = int(params.get("n_contracts", 600))
    ConfluenceStrategy.contract_size       = int(params.get("contract_size", 1))
    ConfluenceStrategy.use_staged_tp       = bool(params.get("use_staged_tp", True))
    ConfluenceStrategy.atr_tp1_mult        = float(params.get("atr_tp1_mult", 1.0))
    ConfluenceStrategy.atr_tp2_mult        = float(params.get("atr_tp2_mult", 2.0))
    ConfluenceStrategy.tp1_portion         = float(params.get("tp1_portion", 0.34))

    try:
        bt = Backtest(
            df,
            ConfluenceStrategy,
            cash            = int(params.get("cash", 100_000)),
            commission      = float(params.get("commission", 0.001)),
            margin          = float(params.get("margin", 1.0)),
            exclusive_orders= True,
        )
        stats = bt.run()
    except Exception:
        return None

    n_trades = stats.get("# Trades", 0)
    if n_trades < 15:
        return None

    ret = safe(stats.get("Return [%]"))
    if ret is None or ret <= 0:
        return None

    return {
        "# Trades":          safe(stats.get("# Trades")),
        "Win Rate [%]":      safe(stats.get("Win Rate [%]")),
        "Return [%]":        safe(stats.get("Return [%]")),
        "Sharpe Ratio":      safe(stats.get("Sharpe Ratio")),
        "Max. Drawdown [%]": safe(stats.get("Max. Drawdown [%]")),
        "Profit Factor":     safe(stats.get("Profit Factor")),
        **{k: override.get(k, base_params.get(k)) for k in override},
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE_DIR / "config_tqqq.yaml"))
    parser.add_argument("--mode",   default="quick", choices=["quick", "full"])
    parser.add_argument("--metric", default="Sharpe Ratio",
                        choices=["Sharpe Ratio", "Return [%]", "Profit Factor"])
    parser.add_argument("--top",    type=int, default=10)
    parser.add_argument("--start",  default=None, help="覆盖回测起始日期（如 2023-01-01）")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    base_params = list(config["timeframes"].values())[0].copy()
    base_params.setdefault("symbol",      config.get("symbol", "TQQQ"))
    base_params.setdefault("allow_short", config.get("allow_short", False))

    if args.start:
        base_params["start"] = args.start

    space  = SEARCH_SPACE[args.mode]
    keys   = list(space.keys())
    combos = list(itertools.product(*[space[k] for k in keys]))

    print(f"\n TQQQ 1H 参数优化  ({args.mode} 模式，{len(combos)} 种组合)")
    print(f" 优化目标: {args.metric}")
    print(f" 数据: {base_params.get('data_file', '')}  起始: {base_params.get('start', '')}\n")

    results = []
    for i, combo in enumerate(combos):
        override = dict(zip(keys, combo))
        if (i + 1) % 30 == 0 or i == 0:
            pct = (i + 1) / len(combos) * 100
            print(f"  [{i+1:4d}/{len(combos)}]  {pct:.0f}%  当前: {override}")
        r = run_single(base_params, override)
        if r:
            results.append(r)

    if not results:
        print("\n 没有找到满足条件的参数组合（trades≥15 且 return>0）")
        return

    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values(args.metric, ascending=False).reset_index(drop=True)

    print(f"\n{'═'*80}")
    print(f"  TOP {args.top} 结果  (按 {args.metric} 排序，已过滤 return≤0)")
    print(f"{'═'*80}")
    show_cols = ["# Trades", "Win Rate [%]", "Return [%]",
                 "Sharpe Ratio", "Max. Drawdown [%]", "Profit Factor"] + keys
    show_cols = [c for c in show_cols if c in df_res.columns]
    print(df_res[show_cols].head(args.top).to_string(index=True))

    out = BASE_DIR / "results" / "optimize_tqqq.csv"
    df_res.to_csv(out, index=False)
    print(f"\n 完整结果已保存: {out}")

    best = df_res.iloc[0]
    print(f"\n{'─'*52}")
    print("  最优参数（复制到 config_tqqq.yaml）:")
    print(f"{'─'*52}")
    for k in keys:
        if k in best:
            print(f"    {k}: {best[k]}")


if __name__ == "__main__":
    main()
