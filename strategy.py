"""
strategy.py — backtesting.py 策略类，完全复刻 Pine Script 状态机逻辑。

出场模式：
  A. 原始模式（use_staged_tp=False）
     - 止盈：收盘价穿越 sslExit 线
     - 止损：收盘价穿越通道线（upperk / lowerk）

  B. 分批止盈模式（use_staged_tp=True）
     - 止损：utTS（UT Bot 动态追踪止损线）
     - TP1：entry ± atr_tp1_mult × ATR，平 tp1_portion（默认 1/3）
     - TP2：entry ± atr_tp2_mult × ATR，再平剩余仓位的 1/2（共 2/3）
     - 剩余 1/3：沿用 sslExit 跟踪出场（吃大趋势）

过滤器：
  - ADX >= adx_threshold
  - Volume > vol_ma × vol_mult
  - allow_short：控制是否允许做空

期货合约：
  - n_contracts > 0：固定合约数，size = n_contracts × contract_size
  - n_contracts = 0：全仓（原始行为）
"""

import numpy as np
from backtesting import Strategy


class ConfluenceStrategy(Strategy):
    # ── 基础信号参数 ──────────────────────────────────────────────
    min_score:            int   = 4
    adx_threshold:        float = 20.0
    use_adx:              bool  = True
    vol_mult:             float = 1.2
    use_vol:              bool  = True
    allow_short:          bool  = True
    reversal_score:       int   = 2
    allow_reversal_flip:  bool  = True
    conflict_threshold:   int   = 6
    use_bbmc_dir:         bool  = False

    # ── Squeeze 均值回归 ──────────────────────────────────────────
    use_squeeze_mr:       bool  = False
    rsi_mr_ob:            float = 65.0
    rsi_mr_os:            float = 35.0

    # ── 方案二：固定 ATR 止盈止损（全仓，不分批）── 未使用，已禁用 ──
    # use_atr_exit:         bool  = False   # 始终 False，不生效
    # atr_tp_mult:          float = 2.0
    # atr_sl_mult:          float = 1.5     # staged_tp=True 时止损用 utTS，此参数无效

    # ── 分批止盈（use_staged_tp=True 时启用）────────────────────
    use_staged_tp:        bool  = False
    atr_tp1_mult:         float = 1.0   # 第一批止盈倍数
    atr_tp2_mult:         float = 2.0   # 第二批止盈倍数
    tp1_portion:          float = 0.34  # TP1 平仓比例（≈1/3）

    # ── 趋势过滤器 ────────────────────────────────────────────────
    use_trend_filter:     bool  = False

    # ── 期货合约设置 ──────────────────────────────────────────────
    n_contracts:          int   = 0    # 0=全仓，>0=固定合约数
    contract_size:        int   = 2    # MNQ=$2/点，NQ=$20/点

    def init(self):
        self._wait_buy_reset  = False
        self._wait_sell_reset = False
        self._mr_mode         = False
        # 分批止盈状态
        self._stage      = 0    # 0=空仓, 1=满仓, 2=已平1/3, 3=已平2/3
        self._entry_price = 0.0
        self._entry_atr   = 0.0
        self._entry_dir   = 0   # 1=多, -1=空
        # 固定交易 size
        self._trade_size = (self.n_contracts * self.contract_size
                            if self.n_contracts > 0 else None)

    def _open_long(self):
        self._stage       = 1 if self.use_staged_tp else 0
        self._entry_price = float(self.data.Close[-1])
        self._entry_atr   = float(self.data.atrVal[-1])
        self._entry_dir   = 1
        self.buy(size=self._trade_size)

    def _open_short(self):
        self._stage       = 1 if self.use_staged_tp else 0
        self._entry_price = float(self.data.Close[-1])
        self._entry_atr   = float(self.data.atrVal[-1])
        self._entry_dir   = -1
        self.sell(size=self._trade_size)

    def _reset_stage(self):
        self._stage = 0
        self._entry_dir = 0

    def next(self):
        if len(self.data.Close) < 3:
            return

        # ── 读取行情数据 ──────────────────────────────────────────
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

        adx         = self.data.adx[-1]
        is_high_vol = bool(self.data.isHighVol[-1])
        bbmc_dir    = self.data.bbmcDir[-1]
        _atr_raw    = float(self.data.atrVal[-1])
        atr_val     = _atr_raw if (_atr_raw == _atr_raw and _atr_raw > 0) else close * 0.01

        sqz_val      = self.data.sqzVal[-1]
        sqz_val_prev = self.data.sqzVal[-2]
        sqz_off      = bool(self.data.sqzOff[-1])

        try:
            rsi_val = float(self.data.rsiVal[-1])
        except Exception:
            rsi_val = 50.0

        # ── 趋势过滤器 ────────────────────────────────────────────
        if self.use_trend_filter:
            trend_sma = float(self.data.trendSMA[-1])
            trend_ok_long  = (trend_sma != trend_sma) or (close > trend_sma)
            trend_ok_short = (trend_sma != trend_sma) or (close < trend_sma)
        else:
            trend_ok_long = trend_ok_short = True

        min_score = self.min_score
        ok_trend  = (not self.use_adx) or (adx >= self.adx_threshold)
        ok_vol    = (not self.use_vol)  or is_high_vol
        ok_bbmc_long  = (not self.use_bbmc_dir) or (bbmc_dir >= 0)
        ok_bbmc_short = (not self.use_bbmc_dir) or (bbmc_dir <= 0)

        # ── 重置等待标志 ──────────────────────────────────────────
        if self._wait_buy_reset  and bull_score < min_score:
            self._wait_buy_reset  = False
        if self._wait_sell_reset and bear_score < min_score:
            self._wait_sell_reset = False

        # ══════════════════════════════════════════════════════════
        # 分批止盈出场模式（use_staged_tp=True）
        # ══════════════════════════════════════════════════════════
        if self.use_staged_tp and self.position and self._stage >= 1:
            ep  = self._entry_price
            ea  = self._entry_atr if self._entry_atr > 0 else atr_val
            d   = self._entry_dir   # +1 多, -1 空

            tp1_price = ep + d * self.atr_tp1_mult * ea
            tp2_price = ep + d * self.atr_tp2_mult * ea

            # ① 止损：UT Bot 动态追踪止损线
            sl_price = float(self.data.utTS[-1])
            hit_sl = (d == 1 and close < sl_price) or (d == -1 and close > sl_price)
            if hit_sl:
                self.position.close()
                self._reset_stage()
                if d == 1:
                    self._wait_buy_reset  = True
                else:
                    self._wait_sell_reset = True
                return

            # ② TP1：平 tp1_portion（≈1/3）
            if self._stage == 1:
                hit_tp1 = ((d ==  1 and close >= tp1_price) or
                           (d == -1 and close <= tp1_price))
                if hit_tp1:
                    self.position.close(portion=self.tp1_portion)
                    self._stage = 2

            # ③ TP2：平剩余的 1/2（共约 2/3 已平）
            elif self._stage == 2:
                hit_tp2 = ((d ==  1 and close >= tp2_price) or
                           (d == -1 and close <= tp2_price))
                if hit_tp2:
                    self.position.close(portion=0.5)
                    self._stage = 3

            # ④ 剩余 1/3：sslExit 跟踪出场
            if self._stage == 3 and self.position:
                ssl_trail_exit = (
                    (d ==  1 and close_prev > ssl_exit_prev and close <= ssl_exit) or
                    (d == -1 and close_prev < ssl_exit_prev and close >= ssl_exit)
                )
                if ssl_trail_exit:
                    if d == 1:
                        can_flip = (
                            self.allow_reversal_flip and self.allow_short
                            and bear_score >= self.reversal_score
                            and bull_score <= self.conflict_threshold
                            and not is_choppy and ok_trend and ok_vol
                            and ok_bbmc_short and trend_ok_short
                            and not self._wait_sell_reset
                        )
                        if can_flip:
                            self._reset_stage()
                            self._open_short()
                        else:
                            self.position.close()
                            self._reset_stage()
                    else:
                        can_flip = (
                            self.allow_reversal_flip
                            and bull_score >= self.reversal_score
                            and bear_score <= self.conflict_threshold
                            and not is_choppy and ok_trend and ok_vol
                            and ok_bbmc_long and trend_ok_long
                            and not self._wait_buy_reset
                        )
                        if can_flip:
                            self._reset_stage()
                            self._open_long()
                        else:
                            self.position.close()
                            self._reset_stage()
                    return

            # 仍在持仓中，不进入新开仓逻辑
            return

        # ══════════════════════════════════════════════════════════
        # 原始出场模式（use_staged_tp=False）
        # ══════════════════════════════════════════════════════════

        # ── MR 出场 ───────────────────────────────────────────────
        mr_exit_long = mr_exit_short = False
        if self._mr_mode and self.position:
            if self.position.is_long:
                mr_exit_long = (sqz_val_prev > 0 and sqz_val <= 0) or sqz_off
            elif self.position.is_short:
                mr_exit_short = (sqz_val_prev < 0 and sqz_val >= 0) or sqz_off

        # ── 趋势出场信号 ──────────────────────────────────────────
        if not self._mr_mode:
            tp_long  = (self.position.is_long
                        and close_prev > ssl_exit_prev and close <= ssl_exit)
            tp_short = (self.position.is_short
                        and close_prev < ssl_exit_prev and close >= ssl_exit)
            sl_long  = (not tp_long and self.position.is_long
                        and close_prev > lowerk_prev and close <= lowerk)
            sl_short = (not tp_short and self.position.is_short
                        and close_prev < upperk_prev and close >= upperk)
        else:
            tp_long = tp_short = False
            sl_long  = (self.position.is_long
                        and close_prev > lowerk_prev and close <= lowerk)
            sl_short = (self.position.is_short
                        and close_prev < upperk_prev and close >= upperk)

        # ── 执行出场 & 翻转 ───────────────────────────────────────
        exited_this_bar = False

        if mr_exit_long or mr_exit_short:
            self.position.close()
            self._mr_mode = False
            exited_this_bar = True

        elif tp_long:
            can_flip_short = (
                self.allow_reversal_flip and self.allow_short
                and bear_score >= self.reversal_score
                and bull_score <= self.conflict_threshold
                and not is_choppy and ok_trend and ok_vol
                and ok_bbmc_short and trend_ok_short
                and not self._wait_sell_reset
            )
            if can_flip_short:
                self._mr_mode = False
                self._open_short()
            else:
                self.position.close()
                self._mr_mode = False
            exited_this_bar = True

        elif sl_long:
            self.position.close()
            self._mr_mode = False
            self._wait_buy_reset = True
            exited_this_bar = True

        elif tp_short:
            can_flip_long = (
                self.allow_reversal_flip
                and bull_score >= self.reversal_score
                and bear_score <= self.conflict_threshold
                and not is_choppy and ok_trend and ok_vol
                and ok_bbmc_long and trend_ok_long
                and not self._wait_buy_reset
            )
            if can_flip_long:
                self._mr_mode = False
                self._open_long()
            else:
                self.position.close()
                self._mr_mode = False
            exited_this_bar = True

        elif sl_short:
            self.position.close()
            self._mr_mode = False
            self._wait_sell_reset = True
            exited_this_bar = True

        # ── 入场 ─────────────────────────────────────────────────
        if exited_this_bar or self.position:
            return

        strong_buy  = bull_score >= min_score
        strong_sell = bear_score >= min_score

        trigger_buy = (strong_buy
                       and bear_score <= self.conflict_threshold
                       and not self._wait_buy_reset
                       and not is_choppy
                       and ok_trend and ok_vol and ok_bbmc_long and trend_ok_long)

        trigger_sell = (self.allow_short and strong_sell
                        and bull_score <= self.conflict_threshold
                        and not self._wait_sell_reset
                        and not is_choppy
                        and ok_trend and ok_vol and ok_bbmc_short and trend_ok_short)

        if trigger_buy:
            self._mr_mode = False
            self._open_long()
            return
        elif trigger_sell:
            self._mr_mode = False
            self._open_short()
            return

        # ── Squeeze 均值回归入场 ───────────────────────────────────
        if not self.use_squeeze_mr:
            return

        in_squeeze = not sqz_off
        if in_squeeze:
            mr_buy = (sqz_val_prev <= 0 and sqz_val > 0
                      and rsi_val < self.rsi_mr_ob
                      and not self._wait_buy_reset)
            mr_sell = (self.allow_short
                       and sqz_val_prev >= 0 and sqz_val < 0
                       and rsi_val > self.rsi_mr_os
                       and not self._wait_sell_reset)
            if mr_buy:
                self._mr_mode = True
                self._open_long()
            elif mr_sell:
                self._mr_mode = True
                self._open_short()
