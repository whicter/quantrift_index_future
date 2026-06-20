"""
weekly_review.py — 每周五自动复盘，发送 Telegram 报告

内容：
  1. 本周回测成交记录（入场/出场/盈亏/胜率）
  2. 错过的机会（bull/ADX 差一点没过门槛）
  3. 本周净值变化
  4. 简要建议

用法：
  python weekly_review.py --port 4001          # 实盘
  python weekly_review.py --port 4001 --send   # 发送 Telegram
"""

import argparse
import os
import sys
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
import warnings
from backtesting import Backtest
from ib_insync import IB, ContFuture, util

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(__file__))
from indicators import compute_signals
from strategy import ConfluenceStrategy

ET = ZoneInfo("America/New_York")

DURATION_MAP = {"1h": "60 D", "4h": "180 D", "1d": "1 Y"}
BAR_SIZE_MAP = {"1h": "1 hour", "4h": "4 hours", "1d": "1 day"}
INSTRUMENTS  = {"NQ": {"symbol": "MNQ", "multiplier": 2},
                "ES": {"symbol": "MES", "multiplier": 5}}


# ── Telegram ──────────────────────────────────────────────────────────

def send_tg(token: str, chat_id: str, msg: str):
    import urllib.request, urllib.parse
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": f"[QuantRift] {msg}",
    }).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    urllib.request.urlopen(req, timeout=10)


# ── 回测 ──────────────────────────────────────────────────────────────

class _CaptureStrategy(ConfluenceStrategy):
    pass


def _set_params(strategy_cls, params, n, multiplier):
    strategy_cls.min_score        = int(params["min_score"])
    strategy_cls.adx_threshold    = float(params.get("adx_threshold", 20.0))
    strategy_cls.use_adx          = bool(params.get("use_adx", True))
    strategy_cls.use_bbmc_dir     = bool(params.get("use_bbmc_dir", True))
    strategy_cls.use_vol          = bool(params.get("use_vol", True))
    strategy_cls.vol_mult         = float(params.get("vol_mult", 1.0))
    strategy_cls.allow_short      = bool(params.get("allow_short", True))
    strategy_cls.reversal_score   = int(params.get("reversal_score", 2))
    strategy_cls.conflict_threshold = int(params.get("conflict_threshold", 2))
    strategy_cls.use_squeeze_mr   = bool(params.get("use_squeeze_mr", False))
    strategy_cls.use_staged_tp    = bool(params.get("use_staged_tp", True))
    strategy_cls.atr_sl_mult      = float(params.get("atr_sl_mult", 1.5))
    strategy_cls.atr_tp1_mult     = float(params.get("atr_tp1_mult", 1.0))
    strategy_cls.atr_tp2_mult     = float(params.get("atr_tp2_mult", 2.0))
    strategy_cls.tp1_portion      = float(params.get("tp1_portion", 0.34))
    strategy_cls.exit_len         = int(params.get("exit_len", 14))
    strategy_cls.use_atr_exit     = bool(params.get("use_atr_exit", False))
    strategy_cls.use_ci           = bool(params.get("use_ci", False))
    strategy_cls.ci_threshold     = float(params.get("ci_threshold", 61.8))
    strategy_cls.ci_len           = int(params.get("ci_len", 14))
    strategy_cls.use_trend_filter = bool(params.get("use_trend_filter", False))
    strategy_cls.n_contracts      = n
    strategy_cls.contract_size    = multiplier
    strategy_cls.rsi_ob           = int(params.get("rsi_ob", 70))
    strategy_cls.rsi_os           = int(params.get("rsi_os", 30))
    strategy_cls.rsi_mr_ob        = float(params.get("rsi_mr_ob", 65.0))
    strategy_cls.rsi_mr_os        = float(params.get("rsi_mr_os", 35.0))
    strategy_cls.allow_reversal_flip = bool(params.get("allow_reversal_flip", True))
    strategy_cls.risk_pct_pyramid = float(params.get("risk_pct_pyramid", 1.0))


def run_backtest(df_sig: pd.DataFrame, params: dict, n: int, multiplier: int):
    _set_params(_CaptureStrategy, params, n, multiplier)
    bt = Backtest(
        df_sig, _CaptureStrategy,
        cash=500_000,
        commission=float(params.get("commission", 0.00002)),
        margin=float(params.get("margin", 0.05)),
        exclusive_orders=True,
        finalize_trades=True,
    )
    stats = bt.run()
    trades_df = stats.get("_trades")
    return trades_df


