#!/usr/bin/env python3
"""
三种仓位管理对比：固定资本 / 每月调仓（半静态复利）/ 每笔动态复利
月度调仓逻辑：每月1日用"上月末已实现净值"重新计算各TF手数，
              当月所有新开仓统一用这个手数，月中不再变动。
"""
import warnings, sys, os
warnings.filterwarnings("ignore")

from pathlib import Path
import yaml, pandas as pd

BASE = str(Path(__file__).resolve().parent)
sys.path.insert(0, BASE)
os.chdir(BASE)
from backtesting import Backtest
from backtest_runner import load_data
from indicators import compute_signals
from strategy import ConfluenceStrategy

with open(Path(BASE) / 'config.yaml') as f:
    config = yaml.safe_load(f)

PYRAMID_RISK = {'1h': 0.004, '4h': 0.015, '1d': 0.045}
CONTRACT_SZ  = 2
MARGIN_PER   = 2100
ACCOUNTS     = [('$30k', 30_000), ('$100k', 100_000), ('$200k', 200_000)]
CUTOFF_2M    = pd.Timestamp('2026-04-12')
CUTOFF_2024  = pd.Timestamp('2024-03-01')

def setup_strategy(p):
    ConfluenceStrategy.min_score           = int(p.get('min_score', 3))
    ConfluenceStrategy.adx_threshold       = float(p.get('adx_threshold', 20.0))
    ConfluenceStrategy.use_adx             = bool(p.get('use_adx', True))
    ConfluenceStrategy.vol_mult            = float(p.get('vol_mult', 1.2))
    ConfluenceStrategy.use_vol             = bool(p.get('use_vol', True))
    ConfluenceStrategy.allow_short         = bool(p.get('allow_short', True))
    ConfluenceStrategy.reversal_score      = int(p.get('reversal_score', 2))
    ConfluenceStrategy.allow_reversal_flip = bool(p.get('allow_reversal_flip', True))
    ConfluenceStrategy.conflict_threshold  = int(p.get('conflict_threshold', 6))
    ConfluenceStrategy.use_bbmc_dir        = bool(p.get('use_bbmc_dir', False))
    ConfluenceStrategy.use_squeeze_mr      = bool(p.get('use_squeeze_mr', False))
    ConfluenceStrategy.rsi_mr_ob           = float(p.get('rsi_mr_ob', 65.0))
    ConfluenceStrategy.rsi_mr_os           = float(p.get('rsi_mr_os', 35.0))
    ConfluenceStrategy.use_atr_exit        = bool(p.get('use_atr_exit', False))
    ConfluenceStrategy.atr_sl_mult         = float(p.get('atr_sl_mult', 1.0))
    ConfluenceStrategy.use_trend_filter    = bool(p.get('use_trend_filter', False))
    ConfluenceStrategy.n_contracts         = int(p.get('n_contracts', 0))
    ConfluenceStrategy.contract_size       = int(p.get('contract_size', 2))
    ConfluenceStrategy.use_staged_tp       = bool(p.get('use_staged_tp', False))
    ConfluenceStrategy.atr_tp1_mult        = float(p.get('atr_tp1_mult', 1.0))
    ConfluenceStrategy.atr_tp2_mult        = float(p.get('atr_tp2_mult', 2.0))
    ConfluenceStrategy.tp1_portion         = float(p.get('tp1_portion', 0.34))

def run_bt(tf_params):
    df = load_data(tf_params)
    df = compute_signals(df, tf_params)
    setup_strategy(tf_params)
    bt = Backtest(df, ConfluenceStrategy, cash=100_000,
                  commission=float(tf_params.get('commission', 0.00002)),
                  margin=float(tf_params.get('margin', 1.0)),
                  exclusive_orders=True)
    stats = bt.run()
    trades = stats._trades
    return (pd.DataFrame() if trades is None else trades.copy()), df

def get_atr_col(df):
    for c in df.columns:
        if c.lower() in ('atr', 'atrval'): return c
    for c in df.columns:
        if 'atr' in c.lower(): return c
    return None

def consolidate(raw, df_data, tf_name, tf_params, cutoff):
    if raw is None or len(raw) == 0:
        return pd.DataFrame()
    t = raw.copy()
    t['EntryTime'] = pd.to_datetime(t['EntryTime'])
    t['ExitTime']  = pd.to_datetime(t['ExitTime'])
    atr_col = get_atr_col(df_data)
    atr_sl  = float(tf_params.get('atr_sl_mult', 1.0))
    rows = []
    for entry_t, grp in t.groupby('EntryTime'):
        last_exit = grp['ExitTime'].max()
        if last_exit < cutoff:
            continue
        orig_size = abs(grp['Size'].iloc[0])
        direction = +1 if grp['Size'].iloc[0] > 0 else -1
        total_pnl = grp['PnL'].sum()
        atr_val = None
        if atr_col:
            idx = df_data.index.searchsorted(entry_t)
            idx = max(0, min(idx, len(df_data)-1))
            atr_val = float(df_data.iloc[idx][atr_col])
        rows.append({
            'tf': tf_name, 'entry': entry_t, 'exit': last_exit,
            'direction': direction, 'orig_size': orig_size,
            'total_pnl': total_pnl, 'win': total_pnl > 0,
            'atr': atr_val, 'atr_sl': atr_sl,
        })
    return pd.DataFrame(rows)

