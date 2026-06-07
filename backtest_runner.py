"""
backtest_runner.py — 加载配置、下载数据、计算指标、运行回测、输出报告。

用法：
  python backtest_runner.py                 # 使用 config.yaml
  python backtest_runner.py --plot          # 同时生成可视化 HTML
"""

import os
import sys
import json
import argparse
import warnings
from datetime import datetime
from pathlib import Path

import yaml
import numpy as np
import pandas as pd
import yfinance as yf
from backtesting import Backtest

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)


def load_config(path: Path = BASE_DIR / "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_from_csv(csv_path: str) -> pd.DataFrame:
    """
    从 CSV 文件加载数据。
    要求列名：Date(索引), Open, High, Low, Close, Volume
    或 TradingView 导出格式（time, open, high, low, close, volume）。
    """
    df = pd.read_csv(csv_path)

    # 统一列名
    df.columns = [c.strip().lower() for c in df.columns]
    col_map = {
        "time": "Date", "date": "Date", "datetime": "Date",
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns=col_map)

    # 设置日期索引
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"  从 CSV 加载: {len(df)} 行  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def _cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    safe = symbol.replace("/", "-").replace("=", "")
    return BASE_DIR / "data" / f"{safe}_{interval}_{start}_{end}.csv"


def download_data(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    """
    下载 OHLCV 数据：
      1. 优先读本地缓存（data/<symbol>_<interval>_<start>_<end>.csv）
      2. 缓存不存在则从 Yahoo Finance 下载并保存缓存
    """
    import time

    cache = _cache_path(symbol, interval, start, end)
    (BASE_DIR / "data").mkdir(exist_ok=True)

    # 命中缓存直接返回
    if cache.exists():
        print(f"  读取缓存: {cache.name}")
        return load_from_csv(str(cache))

    print(f"  下载数据: {symbol} {interval} {start} → {end}")

    df = pd.DataFrame()
    for attempt in range(4):
        if attempt > 0:
            wait = 30 * attempt
            print(f"  [限速] 等待 {wait}s 后重试 ({attempt}/3)...")
            time.sleep(wait)
        try:
            df = yf.download(symbol, start=start, end=end, interval=interval,
                             auto_adjust=True, progress=False)
            if not df.empty:
                break
        except Exception as e:
            print(f"  [下载异常] {e}")

    if df.empty:
        raise ValueError(
            f"无法下载 {symbol} 数据（Yahoo Finance 限速）。\n"
            f"解决方法 A：稍等几分钟再运行。\n"
            f"解决方法 B：在 config.yaml 中设置 data_file: 'data/your_data.csv' "
            f"使用本地 CSV（支持 TradingView 导出格式）。"
        )

    # 拍平 MultiIndex 列
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "Date"
    df = df.dropna()

    # 保存缓存
    df.to_csv(cache)
    print(f"  已缓存至: {cache.name}  ({len(df)} 行)")
    return df


def yearly_breakdown(trades_df: pd.DataFrame) -> dict:
    """按年计算各年度绩效。"""
    if trades_df is None or len(trades_df) == 0:
        return {}

    # backtesting.py trades DataFrame 列名
    exit_col = "ExitTime" if "ExitTime" in trades_df.columns else None
    pnl_col  = "ReturnPct" if "ReturnPct" in trades_df.columns else None

    if exit_col is None or pnl_col is None:
        return {}

    trades_df = trades_df.copy()
    trades_df["Year"] = pd.to_datetime(trades_df[exit_col]).dt.year

    yearly = {}
    for year, grp in trades_df.groupby("Year"):
        rets = grp[pnl_col]
        yearly[str(year)] = {
            "trades":       int(len(grp)),
            "win_rate_pct": round(float((rets > 0).mean() * 100), 2),
            "total_ret_pct": round(float(rets.sum() * 100), 2),
            "avg_trade_pct": round(float(rets.mean() * 100), 2),
            "max_win_pct":  round(float(rets.max() * 100), 2),
            "max_loss_pct": round(float(rets.min() * 100), 2),
        }
    return yearly


def run_backtest(params: dict, plot: bool = False) -> dict:
    from indicators import compute_signals
    from strategy import ConfluenceStrategy

    # 1. 加载数据（优先使用本地 CSV，否则从 Yahoo Finance 下载）
    data_file = params.get("data_file", "")
    if data_file and Path(data_file).exists():
        df = load_from_csv(data_file)
    else:
        df = download_data(params["symbol"], params["start"],
                           params["end"], params["interval"])

    # 2. 计算指标
    print("  计算指标信号...")
    df = compute_signals(df, params)

    # 3. 设置策略参数
    ConfluenceStrategy.min_score = int(params["min_score"])

    # 4. 运行回测
    print("  运行回测...")
    bt = Backtest(
        df,
        ConfluenceStrategy,
        cash=100_000,
        commission=0.001,   # 0.1% 手续费（单边）
        exclusive_orders=True,
    )
    stats = bt.run()

    # 5. 整理报告
    def _safe(v):
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
            return None
        if isinstance(v, (np.integer, np.int64)):
            return int(v)
        if isinstance(v, (np.floating, np.float64)):
            return round(float(v), 4)
        if isinstance(v, pd.Timestamp):
            return str(v)
        return v

    key_metrics = [
        "Start", "End", "Duration",
        "Return [%]", "Buy & Hold Return [%]",
        "Return (Ann.) [%]",
        "Sharpe Ratio", "Calmar Ratio", "Sortino Ratio",
        "Max. Drawdown [%]", "Avg. Drawdown [%]",
        "# Trades", "Win Rate [%]",
        "Best Trade [%]", "Worst Trade [%]",
        "Avg. Trade [%]", "Avg. Winning Trade [%]", "Avg. Losing Trade [%]",
        "Profit Factor", "Expectancy [%]",
        "SQN",
    ]

    report = {}
    for k in key_metrics:
        if k in stats:
            report[k] = _safe(stats[k])

    # 按年度拆解
    try:
        report["yearly"] = yearly_breakdown(stats._trades)
    except Exception:
        report["yearly"] = {}

    # 当前参数快照
    report["params"] = {
        k: params[k]
        for k in [
            "symbol", "interval", "start", "end",
            "ut_key", "ut_atr",
            "ssl_len", "ssl2_len", "ssl_mult",
            "rsi_len", "macd_fast", "macd_slow", "macd_signal",
            "sqz_bbl", "sqz_bbm", "sqz_kcl", "sqz_kcm",
            "min_score", "ci_len", "ci_threshold", "use_ci",
        ]
        if k in params
    }
    report["timestamp"] = datetime.now().isoformat()

    # 6. 保存
    latest = RESULTS_DIR / "latest_report.json"
    with open(latest, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # 也按时间戳存档
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = RESULTS_DIR / f"report_{ts}.json"
    with open(archive, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # 7. 可选：生成 HTML 图表
    if plot:
        plot_path = str(RESULTS_DIR / f"chart_{ts}.html")
        bt.plot(filename=plot_path, open_browser=False)
        print(f"  图表已保存: {plot_path}")

    return report


def print_report(report: dict):
    """在终端打印简洁报告。"""
    print("\n" + "═" * 52)
    print("  回测结果报告")
    print("═" * 52)
    keys = [
        ("Return [%]",          "总收益"),
        ("Buy & Hold Return [%]", "持有收益"),
        ("Sharpe Ratio",        "夏普比率"),
        ("Calmar Ratio",        "卡玛比率"),
        ("Max. Drawdown [%]",   "最大回撤"),
        ("Win Rate [%]",        "胜率"),
        ("# Trades",            "交易次数"),
        ("Avg. Trade [%]",      "平均单笔"),
        ("Profit Factor",       "盈亏比"),
    ]
    for k, label in keys:
        v = report.get(k)
        if v is not None:
            print(f"  {label:<10}  {v}")

    yearly = report.get("yearly", {})
    if yearly:
        print("\n  ── 年度拆解 ────────────────────────────────")
        print(f"  {'年份':<6} {'交易':>5} {'胜率':>7} {'总收益':>8} {'均笔':>8}")
        for yr in sorted(yearly):
            d = yearly[yr]
            print(f"  {yr:<6} {d['trades']:>5} "
                  f"{d['win_rate_pct']:>6.1f}% "
                  f"{d['total_ret_pct']:>7.2f}% "
                  f"{d['avg_trade_pct']:>7.2f}%")

    print("═" * 52)


# ── CLI 入口 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--plot",   action="store_true", help="生成 HTML 图表")
    args = parser.parse_args()

    params = load_config(Path(args.config))
    print(f"\n策略: {params['symbol']} {params['interval']}  "
          f"min_score={params['min_score']}")

    report = run_backtest(params, plot=args.plot)
    print_report(report)
    print(f"\n报告已保存至: results/latest_report.json")
