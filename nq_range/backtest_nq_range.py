"""
NQ Range Strategy — 回测入口

用法：
    cd /Users/congrenhan/Documents/quantrift_index_future
    /opt/homebrew/bin/python3.11 nq_range/backtest_nq_range.py
    /opt/homebrew/bin/python3.11 nq_range/backtest_nq_range.py --config nq_range/config_nq_range.yaml
    /opt/homebrew/bin/python3.11 nq_range/backtest_nq_range.py --tf 15min
    /opt/homebrew/bin/python3.11 nq_range/backtest_nq_range.py --tf 30min
"""
import sys
import argparse
from pathlib import Path
import glob

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
import yaml
from backtesting import Backtest
from nq_range.strategy_nq_range import NQRangeStrategy


def load_data(data_file: str) -> pd.DataFrame:
    df = pd.read_csv(data_file, index_col=0, parse_dates=True)
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    rename = {c: c.capitalize() for c in df.columns}
    rename.update({'open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
    df = df.rename(columns=rename)
    required = ["Open", "High", "Low", "Close"]
    for c in required:
        if c not in df.columns:
            raise ValueError(f"CSV 缺少列：{c}")
    if "Volume" not in df.columns:
        df["Volume"] = 0
    return df


def find_data_file(tf: str) -> str:
    """根据时间级别自动找覆盖时间最长的文件（起始日期最早）"""
    import os
    pattern = str(BASE / f"data/NQF_{tf}_*.csv")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"找不到 {tf} 数据文件: {pattern}")
    # 按文件名起始日期排序，取最早的（数据最多）
    return sorted(files, key=lambda p: os.path.basename(p).split("_")[2])[0]


def run_backtest(cfg: dict, label: str = "", plot: bool = False) -> dict:
    data_file = cfg.get("data_file")
    if not data_file:
        tf = cfg.get("interval", "1h")
        data_file = find_data_file(tf)
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
        NQRangeStrategy,
        cash=cfg.get("cash", 100_000),
        commission=cfg.get("commission", 0.00002),
        margin=cfg.get("margin", 0.05),
        trade_on_close=False,
        exclusive_orders=True,
    )

    strat_keys = [
        "bb_len", "bb_mult", "rsi_len", "rsi_entry",
        "atr_len", "adx_len", "adx_threshold", "ci_len", "ci_threshold",
        "sl_mult",
    ]
    strat_params = {k: cfg[k] for k in strat_keys if k in cfg}

    stats = bt.run(**strat_params)

    hdr = label or f"NQ Range {cfg.get('interval','?')} ({df.index.min().date()} ~ {df.index.max().date()})"
    print(f"\n{'─'*60}")
    print(f"  {hdr}")
    print(f"{'─'*60}")
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
    print(f"{'─'*60}")

    if plot:
        bt.plot()

    return {k: stats.get(k) for k in keys}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="nq_range/config_nq_range.yaml")
    parser.add_argument("--tf", choices=["1h", "15min", "30min"], default=None,
                        help="时间级别（自动找对应数据文件）")
    parser.add_argument("--plot", action="store_true")
    parser.add_argument("--all-tf", action="store_true", help="运行所有时间级别对比")
    args = parser.parse_args()

    config_path = BASE / args.config
    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    if args.all_tf:
        for tf in ["1h", "15min", "30min"]:
            tf_cfg = dict(cfg)
            tf_cfg["interval"] = tf
            tf_cfg.pop("data_file", None)
            tf_cfg.pop("start", None)
            tf_cfg.pop("end", None)
            try:
                run_backtest(tf_cfg, label=f"NQ Range {tf}", plot=False)
            except FileNotFoundError as e:
                print(f"\n  [跳过 {tf}]: {e}")
    elif args.tf:
        tf_cfg = dict(cfg)
        tf_cfg["interval"] = args.tf
        tf_cfg.pop("data_file", None)
        tf_cfg.pop("start", None)
        tf_cfg.pop("end", None)
        run_backtest(tf_cfg, label=f"NQ Range {args.tf}", plot=args.plot)
    else:
        run_backtest(cfg, plot=args.plot)


if __name__ == "__main__":
    main()
