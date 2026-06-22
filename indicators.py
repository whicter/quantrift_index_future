"""
indicators.py — 将 Pine Script Confluence 指标逻辑完整翻译为 Python。

每个函数尽量与 Pine Script 原版保持一致：
  - EMA 使用 ewm(span=n, adjust=False)  对应 ta.ema
  - ATR 使用 ewm(alpha=1/n, adjust=False) 对应 ta.rma / ta.atr
  - WMA / HMA 手动实现
  - CD 背离使用逐 Bar 循环，完全复刻 barssince 逻辑
"""

import math
from pathlib import Path
import numpy as np
import pandas as pd


# ══════════════════════════════════════════════════════════
# 基础数学工具
# ══════════════════════════════════════════════════════════

def _wma(src: pd.Series, length: int) -> pd.Series:
    """加权移动平均（Weighted MA），与 Pine Script ta.wma 一致。"""
    if length <= 0:
        return src.copy()
    weights = np.arange(1, length + 1, dtype=float)

    def _wavg(x: np.ndarray) -> float:
        return float(np.dot(x, weights) / weights.sum())

    return src.rolling(length, min_periods=length).apply(_wavg, raw=True)


def _hma(src: pd.Series, length: int) -> pd.Series:
    """Hull Moving Average，与 Pine Script hma() 一致。"""
    half = max(1, length // 2)
    sqrt_len = max(1, round(math.sqrt(length)))
    return _wma(2 * _wma(src, half) - _wma(src, length), sqrt_len)


def _ema(src: pd.Series, length: int) -> pd.Series:
    """EMA，alpha = 2/(length+1)，对应 ta.ema。"""
    return src.ewm(span=length, adjust=False).mean()


def _sma(src: pd.Series, length: int) -> pd.Series:
    return src.rolling(length, min_periods=length).mean()


def _rma(src: pd.Series, length: int) -> pd.Series:
    """Wilder 平滑均线，alpha = 1/length，对应 ta.rma / ta.atr 内部。"""
    return src.ewm(alpha=1.0 / length, adjust=False).mean()


def _adx(high: pd.Series, low: pd.Series, close: pd.Series, n: int) -> pd.Series:
    """Average Directional Index，对应 Pine Script ta.dmi(n, n)[2]。"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)

    up   =  high.diff()
    down = -low.diff()

    plus_dm  = np.where((up > down) & (up > 0),   up.values,   0.0)
    minus_dm = np.where((down > up) & (down > 0), down.values, 0.0)

    atr_s      = _rma(tr, n)
    plus_dm_s  = _rma(pd.Series(plus_dm,  index=high.index), n)
    minus_dm_s = _rma(pd.Series(minus_dm, index=high.index), n)

    plus_di  = 100 * plus_dm_s  / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm_s / atr_s.replace(0, np.nan)

    dx  = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _rma(dx.fillna(0), n)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """真实波幅（True Range），对应 ta.tr(true)。"""
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def _atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """平均真实波幅，对应 ta.atr。"""
    return _rma(_true_range(high, low, close), length)


def _rsi(close: pd.Series, length: int) -> pd.Series:
    """RSI，对应 ta.rsi。"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = _rma(gain, length)
    avg_loss = _rma(loss, length)
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _macd(close: pd.Series, fast: int, slow: int, signal: int):
    """MACD，返回 (macd_line, signal_line, histogram)。"""
    macd_line = _ema(close, fast) - _ema(close, slow)
    signal_line = _ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def _linreg(src: pd.Series, length: int) -> pd.Series:
    """线性回归当前值，对应 ta.linreg(src, length, 0)。"""
    def _lr(x: np.ndarray) -> float:
        n = len(x)
        if n < 2:
            return float(x[-1])
        t = np.arange(n, dtype=float)
        m, b = np.polyfit(t, x, 1)
        return float(m * (n - 1) + b)

    return src.rolling(length, min_periods=length).apply(_lr, raw=True)


# ══════════════════════════════════════════════════════════
# 状态机工具（逐 Bar 循环）
# ══════════════════════════════════════════════════════════

def _ssl_state(close: pd.Series, sH: pd.Series, sL: pd.Series) -> pd.Series:
    """
    SSL 状态机：
      close > sH → state = 1 (多头)
      close < sL → state = -1 (空头)
      否则       → 保持前值
    对应 Pine Script 中的 var int hH1 := ... 逻辑。
    """
    c = close.values
    h = sH.values
    l = sL.values
    state = np.zeros(len(c), dtype=int)
    for i in range(len(c)):
        if c[i] > h[i]:
            state[i] = 1
        elif c[i] < l[i]:
            state[i] = -1
        else:
            state[i] = state[i - 1] if i > 0 else 0
    return pd.Series(state, index=close.index)


def _ut_trailing_stop(close: pd.Series, atr_vals: pd.Series, key: float) -> pd.Series:
    """
    UT Bot 追踪止损线，逐 Bar 计算，对应 Pine Script 中 var float utTS 的逻辑。
    """
    n_loss = (key * atr_vals).values
    c = close.values
    ts = np.full(len(c), np.nan)

    for i in range(len(c)):
        if i == 0 or np.isnan(ts[i - 1]) or np.isnan(n_loss[i]):
            ts[i] = c[i] - n_loss[i] if not np.isnan(n_loss[i]) else np.nan
            continue

        prev = ts[i - 1]
        curr, prev_c, nl = c[i], c[i - 1], n_loss[i]

        if curr > prev and prev_c > prev:
            ts[i] = max(prev, curr - nl)
        elif curr < prev and prev_c < prev:
            ts[i] = min(prev, curr + nl)
        elif curr > prev:
            ts[i] = curr - nl
        else:
            ts[i] = curr + nl

    return pd.Series(ts, index=close.index)


def _barssince(condition: pd.Series) -> pd.Series:
    """
    返回距上次 condition==True 过了多少 Bar，对应 ta.barssince。
    若从未为 True 则为 NaN。
    """
    result = np.full(len(condition), np.nan)
    cond = condition.values
    last = -1
    for i in range(len(cond)):
        if cond[i]:
            last = i
        if last >= 0:
            result[i] = i - last
    return pd.Series(result, index=condition.index)


# ══════════════════════════════════════════════════════════
# CD 背离（Bottom / Top Divergence）
# ══════════════════════════════════════════════════════════

def _cd_divergence(close: pd.Series, diff: pd.Series, c_macd: pd.Series):
    """
    CD 背离，完全复刻 Pine Script 逻辑（逐 Bar 循环）。
    返回 (cd_bull, cd_bear)，均为 bool Series。
    """
    n = len(close)
    c_arr = close.values
    d_arr = diff.values
    m_arr = c_macd.values

    # 计算 crossunder / crossover 0
    cross_dn = np.zeros(n, dtype=bool)  # cMACD 下穿 0
    cross_up = np.zeros(n, dtype=bool)  # cMACD 上穿 0
    for i in range(1, n):
        if m_arr[i - 1] > 0 and m_arr[i] <= 0:
            cross_dn[i] = True
        if m_arr[i - 1] < 0 and m_arr[i] >= 0:
            cross_up[i] = True

    # barssince
    cN1 = _barssince(pd.Series(cross_dn, index=close.index)).values   # 距下穿 0
    cMM1 = _barssince(pd.Series(cross_up, index=close.index)).values  # 距上穿 0

    cd_bull = np.zeros(n, dtype=bool)
    cd_bear = np.zeros(n, dtype=bool)
    cCCC = np.zeros(n, dtype=bool)
    cDB  = np.zeros(n, dtype=bool)
    cJJJ = np.zeros(n, dtype=bool)
    cBG  = np.zeros(n, dtype=bool)

    def _safe_n1s(idx):
        v = cN1[idx]
        return max(int(v) + 1, 1) if not np.isnan(v) else 1

    def _safe_m1s(idx):
        v = cMM1[idx]
        return max(int(v) + 1, 1) if not np.isnan(v) else 1

    for i in range(2, n):
        n1s = _safe_n1s(i)
        m1s = _safe_m1s(i)

        # ── Bottom divergence ──────────────────────────────────────
        s1 = max(0, i - n1s + 1)
        bCC1 = np.min(c_arr[s1: i + 1])
        bDF1 = np.min(d_arr[s1: i + 1])

        i2 = i - m1s
        if i2 >= 0:
            n1s_i2 = _safe_n1s(i2)
            s2 = max(0, i2 - n1s_i2 + 1)
            bCC2 = np.min(c_arr[s2: i2 + 1])
            bDF2 = np.min(d_arr[s2: i2 + 1])
        else:
            bCC2, bDF2 = bCC1, bDF1

        i3 = i - 2 * m1s
        if i3 >= 0:
            n1s_i3 = _safe_n1s(i3)
            s3 = max(0, i3 - n1s_i3 + 1)
            bCC3 = np.min(c_arr[s3: i3 + 1])
            bDF3 = np.min(d_arr[s3: i3 + 1])
        else:
            bCC3, bDF3 = bCC2, bDF2

        cAAA = bCC1 < bCC2 and bDF1 > bDF2 and m_arr[i - 1] < 0 and d_arr[i] < 0
        cBBB = (bCC1 < bCC3 and bDF1 < bDF2 and bDF1 > bDF3
                and m_arr[i - 1] < 0 and d_arr[i] < 0)
        cCCC_i = (cAAA or cBBB) and d_arr[i] < 0
        cCCC[i] = cCCC_i

        cJJJ_i = cCCC[i - 1] and abs(d_arr[i - 1]) >= abs(d_arr[i]) * 1.01
        cJJJ[i] = cJJJ_i
        cdDX_i = not cJJJ[i - 1] and cJJJ_i
        cd_bull[i] = cCCC_i or cdDX_i

        # ── Top divergence ─────────────────────────────────────────
        s1t = max(0, i - m1s + 1)
        tCH1 = np.max(c_arr[s1t: i + 1])
        tDF1 = np.max(d_arr[s1t: i + 1])

        i2t = i - n1s
        if i2t >= 0:
            m1s_i2 = _safe_m1s(i2t)
            s2t = max(0, i2t - m1s_i2 + 1)
            tCH2 = np.max(c_arr[s2t: i2t + 1])
            tDF2 = np.max(d_arr[s2t: i2t + 1])
        else:
            tCH2, tDF2 = tCH1, tDF1

        i3t = i - 2 * n1s
        if i3t >= 0:
            m1s_i3 = _safe_m1s(i3t)
            s3t = max(0, i3t - m1s_i3 + 1)
            tCH3 = np.max(c_arr[s3t: i3t + 1])
            tDF3 = np.max(d_arr[s3t: i3t + 1])
        else:
            tCH3, tDF3 = tCH2, tDF2

        cZJ = tCH1 > tCH2 and tDF1 < tDF2 and m_arr[i - 1] > 0 and d_arr[i] > 0
        cGX = (tCH1 > tCH3 and tDF1 > tDF2 and tDF1 < tDF3
               and m_arr[i - 1] > 0 and d_arr[i] > 0)
        cDB_i = (cZJ or cGX) and d_arr[i] > 0
        cDB[i] = cDB_i

        cBG_i = cDB[i - 1] and d_arr[i - 1] >= d_arr[i] * 1.01
        cBG[i] = cBG_i
        cdBGX_i = not cBG[i - 1] and cBG_i
        cd_bear[i] = cDB_i or cdBGX_i

    return (pd.Series(cd_bull, index=close.index),
            pd.Series(cd_bear, index=close.index))


# ══════════════════════════════════════════════════════════
# Pattern + 背离检测
# ══════════════════════════════════════════════════════════

def _smi_divergence(close: pd.Series, sqz_val: pd.Series,
                    lookback: int = 5):
    """
    SMI 底背离 / 顶背离（基于 Squeeze Momentum 动量柱）
    底背离：价格在窗口内创新低，但 sqzVal 当前值（负区间）高于窗口内最低值
    顶背离：价格在窗口内创新高，但 sqzVal 当前值（正区间）低于窗口内最高值
    """
    n = len(close)
    c = close.values
    sq = sqz_val.values

    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)

    for i in range(lookback, n):
        if np.isnan(sq[i]):
            continue
        ws = i - lookback
        c_win  = c[ws: i + 1]
        sq_win = sq[ws: i]     # 不含当前 bar，用于历史极值对比

        if len(sq_win) == 0 or np.all(np.isnan(sq_win)):
            continue

        c_min = np.nanmin(c_win)
        c_max = np.nanmax(c_win)
        sq_min = np.nanmin(sq_win)
        sq_max = np.nanmax(sq_win)

        # 底背离：当前价格接近 N-bar 低点，sqzVal 在负区间但高于历史最低
        if (c[i] <= c_min * 1.005          # 价格在低位（容忍 0.5%）
                and sq[i] < 0              # SMI 在负区间（下行动量）
                and sq[i] > sq_min):       # 但动量在收缩（不创新低）
            bull[i] = True

        # 顶背离：当前价格接近 N-bar 高点，sqzVal 在正区间但低于历史最高
        if (c[i] >= c_max * 0.995          # 价格在高位
                and sq[i] > 0              # SMI 在正区间（上行动量）
                and sq[i] < sq_max):       # 但动量在衰减（不创新高）
            bear[i] = True

    return pd.Series(bull, index=close.index), pd.Series(bear, index=close.index)


