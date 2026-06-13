"""
用 risk_pct=1.5% ATR 定仓重新计算各周期仓位和 PnL
公式: 手数 = floor((账户净值 × risk_pct) / (ATR × atr_sl_mult × $2/点))
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

CASH        = 100_000
RISK_PCT    = 0.015       # 1.5%
CONTRACT_SZ = 2           # MNQ $2/点
CUTOFF      = pd.Timestamp('2026-05-29')

def get_atr_series(tf_name, tf_params):
    """返回带 ATR 列的 DataFrame"""
    df = load_data(tf_params)
    df = compute_signals(df, tf_params)
    # compute_signals 应该已经算了 ATR，找一下列名
    atr_cols = [c for c in df.columns if 'atr' in c.lower()]
    return df, atr_cols

def run_bt(tf_name, tf_params):
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

    bt = Backtest(df, ConfluenceStrategy,
                  cash=CASH,
                  commission=float(tf_params.get('commission', 0.00002)),
                  margin=float(tf_params.get('margin', 1.0)),
                  exclusive_orders=True)
    stats = bt.run()
    return stats._trades, df

print(f'risk_pct={RISK_PCT:.1%}  账户={CASH:,}  合约=$2/点\n')

all_results = []

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue

    atr_sl_mult = float(tf_params.get('atr_sl_mult', 1.5))
    trades, df_data = run_bt(tf_name, tf_params)

    if trades is None or len(trades) == 0:
        print(f'{tf_name}: 无交易')
        continue

    recent = trades[pd.to_datetime(trades['ExitTime']) >= CUTOFF].copy()
    if recent.empty:
        print(f'{tf_name}: 最近两周无交易')
        continue

    # 找 ATR 列
    atr_cols = [c for c in df_data.columns if c.lower() == 'atr']
    if not atr_cols:
        atr_cols = [c for c in df_data.columns if 'atr' in c.lower()]
    atr_col = atr_cols[0] if atr_cols else None

    print(f'── {tf_name} (ATR列: {atr_col}) ──')

    for idx, row in recent.iterrows():
        entry_t = pd.to_datetime(row['EntryTime'])
        exit_t  = pd.to_datetime(row['ExitTime'])
        orig_size = abs(row['Size'])
        direction = '多' if row['Size'] > 0 else '空'

        # 取入场时 ATR
        if atr_col and entry_t in df_data.index:
            atr_val = df_data.loc[entry_t, atr_col]
        elif atr_col:
            # 找最近的
            idx_pos = df_data.index.searchsorted(entry_t)
            if idx_pos > 0:
                atr_val = df_data.iloc[idx_pos - 1][atr_col]
            else:
                atr_val = df_data[atr_col].iloc[0]
        else:
            atr_val = None

        if atr_val and atr_val > 0:
            new_size = int(CASH * RISK_PCT / (atr_val * atr_sl_mult * CONTRACT_SZ))
        else:
            new_size = orig_size  # fallback

        # 按比例缩放 PnL（PnL ∝ 手数）
        pnl_per_contract = row['PnL'] / orig_size if orig_size > 0 else 0
        new_pnl = pnl_per_contract * new_size

        print(f"  {direction} {entry_t.strftime('%m/%d')}→{exit_t.strftime('%m/%d')}  "
              f"ATR={atr_val:.0f}  原手数={orig_size:.0f}→新手数={new_size}  "
              f"PnL: ${row['PnL']:,.0f}→${new_pnl:,.0f}")

        all_results.append({
            'tf': tf_name,
            'direction': direction,
            'entry': entry_t,
            'exit': exit_t,
            'atr': atr_val,
            'new_size': new_size,
            'new_pnl': new_pnl,
        })
    print()

if not all_results:
    print("无结果")
    sys.exit(0)

df_res = pd.DataFrame(all_results)

print(f'{"="*60}')
print(f'  三周期叠加汇总（risk_pct={RISK_PCT:.1%}）')
print(f'{"="*60}')
print(f'  总 PnL: ${df_res["new_pnl"].sum():,.0f}')
print(f'  收益率: {df_res["new_pnl"].sum()/CASH:.2%}')

# 最大同时持仓
timeline = pd.date_range(df_res['entry'].min(), df_res['exit'].max(), freq='1h')
max_contracts = 0
max_t = None
for t in timeline:
    open_ = df_res[(df_res['entry'] <= t) & (df_res['exit'] > t)]
    total = open_['new_size'].sum()
    if total > max_contracts:
        max_contracts = total
        max_t = t
        max_detail = list(zip(open_['tf'], open_['new_size']))

print(f'\n  最大同时仓位: {max_contracts} 手 MNQ  @ {max_t}')
print(f'  构成: {max_detail}')
# MNQ 保证金约 $2,100/手（NQ21000 × $2 × 5%）
margin = max_contracts * 2100
print(f'  保证金估算 ($2,100/手): ${margin:,}  ({margin/CASH:.0%} 账户)')
