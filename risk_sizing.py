"""
用 ATR 定仓重新计算各周期仓位和 PnL
用法: python risk_sizing.py [--since 2026-05-01] [--risk 0.015] [--cash 100000] [--config config.yaml]
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
parser.add_argument('--since',  default=None,         help='起始日期 YYYY-MM-DD，默认2周前')
parser.add_argument('--risk',   type=float, default=0.015, help='风险比例，默认1.5%%')
parser.add_argument('--cash',   type=int, default=100_000, help='账户净值，默认100000')
parser.add_argument('--config', default='config.yaml', help='配置文件，默认config.yaml')
args = parser.parse_args()

CASH        = args.cash
RISK_PCT    = args.risk
CONTRACT_SZ = 2
CUTOFF      = pd.Timestamp(args.since) if args.since else \
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

def run_bt(tf_params):
    df = load_data(tf_params)
    df = compute_signals(df, tf_params)
    setup_strategy(tf_params)
    bt = Backtest(df, ConfluenceStrategy,
                  cash=CASH,
                  commission=float(tf_params.get('commission', 0.00002)),
                  margin=float(tf_params.get('margin', 1.0)),
                  exclusive_orders=True)
    return bt.run()._trades, df

print(f'risk_pct={RISK_PCT:.1%}  账户=${CASH:,}  起始={CUTOFF.date()}\n')

all_results = []

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue

    atr_sl_mult = float(tf_params.get('atr_sl_mult', 1.5))
    trades, df_data = run_bt(tf_params)

    if trades is None or len(trades) == 0:
        print(f'{tf_name}: 无交易'); continue

    recent = trades[pd.to_datetime(trades['ExitTime']) >= CUTOFF].copy()
    if recent.empty:
        print(f'{tf_name}: 该区间无交易'); continue

    atr_cols = [c for c in df_data.columns if c.lower() == 'atr'] or \
               [c for c in df_data.columns if 'atr' in c.lower()]
    atr_col = atr_cols[0] if atr_cols else None

    print(f'── {tf_name} (atr_sl_mult={atr_sl_mult}) ──')

    for _, row in recent.iterrows():
        entry_t   = pd.to_datetime(row['EntryTime'])
        exit_t    = pd.to_datetime(row['ExitTime'])
        orig_size = abs(row['Size'])
        direction = '多' if row['Size'] > 0 else '空'

        if atr_col:
            idx_pos = df_data.index.searchsorted(entry_t)
            atr_val = float(df_data.iloc[max(0, idx_pos - 1)][atr_col])
        else:
            atr_val = None

        new_size = max(1, int(CASH * RISK_PCT / (atr_val * atr_sl_mult * CONTRACT_SZ))) \
                   if (atr_val and atr_val > 0) else orig_size
        pnl_per = row['PnL'] / orig_size if orig_size > 0 else 0
        new_pnl  = pnl_per * new_size

        print(f"  {direction} {entry_t.strftime('%m/%d')}→{exit_t.strftime('%m/%d')}  "
              f"ATR={atr_val:.0f}  {orig_size:.0f}手→{new_size}手  "
              f"PnL: ${row['PnL']:,.0f}→${new_pnl:,.0f}")

        all_results.append({'tf': tf_name, 'direction': direction,
                             'entry': entry_t, 'exit': exit_t,
                             'atr': atr_val, 'new_size': new_size, 'new_pnl': new_pnl})
    print()

if not all_results:
    print("无结果"); sys.exit(0)

df_res = pd.DataFrame(all_results)
print(f'{"="*60}')
print(f'  三周期叠加汇总（risk_pct={RISK_PCT:.1%}  账户=${CASH:,}）')
print(f'{"="*60}')
print(f'  总 PnL: ${df_res["new_pnl"].sum():,.0f}')
print(f'  收益率: {df_res["new_pnl"].sum()/CASH:.2%}')

timeline   = pd.date_range(df_res['entry'].min(), df_res['exit'].max(), freq='1h')
max_n, max_t, max_detail = 0, None, []
for t in timeline:
    open_ = df_res[(df_res['entry'] <= t) & (df_res['exit'] > t)]
    n = open_['new_size'].sum()
    if n > max_n:
        max_n = n; max_t = t
        max_detail = list(zip(open_['tf'], open_['new_size']))

margin = max_n * 2100
print(f'\n  最大同时仓位: {max_n} 手 MNQ @ {max_t}')
print(f'  构成: {max_detail}')
print(f'  保证金估算 ($2,100/手): ${margin:,}  ({margin/CASH:.0%} 账户)')
