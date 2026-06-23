"""
mr_engine.py — ES 均值回归实盘引擎

策略：ES 1H 超卖 MR，只做多
合约：MES 次季合约（deferred contract，跳过当期前月，避免临近交割流动性风险）
clientId: 21，pm2 进程: ib-bot-mr

原理（回测重放法）：
  每根 1H Bar 收盘后，从 IB 拉取最新历史数据，运行完整回测，
  从 _CaptureMR 读取策略最终持仓状态，与 mr_state.json 对比后下单。

仓位大小（ATR 动态定仓）：
  风险金额 = 账户净值 × risk_pct%（固定 1.0%）
  止损金额 = ATR × sl_mult × 合约乘数（sl_mult=1.0，MES=$5/点）
  手数 = 风险金额 / 止损金额（最少 1 手）

每日限制：当日最多 1 笔入场（防止同日连续亏损）

用法：
  python mr_engine.py                  # 等 Bar 收盘触发
  python mr_engine.py --run-now        # 立即运行一次
  python mr_engine.py --run-now --dry-run  # 干跑，不下单
  python mr_engine.py --port 4001      # 实盘端口
"""

import argparse
import json
import logging
import os
import sys
import threading
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from backtesting import Backtest
from ib_insync import IB, Future, MarketOrder, util

# 将项目根目录加入 path（mr_engine.py 位于 es_mr/ 子目录）
_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "es_mr"))

from backtest_runner import load_from_csv
from es_mr.strategy_mr import MeanReversionStrategy

import warnings
warnings.filterwarnings("ignore")

# ── 路径 / 时区 ─────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_DIR    = _ROOT / "logs"
STATE_FILE = BASE_DIR / "mr_state.json"
LOG_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

MES_MULTIPLIER = 5   # $5/点
MES_EXCHANGE   = "CME"
BAR_CLOSE_DELAY = 15  # 收盘后等待秒数

# ── 日志 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"mr_{datetime.now().strftime('%Y%m%d')}.log"),
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
                "text": f"[MR] {msg}",
            }).encode()
            urllib.request.urlopen(
                urllib.request.Request(url, data=data, method="POST"), timeout=5
            )
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════
# 状态持久化
# ══════════════════════════════════════════════════════════════════════

def _default_state() -> dict:
    return {
        "signed_contracts": 0,
        "today_date":       None,   # 今日日期字符串，用于每日限制
        "today_trades":     0,      # 今日已入场次数
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            raw = json.load(f)
        # 补全缺失字段（向后兼容）
        for k, v in _default_state().items():
            raw.setdefault(k, v)
        return raw
    state = _default_state()
    save_state(state)
    return state


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _reset_daily_count_if_new_day(state: dict) -> dict:
    today_str = date.today().isoformat()
    if state.get("today_date") != today_str:
        state["today_date"]   = today_str
        state["today_trades"] = 0
    return state


# ══════════════════════════════════════════════════════════════════════
# IB 连接 / 账户
# ══════════════════════════════════════════════════════════════════════

def connect_ib(host: str, port: int, client_id: int = 21) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=30, readonly=False)
    log.info(f"IB 已连接: {host}:{port}  clientId={client_id}  账户: {ib.wrapper.accounts}")
    return ib


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
    """查询 MES 当前净持仓（所有合约月份合计）。"""
    ib.reqPositions()
    ib.sleep(1)
    total = 0
    for pos in ib.positions():
        if pos.contract.symbol == "MES":
            total += int(pos.position)
    return total


# ══════════════════════════════════════════════════════════════════════
# 次季合约（Deferred Contract）
# ══════════════════════════════════════════════════════════════════════

