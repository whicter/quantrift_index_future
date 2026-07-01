"""
gap_engine.py — NQ 隔夜跳空回归实盘引擎

策略：每天 16:00-17:00 ET 维护中断后，重开时若跳空 > 75pts 且 VIX<30，反向入场等 gap 填满
合约：MNQ 次季合约
clientId: 24，pm2 进程: ib-bot-gap

入场：17:00 ET bar open 相对前日 15:00 bar close 跳空超阈值
TP：前日 15:00 close（gap fill）
SL：entry ± gap × sl_mult
强制止损：持仓超 12h（05:00 ET）

用法：
  python gap_reversion/gap_engine.py
  python gap_reversion/gap_engine.py --run-now
  python gap_reversion/gap_engine.py --run-now --dry-run
  python gap_reversion/gap_engine.py --port 4001
"""

import argparse
import json
import logging
import sys
import threading
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from ib_insync import IB, Future, MarketOrder, LimitOrder, StopOrder, util

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

import warnings
warnings.filterwarnings("ignore")

# ── 路径 / 时区 ──────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_DIR    = _ROOT / "logs"
STATE_FILE = BASE_DIR / "gap_state.json"
VIX_CSV    = _ROOT / "data" / "VIX_1d.csv"
VIX_CSV_OLD = _ROOT / "data" / "VIX_1d_2019-01-01_2026-06-09.csv"
LOG_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

MNQ_MULTIPLIER = 2      # $2/点
MNQ_EXCHANGE   = "CME"
CLIENT_ID      = 24

# ── 策略参数 ─────────────────────────────────────────────────────────
GAP_THRESH  = 75.0   # pts
SL_MULT     = 5.0    # SL = gap × sl_mult
VIX_MAX     = 30.0   # VIX 前日收盘上限
MAX_HOLD_H  = 12     # 最长持仓小时数
RISK_PCT    = 1.0    # 风险/笔（占净值%）

# ── 日志 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"gap_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── Telegram ─────────────────────────────────────────────────────────
_TG: dict = {"token": "", "chat_id": ""}


def tg_alert(msg: str):
    token, chat_id = _TG.get("token", ""), _TG.get("chat_id", "")
    if not token or not chat_id:
        return
    def _send():
        try:
            import urllib.request, urllib.parse
            url  = f"https://api.telegram.org/bot{token}/sendMessage"
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"[Gap] {msg}"}).encode()
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=5)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()


# ── 状态持久化 ────────────────────────────────────────────────────────
def _default_state():
    return {"signed_contracts": 0, "today_date": None, "today_traded": False,
            "entry_price": None, "tp_price": None, "sl_price": None,
            "tp_order_id": None, "sl_order_id": None, "entry_time": None}


def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            raw = json.load(f)
        for k, v in _default_state().items():
            raw.setdefault(k, v)
        return raw
    s = _default_state()
    save_state(s)
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2, default=str)


