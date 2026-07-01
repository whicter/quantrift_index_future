"""
spread_engine.py — NQ/ES 收益率价差回归实盘引擎

策略：
  spread = r_NQ - r_ES (1H return)
  z = (spread - mu) / sigma  （滚动 240 bars）
  |z| > 2.0 → 入场：
    z > +2.0: short MNQ(1) + long MES(2)   [spread 偏高，预期回归]
    z < -2.0: long MNQ(1) + short MES(2)   [spread 偏低，预期回归]
  出场：|z| < 0.5 OR |z| > 3.5（止损） OR bars_held >= 8

持仓：1 MNQ ($2/pt) + 2 MES ($5/pt)，近似美元中性
clientId: 25，pm2 进程: ib-bot-spread

用法：
  python nq_es_spread/spread_engine.py
  python nq_es_spread/spread_engine.py --run-now
  python nq_es_spread/spread_engine.py --run-now --dry-run
"""

import argparse
import json
import logging
import os
import sys
import time as _time
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from ib_insync import IB, Future, MarketOrder, util

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

# ── 常量 ────────────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_DIR    = _ROOT / "logs"
STATE_FILE = BASE_DIR / "spread_state.json"
LOG_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

CLIENT_ID     = 25
Z_ENTRY       = 2.0
Z_EXIT        = 0.5
Z_STOP        = 3.5
WINDOW        = 240
MAX_BARS      = 8
MNQ_MULT      = 2      # $2/pt
MES_MULT      = 5      # $5/pt
MES_N         = 2      # 2 手 MES 对冲 1 手 MNQ
BAR_CLOSE_DELAY = 15   # bar 收盘后等待秒数

# ── 日志 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"spread_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Telegram ─────────────────────────────────────────────────────────────
_TG: dict = {"token": "", "chat_id": ""}


def tg_alert(msg: str):
    token   = _TG.get("token", "")
    chat_id = _TG.get("chat_id", "")
    if not token or not chat_id:
        return

    def _send():
        try:
            import urllib.request, urllib.parse
            url  = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": f"[Spread] {msg}",
            }).encode()
            req = urllib.request.Request(url, data=data)
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass

    import threading
    threading.Thread(target=_send, daemon=True).start()


