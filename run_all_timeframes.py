"""
run_all_timeframes.py — 下载 NQ 四个周期数据，分别回测，生成四份报告。
每笔交易按 1 手 NQ E-mini（1点=$20）计算实际美元盈亏。
"""

import warnings, json
warnings.filterwarnings("ignore")

from pathlib import Path
import pandas as pd
import numpy as np
import yfinance as yf
from backtesting import Backtest

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
RESULTS_DIR = BASE_DIR / "results"
DATA_DIR.mkdir(exist_ok=True)
RESULTS_DIR.mkdir(exist_ok=True)

NQ_MULTIPLIER = 20   # 1 NQ 点 = $20
SYMBOL = "NQ=F"
START  = "2025-01-01"
END    = "2026-06-07"

# ══════════════════════════════════════════
# 1. 下载 / 读取数据
# ══════════════════════════════════════════

def get_session():
    from curl_cffi import requests as cr
    return cr.Session(impersonate="chrome")

def download_yf(symbol, interval, start, end):
    """用 curl_cffi session 下载，返回整洁的 DataFrame。"""
    session = get_session()
    ticker  = yf.Ticker(symbol, session=session)
    df = ticker.history(start=start, end=end, interval=interval, auto_adjust=True)
    if df.empty:
        raise ValueError(f"下载失败: {symbol} {interval}")
    df = df.reset_index()
    date_col = [c for c in df.columns if "time" in c.lower() or "date" in c.lower()][0]
    df = df.rename(columns={date_col: "Date"})
    df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
    df = df.set_index("Date")
    df = df[["Open","High","Low","Close","Volume"]].dropna()
    return df

def load_or_download(symbol, interval, start, end):
    safe = symbol.replace("=","").replace("/","-")
    cache = DATA_DIR / f"{safe}_{interval}_{start}_{end}.csv"
    if cache.exists():
        print(f"  [cache] {cache.name}")
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        return df
    print(f"  [下载]  {symbol} {interval} {start}→{end}")
    df = download_yf(symbol, interval, start, end)
    df.to_csv(cache)
    print(f"  [保存]  {cache.name}  ({len(df)} 行)")
    return df

def resample_4h(df_1h):
    """从 1H 数据聚合为 4H。"""
    df = df_1h.resample("4h").agg({
        "Open":  "first",
        "High":  "max",
        "Low":   "min",
        "Close": "last",
        "Volume":"sum",
    }).dropna()
    return df

# ══════════════════════════════════════════
# 2. 策略 (size=1 固定 1 手)
# ══════════════════════════════════════════

from strategy import ConfluenceStrategy as _Base

class NQStrategy(_Base):
    """固定 1 手交易。"""
    min_score: int = 4

    def next(self):
        # 强制 size=1：临时替换 buy/sell
        orig_buy  = self.buy
        orig_sell = self.sell
        def buy1(**kw):  kw["size"] = 1; return orig_buy(**kw)
        def sell1(**kw): kw["size"] = 1; return orig_sell(**kw)
        self.buy  = buy1
        self.sell = sell1
        super().next()
        self.buy  = orig_buy
        self.sell = orig_sell

# ══════════════════════════════════════════
# 3. 回测 + 计算 NQ 美元盈亏
# ══════════════════════════════════════════

from indicators import compute_signals

DEFAULT_PARAMS = {
    "ut_key": 1.0, "ut_atr": 10,
    "ssl_len": 60, "ssl2_len": 5, "ssl_mult": 0.2,
    "rsi_len": 14, "rsi_ob": 70, "rsi_os": 30,
    "macd_fast": 12, "macd_slow": 26, "macd_signal": 9,
    "sqz_bbl": 20, "sqz_bbm": 2.0, "sqz_kcl": 20, "sqz_kcm": 1.5,
    "min_score": 4, "ci_len": 14, "ci_threshold": 61.8, "use_ci": True,
}

def compute_nq_usd_pnl(trades_df):
    """从 trades DataFrame 计算 NQ 实际美元盈亏（1手=$20/点）。"""
    if trades_df is None or len(trades_df) == 0:
        return 0.0, 0.0, []
    details = []
    total = 0.0
    for _, t in trades_df.iterrows():
        entry = float(t.get("EntryPrice", 0))
        exit_ = float(t.get("ExitPrice",  0))
        size  = float(t.get("Size", 1))
        direction = 1 if size > 0 else -1
        usd = direction * (exit_ - entry) * abs(size) * NQ_MULTIPLIER
        total += usd
        details.append({
            "entry": round(entry, 2),
            "exit":  round(exit_, 2),
            "dir":   "LONG" if direction > 0 else "SHORT",
            "pts":   round(direction * (exit_ - entry), 2),
            "usd":   round(usd, 2),
        })
    wins = [d for d in details if d["usd"] > 0]
    return round(total, 2), round(total / len(details), 2) if details else 0, details

def yearly_breakdown(trades_df):
    if trades_df is None or len(trades_df) == 0: return {}
    trades_df = trades_df.copy()
    trades_df["Year"] = pd.to_datetime(trades_df["ExitTime"]).dt.year
    out = {}
    for yr, g in trades_df.groupby("Year"):
        rets = g["ReturnPct"]
        # NQ USD per year
        usd_yr = 0.0
        for _, t in g.iterrows():
            d = 1 if float(t.get("Size",1)) > 0 else -1
            usd_yr += d * (float(t["ExitPrice"]) - float(t["EntryPrice"])) * abs(float(t.get("Size",1))) * NQ_MULTIPLIER
        out[str(yr)] = {
            "trades": int(len(g)),
            "win_rate_pct": round(float((rets > 0).mean() * 100), 1),
            "total_usd": round(usd_yr, 2),
            "avg_trade_usd": round(usd_yr / len(g), 2),
        }
    return out

