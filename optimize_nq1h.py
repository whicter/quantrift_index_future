"""
optimize_nq1h.py — NQ 1H 专项网格搜索

当前问题：
  - ut_key=0.7213 太紧 → 频繁被止损
  - use_squeeze_mr=True → 大量均值回归交易，过度交易
  - 1131笔 / -62.9% 回撤

优化目标：在控制交易频率（15~400笔）的前提下最大化 Sharpe

用法：
  python optimize_nq1h.py                 # quick 模式
  python optimize_nq1h.py --mode full
  python optimize_nq1h.py --metric Return [%]
  python optimize_nq1h.py --max-trades 200  # 限制最大交易数
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

SEARCH_SPACE = {
    "quick": {
        "ut_key":         [0.5, 1.0, 1.5, 2.0, 3.0, 4.0],
        "ssl_len":        [20, 30, 40, 54],
        "min_score":      [3, 4, 5],
        "adx_threshold":  [15.0, 20.0, 25.0, 30.0],
        "use_squeeze_mr": [True, False],
    },
    "full": {
        "ut_key":         [0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0],
        "ssl_len":        [15, 20, 30, 40, 54, 70],
        "min_score":      [3, 4, 5],
        "adx_threshold":  [15.0, 20.0, 25.0, 30.0, 35.0],
        "use_squeeze_mr": [True, False],
        "exit_len":       [10, 14, 17, 20],
    },
}


def safe(v):
    import numpy as np
    if isinstance(v, float) and (v != v or abs(v) == float("inf")):
        return None
    if isinstance(v, pd.Timestamp):
        return str(v)
    return v


def run_single(base_params: dict, override: dict, min_trades: int, max_trades: int) -> dict | None:
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
    ConfluenceStrategy.allow_short          = bool(params.get("allow_short", True))
    ConfluenceStrategy.reversal_score       = int(params.get("reversal_score", 2))
    ConfluenceStrategy.allow_reversal_flip  = bool(params.get("allow_reversal_flip", True))
    ConfluenceStrategy.conflict_threshold   = int(params.get("conflict_threshold", 2))
    ConfluenceStrategy.use_bbmc_dir        = bool(params.get("use_bbmc_dir", True))
    ConfluenceStrategy.use_squeeze_mr      = bool(params.get("use_squeeze_mr", False))
    ConfluenceStrategy.rsi_mr_ob           = float(params.get("rsi_mr_ob", 65.0))
    ConfluenceStrategy.rsi_mr_os           = float(params.get("rsi_mr_os", 35.0))
    ConfluenceStrategy.use_atr_exit        = bool(params.get("use_atr_exit", False))
    ConfluenceStrategy.atr_sl_mult         = float(params.get("atr_sl_mult", 1.5))
    ConfluenceStrategy.use_trend_filter    = bool(params.get("use_trend_filter", False))
    ConfluenceStrategy.n_contracts         = int(params.get("n_contracts", 12))
    ConfluenceStrategy.contract_size       = int(params.get("contract_size", 2))
    ConfluenceStrategy.use_staged_tp       = bool(params.get("use_staged_tp", True))
    ConfluenceStrategy.atr_tp1_mult        = float(params.get("atr_tp1_mult", 1.0))
    ConfluenceStrategy.atr_tp2_mult        = float(params.get("atr_tp2_mult", 2.0))
    ConfluenceStrategy.tp1_portion         = float(params.get("tp1_portion", 0.34))

    try:
        bt = Backtest(
            df,
            ConfluenceStrategy,
            cash            = int(params.get("cash", 100_000)),
            commission      = float(params.get("commission", 0.00002)),
            margin          = float(params.get("margin", 0.05)),
            exclusive_orders= True,
        )
        stats = bt.run()
    except Exception:
        return None

    n_trades = stats.get("# Trades", 0)
    if n_trades < min_trades or n_trades > max_trades:
        return None

    ret = safe(stats.get("Return [%]"))
    if ret is None or ret <= 0:
        return None

    dd = safe(stats.get("Max. Drawdown [%]")) or -999
    if dd < -50:          # 回撤超过 50% 直接排除
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
    parser.add_argument("--config",     default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--mode",       default="quick", choices=["quick", "full"])
    parser.add_argument("--metric",     default="Sharpe Ratio",
                        choices=["Sharpe Ratio", "Return [%]", "Profit Factor"])
    parser.add_argument("--top",        type=int, default=10)
    parser.add_argument("--min-trades", type=int, default=15,  dest="min_trades")
    parser.add_argument("--max-trades", type=int, default=400, dest="max_trades")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    base_params = config["timeframes"]["1h"].copy()
    base_params.setdefault("symbol",      config.get("symbol", "NQ=F"))
    base_params.setdefault("allow_short", config.get("allow_short", True))

    space  = SEARCH_SPACE[args.mode]
    keys   = list(space.keys())
    combos = list(itertools.product(*[space[k] for k in keys]))

    print(f"\n NQ 1H 参数优化  ({args.mode} 模式，{len(combos)} 种组合)")
    print(f" 优化目标: {args.metric}")
    print(f" 交易笔数限制: {args.min_trades} ~ {args.max_trades}")
    print(f" 数据: {base_params.get('data_file', '')}  起始: {base_params.get('start', '')}\n")

    results = []
    for i, combo in enumerate(combos):
        override = dict(zip(keys, combo))
        if (i + 1) % 30 == 0 or i == 0:
            pct = (i + 1) / len(combos) * 100
            print(f"  [{i+1:4d}/{len(combos)}]  {pct:.0f}%  当前: {override}")
        r = run_single(base_params, override, args.min_trades, args.max_trades)
        if r:
            results.append(r)

    if not results:
        print(f"\n 没有找到满足条件的参数组合")
        print(f" 条件：trades∈[{args.min_trades},{args.max_trades}], return>0, DD>-50%")
        return

    df_res = pd.DataFrame(results)
    df_res = df_res.sort_values(args.metric, ascending=False).reset_index(drop=True)

    print(f"\n{'═'*90}")
    print(f"  TOP {args.top} 结果  (按 {args.metric} 排序)")
    print(f"{'═'*90}")
    show_cols = ["# Trades", "Win Rate [%]", "Return [%]",
                 "Sharpe Ratio", "Max. Drawdown [%]", "Profit Factor"] + keys
    show_cols = [c for c in show_cols if c in df_res.columns]
    print(df_res[show_cols].head(args.top).to_string(index=True))

    out = BASE_DIR / "results" / "optimize_nq1h.csv"
    df_res.to_csv(out, index=False)
    print(f"\n 完整结果已保存: {out}")

    best = df_res.iloc[0]
    print(f"\n{'─'*54}")
    print("  最优参数（复制到 config.yaml → 1h 段）:")
    print(f"{'─'*54}")
    for k in keys:
        if k in best:
            print(f"    {k}: {best[k]}")


if __name__ == "__main__":
    main()