def get_deferred_mes_contract(ib: IB):
    """
    获取 MES 次季合约（跳过当期前月）。
    CME 季度月：3(H), 6(M), 9(U), 12(Z)
    逻辑：
      1. 找当期合约月（下一个未到期季度月）
      2. 再往后推一个季度 → 次季合约
    到期判断：MES 在季度月第三个周五到期，简化为当月日期 < 20 仍属当期。
    """
    today = date.today()
    year, month, day = today.year, today.month, today.day
    quarters = [3, 6, 9, 12]

    # 找当期合约月
    front_year, front_month = year, None
    for qm in quarters:
        if qm > month or (qm == month and day < 20):
            front_month = qm
            front_year  = year
            break
    if front_month is None:
        front_month = 3
        front_year  = year + 1

    # 次季 = 当期再往后一个季度
    qidx = quarters.index(front_month)
    if qidx < 3:
        def_month = quarters[qidx + 1]
        def_year  = front_year
    else:
        def_month = 3
        def_year  = front_year + 1

    contract_str = f"{def_year}{def_month:02d}"
    contract = Future("MES", lastTradeDateOrContractMonth=contract_str,
                      exchange=MES_EXCHANGE, currency="USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"无法 qualify MES {contract_str} 合约，请检查行情权限")
    c = qualified[0]
    log.info(f"MES 次季合约: {c.localSymbol}  到期: {c.lastTradeDateOrContractMonth}  "
             f"（当期: {front_year}{front_month:02d}，次季: {contract_str}）")
    return c


# ══════════════════════════════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════════════════════════════

def fetch_bars(ib: IB, contract, retries: int = 3) -> pd.DataFrame:
    """拉取 MES 1H 历史数据（约 60 天）。"""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="60 D",
                barSizeSetting="1 hour",
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            )
            if bars:
                break
            last_err = RuntimeError("IB 返回空数据")
        except Exception as e:
            last_err = e
        log.warning(f"  数据拉取失败 attempt {attempt}/{retries}: {last_err}")
        if attempt < retries:
            ib.sleep(5)
    else:
        raise RuntimeError(f"数据拉取失败，触发重连: {last_err}") from last_err

    df = util.df(bars).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low", "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)
    df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df.iloc[:-1]   # 去掉可能未收盘的最后一根

    log.info(f"  拉取 {len(df)} 根 1H Bar  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


# ══════════════════════════════════════════════════════════════════════
# ATR 动态定仓
# ══════════════════════════════════════════════════════════════════════

def calc_mr_n_contracts(equity: float, risk_pct: float, atr: float) -> int:
    """
    MR 引擎定仓公式（sl_mult=1.0，MES $5/点）：
    手数 = round(equity × risk_pct% / (ATR × 1.0 × 5))，最少 1 手。
    """
    risk_dollars = equity * risk_pct / 100
    stop_dollars = atr * 1.0 * MES_MULTIPLIER
    if stop_dollars <= 0:
        return 1
    n = max(1, round(risk_dollars / stop_dollars))
    return n


# ══════════════════════════════════════════════════════════════════════
# 回测重放（检测当前应持仓位）
# ══════════════════════════════════════════════════════════════════════

_mr_capture: dict = {"in_position": False, "position_size": 0}


class _CaptureMR(MeanReversionStrategy):
    """回测结束后，通过类变量暴露策略最后的持仓状态。"""
    def next(self):
        super().next()
        _mr_capture["in_position"]   = bool(self.position)
        _mr_capture["position_size"] = self.position.size if self.position else 0


def _set_mr_params(cfg: dict, n: int):
    _CaptureMR.bb_len        = int(cfg.get("bb_len",        20))
    _CaptureMR.bb_mult       = float(cfg.get("bb_mult",     2.0))
    _CaptureMR.rsi_len       = int(cfg.get("rsi_len",       14))
    _CaptureMR.rsi_os        = float(cfg.get("rsi_os",      28.0))
    _CaptureMR.atr_len       = int(cfg.get("atr_len",       14))
    _CaptureMR.vwap_atr_mult = float(cfg.get("vwap_atr_mult", 2.0))
    _CaptureMR.adx_len       = int(cfg.get("adx_len",       14))
    _CaptureMR.adx_threshold = float(cfg.get("adx_threshold", 25.0))
    _CaptureMR.min_score     = int(cfg.get("min_score",      3))
    _CaptureMR.sl_mult       = float(cfg.get("sl_mult",     1.0))
    _CaptureMR.tp_atr_mult   = float(cfg.get("tp_atr_mult", 2.0))
    _CaptureMR.max_bars      = int(cfg.get("max_bars",       8))
    _CaptureMR.n_contracts   = n


def run_backtest_replay(df: pd.DataFrame, cfg: dict, n: int) -> bool:
    """运行回测重放，返回当前策略是否应持仓（True=持仓，False=空仓）。"""
    _set_mr_params(cfg, n)
    bt = Backtest(
        df, _CaptureMR,
        cash=500_000,
        commission=float(cfg.get("commission", 0.00002)),
        margin=float(cfg.get("margin", 0.05)),
        exclusive_orders=True,
    )
    bt.run()
    return _mr_capture["in_position"]


# ══════════════════════════════════════════════════════════════════════
# 下单
# ══════════════════════════════════════════════════════════════════════

TRADES_CSV = LOG_DIR / "mr_trades.csv"


def _log_trade(action: str, qty: int, fill_price):
    try:
        write_header = not TRADES_CSV.exists()
        with open(TRADES_CSV, "a") as f:
            if write_header:
                f.write("time,instrument,tf,action,qty,price\n")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts},MES,1h,{action},{qty},{fill_price}\n")
    except Exception:
        pass