def _rsi_divergence(close: pd.Series, rsi_val: pd.Series,
                    lookback: int = 5):
    """
    RSI 底背离 / 顶背离
    底背离：价格创新低，RSI < 45 但高于窗口内 RSI 最低值
    顶背离：价格创新高，RSI > 55 但低于窗口内 RSI 最高值
    """
    n = len(close)
    c = close.values
    r = rsi_val.values

    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)

    for i in range(lookback, n):
        if np.isnan(r[i]):
            continue
        ws = i - lookback
        c_win = c[ws: i + 1]
        r_win = r[ws: i]

        if len(r_win) == 0 or np.all(np.isnan(r_win)):
            continue

        c_min = np.nanmin(c_win)
        c_max = np.nanmax(c_win)
        r_min = np.nanmin(r_win)
        r_max = np.nanmax(r_win)

        if (c[i] <= c_min * 1.005
                and r[i] < 45          # RSI 在超卖偏低区间
                and r[i] > r_min):     # 但不创 RSI 新低 = 底背离
            bull[i] = True

        if (c[i] >= c_max * 0.995
                and r[i] > 55          # RSI 在超买偏高区间
                and r[i] < r_max):     # 但不创 RSI 新高 = 顶背离
            bear[i] = True

    return pd.Series(bull, index=close.index), pd.Series(bear, index=close.index)


