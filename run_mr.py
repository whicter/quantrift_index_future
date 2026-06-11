"""
run_mr.py — 均值回归策略独立回测

逻辑：
  入场条件：
    - ADX < adx_threshold（低趋势强度 = 震荡）
    - 做多：价格 <= 布林下轨 AND RSI < rsi_os
    - 做空：价格 >= 布林上轨 AND RSI > rsi_ob

  出场条件：
    - 止盈：价格回到布林中轨（均值）
    - 止损：ATR × sl_mult
    - 时间止损：持仓超过 max_bars 根 K 线强平

用法：
  python run_mr.py
  python run_mr.py --tf 1h
  python run_mr.py --tf 1h --adx 25 --rsi-ob 72 --rsi-os 28
"""

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from backtesting import Backtest, Strategy

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).parent


# ══════════════════════════════════════════════════════════════════
# 指标计算（独立，不依赖 indicators.py）
# ══════════════════════════════════════════════════════════════════

def _rma(src: pd.Series, n: int) -> pd.Series:
    return src.ewm(alpha=1.0 / n, adjust=False).mean()

def _atr(high, low, close, n=14) -> pd.Series:
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return _rma(tr, n)

def _rsi(close: pd.Series, n=14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0)
    loss  = (-delta).clip(lower=0)
    return 100 - 100 / (1 + _rma(gain, n) / _rma(loss, n).replace(0, np.nan))

def _adx(high, low, close, n=14) -> pd.Series:
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
    plus_di    = 100 * _rma(pd.Series(plus_dm,  index=high.index), n) / atr_s.replace(0, np.nan)
    minus_di   = 100 * _rma(pd.Series(minus_dm, index=high.index), n) / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _rma(dx.fillna(0), n)

def compute_mr_signals(df: pd.DataFrame, p: dict) -> pd.DataFrame:
    high  = df["High"]
    low   = df["Low"]
    close = df["Close"]

    # 布林带
    bb_mid   = close.rolling(p["bb_len"]).mean()
    bb_std   = close.rolling(p["bb_len"]).std(ddof=1)
    bb_upper = bb_mid + p["bb_mult"] * bb_std
    bb_lower = bb_mid - p["bb_mult"] * bb_std

    # RSI / ADX / ATR
    rsi_val = _rsi(close, p["rsi_len"])
    adx_val = _adx(high, low, close, p["adx_len"])
    atr_val = _atr(high, low, close, 14)

    result = df.copy()
    result["bb_mid"]   = bb_mid
    result["bb_upper"] = bb_upper
    result["bb_lower"] = bb_lower
    result["rsiMR"]    = rsi_val
    result["adxMR"]    = adx_val
    result["atrMR"]    = atr_val
    return result


# ══════════════════════════════════════════════════════════════════
# 均值回归策略
# ══════════════════════════════════════════════════════════════════

