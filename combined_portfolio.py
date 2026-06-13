"""
三周期叠加投资组合分析
- 合并 1h/4h/1d 的所有交易
- 计算总PnL、最大同时仓位、合并权益曲线
用法: python combined_portfolio.py [--since 2026-05-01] [--config config.yaml]
"""
import argparse, warnings, sys
warnings.filterwarnings("ignore")

from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))

import yaml, pandas as pd
from backtesting import Backtest
from backtest_runner import load_data
from indicators import compute_signals
from strategy import ConfluenceStrategy

parser = argparse.ArgumentParser()
parser.add_argument('--since',  default=None,          help='起始日期 YYYY-MM-DD，默认2周前')
parser.add_argument('--config', default='config.yaml', help='配置文件，默认config.yaml')
args = parser.parse_args()

CUTOFF = pd.Timestamp(args.since) if args.since else \
         pd.Timestamp.now().normalize() - pd.Timedelta(days=14)

with open(BASE / args.config) as f:
    config = yaml.safe_load(f)

def setup_strategy(p):
    for attr, key, cast in [
        ('min_score','min_score',int), ('adx_threshold','adx_threshold',float),
        ('use_adx','use_adx',bool), ('vol_mult','vol_mult',float),
        ('use_vol','use_vol',bool), ('allow_short','allow_short',bool),
        ('reversal_score','reversal_score',int), ('allow_reversal_flip','allow_reversal_flip',bool),
        ('conflict_threshold','conflict_threshold',int), ('use_bbmc_dir','use_bbmc_dir',bool),
        ('use_squeeze_mr','use_squeeze_mr',bool), ('rsi_mr_ob','rsi_mr_ob',float),
        ('rsi_mr_os','rsi_mr_os',float), ('use_atr_exit','use_atr_exit',bool),
        ('atr_sl_mult','atr_sl_mult',float), ('use_trend_filter','use_trend_filter',bool),
        ('n_contracts','n_contracts',int), ('contract_size','contract_size',int),
        ('use_staged_tp','use_staged_tp',bool), ('atr_tp1_mult','atr_tp1_mult',float),
        ('atr_tp2_mult','atr_tp2_mult',float), ('tp1_portion','tp1_portion',float),
    ]:
        setattr(ConfluenceStrategy, attr, cast(p.get(key, getattr(ConfluenceStrategy, attr, 0))))

def run_tf(tf_params):
    df = load_data(tf_params)
    df = compute_signals(df, tf_params)
    setup_strategy(tf_params)
    bt = Backtest(df, ConfluenceStrategy,
                  cash=int(tf_params.get('cash', 100000)),
                  commission=float(tf_params.get('commission', 0.00002)),
                  margin=float(tf_params.get('margin', 1.0)),
                  exclusive_orders=True)
    stats = bt.run()
    return stats._trades, stats._equity_curve

all_trades = []

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue
    print(f'运行 {tf_name}...')
    trades, _ = run_tf(tf_params)
    if trades is not None and len(trades) > 0:
        recent = trades[pd.to_datetime(trades['ExitTime']) >= CUTOFF].copy()
        recent['tf'] = tf_name
        all_trades.append(recent)

if not all_trades:
    print(f"该区间（{CUTOFF.date()} 起）无交易")
    sys.exit(0)

df_trades = pd.concat(all_trades, ignore_index=True)
df_trades['EntryTime'] = pd.to_datetime(df_trades['EntryTime'])
df_trades['ExitTime']  = pd.to_datetime(df_trades['ExitTime'])

print(f'\n{"="*60}')
print(f'  三周期叠加结果（{CUTOFF.date()} 起）')
print(f'{"="*60}')

total_pnl   = df_trades['PnL'].sum()
total_trades = len(df_trades)
wins        = (df_trades['ReturnPct'] > 0).sum()

print(f'\n  总交易笔数: {total_trades}')
print(f'  胜率: {wins/total_trades:.0%}')
print(f'  总PnL: ${total_pnl:,.0f}')

print(f'\n  按周期汇总:')
for tf in df_trades['tf'].unique():
    grp = df_trades[df_trades['tf'] == tf]
    print(f'    {tf}: {len(grp)}笔  PnL ${grp["PnL"].sum():,.0f}')

print(f'\n  最大同时持仓分析:')
timeline = pd.date_range(df_trades['EntryTime'].min(), df_trades['ExitTime'].max(), freq='1h')
max_contracts, max_t, max_detail = 0, None, []
for t in timeline:
    open_trades = df_trades[(df_trades['EntryTime'] <= t) & (df_trades['ExitTime'] > t)]
    total_size  = open_trades['Size'].abs().sum()
    if total_size > max_contracts:
        max_contracts = total_size; max_t = t
        max_detail = list(zip(open_trades['tf'], open_trades['Size']))

margin = max_contracts * 2100
print(f'    最大同时合约数: {max_contracts:.0f} 手')
print(f'    时间: {max_t}')
print(f'    构成: {max_detail}')
print(f'    所需保证金估算 ($2,100/手): ${margin:,.0f}')

cash = int(config.get('timeframes', {}).get('1h', {}).get('cash', 100000))
print(f'\n  综合收益率 (基于${cash:,}账户):')
print(f'    总PnL:  ${total_pnl:,.0f}')
print(f'    收益率: {total_pnl/cash:.2%}')
