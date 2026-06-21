"""
ES Mean Reversion — 指标计算模块
所有指标函数接收 pandas Series，返回 pandas Series。
"""
import numpy as np
import pandas as pd


def compute_bb(close: pd.Series, length: int = 20, mult: float = 2.0):
    """布林带：返回 (lower, mid, upper)"""
    mid = close.rolling(length).mean()
    std = close.rolling(length).std(ddof=0)
    upper = mid + mult * std
    lower = mid - mult * std
    return lower, mid, upper


def compute_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    """RSI（Wilder 平滑法）"""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    return rsi


def compute_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """ATR（Wilder 平滑）"""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()
    return atr


def compute_adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """ADX（Average Directional Index）"""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    dm_plus = np.where(
        (high - prev_high) > (prev_low - low),
        np.maximum(high - prev_high, 0),
        0
    )
    dm_minus = np.where(
        (prev_low - low) > (high - prev_high),
        np.maximum(prev_low - low, 0),
        0
    )
    dm_plus = pd.Series(dm_plus, index=high.index)
    dm_minus = pd.Series(dm_minus, index=high.index)

    alpha = 1 / length
    atr_s = tr.ewm(alpha=alpha, min_periods=length, adjust=False).mean()
    di_plus = 100 * dm_plus.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr_s
    di_minus = 100 * dm_minus.ewm(alpha=alpha, min_periods=length, adjust=False).mean() / atr_s

    dx = 100 * (di_plus - di_minus).abs() / (di_plus + di_minus).replace(0, np.nan)
    adx = dx.ewm(alpha=alpha, min_periods=length, adjust=False).mean()
    return adx


def compute_ci(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """
    Choppiness Index（CI）
    CI > 61.8 → 强震荡；CI < 38.2 → 强趋势；38.2–61.8 → 中性
    我们用 CI > 55 作为震荡市过滤阈值
    """
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_sum = tr.rolling(length).sum()
    high_roll = high.rolling(length).max()
    low_roll = low.rolling(length).min()
    range_hl = (high_roll - low_roll).replace(0, np.nan)

    ci = 100 * np.log10(atr_sum / range_hl) / np.log10(length)
    return ci


def compute_vwap(high: pd.Series, low: pd.Series, close: pd.Series,
                 volume: pd.Series, timestamps: pd.DatetimeIndex) -> pd.Series:
    """
    日内 VWAP（每个交易日重置）。
    典型价格 = (H + L + C) / 3
    VWAP = cumsum(TP × Vol) / cumsum(Vol)，每日 00:00 重置
    """
    tp = (high + low + close) / 3
    tpv = tp * volume

    dates = timestamps.normalize()  # 取日期部分

    df = pd.DataFrame({"tpv": tpv, "vol": volume, "date": dates}, index=timestamps)

    cumtpv = df.groupby("date")["tpv"].cumsum()
    cumvol = df.groupby("date")["vol"].cumsum()

    vwap = cumtpv / cumvol.replace(0, np.nan)
    return vwap


def compute_mr_signals(df: pd.DataFrame, params: dict) -> pd.DataFrame:
    """
    计算所有 MR 指标并附加到 DataFrame。
    需要列：open, high, low, close, volume（小写）
    返回带新列的 DataFrame：
      bb_lower, bb_mid, bb_upper, rsi, atr, adx, ci, vwap,
      bull_mr（0-3）, bear_mr（0-3）
    """
    df = df.copy()

    bb_len   = params.get("bb_len", 20)
    bb_mult  = params.get("bb_mult", 2.0)
    rsi_len  = params.get("rsi_len", 14)
    rsi_ob   = params.get("rsi_ob", 72.0)
    rsi_os   = params.get("rsi_os", 28.0)
    atr_len  = params.get("atr_len", 14)
    adx_len  = params.get("adx_len", 14)
    adx_thr  = params.get("adx_threshold", 25.0)
    ci_len   = params.get("ci_len", 14)
    ci_thr   = params.get("ci_threshold", 55.0)
    vwap_m   = params.get("vwap_atr_mult", 1.5)

    # 指标计算
    df["bb_lower"], df["bb_mid"], df["bb_upper"] = compute_bb(df["close"], bb_len, bb_mult)
    df["rsi"]  = compute_rsi(df["close"], rsi_len)
    df["atr"]  = compute_atr(df["high"], df["low"], df["close"], atr_len)
    df["adx"]  = compute_adx(df["high"], df["low"], df["close"], adx_len)
    df["ci"]   = compute_ci(df["high"], df["low"], df["close"], ci_len)

    if "volume" in df.columns and df["volume"].sum() > 0:
        df["vwap"] = compute_vwap(
            df["high"], df["low"], df["close"], df["volume"], df.index
        )
    else:
        # 无成交量数据时用 BB 中轨代替 VWAP（降级）
        df["vwap"] = df["bb_mid"]

    # 评分（每个信号 0 或 1，合计最高 3）
    bull_rsi  = (df["rsi"] < rsi_os).astype(int)
    bull_bb   = (df["close"] <= df["bb_lower"]).astype(int)
    bull_vwap = (df["close"] < df["vwap"] - vwap_m * df["atr"]).astype(int)

    bear_rsi  = (df["rsi"] > rsi_ob).astype(int)
    bear_bb   = (df["close"] >= df["bb_upper"]).astype(int)
    bear_vwap = (df["close"] > df["vwap"] + vwap_m * df["atr"]).astype(int)

    df["bull_mr"] = bull_rsi + bull_bb + bull_vwap
    df["bear_mr"] = bear_rsi + bear_bb + bear_vwap

    # 市场状态过滤器（布尔列，方便策略类直接使用）
    df["filter_ok"] = (df["adx"] < adx_thr) & (df["ci"] > ci_thr)

    return df
