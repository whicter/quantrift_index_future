"""
ES Mean Reversion — 回测入口

用法：
    cd /Users/cohan/Documents/quantrift_index_future
    .venv/bin/python es_mr/backtest_mr.py
    .venv/bin/python es_mr/backtest_mr.py --config es_mr/config_mr.yaml
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

    # 标准化列名（backtesting.py 需要首字母大写）
    rename = {}
    for col in df.columns:
        lo = col.lower()
        if lo == "open":    rename[col] = "Open"
        elif lo == "high":  rename[col] = "High"
        elif lo == "low":   rename[col] = "Low"
        elif lo == "close": rename[col] = "Close"
        elif lo == "volume":rename[col] = "Volume"
    df = df.rename(columns=rename)

    required = ["Open", "High", "Low", "Close"]
    for c in required:
        if c not in df.columns:
            raise ValueError(f"CSV 缺少列：{c}")

    if "Volume" not in df.columns:
        df["Volume"] = 0

    return df


def run_backtest(cfg: dict, plot: bool = False) -> dict:
    default_data = str(BASE / "data/ESF_1h_2024-06-10_2026-06-08.csv")
    data_file = cfg.get("data_file", default_data)
    # config_mr.yaml 里的相对路径是相对于项目根（BASE），去掉前导 ../
    if not Path(data_file).is_absolute():
        # 把 ../data/xxx 解析为 BASE/data/xxx
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

    # 把 config 里对应的参数名传给策略
    strat_keys = [
        "bb_len", "bb_mult", "rsi_len", "rsi_ob", "rsi_os",
        "atr_len", "adx_len", "adx_threshold", "ci_len", "ci_threshold",
        "vwap_atr_mult", "min_score", "sl_mult", "max_bars",
    ]
    strat_params = {k: cfg[k] for k in strat_keys if k in cfg}

    stats = bt.run(**strat_params)

    print(f"\n{'─'*55}")
    print(f"  ES MR 1h 回测结果")
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
    parser.add_argument("--config", default="es_mr/config_mr.yaml")
    parser.add_argument("--plot", action="store_true")
    args = parser.parse_args()

    config_path = BASE / args.config
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    run_backtest(cfg, plot=args.plot)


if __name__ == "__main__":
    main()
