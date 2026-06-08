"""
download_ib.py — 使用 Interactive Brokers API 下载 NQ 期货历史数据

策略：
  逐个季度合约（NQH20, NQM20, NQU20, NQZ20 ...）分段下载，拼接成连续数据。
  这样可以绕过 ContFuture 的历史深度限制（通常只有 2-3 年）。

前置条件:
  pip install ib_insync
  TWS 需处于运行状态，且:
    Edit → Global Configuration → API → Settings
    ✅ Enable ActiveX and Socket Clients
    ❌ Read-Only API（取消勾选）

用法:
  python download_ib.py            # 下载所有级别 (1H / 4H / 1D)
  python download_ib.py --tf 1h   # 只下载指定级别
  python download_ib.py --port 7496
"""

import time
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

# ── 连接配置 ────────────────────────────────────────────────────
HOST      = '127.0.0.1'
PORT      = 7496
CLIENT_ID = 15

# ── 数据配置 ────────────────────────────────────────────────────
SYMBOL     = 'NQ'
EXCHANGE   = 'CME'
CURRENCY   = 'USD'
START_DATE = datetime(2020, 1, 1)
END_DATE   = datetime(2026, 6, 8)

OUTPUT_DIR = Path(__file__).parent / 'data'
OUTPUT_DIR.mkdir(exist_ok=True)

# NQ 季度合约到期月份代码
MONTH_CODES = {3: 'H', 6: 'M', 9: 'U', 12: 'Z'}


def get_quarterly_contracts():
    """生成覆盖 START_DATE ~ END_DATE 的所有季度合约列表。"""
    contracts = []
    year = START_DATE.year
    # 提前一个季度开始，确保第一根bar有上下文
    month = 3
    while True:
        expiry = f'{year}{month:02d}'
        # 合约到期日大约在每月第三个星期五，我们用月末近似
        expiry_dt = datetime(year, month, 28)
        # 前一个季度末作为该合约开始时间
        prev_month = month - 3 if month > 3 else 12
        prev_year  = year if month > 3 else year - 1
        start_dt   = datetime(prev_year, prev_month, 1)

        if start_dt > END_DATE:
            break

        contracts.append({
            'expiry':   expiry,
            'start_dt': start_dt,
            'end_dt':   min(expiry_dt, END_DATE),
        })

        month += 3
        if month > 12:
            month = 3
            year += 1

    return contracts


def strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    """去除时区信息，统一为 naive datetime。"""
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


def download_contract_bars(ib, expiry: str, bar_size: str, duration: str) -> pd.DataFrame:
    """下载单个季度合约的历史数据。用合约真实到期日作为 endDateTime。"""
    from ib_insync import Contract
    lookup = Contract(
        secType='FUT',
        symbol=SYMBOL,
        exchange=EXCHANGE,
        currency=CURRENCY,
        lastTradeDateOrContractMonth=expiry,
        includeExpired=True,
    )
    details = ib.reqContractDetails(lookup)
    if not details:
        return pd.DataFrame()

    contract = details[0].contract

    # 用合约真实到期日（不超过 END_DATE）作为 endDateTime
    actual_expiry_str = contract.lastTradeDateOrContractMonth  # 'YYYYMMDD'
    actual_expiry = datetime.strptime(actual_expiry_str[:8], '%Y%m%d')
    end_use = min(actual_expiry, END_DATE)

    # 如果合约尚未到期（当前合约），用空字符串表示当前时间
    if actual_expiry >= datetime.now():
        end_str = ''
    else:
        # UTC 格式：yyyymmdd-HH:MM:SS（无空格，无时区）
        end_str = end_use.strftime('%Y%m%d-16:00:00')

    try:
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=end_str,
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow='TRADES',
            useRTH=False,
            formatDate=1,
        )
    except Exception as e:
        print(f'    [跳过] {expiry}: {e}')
        return pd.DataFrame()

    return _bars_to_df(bars)


# 每个级别每次请求的时长（覆盖约1个季度的数据量即可）
DURATION_MAP = {
    '1h': ('1 hour',  '4 M'),
    '4h': ('4 hours', '4 M'),
    '1d': ('1 day',   '1 Y'),
}


def download_tf(ib, tf: str) -> pd.DataFrame:
    bar_size, duration = DURATION_MAP[tf]
    contracts = get_quarterly_contracts()
    start_ts  = pd.Timestamp(START_DATE)
    chunks    = []

    print(f'\n── 下载 {tf.upper()} ({bar_size}) ─────────────────────────────────────')
    print(f'  共 {len(contracts)} 个季度合约')

    for c in contracts:
        expiry   = c['expiry']
        end_dt   = c['end_dt']
        start_dt = c['start_dt']

        if end_dt < START_DATE:
            continue

        print(f'  {expiry}  ({start_dt.strftime("%Y-%m")}) → 请求至 {end_dt.strftime("%Y-%m-%d")} ...', end=' ')
        df = download_contract_bars(ib, expiry, bar_size, duration)

        if df.empty:
            print('无数据')
        else:
            # 只保留该合约对应时段
            df = df[df.index >= pd.Timestamp(start_dt)]
            df = df[df.index <= pd.Timestamp(end_dt)]
            print(f'{len(df)} 行')
            if not df.empty:
                chunks.append(df)

        time.sleep(0.6)   # IB 限速：60次/10分钟

    if not chunks:
        print('  无数据，跳过。')
        return pd.DataFrame()

    result = pd.concat(chunks).sort_index()
    result = result[~result.index.duplicated(keep='last')]
    result = result[result.index >= start_ts]
    result = result[result.index <= pd.Timestamp(END_DATE)]
    return result


def save(df: pd.DataFrame, tf: str) -> Path:
    start_str = df.index.min().strftime('%Y-%m-%d')
    end_str   = df.index.max().strftime('%Y-%m-%d')
    fname = OUTPUT_DIR / f'NQF_{tf}_{start_str}_{end_str}.csv'
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
    print(f'连接 IB TWS: {HOST}:{args.port}  clientId={CLIENT_ID}')
    try:
        ib.connect(HOST, args.port, clientId=CLIENT_ID, timeout=20, readonly=False)
    except Exception as e:
        print(f'连接失败: {e}')
        return

    tfs = [args.tf] if args.tf else ['1d', '4h', '1h']
    results = {}

    for tf in tfs:
        df = download_tf(ib, tf)
        if not df.empty:
            fname = save(df, tf)
            results[tf] = fname
        time.sleep(2)

    ib.disconnect()
    print('\n下载完成！')

    if results:
        print('\n' + '═' * 56)
        print('  config.yaml 参考配置:')
        print('═' * 56)
        for tf, fname in results.items():
            print(f'  {tf}:')
            print(f'    data_file: data/{fname.name}')
            if tf == '1d':
                print(f'    start: \'2020-01-01\'')
            else:
                print(f'    start: \'2020-01-01\'')
            print()


if __name__ == '__main__':
    main()
