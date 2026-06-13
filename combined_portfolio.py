"""
三周期叠加投资组合分析
- 合并 1h/4h/1d 的所有交易
- 计算总PnL、最大同时仓位、合并权益曲线
"""
import warnings, sys
warnings.filterwarnings("ignore")

BASE = '/Users/congrenhan/Documents/quantrift_index_future'
sys.path.insert(0, BASE)

import yaml, pandas as pd, numpy as np
from pathlib import Path
from backtesting import Backtest
from backtest_runner import load_data
from indicators import compute_signals
from strategy import ConfluenceStrategy

config_path = Path(BASE) / 'config_recent2w.yaml'
with open(config_path) as f:
    config = yaml.safe_load(f)

cutoff = pd.Timestamp('2026-05-29')

def run_tf(tf_name, tf_params):
    df = load_data(tf_params)
    df = compute_signals(df, tf_params)

    ConfluenceStrategy.min_score            = int(tf_params['min_score'])
    ConfluenceStrategy.adx_threshold        = float(tf_params.get('adx_threshold', 20.0))
    ConfluenceStrategy.use_adx              = bool(tf_params.get('use_adx', True))
    ConfluenceStrategy.vol_mult             = float(tf_params.get('vol_mult', 1.2))
    ConfluenceStrategy.use_vol              = bool(tf_params.get('use_vol', True))
    ConfluenceStrategy.allow_short          = bool(tf_params.get('allow_short', True))
    ConfluenceStrategy.reversal_score       = int(tf_params.get('reversal_score', 2))
    ConfluenceStrategy.allow_reversal_flip  = bool(tf_params.get('allow_reversal_flip', True))
    ConfluenceStrategy.conflict_threshold   = int(tf_params.get('conflict_threshold', 6))
    ConfluenceStrategy.use_bbmc_dir         = bool(tf_params.get('use_bbmc_dir', False))
    ConfluenceStrategy.use_squeeze_mr       = bool(tf_params.get('use_squeeze_mr', False))
    ConfluenceStrategy.rsi_mr_ob            = float(tf_params.get('rsi_mr_ob', 65.0))
    ConfluenceStrategy.rsi_mr_os            = float(tf_params.get('rsi_mr_os', 35.0))
    ConfluenceStrategy.use_atr_exit         = bool(tf_params.get('use_atr_exit', False))
    ConfluenceStrategy.atr_sl_mult          = float(tf_params.get('atr_sl_mult', 1.0))
    ConfluenceStrategy.use_trend_filter     = bool(tf_params.get('use_trend_filter', False))
    ConfluenceStrategy.n_contracts          = int(tf_params.get('n_contracts', 0))
    ConfluenceStrategy.contract_size        = int(tf_params.get('contract_size', 2))
    ConfluenceStrategy.use_staged_tp        = bool(tf_params.get('use_staged_tp', False))
    ConfluenceStrategy.atr_tp1_mult         = float(tf_params.get('atr_tp1_mult', 1.0))
    ConfluenceStrategy.atr_tp2_mult         = float(tf_params.get('atr_tp2_mult', 2.0))
    ConfluenceStrategy.tp1_portion          = float(tf_params.get('tp1_portion', 0.34))

    bt = Backtest(
        df, ConfluenceStrategy,
        cash=int(tf_params.get('cash', 100000)),
        commission=float(tf_params.get('commission', 0.00002)),
        margin=float(tf_params.get('margin', 1.0)),
        exclusive_orders=True,
    )
    stats = bt.run()
    return stats._trades, stats._equity_curve

# 收集各周期交易
all_trades = []
equity_curves = {}

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue
    print(f'运行 {tf_name}...')
    trades, equity = run_tf(tf_name, tf_params)
    equity_curves[tf_name] = equity

    if trades is not None and len(trades) > 0:
        recent = trades[pd.to_datetime(trades['ExitTime']) >= cutoff].copy()
        recent['tf'] = tf_name
        all_trades.append(recent)

if not all_trades:
    print("最近两周无交易")
    sys.exit(0)

df_trades = pd.concat(all_trades, ignore_index=True)
df_trades['EntryTime'] = pd.to_datetime(df_trades['EntryTime'])
df_trades['ExitTime']  = pd.to_datetime(df_trades['ExitTime'])

print(f'\n{"="*60}')
print(f'  三周期叠加结果（最近两周）')
print(f'{"="*60}')

# 总PnL
total_pnl = df_trades['PnL'].sum()
total_trades = len(df_trades)
wins = (df_trades['ReturnPct'] > 0).sum()

print(f'\n  总交易笔数: {total_trades}')
print(f'  胜率: {wins/total_trades:.0%}')
print(f'  总PnL: ${total_pnl:,.0f}')

# 各周期明细
print(f'\n  按周期汇总:')
for tf in df_trades['tf'].unique():
    grp = df_trades[df_trades['tf'] == tf]
    print(f'    {tf}: {len(grp)}笔  PnL ${grp["PnL"].sum():,.0f}')

# 最大同时仓位分析
print(f'\n  最大同时持仓分析:')

# 构建1分钟时间轴，统计任意时刻同时开仓的合约数
min_t = df_trades['EntryTime'].min()
max_t = df_trades['ExitTime'].max()
timeline = pd.date_range(min_t, max_t, freq='1h')

max_contracts = 0
max_contracts_time = None
max_contracts_detail = []

for t in timeline:
    # 找出 t 时刻所有仍然开着的仓
    open_trades = df_trades[
        (df_trades['EntryTime'] <= t) & (df_trades['ExitTime'] > t)
    ]
    total_size = open_trades['Size'].abs().sum()
    if total_size > max_contracts:
        max_contracts = total_size
        max_contracts_time = t
        max_contracts_detail = list(zip(open_trades['tf'], open_trades['Size']))

print(f'    最大同时合约数: {max_contracts:.0f} 手')
print(f'    时间: {max_contracts_time}')
print(f'    构成: {max_contracts_detail}')

# MNQ 每手保证金约 $1,500（按 NQ 约 $21,000/手，5% margin）
margin_per_contract = 1500
max_margin = max_contracts * margin_per_contract
print(f'    所需保证金估算 (MNQ @$1500/手): ${max_margin:,.0f}')

# 合并权益曲线近似（用各周期PnL时序叠加）
print(f'\n  综合收益率 (基于$100k账户):')
print(f'    总PnL:  ${total_pnl:,.0f}')
print(f'    收益率: {total_pnl/100000:.2%}')
