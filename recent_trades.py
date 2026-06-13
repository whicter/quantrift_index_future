"""
NQ 各周期最近交易明细
用法: python recent_trades.py [--since 2026-05-01] [--config config.yaml]
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

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue

    print(f'\n{"="*58}')
    print(f'  {tf_name} 交易明细（{CUTOFF.date()} 之后出场）')
    print(f'{"="*58}')

    try:
        df = load_data(tf_params)
        df = compute_signals(df, tf_params)
        setup_strategy(tf_params)

        bt = Backtest(df, ConfluenceStrategy,
                      cash=int(tf_params.get('cash', 100000)),
                      commission=float(tf_params.get('commission', 0.00002)),
                      margin=float(tf_params.get('margin', 1.0)),
                      exclusive_orders=True)
        trades = bt.run()._trades

        if trades is None or len(trades) == 0:
            print('  无交易'); continue

        recent = trades[pd.to_datetime(trades['ExitTime']) >= CUTOFF].copy()
        if recent.empty:
            print('  该区间无已平仓交易'); continue

        print(f"  {'方向':<3} {'入场':^13} {'出场':^13} {'PnL':>8} {'收益%':>7}")
        print(f"  {'-'*50}")
        for _, r in recent.iterrows():
            direction = '多' if r['Size'] > 0 else '空'
            entry = pd.to_datetime(r['EntryTime']).strftime('%m/%d %H:%M')
            exit_ = pd.to_datetime(r['ExitTime']).strftime('%m/%d %H:%M')
            print(f"  {direction:<3} {entry:^13} {exit_:^13} "
                  f"{r.get('PnL', 0):>8.0f} {r.get('ReturnPct', 0):>+7.2%}")

        wins = (recent.get('ReturnPct', pd.Series()) > 0).sum()
        print(f"\n  合计: {len(recent)}笔  胜率:{wins/len(recent):.0%}  "
              f"总PnL: ${recent['PnL'].sum():,.0f}")

    except Exception as e:
        import traceback
        print(f'  错误: {e}')
        traceback.print_exc()
