"""
optimize_nq4h1d.py — NQ 4H / 1D 专项网格搜索，目标：降低最大回撤

当前问题：
  - 4H: -39.3% DD
  - 1D: -36.0% DD

优化目标：在 DD > -30% 的约束下最大化 Sharpe

用法：
  python optimize_nq4h1d.py --tf 4h              # 只优化 4H
  python optimize_nq4h1d.py --tf 1d              # 只优化 1D
  python optimize_nq4h1d.py --tf 4h --mode full
  python optimize_nq4h1d.py --tf 1d --max-dd -25  # 更严格的回撤限制
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
    "4h": {
        "quick": {
            "ut_key":           [1.0, 2.0, 3.0, 4.0, 5.0],
            "ssl_len":          [15, 22, 30, 40, 54],
            "min_score":        [3, 4, 5],
            "adx_threshold":    [25.0, 30.0, 35.0, 40.0],
            "use_trend_filter": [True, False],
        },
        "full": {
            "ut_key":           [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0],
            "ssl_len":          [10, 15, 22, 30, 40, 54, 70],
            "min_score":        [3, 4, 5, 6],
            "adx_threshold":    [20.0, 25.0, 30.0, 35.0, 40.0],
            "use_trend_filter": [True, False],
        },
    },
    "1d": {
        "quick": {
            "ut_key":           [1.0, 2.0, 3.0, 4.0, 5.0],
            "ssl_len":          [30, 40, 54, 70, 100],
            "min_score":        [3, 4, 5],
            "adx_threshold":    [20.0, 25.0, 30.0, 35.0],
            "use_trend_filter": [True, False],
        },
        "full": {
            "ut_key":           [0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0],
            "ssl_len":          [20, 30, 40, 54, 70, 100, 120],
            "min_score":        [3, 4, 5, 6],
            "adx_threshold":    [15.0, 20.0, 25.0, 30.0, 35.0, 40.0],
            "use_trend_filter": [True, False],
        },
    },
}

MIN_TRADES = {"4h": 10, "1d": 5}


def safe(v):
    import numpy as np
    if isinstance(v, float) and (v != v or abs(v) == float("inf")):
        return None
    if isinstance(v, pd.Timestamp):
        return str(v)
    return v


def run_single(base_params: dict, override: dict, min_trades: int, max_dd: float) -> dict | None:
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
    if n_trades < min_trades:
        return None

    ret = safe(stats.get("Return [%]"))
    if ret is None or ret <= 0:
        return None

    dd = safe(stats.get("Max. Drawdown [%]")) or -999
    if dd < max_dd:
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


def optimize_tf(tf: str, base_params: dict, mode: str, metric: str,
                top: int, max_dd: float) -> pd.DataFrame | None:
    space  = SEARCH_SPACE[tf][mode]
    keys   = list(space.keys())
    combos = list(itertools.product(*[space[k] for k in keys]))
    min_t  = MIN_TRADES[tf]

    print(f"\n NQ {tf.upper()} 参数优化  ({mode} 模式，{len(combos)} 种组合)")
    print(f" 优化目标: {metric}   DD 上限: {max_dd}%")
    print(f" 数据: {base_params.get('data_file', '')}  起始: {base_params.get('start', '')}\n")

    results = []
    for i, combo in enumerate(combos):
        override = dict(zip(keys, combo))
        if (i + 1) % 50 == 0 or i == 0:
            pct = (i + 1) / len(combos) * 100
            print(f"  [{i+1:4d}/{len(combos)}]  {pct:.0f}%  当前: {override}")
        r = run_single(base_params, override, min_t, max_dd)
        if r:
            results.append(r)

    if not results:
        print(f"\n 没有找到满足条件的组合（trades≥{min_t}, return>0, DD>{max_dd}%）")
        return None

    df_res = pd.DataFrame(results).sort_values(metric, ascending=False).reset_index(drop=True)

    print(f"\n{'═'*90}")
    print(f"  TOP {top} 结果  [{tf.upper()}]  (按 {metric} 排序)")
    print(f"{'═'*90}")
    show_cols = ["# Trades", "Win Rate [%]", "Return [%]",
                 "Sharpe Ratio", "Max. Drawdown [%]", "Profit Factor"] + keys
    show_cols = [c for c in show_cols if c in df_res.columns]
    print(df_res[show_cols].head(top).to_string(index=True))

    out = BASE_DIR / "results" / f"optimize_nq{tf}.csv"
    df_res.to_csv(out, index=False)
    print(f"\n 完整结果已保存: {out}")

    best = df_res.iloc[0]
    print(f"\n{'─'*54}")
    print(f"  最优参数（复制到 config.yaml → {tf} 段）:")
    print(f"{'─'*54}")
    for k in keys:
        if k in best:
            print(f"    {k}: {best[k]}")

    return df_res


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",  default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--tf",      default="4h", choices=["4h", "1d"])
    parser.add_argument("--mode",    default="quick", choices=["quick", "full"])
    parser.add_argument("--metric",  default="Sharpe Ratio",
                        choices=["Sharpe Ratio", "Return [%]", "Profit Factor"])
    parser.add_argument("--top",     type=int, default=10)
    parser.add_argument("--max-dd",  type=float, default=-30.0, dest="max_dd",
                        help="最大允许回撤（负数，如 -30.0）")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    base_params = config["timeframes"][args.tf].copy()
    base_params.setdefault("symbol",      config.get("symbol", "NQ=F"))
    base_params.setdefault("allow_short", config.get("allow_short", True))

    optimize_tf(args.tf, base_params, args.mode, args.metric, args.top, args.max_dd)


if __name__ == "__main__":
    main()
