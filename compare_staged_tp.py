"""
use_staged_tp=True vs False 对比回测
覆盖三个维度：
  1. 最近2个月（固定$30k）
  2. 全历史-三周期（1h/4h/1d，从2024-03起）固定$100k
  3. 全历史-仅1d（2020-01起）固定$100k
"""
import warnings, sys, os
warnings.filterwarnings("ignore")
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
os.chdir(str(BASE))

import yaml, pandas as pd
from backtesting import Backtest
from backtest_runner import load_data
from indicators import compute_signals
from strategy import ConfluenceStrategy

with open(BASE / 'config.yaml') as f:
    config = yaml.safe_load(f)

CONTRACT_SZ  = 2
MARGIN_PER   = 2100
CUTOFF_2M    = pd.Timestamp('2026-04-12')
CASH_2M      = 30_000
CASH_FULL    = 100_000
PYRAMID_RISK = {'1h': 0.004, '4h': 0.015, '1d': 0.045}

def setup_strategy(p, staged_tp: bool):
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
        ('atr_tp1_mult','atr_tp1_mult',float), ('atr_tp2_mult','atr_tp2_mult',float),
        ('tp1_portion','tp1_portion',float),
    ]:
        setattr(ConfluenceStrategy, attr, cast(p.get(key, getattr(ConfluenceStrategy, attr, 0))))
    ConfluenceStrategy.use_staged_tp = staged_tp

def run_bt(p, staged_tp, cash):
    df = load_data(p)
    df = compute_signals(df, p)
    setup_strategy(p, staged_tp)
    bt = Backtest(df, ConfluenceStrategy, cash=cash,
                  commission=float(p.get('commission', 0.00002)),
                  margin=float(p.get('margin', 1.0)), exclusive_orders=True)
    trades = bt.run()._trades
    return (pd.DataFrame() if trades is None else trades.copy()), df

def get_atr_col(df):
    for c in df.columns:
        if c.lower() in ('atr','atrval'): return c
    for c in df.columns:
        if 'atr' in c.lower(): return c
    return None

def consolidate(raw, df_data, tf, p, cutoff, cash):
    if raw is None or len(raw) == 0: return pd.DataFrame()
    t = raw.copy()
    t['EntryTime'] = pd.to_datetime(t['EntryTime'])
    t['ExitTime']  = pd.to_datetime(t['ExitTime'])
    atr_col = get_atr_col(df_data)
    atr_sl  = float(p.get('atr_sl_mult', 1.5))
    rp = PYRAMID_RISK[tf]
    rows = []
    for entry_t, grp in t.groupby('EntryTime'):
        if entry_t < cutoff: continue
        orig_size = abs(grp['Size'].iloc[0])
        atr_val = None
        if atr_col:
            idx = df_data.index.searchsorted(entry_t)
            atr_val = float(df_data.iloc[max(0, min(idx, len(df_data)-1))][atr_col])
        n = max(1, int(cash * rp / (atr_val * atr_sl * CONTRACT_SZ))) if (atr_val and atr_val > 0) else 1
        total_pnl = grp['PnL'].sum()
        new_pnl = total_pnl * n / orig_size
        rows.append({'tf': tf, 'entry': entry_t, 'exit': grp['ExitTime'].max(),
                     'win': total_pnl > 0, 'n': n, 'new_pnl': new_pnl})
    return pd.DataFrame(rows)

def summarize(df_all, cash, label):
    if df_all.empty:
        return f'  {label}: 无交易'
    lines = []
    for tf in ['1h', '4h', '1d']:
        g = df_all[df_all['tf'] == tf]
        if g.empty: continue
        wr = g['win'].sum() / len(g)
        w = g.loc[g['win'], 'new_pnl']; l = g.loc[~g['win'], 'new_pnl']
        rr = abs(w.mean() / l.mean()) if len(l) else float('inf')
        lines.append(f'    {tf}: {len(g)}笔  胜率{wr:.0%}  盈亏比{rr:.2f}  PnL ${g["new_pnl"].sum():>+,.0f}')
    total = df_all['new_pnl'].sum()
    wr_all = df_all['win'].sum() / len(df_all)
    w_all = df_all.loc[df_all['win'], 'new_pnl']; l_all = df_all.loc[~df_all['win'], 'new_pnl']
    rr_all = abs(w_all.mean() / l_all.mean()) if len(l_all) else float('inf')
    lines.append(f'    合计: {len(df_all)}笔  胜率{wr_all:.0%}  盈亏比{rr_all:.2f}  '
                 f'PnL ${total:>+,.0f}  收益率{total/cash:+.1%}')
    return '\n'.join(lines)

def run_scenario(tfs, cutoff, cash, label):
    results = {}
    for staged in [True, False]:
        all_trades = []
        for tf in tfs:
            p = config['timeframes'].get(tf)
            if not p: continue
            raw, df = run_bt(p, staged, cash)
            df_c = consolidate(raw, df, tf, p, cutoff, cash)
            if not df_c.empty:
                all_trades.append(df_c)
        df_all = pd.concat(all_trades).sort_values('entry').reset_index(drop=True) \
                 if all_trades else pd.DataFrame()
        results[staged] = df_all
    return results

# ── 场景1：最近2个月 ──────────────────────────────────────────────────
print('\n' + '═'*68)
print(f'  场景1：最近2个月（{CUTOFF_2M.date()} 起，固定 ${CASH_2M:,}）')
print('═'*68)
r = run_scenario(['1h','4h','1d'], CUTOFF_2M, CASH_2M, '2m')
print('  [staged_tp=True ]')
print(summarize(r[True],  CASH_2M, '2m'))
print('  [staged_tp=False]')
print(summarize(r[False], CASH_2M, '2m'))

# ── 场景2：全历史三周期（2024-03起）─────────────────────────────────
CUTOFF_ALL3 = pd.Timestamp('2024-03-01')
print('\n' + '═'*68)
print(f'  场景2：全历史三周期（{CUTOFF_ALL3.date()} 起，固定 ${CASH_FULL:,}）')
print('═'*68)
r2 = run_scenario(['1h','4h','1d'], CUTOFF_ALL3, CASH_FULL, 'all3')
print('  [staged_tp=True ]')
print(summarize(r2[True],  CASH_FULL, 'all3'))
print('  [staged_tp=False]')
print(summarize(r2[False], CASH_FULL, 'all3'))

# ── 场景3：仅1d，回溯到2020 ──────────────────────────────────────────
CUTOFF_1D = pd.Timestamp('2020-01-01')
print('\n' + '═'*68)
print(f'  场景3：仅1d，全历史（{CUTOFF_1D.date()} 起，固定 ${CASH_FULL:,}）')
print('═'*68)
r3 = run_scenario(['1d'], CUTOFF_1D, CASH_FULL, '1d')
print('  [staged_tp=True ]')
print(summarize(r3[True],  CASH_FULL, '1d'))
print('  [staged_tp=False]')
print(summarize(r3[False], CASH_FULL, '1d'))

print('\n' + '═'*68)