class MeanReversionStrategy(Strategy):
    adx_threshold: float = 25.0   # ADX 低于此值才入场
    rsi_ob:        float = 72.0   # RSI 超买阈值（做空）
    rsi_os:        float = 28.0   # RSI 超卖阈值（做多）
    sl_mult:       float = 1.5    # 止损 = sl_mult × ATR
    max_bars:      int   = 15     # 时间止损（超过 N 根K线平仓）
    allow_short:   bool  = True
    n_contracts:   int   = 12
    contract_size: int   = 2

    def init(self):
        self._size       = self.n_contracts * self.contract_size
        self._entry_bar  = 0
        self._entry_dir  = 0
        self._sl_price   = 0.0

    def next(self):
        i = len(self.data.Close)
        if i < 30:
            return

        close     = self.data.Close[-1]
        adx       = self.data.adxMR[-1]
        rsi       = self.data.rsiMR[-1]
        bb_upper  = self.data.bb_upper[-1]
        bb_lower  = self.data.bb_lower[-1]
        bb_mid    = self.data.bb_mid[-1]
        atr       = self.data.atrMR[-1]

        if np.isnan(adx) or np.isnan(rsi) or np.isnan(bb_mid):
            return

        # ── 出场逻辑 ────────────────────────────────────────────
        if self.position:
            bars_held = i - self._entry_bar

            # 止损
            if self._entry_dir == 1 and close <= self._sl_price:
                self.position.close()
                return
            if self._entry_dir == -1 and close >= self._sl_price:
                self.position.close()
                return

            # 止盈：回到中轨
            if self._entry_dir == 1 and close >= bb_mid:
                self.position.close()
                return
            if self._entry_dir == -1 and close <= bb_mid:
                self.position.close()
                return

            # 时间止损
            if bars_held >= self.max_bars:
                self.position.close()
                return

            return  # 持仓中，不开新仓

        # ── 入场逻辑（仅在低趋势环境）──────────────────────────
        is_ranging = adx < self.adx_threshold
        if not is_ranging:
            return

        # 做多：触碰下轨 + RSI 超卖
        if close <= bb_lower and rsi < self.rsi_os:
            self.buy(size=self._size)
            self._entry_bar = i
            self._entry_dir = 1
            self._sl_price  = close - self.sl_mult * atr
            return

        # 做空：触碰上轨 + RSI 超买
        if self.allow_short and close >= bb_upper and rsi > self.rsi_ob:
            self.sell(size=self._size)
            self._entry_bar = i
            self._entry_dir = -1
            self._sl_price  = close + self.sl_mult * atr
            return


# ══════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════

