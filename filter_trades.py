import json, pandas as pd

with open('results/latest_report.json') as f:
    report = json.load(f)

cutoff = '2026-05-29'

for tf in ['1h', '4h', '1d']:
    trades = report.get(tf, {}).get('trades', [])
    if not trades:
        print(f'\n{tf}: 无交易记录')
        continue
    df = pd.DataFrame(trades)
    exit_col = [c for c in df.columns if 'exit' in c.lower() and 'time' in c.lower()][0]
    entry_col = [c for c in df.columns if 'entry' in c.lower() and 'time' in c.lower()][0]
    df[exit_col] = pd.to_datetime(df[exit_col])
    df[entry_col] = pd.to_datetime(df[entry_col])
    recent = df[df[exit_col] >= cutoff]
    if recent.empty:
        print(f'\n{tf}: 最近两周无已平仓交易')
        continue
    print(f'\n── {tf} 最近两周交易 ({len(recent)}笔) ──')
    for _, r in recent.iterrows():
        pnl = r.get('PnL', r.get('pnl', 0))
        ret = r.get('ReturnPct', r.get('return_pct', 0))
        direction = '多' if r.get('Size', 0) > 0 else '空'
        entry_str = r[entry_col].strftime('%m/%d %H:%M')
        exit_str = r[exit_col].strftime('%m/%d %H:%M')
        print(f"  {direction} | 入:{entry_str} 出:{exit_str} | PnL:{pnl:.0f} | Ret:{ret:.2%}")