def place_order(ib: IB, contract, delta: int, dry_run: bool = False):
    if delta == 0:
        return
    action = "BUY" if delta > 0 else "SELL"
    qty    = abs(delta)

    if dry_run:
        log.info(f"  [DRY-RUN] {action} {qty} MES（不实际下单）")
        return

    order = MarketOrder(action, qty)
    trade = ib.placeOrder(contract, order)
    ib.sleep(3)
    fill_price = trade.orderStatus.avgFillPrice or "待成交"
    log.info(f"  ✅ {action} {qty} MES  |  状态: {trade.orderStatus.status}  成交均价: {fill_price}")
    tg_alert(f"✅ MR 下单: {action} {qty} MES @ {fill_price}")
    _log_trade(action, qty, fill_price)
    return trade


# ══════════════════════════════════════════════════════════════════════
# 核心处理逻辑
# ══════════════════════════════════════════════════════════════════════

def process(ib: IB, contract, cfg: dict, state: dict,
            equity: float, risk_pct: float, dry_run: bool = False):

    log.info(f"\n── ES MR / 1H 信号处理 {'─' * 40}")

    # 重置每日计数
    state = _reset_daily_count_if_new_day(state)

    # 1. 拉数据
    ib.sleep(10)
    try:
        df = fetch_bars(ib, contract)
    except Exception as e:
        log.error(f"  数据拉取失败: {e}")
        raise RuntimeError(f"数据拉取失败，触发重连: {e}") from e

    # 2. 计算最新 ATR（用于定仓）
    from es_mr.indicators_mr import compute_atr
    idx      = pd.DatetimeIndex(df.index)
    atr_s    = compute_atr(
        pd.Series(df["High"].values, index=idx),
        pd.Series(df["Low"].values,  index=idx),
        pd.Series(df["Close"].values, index=idx),
        int(cfg.get("atr_len", 14)),
    )
    last_atr = float(atr_s.iloc[-1])
    if last_atr != last_atr or last_atr <= 0:
        last_atr = float(df["Close"].iloc[-1]) * 0.005

    # 3. ATR 动态定仓
    n = calc_mr_n_contracts(equity, risk_pct, last_atr)
    stop_usd = last_atr * 1.0 * MES_MULTIPLIER
    log.info(f"  ATR={last_atr:.2f}pt  止损≈${stop_usd:.0f}/手  → {n} 手  "
             f"（风险 ${stop_usd * n:.0f} / ${equity:,.0f}  = "
             f"{stop_usd * n / equity * 100:.2f}%）")

    # 4. 回测重放，判断应持仓位
    try:
        should_be_long = run_backtest_replay(df, cfg, n)
    except Exception as e:
        log.error(f"  回测重放失败: {e}", exc_info=True)
        return

    new_signed = n if should_be_long else 0
    old_signed = state["signed_contracts"]
    delta      = new_signed - old_signed

    log.info(f"  策略应持: {'多头 ' + str(new_signed) + ' 手' if new_signed > 0 else '空仓'}  |  "
             f"当前状态: {'多头 ' + str(old_signed) + ' 手' if old_signed > 0 else '空仓'}  |  "
             f"差额: {delta:+d}")

    # 5. 每日 1 单限制（只限制新入场，平仓不受限）
    if delta > 0 and old_signed == 0:
        if state["today_trades"] >= 1:
            log.info(f"  ⚠ 今日已入场 {state['today_trades']} 次，跳过（每日最多 1 笔）")
            return

    # 6. 下单
    if delta != 0:
        place_order(ib, contract, delta, dry_run=dry_run)
        if not dry_run:
            state["signed_contracts"] = new_signed
            if delta > 0 and old_signed == 0:
                state["today_trades"] += 1
            save_state(state)
            log.info(f"  状态已保存: {new_signed:+d} 手")
    else:
        log.info("  仓位无变化，跳过")