def run_mr(tf_name: str, tf_params: dict, p: dict) -> dict:
    from backtest_runner import load_data

    print(f"\n  ── {tf_name.upper()} 均值回归 ──────────────────────────────")
    print(f"     ADX < {p['adx_threshold']}  |  RSI {p['rsi_os']}/{p['rsi_ob']}"
          f"  |  BB {p['bb_len']}×{p['bb_mult']}  |  SL {p['sl_mult']}×ATR"
          f"  |  时间止损 {p['max_bars']} 根")

    df = load_data(tf_params)
    df = compute_mr_signals(df, p)

    MeanReversionStrategy.adx_threshold = p["adx_threshold"]
    MeanReversionStrategy.rsi_ob        = p["rsi_ob"]
    MeanReversionStrategy.rsi_os        = p["rsi_os"]
    MeanReversionStrategy.sl_mult       = p["sl_mult"]
    MeanReversionStrategy.max_bars      = p["max_bars"]
    MeanReversionStrategy.allow_short   = p.get("allow_short", True)
    MeanReversionStrategy.n_contracts   = p.get("n_contracts", 12)
    MeanReversionStrategy.contract_size = p.get("contract_size", 2)

    bt = Backtest(
        df, MeanReversionStrategy,
        cash            = int(tf_params.get("cash", 100_000)),
        commission      = float(tf_params.get("commission", 0.00002)),
        margin          = float(tf_params.get("margin", 0.05)),
        exclusive_orders= True,
    )
    stats = bt.run()

    n_trades = stats.get("# Trades", 0) or 0
    ret      = stats.get("Return [%]", 0) or 0
    wr       = stats.get("Win Rate [%]", 0) or 0
    dd       = stats.get("Max. Drawdown [%]", 0) or 0
    sharpe   = stats.get("Sharpe Ratio", 0) or 0
    pf       = stats.get("Profit Factor", 0) or 0

    print(f"     笔数: {n_trades}  |  胜率: {wr:.1f}%  |  收益: {ret:.1f}%"
          f"  |  DD: {dd:.1f}%  |  Sharpe: {float(sharpe):.3f}  |  PF: {float(pf):.2f}")

    # 年度拆解
    trades_df = stats._trades
    if trades_df is not None and len(trades_df) > 0:
        trades_df = trades_df.copy()
        trades_df["Year"] = pd.to_datetime(trades_df["ExitTime"]).dt.year
        print(f"     {'年份':<6} {'笔数':>5} {'胜率':>7} {'收益%':>9}")
        for yr, grp in trades_df.groupby("Year"):
            wr_y = (grp["ReturnPct"] > 0).mean() * 100
            ret_y = grp["ReturnPct"].sum() * 100
            print(f"     {yr:<6} {len(grp):>5} {wr_y:>6.0f}%  {ret_y:>8.2f}%")

    return {
        "tf": tf_name, "n_trades": n_trades, "win_rate": wr,
        "return_pct": ret, "max_dd": dd, "sharpe": float(sharpe), "pf": float(pf),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",      default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--tf",          default=None, help="只跑指定周期")
    parser.add_argument("--adx",         type=float, default=25.0)
    parser.add_argument("--rsi-ob",      type=float, default=72.0, dest="rsi_ob")
    parser.add_argument("--rsi-os",      type=float, default=28.0, dest="rsi_os")
    parser.add_argument("--bb-len",      type=int,   default=20,   dest="bb_len")
    parser.add_argument("--bb-mult",     type=float, default=2.0,  dest="bb_mult")
    parser.add_argument("--sl-mult",     type=float, default=1.5,  dest="sl_mult")
    parser.add_argument("--max-bars",    type=int,   default=15,   dest="max_bars")
    args = parser.parse_args()

    with open(args.config) as f:
        config = yaml.safe_load(f)

    symbol      = config.get("symbol", "NQ=F")
    allow_short = config.get("allow_short", True)

    p = {
        "adx_threshold": args.adx,
        "rsi_ob":        args.rsi_ob,
        "rsi_os":        args.rsi_os,
        "bb_len":        args.bb_len,
        "bb_mult":       args.bb_mult,
        "rsi_len":       14,
        "adx_len":       14,
        "sl_mult":       args.sl_mult,
        "max_bars":      args.max_bars,
    }

    print("═" * 60)
    print(f"  均值回归策略  |  {symbol}")
    print(f"  参数: ADX<{p['adx_threshold']}  RSI {p['rsi_os']}/{p['rsi_ob']}"
          f"  BB{p['bb_len']}×{p['bb_mult']}  SL={p['sl_mult']}×ATR  时间={p['max_bars']}根")
    print("═" * 60)

    results = []
    for tf_name, tf_params in config.get("timeframes", {}).items():
        if args.tf and tf_name != args.tf:
            continue
        params = tf_params.copy()
        params.setdefault("symbol",      symbol)
        params.setdefault("allow_short", allow_short)
        p_run = {**p, "allow_short": allow_short,
                 "n_contracts":   tf_params.get("n_contracts", 12),
                 "contract_size": tf_params.get("contract_size", 2)}
        try:
            r = run_mr(tf_name, params, p_run)
            results.append(r)
        except Exception as e:
            import traceback
            print(f"  [ERROR] {tf_name}: {e}")
            traceback.print_exc()

    # 汇总
    print("\n" + "═" * 60)
    print("  均值回归 vs 趋势策略 对比（均值回归结果）")
    print("═" * 60)
    print(f"  {'周期':<6} {'笔数':>5} {'胜率':>7} {'收益%':>8} {'Sharpe':>7} {'DD%':>8} {'PF':>6}")
    print("  " + "─" * 52)
    for r in results:
        print(f"  {r['tf']:<6} {r['n_trades']:>5} {r['win_rate']:>6.1f}% "
              f"{r['return_pct']:>7.1f}% {r['sharpe']:>7.3f} "
              f"{r['max_dd']:>7.1f}% {r['pf']:>6.2f}")
    print("═" * 60)
    print("\n  趋势策略参考（NQ 优化后）:")
    print("  1H:  261笔  68.6%  +49.8%  Sharpe 0.66  DD -19.6%  PF 3.52")
    print("  4H:   79笔  87.3%  +80.8%  Sharpe 0.58  DD -27.6%  PF 4.63")
    print("  1D:  113笔  85.8% +272.0%  Sharpe 0.76  DD -29.2%  PF 6.04")


if __name__ == "__main__":
    main()