# ── 近一周交易过滤 ────────────────────────────────────────────────────

def recent_trades(trades_df, days: int = 7) -> pd.DataFrame:
    if trades_df is None or trades_df.empty:
        return pd.DataFrame()
    cutoff = datetime.now() - timedelta(days=days)
    mask = pd.to_datetime(trades_df["ExitTime"]) >= cutoff
    return trades_df[mask].copy()


# ── 错过机会检测 ──────────────────────────────────────────────────────

def near_misses(df_sig: pd.DataFrame, params: dict, days: int = 7) -> list[str]:
    """找出最近 N 天内 bull 差 1 分或 ADX 差一点的 bar。"""
    min_score  = params.get("min_score", 5)
    adx_thr    = params.get("adx_threshold", 30.0)
    use_adx    = params.get("use_adx", True)
    use_bbmc   = params.get("use_bbmc_dir", True)

    cutoff = datetime.now() - timedelta(days=days)
    recent = df_sig[df_sig.index >= cutoff]
    if recent.empty:
        return []

    hits = []
    for ts, row in recent.iterrows():
        bull  = row.get("bullScore", 0)
        adx   = row.get("adx", 0)
        bbmc  = row.get("bbmcDir", 0)

        # bull 够、方向对，但 ADX 拦截
        bull_ok = bull >= min_score
        bbmc_ok = (not use_bbmc) or bbmc >= 0
        adx_ok  = (not use_adx) or adx >= adx_thr

        if bull_ok and bbmc_ok and not adx_ok:
            hits.append(f"  {ts.strftime('%m-%d %H:%M')} bull={int(bull)} adx={adx:.1f}<{adx_thr} (ADX拦截)")
        elif bull_ok and not bbmc_ok and adx_ok:
            hits.append(f"  {ts.strftime('%m-%d %H:%M')} bull={int(bull)} bbmc={int(bbmc)} (方向拦截)")
        elif bull == min_score - 1 and bbmc_ok and adx_ok:
            hits.append(f"  {ts.strftime('%m-%d %H:%M')} bull={int(bull)}<{min_score} adx={adx:.1f} (差1分)")

    return hits[-10:]  # 最多显示10条