def calc_n(equity, tf, atr, atr_sl):
    if atr and atr > 0:
        return max(1, int(equity * PYRAMID_RISK[tf] / (atr * atr_sl * CONTRACT_SZ)))
    return 1

# ── 固定资本 ──────────────────────────────────────────────
def apply_fixed(consol_by_tf, cash):
    sized = []
    for tf, df_c in consol_by_tf.items():
        if df_c.empty: continue
        df = df_c.copy()
        df['n']        = df.apply(lambda r: calc_n(cash, tf, r['atr'], r['atr_sl']), axis=1)
        df['new_pnl']  = df['total_pnl'] * df['n'] / df['orig_size']
        df['signed_n'] = df['direction'] * df['n']
        df['equity']   = cash
        sized.append(df)
    return pd.concat(sized, ignore_index=True).sort_values('entry').reset_index(drop=True) if sized else pd.DataFrame()

# ── 月度调仓（半静态复利）────────────────────────────────
def apply_monthly(consol_by_tf, cash):
    """
    每月1日重新计算各TF的手数：
      equity_this_month = cash + 所有在本月1日之前已平仓交易的realized PnL
    当月新开仓全部使用 equity_this_month 对应的手数。
    月中新开的仓位不会改变当月手数。
    """
    # 合并全部交易并按入场时间排序
    all_rows = []
    for tf, df_c in consol_by_tf.items():
        if df_c.empty: continue
        for _, row in df_c.iterrows():
            all_rows.append(row.to_dict())
    if not all_rows:
        return pd.DataFrame()
    all_rows.sort(key=lambda x: x['entry'])

    result = []

    # month_start → equity 的缓存，避免重复计算
    month_equity_cache = {}

    for trade in all_rows:
        entry_t = trade['entry']
        # 本笔入场所在月的1日
        month_start = entry_t.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        if month_start not in month_equity_cache:
            # 本月equity = cash + 所有在 month_start 之前已平仓的realized PnL
            realized = sum(r['new_pnl'] for r in result if r['exit'] < month_start)
            month_equity_cache[month_start] = cash + realized

        equity = month_equity_cache[month_start]
        tf     = trade['tf']
        n      = calc_n(equity, tf, trade['atr'], trade['atr_sl'])
        new_pnl = trade['total_pnl'] * n / trade['orig_size']

        result.append({
            **trade,
            'n':           n,
            'new_pnl':     new_pnl,
            'equity':      equity,
            'month_start': month_start,
            'signed_n':    trade['direction'] * n,
        })

    return pd.DataFrame(result).sort_values('entry').reset_index(drop=True)

# ── 每笔动态复利 ──────────────────────────────────────────
def apply_compound(consol_by_tf, cash):
    all_rows = []
    for tf, df_c in consol_by_tf.items():
        if df_c.empty: continue
        for _, row in df_c.iterrows():
            all_rows.append(row.to_dict())
    if not all_rows:
        return pd.DataFrame()
    all_rows.sort(key=lambda x: x['entry'])
    result = []
    for trade in all_rows:
        realized = sum(r['new_pnl'] for r in result if r['exit'] <= trade['entry'])
        equity   = cash + realized
        tf       = trade['tf']
        n        = calc_n(equity, tf, trade['atr'], trade['atr_sl'])
        new_pnl  = trade['total_pnl'] * n / trade['orig_size']
        result.append({**trade, 'n': n, 'new_pnl': new_pnl,
                       'equity': equity, 'signed_n': trade['direction'] * n})
    return pd.DataFrame(result).sort_values('entry').reset_index(drop=True)

def net_peak(df_all):
    if df_all.empty: return 0
    ev = []
    for _, r in df_all.iterrows():
        ev.append((r['entry'],  r['signed_n']))
        ev.append((r['exit'],  -r['signed_n']))
    ev.sort(key=lambda x: x[0])
    cur = peak = 0
    for _, d in ev:
        cur += d
        peak = max(peak, abs(cur))
    return int(peak)

