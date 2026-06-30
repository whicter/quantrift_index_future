"""
NQ Mean Reversion — 回测入口

用法：
    cd /Users/congrenhan/Documents/quantrift_index_future
    .venv/bin/python nq_mr/backtest_nq_mr.py
    .venv/bin/python nq_mr/backtest_nq_mr.py --config nq_mr/config_nq_mr.yaml
"""
import sys
import argparse
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
import yaml
from backtesting import Backtest

from es_mr.strategy_mr import MeanReversionStrategy


def load_data(data_file: str) -> pd.DataFrame:
    df = pd.read_csv(data_file, index_col=0, parse_dates=True)
    df.index = pd.to_datetime(df.index, utc=True).tz_convert(None)

    rename = {}
    for col in df.columns:
        lo = col.lower()
        if lo == "open":     rename[col] = "Open"
        elif lo == "high":   rename[col] = "High"
        elif lo == "low":    rename[col] = "Low"
        elif lo == "close":  rename[col] = "Close"
        elif lo == "volume": rename[col] = "Volume"
    df = df.rename(columns=rename)

    required = ["Open", "High", "Low", "Close"]
    for c in required:
        if c not in df.columns:
            raise ValueError(f"CSV 缺少列：{c}")
    if "Volume" not in df.columns:
        df["Volume"] = 0

    return df


def run_backtest(cfg: dict, plot: bool = False) -> dict:
    default_data = str(BASE / "data/NQF_1h_2024-03-01_2026-06-24.csv")
    data_file = cfg.get("data_file", default_data)
    if not Path(data_file).is_absolute():
        data_file = str((BASE / data_file).resolve())

    df = load_data(data_file)

    start = cfg.get("start")
    end   = cfg.get("end")
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]

    bt = Backtest(
        df,
        MeanReversionStrategy,
        cash=cfg.get("cash", 100_000),
        commission=cfg.get("commission", 0.00002),
        margin=cfg.get("margin", 0.05),
        trade_on_close=False,
        exclusive_orders=True,
    )

    strat_keys = [
        "bb_len", "bb_mult", "rsi_len", "rsi_ob", "rsi_os",
        "atr_len", "adx_len", "adx_threshold", "ci_len", "ci_threshold",
        "vwap_atr_mult", "min_score", "sl_mult", "tp_atr_mult", "max_bars",
        "n_contracts",
    ]
    strat_params = {k: cfg[k] for k in strat_keys if k in cfg}

    stats = bt.run(**strat_params)

    print(f"\n{'─'*55}")
    print(f"  NQ MR 1h 回测结果  ({cfg.get('start')} ~ {cfg.get('end')})")
    print(f"{'─'*55}")
    keys = [
        "# Trades", "Win Rate [%]", "Return [%]",
        "Sharpe Ratio", "Max. Drawdown [%]", "Profit Factor",
        "Avg. Trade Duration",
    ]
    for k in keys:
        if k in stats:
            val = stats[k]
            if isinstance(val, float):
                print(f"  {k:<30} {val:.3f}")
            else:
                print(f"  {k:<30} {val}")
    print(f"{'─'*55}")

    if plot:
        bt.plot()

    return {k: stats.get(k) for k in keys}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="nq_mr/config_nq_mr.yaml")
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()

    config_path = BASE / args.config
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    run_backtest(cfg, plot=args.plot)


if __name__ == "__main__":
    main()