# ── 主逻辑 ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host",   default="127.0.0.1")
    parser.add_argument("--port",   type=int, default=4001)
    parser.add_argument("--send",   action="store_true", help="发送 Telegram")
    parser.add_argument("--days",   type=int, default=7, help="回看天数")
    args = parser.parse_args()

    with open(os.path.join(os.path.dirname(__file__), "config.yaml")) as f:
        cfg = yaml.safe_load(f)

    tg_cfg  = cfg.get("telegram", {})
    tg_token   = tg_cfg.get("token")   or os.environ.get("TG_TOKEN", "")
    tg_chat_id = tg_cfg.get("chat_id") or os.environ.get("TG_CHAT_ID", "")

    ib = IB()
    ib.connect(args.host, args.port, clientId=98)

    now_et   = datetime.now(ET)
    week_str = f"{(now_et - timedelta(days=args.days)).strftime('%m/%d')}–{now_et.strftime('%m/%d')}"

    lines = [f"📊 周度复盘  {now_et.strftime('%Y-%m-%d')}  ({week_str})"]
    lines.append("=" * 36)

    # 账户净值
    try:
        vals = ib.accountValues()
        equity = next((float(v.value) for v in vals
                       if v.tag == "NetLiquidation" and v.currency == "USD"), None)
        if equity:
            lines.append(f"账户净值: ${equity:,.2f}")
    except Exception:
        pass

    all_trades   = []
    near_miss_lines = []

    for inst, cfg_key in [("NQ", "timeframes"), ("ES", "es_timeframes")]:
        tfs_cfg   = cfg.get(cfg_key, {})
        sym       = INSTRUMENTS[inst]["symbol"]
        mult      = INSTRUMENTS[inst]["multiplier"]
        contract  = ib.qualifyContracts(ContFuture(sym, "CME", "USD"))[0]

        lines.append(f"\n── {inst} ──────────────────")

        for tf in ["1h", "4h", "1d"]:
            if tf not in tfs_cfg:
                continue
            params = deepcopy(tfs_cfg[tf])
            try:
                bars = ib.reqHistoricalData(
                    contract, endDateTime="", durationStr=DURATION_MAP[tf],
                    barSizeSetting=BAR_SIZE_MAP[tf], whatToShow="TRADES",
                    useRTH=False, formatDate=1, keepUpToDate=False)
                ib.sleep(2)
                if not bars:
                    lines.append(f"  {tf}: 无数据")
                    continue

                df = util.df(bars).rename(columns={
                    "date":"Date","open":"Open","high":"High",
                    "low":"Low","close":"Close","volume":"Volume"})
                df["Date"] = pd.to_datetime(df["Date"])
                if df["Date"].dt.tz is not None:
                    df["Date"] = df["Date"].dt.tz_localize(None)
                df = df.set_index("Date")[["Open","High","Low","Close","Volume"]].dropna()
                df = df.iloc[:-1]

                df_sig = compute_signals(df, params)

                # 错过机会
                misses = near_misses(df_sig, params, days=args.days)
                if misses:
                    near_miss_lines.append(f"{inst}/{tf} 错过机会（共{len(misses)}次）:")
                    near_miss_lines.extend(misses)

                # 回测交易
                trades_df = run_backtest(df_sig, params, n=1, multiplier=mult)
                rct = recent_trades(trades_df, days=args.days)

                if rct.empty:
                    lines.append(f"  {tf}: 本周无成交")
                else:
                    wins  = (rct["PnL"] > 0).sum()
                    total = len(rct)
                    pnl   = rct["PnL"].sum()
                    win_r = wins / total * 100 if total else 0
                    lines.append(f"  {tf}: {total}笔  胜率{win_r:.0f}%  盈亏${pnl:+.0f}")
                    for _, t in rct.iterrows():
                        entry_t = pd.to_datetime(t["EntryTime"]).strftime("%m-%d %H:%M")
                        exit_t  = pd.to_datetime(t["ExitTime"]).strftime("%m-%d %H:%M")
                        side    = "多" if t["Size"] > 0 else "空"
                        lines.append(f"    {entry_t}→{exit_t} {side} ${t['PnL']:+.0f}")
                    all_trades.append(rct)

            except Exception as e:
                lines.append(f"  {tf}: 处理失败 {e}")

    # 错过机会汇总
    if near_miss_lines:
        lines.append("\n── 错过的机会 ──────────────────")
        lines.extend(near_miss_lines)
    else:
        lines.append("\n── 错过的机会 ──────────────────")
        lines.append("  本周无明显错过机会")

    # 总结
    lines.append("\n── 总结 ──────────────────")
    if all_trades:
        all_df  = pd.concat(all_trades)
        total_n = len(all_df)
        total_w = (all_df["PnL"] > 0).sum()
        total_p = all_df["PnL"].sum()
        lines.append(f"  本周共 {total_n} 笔  胜率 {total_w/total_n*100:.0f}%  总盈亏 ${total_p:+.0f}")

        # 简要建议
        lines.append("\n── 优化建议 ──────────────────")
        if near_miss_lines:
            adx_blocked = sum(1 for l in near_miss_lines if "ADX拦截" in l)
            if adx_blocked >= 3:
                lines.append(f"  ⚠️  本周 {adx_blocked} 次被 ADX 拦截，可考虑回测验证降低 adx_threshold")
        if total_n > 0 and total_w / total_n < 0.4:
            lines.append("  ⚠️  胜率低于 40%，近期信号质量偏差，建议观察是否进入震荡市")
        if not near_miss_lines and total_n == 0:
            lines.append("  本周市场未达入场条件，策略正常观望")
    else:
        lines.append("  本周无成交，策略正常观望")
        if near_miss_lines:
            adx_blocked = sum(1 for l in near_miss_lines if "ADX拦截" in l)
            lines.append(f"\n── 优化建议 ──────────────────")
            if adx_blocked >= 2:
                lines.append(f"  ⚠️  {adx_blocked} 次被 ADX 拦截入场，可回测验证降低 adx_threshold")

    ib.disconnect()

    report = "\n".join(lines)
    print(report)

    if args.send and tg_token and tg_chat_id:
        send_tg(tg_token, tg_chat_id, report)
        print("\n✅ Telegram 已发送")
    elif args.send:
        print("\n⚠️  未找到 TG_TOKEN/TG_CHAT_ID，未发送")


if __name__ == "__main__":
    main()
