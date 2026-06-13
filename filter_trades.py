"""
从 JSON 报告中过滤指定时间段的交易明细
用法: python filter_trades.py [--since 2026-05-01] [--report results/latest_report.json]
"""
import argparse, sys
import json, pandas as pd
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--since',  default=None,                         help='起始日期 YYYY-MM-DD，默认2周前')
parser.add_argument('--report', default='results/latest_report.json', help='JSON 报告路径')
args = parser.parse_args()

BASE   = Path(__file__).parent
CUTOFF = pd.Timestamp(args.since) if args.since else \
         pd.Timestamp.now().normalize() - pd.Timedelta(days=14)
report_path = BASE / args.report

if not report_path.exists():
    print(f'找不到报告文件: {report_path}')
    sys.exit(1)

with open(report_path) as f:
    report = json.load(f)

print(f'报告: {report_path}  |  起始: {CUTOFF.date()}\n')

for tf in ['1h', '4h', '1d']:
    trades = report.get(tf, {}).get('trades', [])
    if not trades:
        print(f'{tf}: 无交易记录'); continue

    df = pd.DataFrame(trades)
    exit_col  = next((c for c in df.columns if 'exit'  in c.lower() and 'time' in c.lower()), None)
    entry_col = next((c for c in df.columns if 'entry' in c.lower() and 'time' in c.lower()), None)
    if not exit_col or not entry_col:
        print(f'{tf}: 找不到时间列'); continue

    df[exit_col]  = pd.to_datetime(df[exit_col])
    df[entry_col] = pd.to_datetime(df[entry_col])
    recent = df[df[exit_col] >= CUTOFF]

    if recent.empty:
        print(f'{tf}: 该区间无已平仓交易'); continue

    print(f'── {tf} ({CUTOFF.date()} 起，{len(recent)}笔) ──')
    for _, r in recent.iterrows():
        pnl       = r.get('PnL', r.get('pnl', 0))
        ret       = r.get('ReturnPct', r.get('return_pct', 0))
        direction = '多' if r.get('Size', 0) > 0 else '空'
        entry_str = r[entry_col].strftime('%m/%d %H:%M')
        exit_str  = r[exit_col].strftime('%m/%d %H:%M')
        print(f"  {direction} | 入:{entry_str} 出:{exit_str} | PnL:{pnl:.0f} | Ret:{ret:.2%}")
    print()