def run_tf(tf_name, df_raw, params):
    print(f"\n  ── {tf_name} ({len(df_raw)} 根K线) ──────────────────")
    df = compute_signals(df_raw, params)
    NQStrategy.min_score = params["min_score"]

    bt = Backtest(df, NQStrategy,
                  cash=200_000,        # 足够的账户资金
                  commission=0.00005,  # NQ手续费约$4/手，忽略不计
                  exclusive_orders=True)
    stats = bt.run()

    trades_df = stats._trades

    def safe(v):
        if isinstance(v, float) and (np.isnan(v) or np.isinf(v)): return None
        if isinstance(v, (np.integer,)): return int(v)
        if isinstance(v, (np.floating,)): return round(float(v), 4)
        if isinstance(v, pd.Timestamp): return str(v)
        return v

    total_usd, avg_usd, trade_list = compute_nq_usd_pnl(trades_df)
    wins = [t for t in trade_list if t["usd"] > 0]

    report = {
        "timeframe": tf_name,
        "symbol": SYMBOL,
        "bars": len(df_raw),
        "period": f"{df_raw.index[0].date()} ~ {df_raw.index[-1].date()}",
        # 核心指标
        "trades":          safe(stats["# Trades"]),
        "win_rate_pct":    safe(stats["Win Rate [%]"]),
        "return_pct":      safe(stats["Return [%]"]),
        "sharpe":          safe(stats["Sharpe Ratio"]),
        "max_drawdown_pct":safe(stats["Max. Drawdown [%]"]),
        "profit_factor":   safe(stats.get("Profit Factor")),
        # NQ 美元盈亏（1手 × $20/点）
        "nq_total_usd":    total_usd,
        "nq_avg_per_trade_usd": avg_usd,
        "nq_wins":         len(wins),
        "nq_losses":       len(trade_list) - len(wins),
        # 年度
        "yearly": yearly_breakdown(trades_df),
        # 每笔交易详情（最多保留 200 笔，太多了省略）
        "trades_detail": trade_list[:200],
        "params": params,
    }

    out_path = RESULTS_DIR / f"report_NQ_{tf_name}.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"  → 报告已保存: {out_path.name}")
    return report

# ══════════════════════════════════════════
# 4. 主流程
# ══════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print(f"  NQ 期货多周期回测  {START} ~ {END}")
    print("=" * 60)

    # ── 下载 / 读取数据 ───────────────────────────────────────────
    print("\n[1] 准备数据...")

    # 30m 数据：yfinance 仅支持最近 60 天；若下载失败则跳过
    START_30M = "2026-04-08"
    df_30m = None
    cache_30m = DATA_DIR / f"NQF_30m_{START_30M}_{END}.csv"
    if cache_30m.exists():
        df_30m = pd.read_csv(cache_30m, index_col=0, parse_dates=True)
        print(f"  [cache] {cache_30m.name}  ({len(df_30m)} 行)")
    else:
        try:
            df_30m = load_or_download(SYMBOL, "30m", START_30M, END)
        except Exception as e:
            print(f"  [跳过 30m] 下载失败（Yahoo Finance 限速）: {e}")

    df_1h  = load_or_download(SYMBOL, "1h",  START, END)
    df_4h  = resample_4h(df_1h)
    print(f"  [resample] 4H: {len(df_4h)} 行（从 1H 聚合）")
    df_1d  = load_or_download(SYMBOL, "1d",  "2020-01-01", END)

    # ── 分别回测 ──────────────────────────────────────────────────
    print("\n[2] 逐周期回测...")
    results = {}
    timeframes = [("1H", df_1h), ("4H", df_4h), ("1D", df_1d)]
    if df_30m is not None:
        timeframes = [("30m", df_30m)] + timeframes
    for tf_name, df_raw in timeframes:
        r = run_tf(tf_name, df_raw.copy(), DEFAULT_PARAMS)
        results[tf_name] = r

    # ── 汇总表格 ──────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print(f"  NQ 多周期回测汇总（1手 NQ E-mini，每点 $20）")
    print("=" * 70)
    print(f"  {'周期':<6} {'笔数':>5} {'胜率':>7} {'总盈亏USD':>12} "
          f"{'均笔USD':>10} {'夏普':>7} {'最大回撤':>9}")
    print("  " + "-" * 64)
    for tf, r in results.items():
        sharpe = r['sharpe'] if r['sharpe'] is not None else float('nan')
        print(f"  {tf:<6} {r['trades']:>5} "
              f"{r['win_rate_pct']:>6.1f}% "
              f"${r['nq_total_usd']:>11,.0f} "
              f"${r['nq_avg_per_trade_usd']:>9,.0f} "
              f"{sharpe:>7.3f} "
              f"{r['max_drawdown_pct']:>8.1f}%")
    print("=" * 70)

    # 保存汇总
    summary_path = RESULTS_DIR / "summary_NQ_all_timeframes.json"
    with open(summary_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  汇总已保存: {summary_path.name}")
