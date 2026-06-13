"""最近两周 NQ 各周期交易明细"""
import warnings, sys
warnings.filterwarnings("ignore")

BASE = '/Users/congrenhan/Documents/quantrift_index_future'
sys.path.insert(0, BASE)

import yaml, pandas as pd
from pathlib import Path
from backtesting import Backtest
from backtest_runner import load_data
from indicators import compute_signals
from strategy import ConfluenceStrategy

config_path = Path(BASE) / 'config_recent2w.yaml'
with open(config_path) as f:
    config = yaml.safe_load(f)

cutoff = pd.Timestamp('2026-05-29')

for tf_name in ['1h', '4h', '1d']:
    tf_params = config['timeframes'].get(tf_name)
    if not tf_params:
        continue

    print(f'\n{"="*58}')
    print(f'  {tf_name} 最近两周交易明细（5/29 之后出场）')
    print(f'{"="*58}')

    try:
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
        trades = stats._trades

        if trades is None or len(trades) == 0:
            print('  无交易')
            continue

        recent = trades[pd.to_datetime(trades['ExitTime']) >= cutoff].copy()
        if recent.empty:
            print('  最近两周无已平仓交易')
            continue

        print(f"  {'方向':<3} {'入场':^13} {'出场':^13} {'PnL':>8} {'收益%':>7}")
        print(f"  {'-'*50}")
        for _, r in recent.iterrows():
            direction = '多' if r['Size'] > 0 else '空'
            entry = pd.to_datetime(r['EntryTime']).strftime('%m/%d %H:%M')
            exit_ = pd.to_datetime(r['ExitTime']).strftime('%m/%d %H:%M')
            pnl = r.get('PnL', 0)
            ret = r.get('ReturnPct', 0)
            print(f"  {direction:<3} {entry:^13} {exit_:^13} {pnl:>8.0f} {ret:>+7.2%}")

        wins = (recent.get('ReturnPct', pd.Series()) > 0).sum()
        total_pnl = recent['PnL'].sum() if 'PnL' in recent.columns else 0
        print(f"\n  合计: {len(recent)}笔  胜率:{wins/len(recent):.0%}  总PnL: {total_pnl:.0f}")

    except Exception as e:
        import traceback
        print(f'  错误: {e}')
        traceback.print_exc()