def _pin_bar(high: pd.Series, low: pd.Series,
             open_: pd.Series, close: pd.Series,
             wick_ratio: float = 1.5):
    """
    锤子线（看涨 pin bar）/ 射击之星（看跌 pin bar）
    锤子线：下影线 > 实体 × ratio 且下影线 > 上影线 × ratio
    射击之星：上影线 > 实体 × ratio 且上影线 > 下影线 × ratio
    """
    body        = (close - open_).abs()
    lower_wick  = pd.concat([open_, close], axis=1).min(axis=1) - low
    upper_wick  = high - pd.concat([open_, close], axis=1).max(axis=1)

    lower_wick  = lower_wick.clip(lower=0)
    upper_wick  = upper_wick.clip(lower=0)

    pin_bull = ((lower_wick > body * wick_ratio) &
                (lower_wick > upper_wick * wick_ratio))
    pin_bear = ((upper_wick > body * wick_ratio) &
                (upper_wick > lower_wick * wick_ratio))

    return pin_bull.fillna(False), pin_bear.fillna(False)


def _double_bottom(close: pd.Series, low: pd.Series, high: pd.Series,
                   atr: pd.Series, lookback: int = 20, tol_atr: float = 0.5):
    """
    双底形态（底部两次测试相近低点 + 中间有明显反弹）
    双顶形态（顶部两次测试相近高点 + 中间有明显回调）
    """
    n = len(close)
    l_arr = low.values
    h_arr = high.values
    c_arr = close.values
    a_arr = atr.values

    bull = np.zeros(n, dtype=bool)
    bear = np.zeros(n, dtype=bool)

    for i in range(lookback + 3, n):
        if np.isnan(a_arr[i]) or a_arr[i] <= 0:
            continue
        tol = a_arr[i] * tol_atr
        ws  = i - lookback

        cur_low   = l_arr[i]
        cur_close = c_arr[i]
        cur_high  = h_arr[i]

        # 当前 bar 必须接近区间低点（双底）
        win_low = np.nanmin(l_arr[ws:i])
        if cur_low > win_low + tol:
            goto_top = True
        else:
            goto_top = False
            # 在窗口内找价格接近的前一个低点
            for j in range(ws, i - 3):
                if abs(l_arr[j] - cur_low) <= tol:
                    # 两低点之间有明显反弹
                    between_max = np.nanmax(h_arr[j: i])
                    if between_max - max(l_arr[j], cur_low) > tol * 2:
                        # 当前 bar 收盘需在低点上方（确认反转）
                        if cur_close > cur_low + tol * 0.5:
                            bull[i] = True
                            break

        # 双顶：当前 bar 接近区间高点
        win_high = np.nanmax(h_arr[ws:i])
        if cur_high >= win_high - tol:
            for j in range(ws, i - 3):
                if abs(h_arr[j] - cur_high) <= tol:
                    between_min = np.nanmin(l_arr[j: i])
                    if max(h_arr[j], cur_high) - between_min > tol * 2:
                        if cur_close < cur_high - tol * 0.5:
                            bear[i] = True
                            break

    return pd.Series(bull, index=close.index), pd.Series(bear, index=close.index)


