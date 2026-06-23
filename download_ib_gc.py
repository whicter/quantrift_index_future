"""
download_ib_gc.py — 通过 IB API 下载 GC 黄金期货历史数据

黄金期货 (COMEX) 合约月份：G(Feb) J(Apr) M(Jun) Q(Aug) V(Oct) Z(Dec)

用法：
  python download_ib_gc.py            # 下载所有级别 (1H / 4H / 1D)
  python download_ib_gc.py --tf 4h
  python download_ib_gc.py --port 4001
"""

import time
import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

HOST      = '127.0.0.1'
PORT      = 4001
CLIENT_ID = 16

SYMBOL    = 'GC'
EXCHANGE  = 'COMEX'
CURRENCY  = 'USD'
START_DATE = datetime(2020, 1, 1)
END_DATE   = datetime(2026, 6, 22)

OUTPUT_DIR = Path(__file__).parent / 'data'
OUTPUT_DIR.mkdir(exist_ok=True)

# GC 双月合约代码
MONTH_CODES = {2: 'G', 4: 'J', 6: 'M', 8: 'Q', 10: 'V', 12: 'Z'}


def get_bimonthly_contracts():
    contracts = []
    year  = START_DATE.year
    month = 2   # 从 Feb 开始
    while True:
        code  = MONTH_CODES[month]
        expiry = f'{year}{month:02d}'
        expiry_dt = datetime(year, month, 28)
        # 上一个双月末作为起始
        prev_m = month - 2 if month > 2 else 12
        prev_y = year if month > 2 else year - 1
        start_dt = datetime(prev_y, prev_m, 1)
        if start_dt > END_DATE:
            break
        contracts.append({
            'expiry':   expiry,
            'start_dt': start_dt,
            'end_dt':   min(expiry_dt, END_DATE),
        })
        month += 2
        if month > 12:
            month = 2
            year += 1
    return contracts


def strip_tz(df):
    if hasattr(df.index, 'tz') and df.index.tz is not None:
        df.index = df.index.tz_localize(None)
    return df


def _bars_to_df(bars):
    from ib_insync import util
    if not bars:
        return pd.DataFrame()
    df = util.df(bars).rename(columns={
        'date': 'Date', 'open': 'Open', 'high': 'High',
        'low': 'Low', 'close': 'Close', 'volume': 'Volume',
    })
    df['Date'] = pd.to_datetime(df['Date'])
    df = df[['Date', 'Open', 'High', 'Low', 'Close', 'Volume']].set_index('Date').sort_index()
    return strip_tz(df).dropna()


def download_contract_bars(ib, expiry, bar_size, duration):
    from ib_insync import Contract
    lookup = Contract(secType='FUT', symbol=SYMBOL, exchange=EXCHANGE,
                      currency=CURRENCY,
                      lastTradeDateOrContractMonth=expiry,
                      includeExpired=True)
    details = ib.reqContractDetails(lookup)
    if not details:
        return pd.DataFrame()
    contract = details[0].contract
    actual_expiry = datetime.strptime(
        contract.lastTradeDateOrContractMonth[:8], '%Y%m%d')
    end_use = min(actual_expiry, END_DATE)
    end_str = '' if actual_expiry >= datetime.now() \
              else end_use.strftime('%Y%m%d-18:00:00')
    try:
        bars = ib.reqHistoricalData(
            contract, endDateTime=end_str,
            durationStr=duration, barSizeSetting=bar_size,
            whatToShow='TRADES', useRTH=False, formatDate=1,
        )
    except Exception as e:
        print(f'    [跳过] {expiry}: {e}')
        return pd.DataFrame()
    return _bars_to_df(bars)


DURATION_MAP = {
    '1h': ('1 hour',  '3 M'),
    '4h': ('4 hours', '3 M'),
    '1d': ('1 day',   '1 Y'),
}


def download_tf(ib, tf):
    bar_size, duration = DURATION_MAP[tf]
    contracts = get_bimonthly_contracts()
    start_ts  = pd.Timestamp(START_DATE)
    chunks    = []
    print(f'\n── 下载 GC {tf.upper()} ({bar_size}) ──────────────────────────')
    for c in contracts:
        if c['end_dt'] < START_DATE:
            continue
        print(f"  {c['expiry']}  → {c['end_dt'].strftime('%Y-%m-%d')} ...", end=' ')
        df = download_contract_bars(ib, c['expiry'], bar_size, duration)
        if df.empty:
            print('无数据')
        else:
            df = df[(df.index >= pd.Timestamp(c['start_dt'])) &
                    (df.index <= pd.Timestamp(c['end_dt']))]
            print(f'{len(df)} 行')
            if not df.empty:
                chunks.append(df)
        time.sleep(0.6)

    if not chunks:
        return pd.DataFrame()
    result = pd.concat(chunks).sort_index()
    result = result[~result.index.duplicated(keep='last')]
    return result[(result.index >= start_ts) &
                  (result.index <= pd.Timestamp(END_DATE))]


def save(df, tf):
    start_str = df.index.min().strftime('%Y-%m-%d')
    end_str   = df.index.max().strftime('%Y-%m-%d')
    fname = OUTPUT_DIR / f'GCF_{tf}_{start_str}_{end_str}.csv'
    df.to_csv(fname)
    print(f'  ✓ 保存: {fname.name}  ({len(df)} 行)')
    return fname


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--tf',   choices=['1h', '4h', '1d'], default=None)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()

    from ib_insync import IB
    ib = IB()
    print(f'连接 IB: {HOST}:{args.port}  clientId={CLIENT_ID}')
    try:
        ib.connect(HOST, args.port, clientId=CLIENT_ID, timeout=20, readonly=False)
    except Exception as e:
        print(f'连接失败: {e}'); return

    tfs = [args.tf] if args.tf else ['1d', '4h', '1h']
    for tf in tfs:
        df = download_tf(ib, tf)
        if not df.empty:
            fname = save(df, tf)
            print(f'  config 配置: data_file: data/{fname.name}')
        time.sleep(2)

    ib.disconnect()
    print('\n下载完成！')


if __name__ == '__main__':
    main()