# ── 状态文件 ──────────────────────────────────────────────────────────────
def _default_state() -> dict:
    return {
        "direction":    0,      # +1=long spread, -1=short spread, 0=flat
        "bars_held":    0,
        "entry_time":   None,
        "entry_nq":     None,
        "entry_es":     None,
        "mnq_signed":   0,      # MNQ 实际持仓（+1 or -1）
        "mes_signed":   0,      # MES 实际持仓（+2 or -2）
        "today_date":   None,
        "today_trades": 0,
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            s = json.load(f)
        for k, v in _default_state().items():
            s.setdefault(k, v)
        return s
    return _default_state()


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _reset_daily_count(state: dict) -> dict:
    today_str = date.today().isoformat()
    if state.get("today_date") != today_str:
        state["today_date"]   = today_str
        state["today_trades"] = 0
    return state


# ── 合约（次季）────────────────────────────────────────────────────────────
def _deferred_contract_str() -> str:
    today = date.today()
    y, m, d = today.year, today.month, today.day
    quarters = [3, 6, 9, 12]
    front_month = next((q for q in quarters if q > m or (q == m and d < 20)), None)
    if front_month is None:
        front_month, front_year = 3, y + 1
    else:
        front_year = y
    qidx = quarters.index(front_month)
    def_month = quarters[qidx + 1] if qidx < 3 else 3
    def_year  = front_year if qidx < 3 else front_year + 1
    return f"{def_year}{def_month:02d}"


def get_deferred_contract(ib: IB, symbol: str, exchange: str = "CME"):
    cs = _deferred_contract_str()
    contract = Future(symbol, lastTradeDateOrContractMonth=cs,
                      exchange=exchange, currency="USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"无法 qualify {symbol} {cs}")
    c = qualified[0]
    log.info(f"{symbol} 次季合约: {c.localSymbol}  到期: {c.lastTradeDateOrContractMonth}")
    return c


# ── 账户净值 ──────────────────────────────────────────────────────────────
def get_account_equity(ib: IB) -> float:
    vals = ib.accountValues()
    for v in vals:
        if v.tag == "NetLiquidation" and v.currency == "USD":
            val = float(v.value)
            log.info(f"账户净值: ${val:,.2f}")
            return val
    raise RuntimeError("无法获取账户净值")


# ── IB 持仓查询 ────────────────────────────────────────────────────────────
def get_ib_positions(ib: IB, mnq: Future, mes: Future) -> tuple[int, int]:
    """返回 (mnq_signed, mes_signed)"""
    mnq_pos = mes_pos = 0
    for p in ib.positions():
        if p.contract.symbol == "MNQ":
            mnq_pos = int(p.position)
        elif p.contract.symbol == "MES":
            mes_pos = int(p.position)
    return mnq_pos, mes_pos


# ── 数据获取 ──────────────────────────────────────────────────────────────
def fetch_bars(ib: IB, contract, retries: int = 3) -> pd.DataFrame:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            bars = ib.reqHistoricalData(
                contract, endDateTime="", durationStr="30 D",
                barSizeSetting="1 hour", whatToShow="TRADES",
                useRTH=False, formatDate=1, keepUpToDate=False, timeout=30,
            )
            if not bars:
                raise RuntimeError("返回0根bar")
            df = util.df(bars).rename(columns={
                "date": "Date", "open": "Open", "high": "High",
                "low": "Low", "close": "Close", "volume": "Volume",
            })
            df["Date"] = pd.to_datetime(df["Date"])
            if df["Date"].dt.tz is not None:
                df["Date"] = df["Date"].dt.tz_convert(ET).dt.tz_localize(None)
            df = df.set_index("Date")
            df = df[df.index.dayofweek != 5]
            return df
        except Exception as e:
            last_err = e
            log.warning(f"  fetch_bars {contract.symbol} 第{attempt}次失败: {e}")
            if attempt < retries:
                _time.sleep(3)
    raise RuntimeError(f"fetch_bars 失败: {last_err}") from last_err


# ── 信号计算 ──────────────────────────────────────────────────────────────
def compute_z(df_nq: pd.DataFrame, df_es: pd.DataFrame, window: int = WINDOW):
    """计算最新一根 bar 的 z-score。返回 (z, last_nq_close, last_es_close)"""
    # 对齐时间轴
    common = df_nq.index.intersection(df_es.index)
    nq = df_nq.loc[common, "Close"]
    es = df_es.loc[common, "Close"]

    r_nq = nq.pct_change()
    r_es = es.pct_change()
    spread = r_nq - r_es

    mu   = spread.rolling(window).mean()
    sig  = spread.rolling(window).std()
    z    = (spread - mu) / sig.replace(0, np.nan)

    last_z  = float(z.iloc[-1])
    last_nq = float(nq.iloc[-1])
    last_es = float(es.iloc[-1])
    return last_z, last_nq, last_es


# ── 下单 ──────────────────────────────────────────────────────────────────
def place_two_legs(ib: IB, mnq: Future, mes: Future,
                   mnq_action: str, mes_action: str,
                   dry_run: bool) -> bool:
    """同时下 MNQ(1手) + MES(2手) 市价单。返回是否成功。"""
    mnq_order = MarketOrder(mnq_action, 1)
    mes_order = MarketOrder(mes_action, MES_N)

    if dry_run:
        log.info(f"  [DRY-RUN] {mnq_action} 1 MNQ  {mes_action} {MES_N} MES")
        return True

    try:
        t1 = ib.placeOrder(mnq, mnq_order)
        t2 = ib.placeOrder(mes, mes_order)
        ib.sleep(2)
        log.info(f"  下单: {mnq_action} 1 {mnq.localSymbol}  orderId={t1.order.orderId}")
        log.info(f"  下单: {mes_action} {MES_N} {mes.localSymbol}  orderId={t2.order.orderId}")
        tg_alert(f"{'入场' if mnq_action in ('BUY','SELL') else '出场'}: "
                 f"{mnq_action} 1 MNQ + {mes_action} {MES_N} MES")
        return True
    except Exception as e:
        log.error(f"  下单失败: {e}", exc_info=True)
        tg_alert(f"❌ Spread 下单失败: {e}")
        return False


# ── 核心处理 ──────────────────────────────────────────────────────────────
def process(ib: IB, mnq: Future, mes: Future, state: dict, dry_run: bool = False):
    state = _reset_daily_count(state)

    log.info(f"\n── Spread 信号处理  {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} {'─'*30}")

    # 拉取两个合约的 bar 数据
    try:
        df_nq = fetch_bars(ib, mnq)
        df_es = fetch_bars(ib, mes)
        log.info(f"  NQ {len(df_nq)} bars  ES {len(df_es)} bars")
    except Exception as e:
        log.error(f"  数据拉取失败: {e}")
        raise RuntimeError(f"数据拉取失败: {e}") from e

    # 计算 z-score
    try:
        z, last_nq, last_es = compute_z(df_nq, df_es, WINDOW)
    except Exception as e:
        log.error(f"  z-score 计算失败: {e}")
        raise RuntimeError(f"z-score 计算失败: {e}") from e

    log.info(f"  z={z:+.3f}  NQ={last_nq:.2f}  ES={last_es:.2f}")

    direction = state["direction"]
    bars_held = state["bars_held"]

    # ── 持仓中：检查出场 ──
    if direction != 0:
        state["bars_held"] = bars_held + 1
        bars_held = state["bars_held"]

        exit_reason = None
        if abs(z) < Z_EXIT:
            exit_reason = "mean_revert"
        elif abs(z) > Z_STOP:
            exit_reason = "stop_loss"
        elif bars_held >= MAX_BARS:
            exit_reason = "timeout"

        if exit_reason:
            log.info(f"  出场触发: {exit_reason}  (bars_held={bars_held}, z={z:+.3f})")
            # 平仓: 反向
            mnq_action = "SELL" if direction == 1 else "BUY"
            mes_action = "BUY"  if direction == 1 else "SELL"
            ok = place_two_legs(ib, mnq, mes, mnq_action, mes_action, dry_run)
            if ok or dry_run:
                mnq_pnl = direction * (last_nq - state["entry_nq"]) * MNQ_MULT
                mes_pnl = -direction * MES_N * (last_es - state["entry_es"]) * MES_MULT
                total   = mnq_pnl + mes_pnl
                log.info(f"  PnL: MNQ ${mnq_pnl:+,.0f}  MES ${mes_pnl:+,.0f}  合计 ${total:+,.0f}  ({exit_reason})")
                tg_alert(f"出场({exit_reason}): MNQ ${mnq_pnl:+,.0f} + MES ${mes_pnl:+,.0f} = ${total:+,.0f}")
                state["direction"]  = 0
                state["bars_held"]  = 0
                state["entry_time"] = None
                state["entry_nq"]   = None
                state["entry_es"]   = None
                state["mnq_signed"] = 0
                state["mes_signed"] = 0
        else:
            log.info(f"  持仓中 {direction:+d}  bars_held={bars_held}  等待出场条件")
        return

    # ── 空仓：检查入场 ──
    if abs(z) < Z_ENTRY:
        log.info(f"  z={z:+.3f} 未达阈值 ±{Z_ENTRY}，不入场")
        return

    # 入场
    if z > Z_ENTRY:
        direction = -1   # short spread: short MNQ + long MES
        mnq_action = "SELL"
        mes_action = "BUY"
    else:
        direction = +1   # long spread: long MNQ + short MES
        mnq_action = "BUY"
        mes_action = "SELL"

    log.info(f"  入场信号: z={z:+.3f}  direction={direction:+d}  "
             f"{'SHORT MNQ + LONG MES' if direction==-1 else 'LONG MNQ + SHORT MES'}")

    ok = place_two_legs(ib, mnq, mes, mnq_action, mes_action, dry_run)
    if ok or dry_run:
        state["direction"]    = direction
        state["bars_held"]    = 0
        state["entry_time"]   = datetime.now(ET).isoformat()
        state["entry_nq"]     = last_nq
        state["entry_es"]     = last_es
        state["mnq_signed"]   = direction
        state["mes_signed"]   = direction * MES_N
        state["today_trades"] += 1
        log.info(f"  入场完成  entry_nq={last_nq:.2f}  entry_es={last_es:.2f}")


# ── 辅助：市场开市判断 ──────────────────────────────────────────────────────
def is_market_open(now_et: datetime) -> bool:
    wd = now_et.weekday()
    if wd == 5:
        return False
    if wd == 6 and now_et.hour < 18:
        return False
    if now_et.hour == 17:
        return False
    return True


def is_bar_close(now_et: datetime) -> bool:
    return now_et.minute == 0 and now_et.second <= 30


# ── 主函数 ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NQ/ES 价差回归实盘引擎")
    parser.add_argument("--port",      type=int, default=4001)
    parser.add_argument("--host",      default="127.0.0.1")
    parser.add_argument("--run-now",   action="store_true")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--client-id", type=int, default=CLIENT_ID)
    args = parser.parse_args()

    _TG["token"]   = os.environ.get("TG_TOKEN",   "")
    _TG["chat_id"] = os.environ.get("TG_CHAT_ID", "")

    client_id = args.client_id

    log.info("═" * 62)
    log.info(f"  NQ/ES Spread 引擎  |  clientId: {client_id}")
    log.info(f"  {'📄 模拟盘' if args.port == 4002 else '⚠️  实  盘'}  端口: {args.port}")
    log.info(f"  z_entry={Z_ENTRY}  z_exit={Z_EXIT}  z_stop={Z_STOP}  "
             f"window={WINDOW}  max_bars={MAX_BARS}")
    if args.dry_run:
        log.info("  dry-run 模式：只打印信号，不下单")
    log.info("═" * 62)

    ib      = IB()
    state   = load_state()
    mnq_c   = [None]
    mes_c   = [None]
    equity  = [0.0]

    def do_connect():
        _last_fail = [0.0]
        while True:
            try:
                if ib.isConnected():
                    try: ib.disconnect()
                    except Exception: pass
                log.info(f"连接 IB Gateway {args.host}:{args.port}...")
                ib.connect(args.host, args.port, clientId=client_id, timeout=30, readonly=False)
                log.info(f"✅ IB 已连接  账户: {ib.wrapper.accounts}")

                equity[0] = get_account_equity(ib)
                mnq_c[0]  = get_deferred_contract(ib, "MNQ")
                mes_c[0]  = get_deferred_contract(ib, "MES")

                # 持仓核对
                ib_mnq, ib_mes = get_ib_positions(ib, mnq_c[0], mes_c[0])
                st_mnq = state["mnq_signed"]
                st_mes = state["mes_signed"]
                if ib_mnq != st_mnq or ib_mes != st_mes:
                    log.warning(f"  ⚠ 持仓不一致: IB MNQ={ib_mnq:+d} MES={ib_mes:+d} "
                                f"状态文件 MNQ={st_mnq:+d} MES={st_mes:+d}，以 IB 为准")
                    state["mnq_signed"] = ib_mnq
                    state["mes_signed"] = ib_mes
                    state["direction"]  = 1 if ib_mnq > 0 else (-1 if ib_mnq < 0 else 0)
                    save_state(state)
                    tg_alert(f"⚠ Spread 持仓不一致，已修正 MNQ={ib_mnq:+d} MES={ib_mes:+d}")
                else:
                    log.info(f"  MNQ={ib_mnq:+d}  MES={ib_mes:+d}  ✅")

                tg_alert(f"✅ Spread 引擎已连接  净值: ${equity[0]:,.0f}  "
                         f"{mnq_c[0].localSymbol} / {mes_c[0].localSymbol}")
                return
            except Exception as exc:
                log.error(f"  连接失败: {exc}，10秒后重试")
                now = _time.time()
                if now - _last_fail[0] > 3600:
                    tg_alert(f"❌ Spread 引擎连接失败\n{exc}")
                    _last_fail[0] = now
                try: ib.disconnect()
                except Exception: pass
                _time.sleep(10)

    needs_reconnect = [False]

    def on_error(reqId, errorCode, errorString, contract_):
        if errorCode in (1100, 1101):
            needs_reconnect[0] = True
            log.warning(f"⚠ Error {errorCode}: IB 连接异常，将触发重连")
            tg_alert(f"⚠ Spread 引擎 Error {errorCode}，正在重连...")
        elif errorCode in (2105, 2110):
            log.warning(f"⚠ Error {errorCode}: {errorString}（等待自动恢复）")
        elif errorCode not in (2104, 2106, 2158, 10349):
            log.warning(f"Warning {errorCode}: {errorString}")

    ib.errorEvent += on_error
    do_connect()
    save_state(state)

    if args.run_now:
        log.info("\n▶ --run-now: 立即处理 Spread\n")
        process(ib, mnq_c[0], mes_c[0], state, args.dry_run)
        save_state(state)
        log.info("\n✅ --run-now 完成")
        ib.disconnect()
        return

    log.info("\n▶ 等待 1H Bar 收盘触发...\n")
    _last_bar_time = [None]
    while True:
        _time.sleep(1)
        now_et = datetime.now(ET)

        if needs_reconnect[0]:
            needs_reconnect[0] = False
            _time.sleep(5)
            do_connect()
            save_state(state)
            continue

        if not is_market_open(now_et):
            continue

        if is_bar_close(now_et):
            bar_key = now_et.replace(minute=0, second=0, microsecond=0)
            if _last_bar_time[0] == bar_key:
                continue
            _last_bar_time[0] = bar_key
            log.info(f"\n{'═'*62}")
            log.info(f"  1H Bar 收盘触发  {now_et.strftime('%Y-%m-%d %H:%M ET')}")
            log.info(f"{'═'*62}")
            _time.sleep(BAR_CLOSE_DELAY)
            try:
                equity[0] = get_account_equity(ib)
                process(ib, mnq_c[0], mes_c[0], state, args.dry_run)
            except RuntimeError as e:
                log.error(f"  ❌ 处理失败，触发重连: {e}")
                needs_reconnect[0] = True
            except Exception as e:
                log.error(f"  ❌ 未预期错误: {e}", exc_info=True)
                tg_alert(f"❌ Spread 引擎异常: {e}")
                needs_reconnect[0] = True
            else:
                save_state(state)
        else:
            # 空仓期每小时更新状态文件 mtime，防止健康检查误报 stale
            if now_et.minute == 30 and now_et.second <= 5:
                save_state(state)


if __name__ == "__main__":
    while True:
        try:
            main()
        except KeyboardInterrupt:
            log.info("用户中断，退出")
            break
        except Exception as e:
            log.error(f"main() 崩溃: {e}，30s 后重启", exc_info=True)
            tg_alert(f"❌ Spread 引擎崩溃，30s 后重启\n{e}")
            _time.sleep(30)