# ══════════════════════════════════════════════════════════
# 主入口：计算所有信号
# ══════════════════════════════════════════════════════════

def compute_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    输入标准 OHLCV DataFrame（列名 Open/High/Low/Close/Volume）。
    返回追加了信号列的新 DataFrame，关键列：
      bullScore, bearScore, isChoppy, sslExit, upperk, lowerk
    """
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]
    tr    = _true_range(high, low, close)

    # ── ① UT Bot ──────────────────────────────────────────────────
    ut_atr_val = _atr(high, low, close, params["ut_atr"])
    utTS   = _ut_trailing_stop(close, ut_atr_val, params["ut_key"])
    ut_bull = close > utTS
    ut_bear = close < utTS

    # ── ② SSL Hybrid ──────────────────────────────────────────────
    ssl_len  = params["ssl_len"]
    ssl2_len = params["ssl2_len"]
    ssl_mult = params["ssl_mult"]

    BBMC   = _hma(close, ssl_len)
    atrSSL = _ema(tr, ssl_len)
    upperk = BBMC + atrSSL * ssl_mult
    lowerk = BBMC - atrSSL * ssl_mult

    sH1  = _hma(high, ssl_len)
    sL1  = _hma(low,  ssl_len)
    hH1  = _ssl_state(close, sH1, sL1)
    ssl1 = pd.Series(np.where(hH1 < 0, sH1, sL1), index=close.index)

    sH2  = _ema(high, ssl2_len)
    sL2  = _ema(low,  ssl2_len)
    hH2  = _ssl_state(close, sH2, sL2)
    ssl2 = pd.Series(np.where(hH2 < 0, sH2, sL2), index=close.index)

    atr14    = _atr(high, low, close, 14)
    buyCont  = (close > BBMC) & (close > ssl2) & ((close - atr14 * 0.9) < ssl2)
    sellCont = (close < BBMC) & (close < ssl2) & ((close + atr14 * 0.9) > ssl2)
    ssl_bull = (close > BBMC) & (close > ssl1)
    ssl_bear = (close < BBMC) & (close < ssl1)

    # SSL Exit 止盈线（周期可配置，默认15）
    exit_len = int(params.get("exit_len", 15))
    exitH   = _hma(high, exit_len)
    exitL   = _hma(low,  exit_len)
    hlv_ex  = _ssl_state(close, exitH, exitL)
    sslExit = pd.Series(np.where(hlv_ex < 0, exitH, exitL), index=close.index)

    # ── ③ RSI ─────────────────────────────────────────────────────
    rsiVal   = _rsi(close, params["rsi_len"])
    rsi_bull = rsiVal > 50
    rsi_bear = rsiVal < 50

    # ── ④ MACD ────────────────────────────────────────────────────
    macdL, sigL, _ = _macd(close, params["macd_fast"],
                            params["macd_slow"], params["macd_signal"])
    macd_bull = macdL > sigL
    macd_bear = macdL < sigL

    # ── ⑤ Squeeze Momentum ────────────────────────────────────────
    sqz_bbl = params["sqz_bbl"]
    sqz_bbm = params["sqz_bbm"]
    sqz_kcl = params["sqz_kcl"]
    sqz_kcm = params["sqz_kcm"]

    bb_basis = _sma(close, sqz_bbl)
    bb_std   = close.rolling(sqz_bbl, min_periods=sqz_bbl).std(ddof=1)
    bb_upper = bb_basis + sqz_bbm * bb_std
    bb_lower = bb_basis - sqz_bbm * bb_std

    kc_basis = _sma(close, sqz_kcl)
    kc_rng   = _sma(tr, sqz_kcl)
    kc_upper = kc_basis + sqz_kcm * kc_rng
    kc_lower = kc_basis - sqz_kcm * kc_rng

    sqzOff = (bb_lower < kc_lower) & (bb_upper > kc_upper)

    hi_kc  = high.rolling(sqz_kcl, min_periods=sqz_kcl).max()
    lo_kc  = low.rolling(sqz_kcl,  min_periods=sqz_kcl).min()
    sqzSrc = close - ((hi_kc + lo_kc) / 2 + kc_basis) / 2
    sqzVal = _linreg(sqzSrc, sqz_kcl)

    sqz_bull = (sqzVal > 0) & (sqzVal > sqzVal.shift(1))
    sqz_bear = (sqzVal < 0) & (sqzVal < sqzVal.shift(1))

    # ── ⑥ CD 背离 ─────────────────────────────────────────────────
    cDIFF   = _ema(close, 12) - _ema(close, 26)
    cDEA    = _ema(cDIFF, 9)
    cMACD_cd = (cDIFF - cDEA) * 2

    cd_bull, cd_bear = _cd_divergence(close, cDIFF, cMACD_cd)

    # ── ⑦ Choppiness Index 震荡过滤 ───────────────────────────────
    ci_len = params["ci_len"]
    ci_atr_sum = tr.rolling(ci_len, min_periods=ci_len).sum()
    ci_range   = (high.rolling(ci_len, min_periods=ci_len).max()
                  - low.rolling(ci_len,  min_periods=ci_len).min())
    choppiness = 100 * np.log10(ci_atr_sum / ci_range) / np.log10(ci_len)

    if params.get("use_ci", True):
        isChoppy = (choppiness > params["ci_threshold"]).fillna(False)
    else:
        isChoppy = pd.Series(False, index=close.index)

    # ── ⑧ ADX 趋势强度 ────────────────────────────────────────────
    adx_len = int(params.get("adx_len", 14))
    adxVal  = _adx(high, low, close, adx_len)

    # ── ⑨ Volume 放量确认 ─────────────────────────────────────────
    vol_len  = int(params.get("vol_len",  20))
    vol_mult = float(params.get("vol_mult", 1.2))
    vol_ma   = df["Volume"].rolling(vol_len, min_periods=vol_len).mean()
    isHighVol = (df["Volume"] > vol_ma * vol_mult).fillna(False)

    # ── 综合评分 ───────────────────────────────────────────────────
    b1 = ut_bull.astype(int)
    b2 = (ssl_bull | buyCont).astype(int)
    b3 = rsi_bull.astype(int)
    b4 = macd_bull.astype(int)
    b5 = sqz_bull.astype(int)
    b6 = cd_bull.astype(int)

    s1 = ut_bear.astype(int)
    s2 = (ssl_bear | sellCont).astype(int)
    s3 = rsi_bear.astype(int)
    s4 = macd_bear.astype(int)
    s5 = sqz_bear.astype(int)
    s6 = cd_bear.astype(int)

    result = df.copy()
    result["bullScore"] = (b1 + b2 + b3 + b4 + b5 + b6).astype(float)
    result["bearScore"] = (s1 + s2 + s3 + s4 + s5 + s6).astype(float)
    result["isChoppy"]  = isChoppy.astype(float)
    result["sslExit"]   = sslExit
    result["upperk"]    = upperk
    result["lowerk"]    = lowerk
    result["adx"]       = adxVal
    result["isHighVol"] = isHighVol.astype(float)
    result["bbmcDir"]   = np.sign(BBMC.diff()).fillna(0).astype(float)
    result["sqzVal"]    = sqzVal
    result["sqzOff"]    = sqzOff.astype(float)   # 1 = squeeze fired (BB外扩), 0 = squeeze ON
    result["rsiVal"]    = rsiVal
    result["atrVal"]    = atr14   # 14 周期 ATR，供方案二固定止盈止损使用

    # 趋势方向过滤器（SMA，周期可配置）
    tf_len = int(params.get("trend_filter_len", 200))
    if params.get("use_trend_filter", False):
        result["trendSMA"] = _sma(close, tf_len)
    else:
        result["trendSMA"] = pd.Series(np.nan, index=close.index)
    # 调试用分项分数
    result["b1_UT"]    = b1.astype(float)
    result["b2_SSL"]   = b2.astype(float)
    result["b3_RSI"]   = b3.astype(float)
    result["b4_MACD"]  = b4.astype(float)
    result["b5_SQZ"]   = b5.astype(float)
    result["b6_CD"]    = b6.astype(float)
    result["utTS"]     = utTS   # UT Bot 动态追踪止损线

    # ── Pattern + 背离信号 ─────────────────────────────────────────
    div_lb    = int(params.get('divergence_lookback', 5))
    pin_ratio = float(params.get('pin_wick_ratio', 1.5))
    db_lb     = int(params.get('double_bottom_lookback', 20))
    db_tol    = float(params.get('double_bottom_atr_tol', 0.5))

    smi_bull_div, smi_bear_div = _smi_divergence(close, sqzVal, div_lb)
    rsi_bull_div, rsi_bear_div = _rsi_divergence(close, rsiVal, div_lb)
    pin_bar_bull, pin_bar_bear = _pin_bar(high, low, df['Open'], close, pin_ratio)
    double_bottom, double_top  = _double_bottom(close, low, high, atr14, db_lb, db_tol)

    result['smi_bull_div']  = smi_bull_div.astype(float)
    result['smi_bear_div']  = smi_bear_div.astype(float)
    result['rsi_bull_div']  = rsi_bull_div.astype(float)
    result['rsi_bear_div']  = rsi_bear_div.astype(float)
    result['pin_bar_bull']  = pin_bar_bull.astype(float)
    result['pin_bar_bear']  = pin_bar_bear.astype(float)
    result['double_bottom'] = double_bottom.astype(float)
    result['double_top']    = double_top.astype(float)

    # ── VIX 过滤 ──────────────────────────────────────────────────
    if params.get('use_vix_filter', False):
        vix_path = Path(__file__).parent / 'data' / 'VIX_1d_2019-01-01_2026-06-09.csv'
        vix_df   = pd.read_csv(vix_path, index_col=0, parse_dates=True)
        vix_df.index = pd.to_datetime(vix_df.index).tz_localize(None).normalize()
        bar_dates    = result.index.normalize()
        vix_aligned  = vix_df['Close'].reindex(bar_dates, method='ffill')
        vix_aligned.index = result.index
        result['vixLevel'] = vix_aligned.fillna(0.0)
    else:
        result['vixLevel'] = 0.0

    return result
