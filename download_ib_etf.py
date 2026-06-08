"""
download_ib_etf.py — 使用 IB TWS 下载 ETF / 股票历史数据

用法：
  python download_ib_etf.py                        # 下载 TQQQ 1H（默认）
  python download_ib_etf.py --symbol QQQ --tf 1h
  python download_ib_etf.py --symbol TQQQ --tf 1d --start 2020-01-01
  python download_ib_etf.py --port 7497            # 使用 Gateway 端口

前置条件：
  pip install ib_insync
  TWS: Edit → Global Configuration → API → Settings
    ✅ Enable ActiveX and Socket Clients
    ✅ Read-Only API（只读即可）
"""

import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

HOST      = '127.0.0.1'
PORT      = 7496
CLIENT_ID = 16       # 与 download_ib.py 不同，避免冲突

OUTPUT_DIR = Path(__file__).parent / 'data'
OUTPUT_DIR.mkdir(exist_ok=True)

# IB 每个请求的最大时长
DURATION_MAP = {
    '1h':  ('1 hour',  '6 M'),   # 每次最多6个月
    '4h':  ('4 hours', '1 Y'),   # 每次最多1年
    '1d':  ('1 day',   '5 Y'),   # 每次最多5年
}


def strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _bars_to_df(bars) -> pd.DataFrame:
    from ib_insync import util
    if not bars:
        return pd.DataFrame()
    df = util.df(bars)
    df = df.rename(columns={
        'date': 'Date', 'open': 'Open', 'high': 'High',
        'low': 'Low', 'close': 'Close', 'volume': 'Volume',
    })
    df['Date'] = pd.to_datetime(df['Date'])
    df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].set_index('Date').sort_index()
    df = strip_tz(df)
    return df.dropna()


def download_chunk(ib, contract, end_dt: datetime, bar_size: str, duration: str) -> pd.DataFrame:
    """下载单个时间段的历史数据。"""
    end_str = end_dt.strftime('%Y%m%d %H:%M:%S')
    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_str,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=True,    # 只要正常交易时段（9:30-16:00）
            formatDate=1,
        )
    except Exception as e:
        print(f'    [跳过] {end_str}: {e}')
        return pd.DataFrame()
    return _bars_to_df(bars)


def download_etf(ib, symbol: str, tf: str, start: datetime, end: datetime) -> pd.DataFrame:
    """按时间段分块下载 ETF 历史数据。"""
    from ib_insync import Stock

    contract = Stock(symbol, 'SMART', 'USD')
    ib.qualifyContracts(contract)

    bar_size, chunk_duration = DURATION_MAP[tf]
    # chunk_months: 6M→6, 1Y→12, 5Y→60
    chunk_months = {'6 M': 6, '1 Y': 12, '5 Y': 60}.get(chunk_duration, 6)

    # 生成分块的 endDateTime 列表（从 end 往前走）
    chunks = []
    cur_end = end
    while cur_end > start:
        chunks.append(cur_end)
        # 往前移一个 chunk
        m = cur_end.month - chunk_months
        y = cur_end.year
        while m <= 0:
            m += 12
            y -= 1
        cur_end = cur_end.replace(year=y, month=m)

    chunks = list(reversed(chunks))  # 从早到晚

    print(f'\n── 下载 {symbol} {tf.upper()} ({bar_size}) ──────────────────────────')
    print(f'  时间范围: {start.strftime("%Y-%m-%d")} → {end.strftime("%Y-%m-%d")}')
    print(f'  共 {len(chunks)} 个请求块')

    all_dfs = []
    for i, end_dt in enumerate(chunks):
        print(f'  [{i+1}/{len(chunks)}] endDateTime={end_dt.strftime("%Y-%m-%d")} duration={chunk_duration} ...', end=' ')
        df = download_chunk(ib, contract, end_dt, bar_size, chunk_duration)
        if df.empty:
            print('无数据')
        else:
            df = df[df.index >= pd.Timestamp(start)]
            df = df[df.index <= pd.Timestamp(end)]
            print(f'{len(df)} 行')
            if not df.empty:
                all_dfs.append(df)
        time.sleep(1.0)    # IB pacing: 避免触发限速

    if not all_dfs:
        return pd.DataFrame()

    result = pd.concat(all_dfs).sort_index()
    result = result[~result.index.duplicated(keep='last')]
    return result


def save(df: pd.DataFrame, symbol: str, tf: str) -> Path:
    start_str = df.index.min().strftime('%Y-%m-%d')
    end_str   = df.index.max().strftime('%Y-%m-%d')
    fname = OUTPUT_DIR / f'{symbol}_{tf}_{start_str}_{end_str}.csv'
    df.to_csv(fname)
    print(f'\n  ✓ 保存: {fname.name}  ({len(df)} 行)')
    print(f'    价格范围: ${df.Close.min():.2f} ~ ${df.Close.max():.2f}')
    return fname


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--symbol', default='TQQQ')
    parser.add_argument('--tf',     default='1h', choices=['1h', '4h', '1d'])
    parser.add_argument('--start',  default='2022-01-01', help='下载起始日期')
    parser.add_argument('--end',    default=datetime.now().strftime('%Y-%m-%d'))
    parser.add_argument('--port',   type=int, default=PORT)
    args = parser.parse_args()

    start_dt = datetime.strptime(args.start, '%Y-%m-%d')
    end_dt   = datetime.strptime(args.end,   '%Y-%m-%d')

    from ib_insync import IB, util
    ib = IB()
    print(f'连接 IB TWS: {HOST}:{args.port}  clientId={CLIENT_ID}')
    try:
        ib.connect(HOST, args.port, clientId=CLIENT_ID, timeout=20, readonly=True)
    except Exception as e:
        print(f'连接失败: {e}')
        print('请确认 TWS 已启动，并在 API 设置中启用了 Socket 连接。')
        return

    print(f'已连接  账户: {ib.managedAccounts()}')

    df = download_etf(ib, args.symbol, args.tf, start_dt, end_dt)
    ib.disconnect()

    if df.empty:
        print('下载失败，未获取到数据。')
        return

    fname = save(df, args.symbol, args.tf)

    print(f'\n在 config_tqqq.yaml 中使用:')
    print(f'    data_file: data/{fname.name}')
    print(f'    start: \'{df.index.min().strftime("%Y-%m-%d")}\'')
    print(f'    end:   \'{df.index.max().strftime("%Y-%m-%d")}\'')


if __name__ == '__main__':
    main()
