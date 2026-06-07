"""
strategy.py — backtesting.py 策略类，完全复刻 Pine Script 状态机逻辑。

状态机规则（与 Pine Script 完全一致）：
  - 强买信号（bullScore >= min_score）且非震荡市 → 做多
  - 强卖信号（bearScore >= min_score）且非震荡市 → 做空
  - 止盈：收盘价穿越 sslExit 线（HMA-15 of high/low）
  - 止损：收盘价穿越通道线（upperk / lowerk）
  - 同一根 K 线退出后不再入场
  - 止损后等分数回落才能重新入场（waitReset 逻辑）

新增过滤：
  - ADX >= adx_threshold：趋势强度不够不入场
  - Volume > vol_ma × vol_mult：成交量不放大不入场
  - allow_short：控制是否允许做空
"""

from backtesting import Strategy


class ConfluenceStrategy(Strategy):
    # 由 backtest_runner 在运行前设置
    min_score:      int   = 4
    adx_threshold:  float = 20.0
    use_adx:        bool  = True
    vol_mult:       float = 1.2
    use_vol:        bool  = True
    allow_short:    bool  = True

    def init(self):
        self._wait_buy_reset  = False
        self._wait_sell_reset = False

    def next(self):
        if len(self.data.Close) < 2:
            return

        # ── 读取当前 & 前一根 K 线数据 ──────────────────────────────
        close      = self.data.Close[-1]
        close_prev = self.data.Close[-2]

        bull_score = self.data.bullScore[-1]
        bear_score = self.data.bearScore[-1]
        is_choppy  = bool(self.data.isChoppy[-1])

        ssl_exit      = self.data.sslExit[-1]
        ssl_exit_prev = self.data.sslExit[-2]
        upperk        = self.data.upperk[-1]
        upperk_prev   = self.data.upperk[-2]
        lowerk        = self.data.lowerk[-1]
        lowerk_prev   = self.data.lowerk[-2]

        adx        = self.data.adx[-1]
        is_high_vol = bool(self.data.isHighVol[-1])

        min_score = self.min_score

        # ── 重置等待标志 ─────────────────────────────────────────────
        if self._wait_buy_reset  and bull_score < min_score:
            self._wait_buy_reset  = False
        if self._wait_sell_reset and bear_score < min_score:
            self._wait_sell_reset = False

        # ── 出场信号 ─────────────────────────────────────────────────
        tp_long  = (self.position.is_long
                    and close_prev > ssl_exit_prev
                    and close <= ssl_exit)
        tp_short = (self.position.is_short
                    and close_prev < ssl_exit_prev
                    and close >= ssl_exit)

        sl_long  = (not tp_long
                    and self.position.is_long
                    and close_prev > lowerk_prev
                    and close <= lowerk)
        sl_short = (not tp_short
                    and self.position.is_short
                    and close_prev < upperk_prev
                    and close >= upperk)

        # ── 处理出场 ─────────────────────────────────────────────────
        exited_this_bar = False

        if tp_long or sl_long:
            self.position.close()
            exited_this_bar = True
            if sl_long:
                self._wait_buy_reset = True

        elif tp_short or sl_short:
            self.position.close()
            exited_this_bar = True
            if sl_short:
                self._wait_sell_reset = True

        # ── 处理入场 ─────────────────────────────────────────────────
        if exited_this_bar or self.position:
            return

        strong_buy  = bull_score >= min_score
        strong_sell = bear_score >= min_score

        # ADX 和 Volume 硬性前置条件
        ok_trend = (not self.use_adx) or (adx >= self.adx_threshold)
        ok_vol   = (not self.use_vol)  or is_high_vol

        trigger_buy = (strong_buy
                       and not self._wait_buy_reset
                       and not is_choppy
                       and ok_trend
                       and ok_vol
                       and not tp_short
                       and not sl_short)

        trigger_sell = (self.allow_short
                        and strong_sell
                        and not self._wait_sell_reset
                        and not is_choppy
                        and ok_trend
                        and ok_vol
                        and not tp_long
                        and not sl_long)

        if trigger_buy:
            self.buy()
        elif trigger_sell:
            self.sell()
