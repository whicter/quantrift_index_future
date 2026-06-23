"""
backtest_runner.py — 加载配置、下载数据、计算指标、运行回测、输出报告。

支持三周期独立参数（1h / 4h / 1d），每个周期单独优化。

用法：
  python backtest_runner.py                 # 使用 config.yaml，跑所有周期
  python backtest_runner.py --tf 1h         # 只跑指定周期
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

BASE_DIR    = Path(__file__).parent
RESULTS_DIR = BASE_DIR / "results"
RESULTS_DIR.mkdir(exist_ok=True)


# ══════════════════════════════════════════════════════════
# 数据加载
# ══════════════════════════════════════════════════════════

def load_from_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df.columns = [c.strip().lower() for c in df.columns]
    col_map = {
        "time": "Date", "date": "Date", "datetime": "Date",
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume",
    }
    df = df.rename(columns=col_map)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    print(f"  CSV: {len(df)} 行  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


def _cache_path(symbol: str, interval: str, start: str, end: str) -> Path:
    safe = symbol.replace("/", "-").replace("=", "")
    return BASE_DIR / "data" / f"{safe}_{interval}_{start}_{end}.csv"


def download_data(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    import time
    cache = _cache_path(symbol, interval, start, end)
    (BASE_DIR / "data").mkdir(exist_ok=True)

    if cache.exists():
        print(f"  缓存: {cache.name}")
        return load_from_csv(str(cache))

    print(f"  下载: {symbol} {interval} {start} → {end}")
    df = pd.DataFrame()
    for attempt in range(4):
        if attempt > 0:
            wait = 30 * attempt
            print(f"  [限速] 等待 {wait}s 后重试 ({attempt}/3)...")
            time.sleep(wait)
        try:
            try:
                from curl_cffi import requests as cr
                session = cr.Session(impersonate="chrome")
                ticker  = yf.Ticker(symbol, session=session)
                df = ticker.history(start=start, end=end, interval=interval,
                                    auto_adjust=True)
                if not df.empty:
                    df = df.reset_index()
                    date_col = [c for c in df.columns
                                if "time" in c.lower() or "date" in c.lower()][0]
                    df = df.rename(columns={date_col: "Date"})
                    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
                    df = df.set_index("Date")
            except ImportError:
                df = yf.download(symbol, start=start, end=end, interval=interval,
                                 auto_adjust=True, progress=False)
            if not df.empty:
                break
        except Exception as e:
            print(f"  [下载异常] {e}")

    if df.empty:
        raise ValueError(f"无法下载 {symbol} 数据。")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "Date"
    df = df.dropna()
    df.to_csv(cache)
    print(f"  已缓存: {cache.name}  ({len(df)} 行)")
    return df


def load_data(tf_params: dict) -> pd.DataFrame:
    """根据 tf_params 加载单个周期数据（CSV / resample / 下载）。"""
    start = tf_params.get("start", "")

    def _filter_start(df: pd.DataFrame) -> pd.DataFrame:
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        return df

    data_file = tf_params.get("data_file", "")
    if data_file and Path(data_file).exists():
        return _filter_start(load_from_csv(data_file))

    resample_from = tf_params.get("resample_from", "")
    if resample_from and Path(resample_from).exists():
        df_base = load_from_csv(resample_from)
        interval = tf_params.get("interval", "4h")
        df = df_base.resample(interval).agg({
            "Open": "first", "High": "max",
            "Low":  "min",   "Close": "last",
            "Volume": "sum",
        }).dropna()
        df = _filter_start(df)
        print(f"  resample → {interval}: {len(df)} 行")
        return df

    return download_data(
        tf_params["symbol"],
        tf_params["start"],
        tf_params["end"],
        tf_params["interval"],
    )


# ══════════════════════════════════════════════════════════
# 辅助函数
# ══════════════════════════════════════════════════════════

def yearly_breakdown(trades_df: pd.DataFrame) -> dict:
    if trades_df is None or len(trades_df) == 0:
        return {}
    exit_col = "ExitTime"  if "ExitTime"  in trades_df.columns else None
    pnl_col  = "ReturnPct" if "ReturnPct" in trades_df.columns else None
    if not exit_col or not pnl_col:
        return {}
    trades_df = trades_df.copy()
    trades_df["Year"] = pd.to_datetime(trades_df[exit_col]).dt.year
    yearly = {}
    for year, grp in trades_df.groupby("Year"):
        rets = grp[pnl_col]
        yearly[str(year)] = {
            "trades":        int(len(grp)),
            "win_rate_pct":  round(float((rets > 0).mean() * 100), 2),
            "total_ret_pct": round(float(rets.sum() * 100), 2),
            "avg_trade_pct": round(float(rets.mean() * 100), 2),
            "max_win_pct":   round(float(rets.max() * 100), 2),
            "max_loss_pct":  round(float(rets.min() * 100), 2),
        }
    return yearly


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


# ══════════════════════════════════════════════════════════
# 单周期回测
# ══════════════════════════════════════════════════════════

def run_one(tf_name: str, tf_params: dict, plot: bool = False) -> dict:
    from indicators import compute_signals
    from strategy   import ConfluenceStrategy

    print(f"\n  ── {tf_name} ─────────────────────────────────────")

    df = load_data(tf_params)
    df = compute_signals(df, tf_params)

    # 设置策略类变量
    ConfluenceStrategy.min_score     = int(tf_params["min_score"])
    ConfluenceStrategy.adx_threshold = float(tf_params.get("adx_threshold", 20.0))
    ConfluenceStrategy.use_adx       = bool(tf_params.get("use_adx",  True))
    ConfluenceStrategy.vol_mult      = float(tf_params.get("vol_mult", 1.2))
    ConfluenceStrategy.use_vol       = bool(tf_params.get("use_vol",  True))
    ConfluenceStrategy.allow_short          = bool(tf_params.get("allow_short", True))
    ConfluenceStrategy.reversal_score       = int(tf_params.get("reversal_score", 2))
    ConfluenceStrategy.allow_reversal_flip  = bool(tf_params.get("allow_reversal_flip", True))
    ConfluenceStrategy.conflict_threshold   = int(tf_params.get("conflict_threshold", 6))
    ConfluenceStrategy.use_bbmc_dir        = bool(tf_params.get("use_bbmc_dir", False))
    ConfluenceStrategy.use_squeeze_mr      = bool(tf_params.get("use_squeeze_mr", False))
    ConfluenceStrategy.rsi_mr_ob           = float(tf_params.get("rsi_mr_ob", 65.0))
    ConfluenceStrategy.rsi_mr_os           = float(tf_params.get("rsi_mr_os", 35.0))
    ConfluenceStrategy.use_atr_exit        = bool(tf_params.get("use_atr_exit", False))
    ConfluenceStrategy.atr_tp_mult         = float(tf_params.get("atr_tp_mult", 2.0))
    ConfluenceStrategy.atr_sl_mult         = float(tf_params.get("atr_sl_mult", 1.0))
    ConfluenceStrategy.use_trend_filter    = bool(tf_params.get("use_trend_filter", False))
    ConfluenceStrategy.n_contracts         = int(tf_params.get("n_contracts", 0))
    ConfluenceStrategy.contract_size       = int(tf_params.get("contract_size", 2))
    ConfluenceStrategy.use_staged_tp       = bool(tf_params.get("use_staged_tp", False))
    ConfluenceStrategy.atr_tp1_mult        = float(tf_params.get("atr_tp1_mult", 1.0))
    ConfluenceStrategy.atr_tp2_mult        = float(tf_params.get("atr_tp2_mult", 2.0))
    ConfluenceStrategy.tp1_portion         = float(tf_params.get("tp1_portion", 0.34))
    # Pattern exit / entry
    ConfluenceStrategy.use_pattern_exit      = bool(tf_params.get("use_pattern_exit", True))
    ConfluenceStrategy.use_pattern_long_exit = bool(tf_params.get("use_pattern_long_exit", False))
    ConfluenceStrategy.pattern_exit_score    = int(tf_params.get("pattern_exit_score", 2))
    ConfluenceStrategy.use_pattern_entry     = bool(tf_params.get("use_pattern_entry", False))
    ConfluenceStrategy.pattern_entry_score   = int(tf_params.get("pattern_entry_score", 2))
    # VIX 极端恐慌过滤
    ConfluenceStrategy.use_vix_filter        = bool(tf_params.get("use_vix_filter", False))
    ConfluenceStrategy.vix_exit_threshold    = float(tf_params.get("vix_exit_threshold", 40.0))
    ConfluenceStrategy.use_vix_entry         = bool(tf_params.get("use_vix_entry", False))
    ConfluenceStrategy.vix_entry_threshold   = float(tf_params.get("vix_entry_threshold", 40.0))
    # VIX 中度压力 MR（30-40 zone）
    ConfluenceStrategy.use_vix_mr            = bool(tf_params.get("use_vix_mr", False))
    ConfluenceStrategy.vix_mr_lower          = float(tf_params.get("vix_mr_lower", 30.0))
    ConfluenceStrategy.vix_mr_score          = int(tf_params.get("vix_mr_score", 3))

    bt = Backtest(
        df,
        ConfluenceStrategy,
        cash            = int(tf_params.get("cash", 100_000)),
        commission      = float(tf_params.get("commission", 0.00002)),
        margin          = float(tf_params.get("margin", 1.0)),
        exclusive_orders= True,
    )
    stats = bt.run()

    key_metrics = [
        "Start", "End", "Duration",
        "Return [%]", "Buy & Hold Return [%]", "Return (Ann.) [%]",
        "Sharpe Ratio", "Calmar Ratio", "Sortino Ratio",
        "Max. Drawdown [%]", "Avg. Drawdown [%]",
        "# Trades", "Win Rate [%]",
        "Best Trade [%]", "Worst Trade [%]",
        "Avg. Trade [%]", "Avg. Winning Trade [%]", "Avg. Losing Trade [%]",
        "Profit Factor", "Expectancy [%]", "SQN",
    ]
    report = {k: _safe(stats[k]) for k in key_metrics if k in stats}
    report["timeframe"] = tf_name
    report["timestamp"] = datetime.now().isoformat()

    try:
        report["yearly"] = yearly_breakdown(stats._trades)
    except Exception:
        report["yearly"] = {}

    report["params"] = {k: tf_params[k] for k in [
        "ut_key", "ut_atr", "ssl_len", "ssl2_len", "ssl_mult",
        "rsi_len", "macd_fast", "macd_slow", "macd_signal",
        "sqz_bbl", "sqz_bbm", "sqz_kcl", "sqz_kcm",
        "min_score", "ci_len", "ci_threshold", "use_ci",
        "adx_len", "adx_threshold", "use_adx",
        "vol_len", "vol_mult", "use_vol",
    ] if k in tf_params}

    if plot:
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = str(RESULTS_DIR / f"chart_{tf_name}_{ts}.html")
        bt.plot(filename=out, open_browser=False)
        print(f"  图表: {out}")

    return report


# ══════════════════════════════════════════════════════════
# 全周期回测入口
# ══════════════════════════════════════════════════════════

def run_all(config: dict, only_tf: str = None, plot: bool = False) -> dict:
    """运行 config 中所有（或指定）周期，返回 {tf_name: report}。"""
    symbol      = config.get("symbol", "")
    allow_short = config.get("allow_short", True)
    results     = {}

    timeframes = config.get("timeframes", {})
    for tf_name, tf_params in timeframes.items():
        if only_tf and tf_name != only_tf:
            continue
        params = tf_params.copy()
        params.setdefault("symbol",      symbol)
        params.setdefault("allow_short", allow_short)
        try:
            results[tf_name] = run_one(tf_name, params, plot)
        except Exception as e:
            print(f"  [ERROR] {tf_name}: {e}")
            results[tf_name] = {"error": str(e), "timeframe": tf_name}

    return results


def save_results(results: dict):
    """保存结果：latest_report.json（含所有周期）+ 时间戳存档。"""
    latest = RESULTS_DIR / "latest_report.json"
    with open(latest, "w") as f:
        json.dump(results, f, indent=2, default=str)

    ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = RESULTS_DIR / f"report_{ts}.json"
    with open(archive, "w") as f:
        json.dump(results, f, indent=2, default=str)


def print_summary(results: dict):
    print("\n" + "═" * 62)
    print("  多周期回测汇总")
    print("═" * 62)
    print(f"  {'周期':<6} {'笔数':>5} {'胜率':>7} {'收益%':>8} "
          f"{'夏普':>7} {'最大回撤':>9} {'PF':>6}")
    print("  " + "─" * 56)
    for tf, r in results.items():
        if "error" in r:
            print(f"  {tf:<6}  ERROR: {r['error']}")
            continue
        sharpe = r.get("Sharpe Ratio") or 0
        pf     = r.get("Profit Factor") or 0
        print(f"  {tf:<6} "
              f"{(r.get('# Trades') or 0):>5} "
              f"{(r.get('Win Rate [%]') or 0):>6.1f}% "
              f"{(r.get('Return [%]') or 0):>7.2f}% "
              f"{sharpe:>7.3f} "
              f"{(r.get('Max. Drawdown [%]') or 0):>8.1f}% "
              f"{pf:>6.3f}")
    print("═" * 62)

    # 年度拆解
    for tf, r in results.items():
        if r.get("yearly"):
            print(f"\n  ── {tf} 年度 ──────────────────────────────────")
            print(f"  {'年份':<6} {'笔数':>5} {'胜率':>7} {'总收益%':>9} {'均笔%':>8}")
            for yr, d in sorted(r["yearly"].items()):
                print(f"  {yr:<6} {d['trades']:>5} "
                      f"{d['win_rate_pct']:>6.1f}% "
                      f"{d['total_ret_pct']:>8.2f}% "
                      f"{d['avg_trade_pct']:>7.2f}%")


# ── CLI 入口 ───────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--tf",     default=None, help="只跑指定周期 (1h/4h/1d)")
    parser.add_argument("--plot",   action="store_true")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    print(f"\n策略: {config['symbol']}  "
          f"周期: {list(config.get('timeframes', {}).keys())}")

    results = run_all(config, only_tf=args.tf, plot=args.plot)
    save_results(results)
    print_summary(results)
    print(f"\n报告已保存至: results/latest_report.json")