# ══════════════════════════════════════════════════════════════════════
# Bar 收盘触发
# ══════════════════════════════════════════════════════════════════════

def is_bar_close(now_et: datetime) -> bool:
    return now_et.minute == 0 and now_et.second <= BAR_CLOSE_DELAY


def is_market_open(now_et: datetime) -> bool:
    wd = now_et.weekday()
    h  = now_et.hour
    if wd == 5:
        return False
    if wd == 6 and h < 18:
        return False
    if h == 17:
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ES MR 均值回归实盘引擎")
    parser.add_argument("--config",   default=str(BASE_DIR / "config_mr.yaml"))
    parser.add_argument("--port",     type=int, default=4001,
                        help="IB Gateway 端口（实盘=4001，模拟盘=4002）")
    parser.add_argument("--host",     default="127.0.0.1")
    parser.add_argument("--run-now",  action="store_true", help="立即运行一次")
    parser.add_argument("--dry-run",  action="store_true", help="只打印信号，不下单")
    args = parser.parse_args()

    with open(args.config) as f:
        cfg = yaml.safe_load(f)

    # Telegram
    _TG["token"]   = os.environ.get("TG_TOKEN",   "")
    _TG["chat_id"] = os.environ.get("TG_CHAT_ID", "")

    risk_pct = 1.0   # MR 引擎固定 1% 风险/笔，不使用金字塔（单 TF）

    log.info("═" * 62)
    log.info(f"  ES MR 引擎  |  风险/笔: {risk_pct}%  |  clientId: 21")
    log.info(f"  {'📄 模拟盘' if args.port == 4002 else '⚠️  实  盘'}  端口: {args.port}")
    if args.dry_run:
        log.info("  dry-run 模式：只打印信号，不下单")
    log.info("═" * 62)

    import time as _time
    ib       = IB()
    equity   = 0.0
    contract = None
    state    = load_state()

    def do_connect():
        nonlocal equity, contract
        _last_fail_alert = [0.0]
        while True:
            try:
                if ib.isConnected():
                    try: ib.disconnect()
                    except Exception: pass
                log.info(f"连接 IB Gateway {args.host}:{args.port}...")
                ib.connect(args.host, args.port, clientId=21, timeout=30, readonly=False)
                log.info(f"✅ IB 已连接  账户: {ib.wrapper.accounts}")

                equity   = get_account_equity(ib)
                contract = get_deferred_mes_contract(ib)

                # 持仓核对（以 IB 为准）
                ib_pos = get_ib_position(ib)
                st_pos = state["signed_contracts"]
                if ib_pos != st_pos:
                    log.warning(f"  ⚠ 持仓不一致：IB={ib_pos:+d}  状态文件={st_pos:+d}，以 IB 为准修正")
                    state["signed_contracts"] = ib_pos
                    save_state(state)
                    tg_alert(f"⚠ MR 持仓不一致，已修正 → {ib_pos:+d} 手")
                else:
                    log.info(f"  MES 持仓: {ib_pos:+d} 手  ✅")

                tg_alert(f"✅ MR 引擎已连接  净值: ${equity:,.0f}  合约: {contract.localSymbol}")
                return
            except Exception as exc:
                log.error(f"  连接失败: {exc}，10秒后重试")
                now = _time.time()
                if now - _last_fail_alert[0] > 3600:  # 每小时最多提醒一次
                    tg_alert(f"❌ MR 引擎连接失败，持续重试...\n{exc}")
                    _last_fail_alert[0] = now
                try: ib.disconnect()
                except Exception: pass
                _time.sleep(10)

    needs_reconnect = [False]
    _last_reconnect = [0.0]

    def on_error(reqId, errorCode, errorString, contract_):
        if errorCode in (1100, 1101, 2110):
            needs_reconnect[0] = True
            log.warning(f"⚠ Error {errorCode}: IB 连接异常，将触发重连")
            tg_alert(f"⚠ MR 引擎 Error {errorCode}，正在重连...")
        elif errorCode == 2105:
            now = _time.time()
            if now - _last_reconnect[0] > 60:
                _last_reconnect[0] = now
                needs_reconnect[0] = True
                log.warning("⚠ Error 2105: HMDS 断连，触发重连")

    ib.errorEvent += on_error
    do_connect()

    # ── 立即运行模式 ──────────────────────────────────────────────────
    if args.run_now:
        log.info("\n▶ --run-now: 立即处理 ES MR\n")
        process(ib, contract, cfg, state, equity, risk_pct, args.dry_run)
        log.info("\n✅ --run-now 完成")
        ib.disconnect()
        return

    # ── 定时主循环 ────────────────────────────────────────────────────
    log.info("\n▶ 等待 1H Bar 收盘触发...\n")
    while True:
        _time.sleep(1)
        now_et = datetime.now(ET)

        if needs_reconnect[0]:
            needs_reconnect[0] = False
            _time.sleep(5)
            do_connect()
            continue

        if not is_market_open(now_et):
            continue

        if is_bar_close(now_et):
            log.info(f"\n{'═' * 62}")
            log.info(f"  1H Bar 收盘触发  {now_et.strftime('%Y-%m-%d %H:%M ET')}")
            log.info(f"{'═' * 62}")
            _time.sleep(BAR_CLOSE_DELAY)
            try:
                equity = get_account_equity(ib)
                process(ib, contract, cfg, state, equity, risk_pct, args.dry_run)
            except RuntimeError as e:
                log.error(f"  ❌ 处理失败，触发重连: {e}")
                needs_reconnect[0] = True
            except Exception as e:
                log.error(f"  ❌ 未预期错误: {e}", exc_info=True)
                tg_alert(f"❌ MR 引擎异常: {e}")
                needs_reconnect[0] = True  # 任何未预期错误都触发重连
            else:
                # 每次bar处理成功后更新状态文件时间戳（供_mr_status()健康检查用）
                save_state(state)


if __name__ == "__main__":
    import time as _time
    while True:
        try:
            main()
        except KeyboardInterrupt:
            log.info("用户中断，退出")
            break
        except Exception as e:
            log.error(f"main() 崩溃: {e}，30s 后重启", exc_info=True)
            tg_alert(f"❌ MR 引擎崩溃，30s 后重启\n{e}")
            _time.sleep(30)