# ── VIX 读取 ─────────────────────────────────────────────────────────
def get_prev_vix() -> float | None:
    """读取前日 VIX 收盘价。"""
    csv = VIX_CSV if VIX_CSV.exists() else VIX_CSV_OLD
    try:
        df = pd.read_csv(csv, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        col = "Close" if "Close" in df.columns else df.columns[3]
        today = pd.Timestamp(date.today())
        prev = df[df.index < today][col]
        if len(prev) == 0:
            return None
        return float(prev.iloc[-1])
    except Exception as e:
        log.warning(f"VIX 读取失败: {e}")
        return None


# ── 账户 / 持仓 ───────────────────────────────────────────────────────
def get_account_equity(ib: IB) -> float:
    ib.reqAccountSummary()
    ib.sleep(2)
    for v in ib.accountValues():
        if v.tag == "NetLiquidation" and v.currency == "USD":
            eq = float(v.value)
            log.info(f"账户净值: ${eq:,.2f}")
            return eq
    log.warning("无法获取账户净值，使用默认 $25,000")
    return 25_000.0


def get_ib_position(ib: IB) -> int:
    ib.reqPositions()
    ib.sleep(1)
    total = 0
    for pos in ib.positions():
        if pos.contract.symbol == "MNQ":
            total += int(pos.position)
    return total


# ── 次季合约 ──────────────────────────────────────────────────────────
def get_deferred_mnq_contract(ib: IB):
    today = date.today()
    y, m, d = today.year, today.month, today.day
    quarters = [3, 6, 9, 12]
    front_month, front_year = None, y
    for qm in quarters:
        if qm > m or (qm == m and d < 20):
            front_month = qm; break
    if front_month is None:
        front_month = 3; front_year = y + 1
    qi = quarters.index(front_month)
    def_month = quarters[qi + 1] if qi < 3 else 3
    def_year  = front_year if qi < 3 else front_year + 1
    contract_str = f"{def_year}{def_month:02d}"
    contract = Future("MNQ", lastTradeDateOrContractMonth=contract_str,
                      exchange=MNQ_EXCHANGE, currency="USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"无法 qualify MNQ {contract_str}")
    c = qualified[0]
    log.info(f"MNQ 次季合约: {c.localSymbol}  到期: {c.lastTradeDateOrContractMonth}")
    return c


# ── 历史数据：获取最近 15:00 ET close ────────────────────────────────
def fetch_prev_rth_close(ib: IB, contract) -> tuple[float | None, datetime | None]:
    """
    拉取最近 48h 的 1H 数据，找最后一个 15:00 ET bar 的 Close。
    返回 (prev_close, bar_time)。
    """
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="3 D",
        barSizeSetting="1 hour", whatToShow="TRADES",
        useRTH=False, formatDate=1, keepUpToDate=False, timeout=60,
    )
    if not bars:
        return None, None

    df = util.df(bars).rename(columns={"date": "Date", "close": "Close"})
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_convert(ET).dt.tz_localize(None)
    df = df.set_index("Date")

    # 找所有 15:00 ET bar（3pm bar），取最近一个
    bars_15 = df[df.index.hour == 15]
    if bars_15.empty:
        return None, None

    last = bars_15.iloc[-1]
    return float(last["Close"]), last.name


def fetch_current_open(ib: IB, contract) -> float | None:
    """拉取最近 1 根 1H bar 的 Open（即当前 session open）。"""
    bars = ib.reqHistoricalData(
        contract, endDateTime="", durationStr="7200 S",
        barSizeSetting="1 hour", whatToShow="TRADES",
        useRTH=False, formatDate=1, keepUpToDate=False, timeout=30,
    )
    if not bars:
        return None
    df = util.df(bars).rename(columns={"date": "Date", "open": "Open"})
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_convert(ET).dt.tz_localize(None)
    df = df.set_index("Date")
    bars_17 = df[df.index.hour == 17]
    if not bars_17.empty:
        return float(bars_17.iloc[-1]["Open"])
    return float(df.iloc[-1]["Open"])


# ── 定仓 ─────────────────────────────────────────────────────────────
def calc_n_contracts(equity: float, gap_pts: float) -> int:
    """风险 = gap × sl_mult × $2/pt，定仓最少1手。"""
    risk_usd  = equity * RISK_PCT / 100
    stop_usd  = abs(gap_pts) * SL_MULT * MNQ_MULTIPLIER
    if stop_usd <= 0:
        return 1
    return max(1, round(risk_usd / stop_usd))


# ── 下单 ─────────────────────────────────────────────────────────────
TRADES_CSV = LOG_DIR / "gap_trades.csv"


def _log_trade(action, qty, price, reason="entry"):
    try:
        write_hdr = not TRADES_CSV.exists()
        with open(TRADES_CSV, "a") as f:
            if write_hdr:
                f.write("time,action,qty,price,reason\n")
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')},{action},{qty},{price},{reason}\n")
    except Exception:
        pass


def place_bracket(ib: IB, contract, direction: int, qty: int,
                  tp_price: float, sl_price: float, dry_run: bool):
    """下市价单 + TP限价 + SL止损（bracket order）。"""
    action = "BUY" if direction > 0 else "SELL"
    tp_action = "SELL" if direction > 0 else "BUY"

    if dry_run:
        log.info(f"  [DRY-RUN] {action} {qty} MNQ  TP={tp_price:.2f}  SL={sl_price:.2f}")
        return None, None

    # 主单
    parent = MarketOrder(action, qty)
    parent.transmit = False

    # TP 限价
    tp_order = LimitOrder(tp_action, qty, tp_price)
    tp_order.parentId = parent.orderId
    tp_order.transmit = False

    # SL 止损
    sl_order = StopOrder(tp_action, qty, sl_price)
    sl_order.parentId = parent.orderId
    sl_order.transmit = True  # 最后一个transmit=True触发全部

    parent_trade = ib.placeOrder(contract, parent)
    ib.sleep(1)
    tp_trade = ib.placeOrder(contract, tp_order)
    sl_trade = ib.placeOrder(contract, sl_order)
    ib.sleep(3)

    fill = parent_trade.orderStatus.avgFillPrice or "待成交"
    log.info(f"  ✅ {action} {qty} MNQ @ {fill}  TP={tp_price:.2f}  SL={sl_price:.2f}")
    tg_alert(f"✅ Gap入场: {action} {qty} MNQ @ {fill}  gap={abs(tp_price - fill):.0f}pts  TP={tp_price:.2f}  SL={sl_price:.2f}")
    _log_trade(action, qty, fill)

    return tp_trade.order.orderId, sl_trade.order.orderId


def cancel_orders(ib: IB, *order_ids):
    for oid in order_ids:
        if oid is None:
            continue
        try:
            from ib_insync import Trade
            for t in ib.trades():
                if t.order.orderId == oid and t.orderStatus.status not in ("Filled", "Cancelled"):
                    ib.cancelOrder(t.order)
                    log.info(f"  取消订单 {oid}")
        except Exception as e:
            log.warning(f"  取消订单 {oid} 失败: {e}")


def force_close(ib: IB, contract, state: dict, dry_run: bool):
    """强制平仓（时间止损）。"""
    pos = get_ib_position(ib)
    if pos == 0:
        return
    action = "SELL" if pos > 0 else "BUY"
    qty = abs(pos)
    cancel_orders(ib, state.get("tp_order_id"), state.get("sl_order_id"))
    if dry_run:
        log.info(f"  [DRY-RUN] 时间止损: {action} {qty} MNQ")
        return
    order = MarketOrder(action, qty)
    trade = ib.placeOrder(contract, order)
    ib.sleep(3)
    fill = trade.orderStatus.avgFillPrice or "?"
    log.info(f"  ⏰ 时间止损: {action} {qty} MNQ @ {fill}")
    tg_alert(f"⏰ Gap时间止损: {action} {qty} MNQ @ {fill}")
    _log_trade(action, qty, fill, reason="time_stop")


# ── 核心处理 ──────────────────────────────────────────────────────────
def process(ib: IB, contract, state: dict, equity: float, dry_run: bool):
    now_et = datetime.now(ET)
    log.info(f"\n{'═'*60}")
    log.info(f"  Gap 检查  {now_et.strftime('%Y-%m-%d %H:%M ET')}")
    log.info(f"{'═'*60}")

    # 已有持仓 → 检查是否需要时间止损
    if state.get("signed_contracts", 0) != 0:
        entry_time = state.get("entry_time")
        if entry_time:
            entry_dt = datetime.fromisoformat(entry_time).replace(tzinfo=ET)
            elapsed_h = (datetime.now(ET) - entry_dt).total_seconds() / 3600
            if elapsed_h >= MAX_HOLD_H:
                log.info(f"  持仓已 {elapsed_h:.1f}h，触发时间止损")
                force_close(ib, contract, state, dry_run)
                state.update({"signed_contracts": 0, "today_traded": False,
                              "entry_price": None, "tp_price": None, "sl_price": None,
                              "tp_order_id": None, "sl_order_id": None, "entry_time": None})
                save_state(state)
        return

    # 今日已交易 → 跳过
    today_str = date.today().isoformat()
    if state.get("today_date") == today_str and state.get("today_traded"):
        log.info("  今日已有交易记录，跳过")
        return

    # VIX 过滤
    vix = get_prev_vix()
    if vix is None:
        log.warning("  VIX 数据不可用，跳过")
        return
    log.info(f"  VIX前日: {vix:.1f}")
    if vix >= VIX_MAX:
        log.info(f"  VIX {vix:.1f} ≥ {VIX_MAX}，跳过（高波动期不入场）")
        return

    # 获取前日 15:00 close 和当前 open
    prev_close, prev_time = fetch_prev_rth_close(ib, contract)
    if prev_close is None:
        log.warning("  无法获取前日 15:00 close，跳过")
        return

    current_open = fetch_current_open(ib, contract)
    if current_open is None:
        log.warning("  无法获取当前开盘价，跳过")
        return

    gap = current_open - prev_close
    log.info(f"  前日15:00 close={prev_close:.2f}  当前 open={current_open:.2f}  gap={gap:+.2f}pts")

    if abs(gap) < GAP_THRESH:
        log.info(f"  |gap|={abs(gap):.1f}pts < {GAP_THRESH}pts 阈值，不入场")
        return

    # 计算方向和价格
    direction = -1 if gap > 0 else 1
    tp_price  = prev_close
    sl_price  = current_open - direction * abs(gap) * SL_MULT
    n = calc_n_contracts(equity, gap)

    dir_str = "做空（gap up）" if direction < 0 else "做多（gap down）"
    log.info(f"  信号: {dir_str}  gap={gap:+.1f}pts  → {n}手 MNQ")
    log.info(f"  TP={tp_price:.2f}  SL={sl_price:.2f}  风险={abs(gap)*SL_MULT:.0f}pts/手")

    tp_id, sl_id = place_bracket(ib, contract, direction, n, tp_price, sl_price, dry_run)

    if not dry_run:
        now_str = datetime.now(ET).isoformat()
        state.update({
            "signed_contracts": direction * n,
            "today_date": today_str,
            "today_traded": True,
            "entry_price": current_open,
            "tp_price": tp_price,
            "sl_price": sl_price,
            "tp_order_id": tp_id,
            "sl_order_id": sl_id,
            "entry_time": now_str,
        })
        save_state(state)
    else:
        log.info("  [DRY-RUN] 状态未保存")


# ── 主循环 ────────────────────────────────────────────────────────────
def is_session_open_time(now_et: datetime) -> bool:
    """17:00-17:30 ET 是 session 重开窗口，检查gap。"""
    wd = now_et.weekday()  # 0=Mon, 6=Sun
    if wd == 5:  # 周六
        return False
    return now_et.hour == 17 and now_et.minute < 30


def is_time_stop_check(now_et: datetime) -> bool:
    """每小时整点检查持仓是否需要时间止损。"""
    return now_et.minute == 5


def main():
    import time as _time
    parser = argparse.ArgumentParser(description="NQ Gap 跳空回归实盘引擎")
    parser.add_argument("--config", default=str(BASE_DIR / "config_gap.yaml"))
    parser.add_argument("--port",      type=int, default=4001)
    parser.add_argument("--host",      default="127.0.0.1")
    parser.add_argument("--run-now",   action="store_true")
    parser.add_argument("--dry-run",   action="store_true")
    parser.add_argument("--client-id", type=int, default=CLIENT_ID, dest="client_id")
    args = parser.parse_args()

    # 读 TG 配置
    import yaml, os
    cfg_path = _ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg_main = yaml.safe_load(f)
        _TG["token"]   = cfg_main.get("telegram", {}).get("token", "")
        _TG["chat_id"] = cfg_main.get("telegram", {}).get("chat_id", "")

    client_id = args.client_id
    log.info("═" * 60)
    log.info(f"  NQ Gap 跳空回归引擎  |  clientId: {client_id}")
    log.info(f"  ⚠️  实盘端口: {args.port}")
    if args.dry_run:
        log.info("  dry-run 模式：只打印信号，不下单")
    log.info("═" * 60)

    ib       = IB()
    equity   = 0.0
    contract = None
    state    = load_state()

    def do_connect():
        nonlocal equity, contract
        _last_fail = [0.0]
        while True:
            try:
                if ib.isConnected():
                    try: ib.disconnect()
                    except Exception: pass
                log.info(f"连接 IB Gateway {args.host}:{args.port}...")
                ib.connect(args.host, args.port, clientId=client_id, timeout=30, readonly=False)
                log.info(f"✅ IB 已连接  账户: {ib.wrapper.accounts}")
                equity   = get_account_equity(ib)
                contract = get_deferred_mnq_contract(ib)
                ib_pos   = get_ib_position(ib)
                st_pos   = state["signed_contracts"]
                if ib_pos != st_pos:
                    log.warning(f"  ⚠ 持仓不一致: IB={ib_pos:+d}  状态={st_pos:+d}，以IB为准")
                    state["signed_contracts"] = ib_pos
                    save_state(state)
                    tg_alert(f"⚠ Gap持仓不一致，已修正 → {ib_pos:+d}手")
                else:
                    log.info(f"  MNQ 持仓: {ib_pos:+d}手  ✅")
                tg_alert(f"✅ Gap引擎已连接  净值: ${equity:,.0f}  合约: {contract.localSymbol}")
                return
            except Exception as exc:
                log.error(f"  连接失败: {exc}，10秒后重试")
                now = _time.time()
                if now - _last_fail[0] > 3600:
                    tg_alert(f"❌ Gap引擎连接失败: {exc}")
                    _last_fail[0] = now
                try: ib.disconnect()
                except Exception: pass
                _time.sleep(10)

    needs_reconnect = [False]

    def on_error(reqId, errorCode, errorString, contract_):
        if errorCode in (1100, 1101):
            log.warning(f"⚠️  Error {errorCode}: IB连接异常，将触发重连")
            needs_reconnect[0] = True
        elif errorCode == 2110:
            log.warning(f"⚠️  Error 2110: TWS→IBKR 断连，等待自动恢复")
        elif errorCode in (2104, 2106, 2158):
            log.info(f"  Warning {errorCode}: {errorString}")
        elif errorCode == 2105:
            log.warning(f"⚠️  Error 2105: HMDS断连")
        elif errorCode not in (202, 321, 10167):
            log.warning(f"  Error {errorCode}: {errorString}")

    ib.errorEvent += on_error
    do_connect()

    if args.run_now:
        log.info("\n▶ --run-now: 立即处理 Gap")
        equity = get_account_equity(ib)
        process(ib, contract, state, equity, args.dry_run)
        log.info("\n✅ --run-now 完成")
        ib.disconnect()
        do_connect()

    log.info(f"\n▶ 等待 17:00 ET session reopen（每日gap检查）...\n")
    _last_check_date = [None]

    while True:
        _time.sleep(10)
        if needs_reconnect[0]:
            needs_reconnect[0] = False
            _time.sleep(5)
            do_connect()
            continue

        now_et = datetime.now(ET)

        # 时间止损检查（每小时:05）
        if state.get("signed_contracts", 0) != 0 and is_time_stop_check(now_et):
            equity = get_account_equity(ib)
            process(ib, contract, state, equity, args.dry_run)

        # Gap入场检查（17:00-17:30 ET，每天只触发一次）
        if is_session_open_time(now_et):
            today = now_et.date()
            if _last_check_date[0] != today:
                _last_check_date[0] = today
                _time.sleep(30)  # 等 17:00 bar完全形成
                equity = get_account_equity(ib)
                process(ib, contract, state, equity, args.dry_run)


if __name__ == "__main__":
    import time as _time
    while True:
        try:
            main()
        except KeyboardInterrupt:
            log.info("用户中断，退出")
            break
        except Exception as e:
            log.error(f"main() 崩溃: {e}，30s后重启", exc_info=True)
            tg_alert(f"❌ Gap引擎崩溃，30s后重启\n{e}")
            _time.sleep(30)