def print_result(df_all, cash, label):
    if df_all.empty:
        print(f'    [{label}] 无交易')
        return
    total_pnl = df_all['new_pnl'].sum()
    wr        = df_all['win'].sum() / len(df_all)
    w = df_all.loc[df_all['win'],  'new_pnl']
    l = df_all.loc[~df_all['win'], 'new_pnl']
    rr = abs(w.mean() / l.mean()) if len(l) else float('inf')
    np_ = net_peak(df_all)
    margin = np_ * MARGIN_PER
    avg_n_all = df_all['n'].mean()

    tf_lines = []
    for tf in ['1h', '4h', '1d']:
        g = df_all[df_all['tf'] == tf]
        if g.empty: continue
        tf_lines.append(f'{tf}:{len(g)}笔 avg{g["n"].mean():.1f}手 ${g["new_pnl"].sum():>+,.0f}')
    print(f'    [{label}]  ' + '  |  '.join(tf_lines))
    print(f'      {len(df_all)}笔  胜率{wr:.0%}  盈亏比{rr:.2f}  avg手数{avg_n_all:.1f}  '
          f'总PnL ${total_pnl:>+,.0f}  收益率 {total_pnl/cash:+.1%}')
    print(f'      净持仓峰值 {np_}手  保证金 ${margin:,}  ({margin/cash:.0%} 账户)')

# ── 主程序 ─────────────────────────────────────────────────
print('运行各周期回测...', flush=True)
tf_data = {}
for tf in ['1h', '4h', '1d']:
    p = config['timeframes'].get(tf)
    if not p: continue
    print(f'  {tf}...', end='', flush=True)
    raw, df = run_bt(p)
    atr_col = get_atr_col(df)
    atr_now = float(df.iloc[-1][atr_col]) if atr_col else float('nan')
    print(f' {len(raw)}条  ATR末值={atr_now:.0f}', flush=True)
    tf_data[tf] = (raw, df, p)

PERIODS = [
    ('最近2月  (Exit >= 2026-04-12)', CUTOFF_2M),
    ('2024-03起全历史',               CUTOFF_2024),
]

for period_label, cutoff in PERIODS:
    consol = {tf: consolidate(raw, df, tf, p, cutoff)
              for tf, (raw, df, p) in tf_data.items()}

    print()
    print('═' * 72)
    print(f'  {period_label}')
    print('═' * 72)

    for lbl, cash in ACCOUNTS:
        print(f'\n  ── 账户 {lbl} ──────────────────────────────────────────────────')

        df_fixed    = apply_fixed(consol, cash)
        df_monthly  = apply_monthly(consol, cash)
        df_compound = apply_compound(consol, cash)

        if df_fixed.empty:
            print('    无交易'); continue

        print_result(df_fixed,    cash, '固定资本  ')
        print()
        print_result(df_monthly,  cash, '★月度调仓')
        print()
        print_result(df_compound, cash, '每笔动态  ')

        pnl_f = df_fixed['new_pnl'].sum()
        pnl_m = df_monthly['new_pnl'].sum()
        pnl_c = df_compound['new_pnl'].sum()
        print(f'\n    月度 vs 固定: ${pnl_m-pnl_f:>+,.0f} ({(pnl_m/pnl_f-1)*100:+.1f}%)  '
              f'| 每笔 vs 固定: ${pnl_c-pnl_f:>+,.0f} ({(pnl_c/pnl_f-1)*100:+.1f}%)')

        # 月度调仓明细
        if 'month_start' in df_monthly.columns and cutoff == CUTOFF_2024:
            print(f'\n    月度净值变化 ({lbl}):')
            months = sorted(df_monthly['month_start'].unique())
            prev_eq = cash
            for ms in months:
                g = df_monthly[df_monthly['month_start'] == ms]
                eq = g['equity'].iloc[0]
                month_pnl = g['new_pnl'].sum()
                n_trades  = len(g)
                # 各TF手数快照（取该月第一笔的手数）
                tf_ns = []
                for tf in ['1h', '4h', '1d']:
                    gt = g[g['tf'] == tf]
                    if not gt.empty:
                        tf_ns.append(f'{tf}:{gt["n"].iloc[0]}手')
                tf_str = ' '.join(tf_ns) if tf_ns else '-'
                print(f'      {ms.strftime("%Y-%m")}  净值${eq:>8,.0f}  '
                      f'{tf_str:30s}  {n_trades}笔  月PnL${month_pnl:>+8,.0f}')

print()
print('═' * 72)
print('  月度调仓：每月1日用上月末已实现净值重新计算手数，当月不变')
print('  每笔动态：每笔入场前用截至该笔所有已平仓的实现净值实时计算')
print('  复利双刃：盈利期放大仓位，亏损期自动收缩，月度操作更可控')
print('═' * 72)
