"""
fetch_data.py — 一次性下载并缓存行情数据。

用法：
  python fetch_data.py BTC-USD 1d 2020-01-01 2024-12-31
  python fetch_data.py NQ=F   1h 2025-01-01 2025-12-31
"""

import sys
import time
import pandas as pd
import yfinance as yf
from pathlib import Path

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


def fetch(symbol: str, interval: str, start: str, end: str) -> pd.DataFrame:
    safe = symbol.replace("/", "-").replace("=", "")
    cache = DATA_DIR / f"{safe}_{interval}_{start}_{end}.csv"

    if cache.exists():
        print(f"已有缓存: {cache}")
        df = pd.read_csv(cache, index_col=0, parse_dates=True)
        print(f"  {len(df)} 行  {df.index[0].date()} ~ {df.index[-1].date()}")
        return df

    print(f"下载: {symbol} {interval} {start} → {end} ...")
    # 用 curl_cffi 模拟浏览器，绕过 Yahoo Finance 限速
    try:
        from curl_cffi import requests as crequests
        session = crequests.Session(impersonate="chrome")
        ticker = yf.Ticker(symbol, session=session)
        df = ticker.history(start=start, end=end, interval=interval, auto_adjust=True)
        # 整理索引
        if not df.empty:
            df = df.reset_index()
            date_col = [c for c in df.columns if "time" in c.lower() or "date" in c.lower()][0]
            df = df.rename(columns={date_col: "Date"})
            df["Date"] = pd.to_datetime(df["Date"]).dt.tz_localize(None)
            df = df.set_index("Date")
    except ImportError:
        df = yf.download(symbol, start=start, end=end, interval=interval,
                         auto_adjust=True, progress=False)

    if df.empty:
        raise ValueError(f"下载失败，请检查 symbol 或稍后再试。symbol={symbol}")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.index.name = "Date"
    df = df.dropna()
    df.to_csv(cache)
    print(f"  已保存: {cache}  ({len(df)} 行)")
    return df


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("用法: python fetch_data.py <symbol> <interval> <start> <end>")
        print("例如: python fetch_data.py NQ=F 1h 2025-01-01 2025-12-31")
        sys.exit(1)

    symbol, interval, start, end = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
    df = fetch(symbol, interval, start, end)
    print(df.tail(3))
