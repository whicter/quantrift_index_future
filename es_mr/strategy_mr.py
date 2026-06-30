"""
ES Mean Reversion Strategy — backtesting.py 策略类

策略逻辑：
- 只做多（ES 在牛市背景下空头 MR 胜率 < 40%）
- 需要 3 个信号同时满足：RSI < rsi_os + 价格 ≤ BB 下轨 + 价格 < VWAP - vwap_atr_mult × ATR
- ADX < adx_threshold 过滤趋势行情
- 止盈：入场 + tp_atr_mult × ATR；或价格回到 VWAP（先到先出）
- 止损：入场 - sl_mult × ATR
- 时间止损：持仓超过 max_bars 根 bar 强平
"""
from backtesting import Strategy
import numpy as np
import pandas as pd


class MeanReversionStrategy(Strategy):
    # 布林带
    bb_len: int    = 20
    bb_mult: float = 2.0

    # RSI
    rsi_len: int   = 14
    rsi_os: float  = 28.0   # 超卖阈值（只做多，不用超买阈值）

    # ATR
    atr_len: int = 14

    # VWAP 偏离倍数（价格必须在 VWAP 以下 vwap_atr_mult × ATR）
    vwap_atr_mult: float = 2.0

    # 市场过滤器
    adx_len: int         = 14
    adx_threshold: float = 25.0
    ci_len: int          = 14
    ci_threshold: float  = 0.0   # 0 = 禁用；>0 时要求 CI >= 此值才入场

    # 入场（3 个信号全中才入场）
    min_score: int = 3

    # 仓位（固定手数，0 = 全仓买入，1 = 1 手 MES）
    n_contracts: int = 1

    # 出场
    sl_mult: float     = 1.0   # 止损 = entry - ATR × sl_mult
    tp_atr_mult: float = 2.0   # 止盈 = entry + ATR × tp_atr_mult
    max_bars: int      = 8     # 时间止损

    def init(self):
        from es_mr.indicators_mr import (
            compute_bb, compute_rsi, compute_atr, compute_adx, compute_vwap, compute_ci,
        )

        idx = pd.DatetimeIndex(self.data.index)
        close_s  = pd.Series(self.data.Close,  index=idx)
        high_s   = pd.Series(self.data.High,   index=idx)
        low_s    = pd.Series(self.data.Low,    index=idx)
        vol_arr  = self.data.Volume if hasattr(self.data, "Volume") else None
        volume_s = pd.Series(vol_arr if vol_arr is not None else 0, index=idx)

        bb_lower_s, _, _ = compute_bb(close_s, self.bb_len, self.bb_mult)
        rsi_s  = compute_rsi(close_s, self.rsi_len)
        atr_s  = compute_atr(high_s, low_s, close_s, self.atr_len)
        adx_s  = compute_adx(high_s, low_s, close_s, self.adx_len)

        if volume_s.sum() > 0:
            vwap_s = compute_vwap(high_s, low_s, close_s, volume_s, idx)
        else:
            # 无成交量：不使用 VWAP 信号（min_score=3 不可达，策略不交易）
            vwap_s = pd.Series(np.nan, index=idx)

        ci_s = compute_ci(high_s, low_s, close_s, self.ci_len)

        self.bb_lower = self.I(lambda: bb_lower_s.values, name="bb_lower")
        self.rsi      = self.I(lambda: rsi_s.values,      name="rsi")
        self.atr      = self.I(lambda: atr_s.values,      name="atr")
        self.adx      = self.I(lambda: adx_s.values,      name="adx")
        self.ci       = self.I(lambda: ci_s.values,       name="ci")
        self.vwap     = self.I(lambda: vwap_s.values,     name="vwap")

        self._bars_held = 0
        self._tp_price  = None

    def next(self):
        close = self.data.Close[-1]
        atr   = self.atr[-1]

        # 有持仓时检查出场
        if self.position:
            self._bars_held += 1

            # 1. 时间止损
            if self._bars_held >= self.max_bars:
                self.position.close()
                self._bars_held = 0
                self._tp_price  = None
                return

            # 2. ATR 止盈
            if self._tp_price is not None and close >= self._tp_price:
                self.position.close()
                self._bars_held = 0
                self._tp_price  = None
                return

            # 3. VWAP 止盈（价格回到 VWAP）
            vwap = self.vwap[-1]
            if not np.isnan(vwap) and close >= vwap:
                self.position.close()
                self._bars_held = 0
                self._tp_price  = None
                return

            return  # 止损由 SL 参数处理

        # 无持仓时检查入场
        if np.isnan(atr) or np.isnan(self.adx[-1]):
            return

        # ADX 过滤：只在非趋势行情入场
        if self.adx[-1] >= self.adx_threshold:
            return

        # CI 过滤：ci_threshold > 0 时，只在震荡市入场
        if self.ci_threshold > 0 and self.ci[-1] < self.ci_threshold:
            return

        rsi   = self.rsi[-1]
        bb_lo = self.bb_lower[-1]
        vwap  = self.vwap[-1]

        if np.isnan(rsi) or np.isnan(bb_lo) or np.isnan(vwap):
            return

        # 三合一评分（全中才入场）
        bull_score = (
            int(rsi < self.rsi_os) +
            int(close <= bb_lo) +
            int(close < vwap - self.vwap_atr_mult * atr)
        )

        if bull_score >= self.min_score:
            sl = close - atr * self.sl_mult
            self._tp_price = close + atr * self.tp_atr_mult
            size = self.n_contracts if self.n_contracts > 0 else None
            self.buy(sl=sl, size=size)
            self._bars_held = 0
