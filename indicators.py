"""
indicators.py — 将 Pine Script Confluence 指标逻辑完整翻译为 Python。

每个函数尽量与 Pine Script 原版保持一致：
  - EMA 使用 ewm(span=n, adjust=False)  对应 ta.ema
  - ATR 使用 ewm(alpha=1/n, adjust=False) 对应 ta.rma / ta.atr
  - WMA / HMA 手动实现
  - CD 背离使用逐 Bar 循环，完全复刻 barssince 逻辑
"""

import math
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

    return result
