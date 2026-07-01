"""
NQ Range Strategy (均值回归区间策略)

入场条件（全部满足）:
  1. 市场状态: CI > 55 (震荡) 且 ADX < 25 (非趋势)
  2. 价格跌至 BB 下轨以下
  3. RSI < 45 (偏超卖)

出场:
  - TP: 价格回归 BB 中轨
  - SL: 入场价 ± ATR × sl_mult
"""
from backtesting import Strategy
import numpy as np
import pandas as pd


def _rsi(series, n=14):
    delta = pd.Series(series).diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    avg_g = gain.ewm(alpha=1/n, min_periods=n).mean()
    avg_l = loss.ewm(alpha=1/n, min_periods=n).mean()
    rs = avg_g / avg_l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _atr(high, low, close, n=14):
    tr = pd.DataFrame({
        'hl': pd.Series(high) - pd.Series(low),
        'hc': (pd.Series(high) - pd.Series(close).shift(1)).abs(),
        'lc': (pd.Series(low)  - pd.Series(close).shift(1)).abs(),
    }).max(axis=1)
    return tr.ewm(alpha=1/n, min_periods=n).mean()


def _adx(high, low, close, n=14):
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    tr_s = pd.DataFrame({
        'hl': h - l,
        'hc': (h - c.shift(1)).abs(),
        'lc': (l - c.shift(1)).abs(),
    }).max(axis=1)
    atr14 = tr_s.ewm(alpha=1/n, min_periods=n).mean()
    dm_plus  = (h.diff()).clip(lower=0)
    dm_minus = (-l.diff()).clip(lower=0)
    dm_plus  = dm_plus.where(dm_plus > dm_minus, 0)
    dm_minus = dm_minus.where(dm_minus > dm_plus, 0)
    di_plus  = 100 * dm_plus.ewm(alpha=1/n, min_periods=n).mean() / atr14
    di_minus = 100 * dm_minus.ewm(alpha=1/n, min_periods=n).mean() / atr14
    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    return dx.ewm(alpha=1/n, min_periods=n).mean()


def _ci(high, low, close, n=14):
    """Choppiness Index: 100 = fully choppy, 38.2 = trending."""
    h = pd.Series(high)
    l = pd.Series(low)
    c = pd.Series(close)
    tr = pd.DataFrame({
        'hl': h - l,
        'hc': (h - c.shift(1)).abs(),
        'lc': (l - c.shift(1)).abs(),
    }).max(axis=1)
    atr_sum = tr.rolling(n).sum()
    hh      = h.rolling(n).max()
    ll      = l.rolling(n).min()
    rng     = (hh - ll).replace(0, np.nan)
    return 100 * np.log10(atr_sum / rng) / np.log10(n)


def _bb(close, n=20, mult=2.0):
    s = pd.Series(close)
    mid = s.rolling(n).mean()
    std = s.rolling(n).std()
    return mid - mult * std, mid, mid + mult * std


class NQRangeStrategy(Strategy):
    # ── 超参 ────────────────────────────────────────────
    n_contracts  = 1
    bb_len       = 20
    bb_mult      = 2.0
    rsi_len      = 14
    rsi_entry    = 45.0
    atr_len      = 14
    adx_len      = 14
    ci_len       = 14
    ci_threshold = 55.0
    adx_threshold= 25.0
    sl_mult      = 1.5

    def init(self):
        c = self.data.Close
        h = self.data.High
        l = self.data.Low

        bb_lower, bb_mid, bb_upper = _bb(c, self.bb_len, self.bb_mult)
        self.bb_lower = self.I(lambda x: x, bb_lower, name='BB_lower')
        self.bb_mid   = self.I(lambda x: x, bb_mid,   name='BB_mid')
        self.bb_upper = self.I(lambda x: x, bb_upper, name='BB_upper')
        self.rsi = self.I(_rsi, c, self.rsi_len, name='RSI')
        self.atr = self.I(_atr, h, l, c, self.atr_len, name='ATR')
        self.adx = self.I(_adx, h, l, c, self.adx_len, name='ADX')
        self.ci  = self.I(_ci,  h, l, c, self.ci_len,  name='CI')

    def next(self):
        price = self.data.Close[-1]
        atr   = self.atr[-1]
        size  = max(1, int(self.n_contracts))

        # ── 出场 ──────────────────────────────────────
        if self.position.is_long:
            if price >= self.bb_mid[-1]:
                self.position.close()
            return
        if self.position.is_short:
            if price <= self.bb_mid[-1]:
                self.position.close()
            return

        # ── 前置过滤 ──────────────────────────────────
        if np.isnan(atr) or atr <= 0:
            return
        if np.isnan(self.ci[-1]) or np.isnan(self.adx[-1]):
            return

        choppy   = self.ci[-1]  > self.ci_threshold
        no_trend = self.adx[-1] < self.adx_threshold
        if not (choppy and no_trend):
            return

        # ── 多头入场 ──────────────────────────────────
        low = self.data.Low[-1]
        if low < self.bb_lower[-1] and self.rsi[-1] < self.rsi_entry:
            sl = price - atr * self.sl_mult
            tp = self.bb_mid[-1]
            if tp > price:
                self.buy(size=size, sl=sl, tp=tp)
