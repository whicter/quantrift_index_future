"""
live_engine.py — NQ + ES 多周期模拟盘/实盘执行引擎

原理（回测重放法）：
  每根 Bar 收盘后，从 IB 拉取最新历史数据，运行完整回测，
  从 _CaptureStrategy 读取当前应持仓位，与状态文件对比后下单。

合约：
  NQ → MNQ（$2/点，CME）
  ES → MES（$5/点，CME）

仓位大小（动态计算）：
  每笔信号风险 = 账户净值 × risk_pct%
  手数 = 风险金额 / (ATR × 1.5 × 合约乘数)  # 止损距离用 ATR×1.5 估算仓位，实际止损为 utTS 动态线

用法：
  python live_engine.py                        # 模拟盘，NQ+ES，等 Bar 收盘
  python live_engine.py --instrument NQ        # 只跑 NQ
  python live_engine.py --tf 4h               # 只跑 4H 周期
  python live_engine.py --run-now --dry-run    # 立即测试，不下单
  python live_engine.py --port 7496            # 切换实盘（谨慎！）
"""

import argparse
import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml
from backtesting import Backtest
from ib_insync import IB, ContFuture, MarketOrder, util

from indicators import compute_signals
from strategy import ConfluenceStrategy

import warnings
warnings.filterwarnings("ignore")

# ── Telegram 告警（模块级，main() 初始化后填入 token/chat_id）────────────
_TG: dict = {"token": "", "chat_id": ""}


def tg_alert(msg: str):
    """发送 Telegram 告警（非阻塞，静默失败，不影响主逻辑）。"""
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
                "text": f"[QuantRift] {msg}",
            }).encode()
            req = urllib.request.Request(url, data=data, method="POST")
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    threading.Thread(target=_send, daemon=True).start()


def update_vix_csv(ib: IB):
    """更新 VIX 日线 CSV（data/VIX_1d.csv）。
    主链路：yfinance（免费）；失败时回退到 IB Index 数据。
    静默失败，不影响引擎运行。
    """
    _vix_old = BASE_DIR / "data" / "VIX_1d_2019-01-01_2026-06-09.csv"
    csv_src  = VIX_CSV if VIX_CSV.exists() else _vix_old
    try:
        vix_df = pd.read_csv(csv_src, index_col=0, parse_dates=True)
        vix_df.index = pd.to_datetime(vix_df.index).tz_localize(None).normalize()
        vix_df = vix_df[['Close']].copy()
    except Exception as e:
        log.warning(f"VIX CSV 读取失败: {e}")
        return

    last_date = vix_df.index.max()
    yesterday = pd.Timestamp.now().normalize() - pd.Timedelta(days=1)
    if last_date >= yesterday:
        log.info(f"VIX CSV 已是最新（{last_date.date()}），跳过更新")
        return

    start_str   = (last_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    days_needed = (pd.Timestamp.now().normalize() - last_date).days + 2
    new_data    = None

    # 1. yfinance（免费，首选）
    try:
        import yfinance as yf
        raw = yf.download("^VIX", start=start_str, progress=False, auto_adjust=False)
        if not raw.empty:
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw = raw[['Close']].copy()
            raw.index = pd.to_datetime(raw.index).tz_localize(None).normalize()
            new_data = raw
            log.info(f"VIX yfinance 更新成功，新增 {len(new_data)} 行，至 {new_data.index.max().date()}")
    except Exception as e:
        log.warning(f"yfinance 获取 VIX 失败（尝试 IB 备用）: {e}")

    # 2. IB 备用（需要 CBOE 订阅或延迟数据权限）
    if (new_data is None or new_data.empty) and ib is not None and ib.isConnected():
        try:
            from ib_insync import Contract as IBContract
            vix_contract = IBContract(symbol="VIX", secType="IND", exchange="CBOE", currency="USD")
            bars = ib.reqHistoricalData(
                vix_contract,
                endDateTime="",
                durationStr=f"{min(days_needed, 365)} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                keepUpToDate=False,
            )
            if bars:
                tmp = util.df(bars)[['date', 'close']].copy()
                tmp['date'] = pd.to_datetime(tmp['date']).dt.tz_localize(None).dt.normalize()
                tmp = tmp.set_index('date').rename(columns={'close': 'Close'})
                tmp = tmp[tmp.index > last_date]
                new_data = tmp
                log.info(f"VIX IB 备用成功，新增 {len(new_data)} 行，至 {new_data.index.max().date()}")
        except Exception as e:
            log.warning(f"IB 获取 VIX 失败: {e}  VIX 数据维持旧版（至 {last_date.date()}）")

    # 合并并写入统一 CSV
    if new_data is not None and not new_data.empty:
        combined = pd.concat([vix_df, new_data])
        combined = combined[~combined.index.duplicated(keep='last')].sort_index()
        combined.to_csv(VIX_CSV)
        log.info(f"VIX CSV 已写入 {VIX_CSV.name}（共 {len(combined)} 行）")


def _mr_status() -> str:
    """读取 ib-bot-mr 的状态文件，返回单行状态描述。"""
    import subprocess
    try:
        # 检查进程是否存活
        r = subprocess.run(["/usr/bin/pgrep", "-f", "mr_engine.py"], capture_output=True)
        alive = r.returncode == 0

        # 读状态文件
        if MR_STATE_FILE.exists():
            with open(MR_STATE_FILE) as f:
                mr = json.load(f)
            pos = mr.get("signed_contracts", 0)
            pos_str = f"{pos:+d}手" if pos != 0 else "空仓"
            import time as _t
            stale = (_t.time() - MR_STATE_FILE.stat().st_mtime) > 7200  # >2h 未更新
            # 进程存活但状态文件 >2h 未更新 = 连接可能断了
            if alive and stale:
                status = "⚠️ 连接可能断开（状态文件>2h未更新）"
            elif alive:
                status = "✅"
            else:
                status = "❌ 进程不存在"
            return f"ib-bot-mr（ES MR）{status}  MESZ6 {pos_str}"
        else:
            return f"ib-bot-mr（ES MR）{'✅' if alive else '❌ 未运行'}  状态文件不存在"
    except Exception as e:
        log.error(f"_mr_status() 异常: {e}", exc_info=True)
        return f"ib-bot-mr（ES MR）⚠️ 状态读取失败: {e}"


# ── 路径 / 时区 ────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
LOG_DIR       = BASE_DIR / "logs"
STATE_FILE    = BASE_DIR / "live_state.json"
MR_STATE_FILE = BASE_DIR / "es_mr" / "mr_state.json"
VIX_CSV       = BASE_DIR / "data" / "VIX_1d.csv"
LOG_DIR.mkdir(exist_ok=True)
ET = ZoneInfo("America/New_York")

# ── 日志 ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"live_{datetime.now().strftime('%Y%m%d')}.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── 合约配置 ──────────────────────────────────────────────────────────
INSTRUMENTS = {
    "NQ": {"micro": "MNQ", "exchange": "CME", "multiplier": 2},
    "ES": {"micro": "MES", "exchange": "CME", "multiplier": 5},
}

BAR_SIZE_MAP = {"1h": "1 hour", "4h": "4 hours", "1d": "1 day"}
DURATION_MAP = {"1h": "60 D",   "4h": "120 D",   "1d": "1 Y"}
BAR_CLOSE_DELAY = 15  # 收盘后等待秒数


# ══════════════════════════════════════════════════════════════════════
# 状态持久化
# ══════════════════════════════════════════════════════════════════════

def _empty_inst_state():
    return {tf: {"signed_contracts": 0} for tf in ["1h", "4h", "1d"]}


def load_state(active_instruments: list) -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            raw = json.load(f)
        # 旧格式迁移：{"1h": {...}, "4h": {...}} → {"NQ": {...}, "ES": {...}}
        if raw and list(raw.keys())[0] in ["1h", "4h", "1d"]:
            log.info("状态文件格式升级（单品种 → 多品种）")
            raw = {"NQ": raw}
        # 补全缺失品种
        for inst in active_instruments:
            raw.setdefault(inst, _empty_inst_state())
        # 补全参考净值字段
        raw.setdefault("reference_equity", None)
        log.info(f"加载状态文件: {STATE_FILE}")
        return raw

    default = {inst: _empty_inst_state() for inst in active_instruments}
    default["reference_equity"] = None
    save_state(default)
    return default


def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _is_all_flat(state: dict, active_instruments: list) -> bool:
    """检查是否所有品种所有周期均无仓位。"""
    for inst in active_instruments:
        for tf in ["1h", "4h", "1d"]:
            if state.get(inst, {}).get(tf, {}).get("signed_contracts", 0) != 0:
                return False
    return True


def _refresh_equity(ib: IB, state: dict, active_instruments: list,
                    current_equity: float) -> float:
    """空仓时从 IB 查询净值并锁定；持仓中使用上次锁定的净值。"""
    if _is_all_flat(state, active_instruments):
        new_equity = get_account_equity(ib)
        state["reference_equity"] = new_equity
        save_state(state)
        log.info(f"  空仓，参考净值已更新: ${new_equity:,.2f}")
        return new_equity
    ref = state.get("reference_equity")
    if ref:
        log.info(f"  持仓中，使用锁定净值: ${float(ref):,.2f}")
        return float(ref)
    return current_equity


# ══════════════════════════════════════════════════════════════════════
# IB 连接 / 账户
# ══════════════════════════════════════════════════════════════════════

def connect_ib(host: str, port: int, client_id: int = 20) -> IB:
    ib = IB()
    ib.connect(host, port, clientId=client_id, timeout=30, readonly=False)
    log.info(f"IB 已连接: {host}:{port}  clientId={client_id}  "
             f"账户: {ib.wrapper.accounts}")
    return ib


def get_account_equity(ib: IB) -> float:
    """获取账户净值（Net Liquidation Value）。"""
    ib.reqAccountSummary()
    ib.sleep(2)
    for v in ib.accountValues():
        if v.tag == "NetLiquidation" and v.currency == "USD":
            eq = float(v.value)
            log.info(f"账户净值: ${eq:,.2f}")
            return eq
    log.warning("无法获取账户净值，使用默认 $25,000")
    return 25_000.0


def reconcile_state(state: dict, inst: str, ib_pos: int) -> bool:
    """以 IB 实际净仓为准，修正状态文件中该品种的仓位分布。
    返回 True 表示做了修正。"""
    tf_list = ["1h", "4h", "1d"]
    st_pos = sum(state[inst][tf]["signed_contracts"] for tf in tf_list)
    diff = ib_pos - st_pos
    if diff == 0:
        return False

    if ib_pos == 0:
        # IB 已空仓 → 全清，最安全的操作
        for tf in tf_list:
            state[inst][tf]["signed_contracts"] = 0
    else:
        # 找绝对值最大的 TF 吸收差额（该 TF 最可能是发生变化的那个）
        largest_tf = max(tf_list, key=lambda tf: abs(state[inst][tf]["signed_contracts"]))
        state[inst][largest_tf]["signed_contracts"] += diff
    return True


def get_contract(ib: IB, instrument: str):
    """获取指定品种的前月连续合约并 qualify。"""
    cfg = INSTRUMENTS[instrument]
    contract = ContFuture(cfg["micro"], exchange=cfg["exchange"], currency="USD")
    qualified = ib.qualifyContracts(contract)
    if not qualified:
        raise RuntimeError(f"无法 qualify {cfg['micro']} 合约，请检查 TWS 行情权限")
    c = qualified[0]
    log.info(f"{instrument}({cfg['micro']}) 合约: {c.localSymbol}  "
             f"到期: {c.lastTradeDateOrContractMonth}")
    return c


def get_ib_position(ib: IB, instrument: str) -> int:
    """查询指定品种的当前净持仓（有符号手数）。"""
    micro = INSTRUMENTS[instrument]["micro"]
    ib.reqPositions()
    ib.sleep(1)
    for pos in ib.positions():
        if pos.contract.symbol == micro:
            return int(pos.position)
    return 0


# ══════════════════════════════════════════════════════════════════════
# 动态仓位计算
# ══════════════════════════════════════════════════════════════════════

def calc_n_contracts(equity: float, risk_pct: float,
                     atr: float, sl_mult: float, multiplier: int) -> int:
    """
    根据账户净值和当前 ATR 动态计算合约数。
    风险金额 = equity × risk_pct / 100
    止损金额 = ATR × sl_mult × 合约乘数
    手数     = 风险金额 / 止损金额（最少 1 手）
    """
    risk_dollars = equity * risk_pct / 100
    stop_dollars = atr * sl_mult * multiplier
    if stop_dollars <= 0:
        return 1
    return max(1, round(risk_dollars / stop_dollars))


# ══════════════════════════════════════════════════════════════════════
# 数据获取
# ══════════════════════════════════════════════════════════════════════

def fetch_bars(ib: IB, contract, tf: str, retries: int = 3) -> pd.DataFrame:
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr=DURATION_MAP[tf],
                barSizeSetting=BAR_SIZE_MAP[tf],
                whatToShow="TRADES",
                useRTH=False,
                formatDate=1,
                keepUpToDate=False,
            )
            if bars:
                break
            last_err = RuntimeError(f"IB 返回空数据 (tf={tf})")
        except Exception as e:
            last_err = e
        log.warning(f"  数据拉取失败 attempt {attempt}/{retries}: {last_err}")
        if attempt < retries:
            ib.sleep(5)
    else:
        raise RuntimeError(f"数据拉取失败，触发重连: {last_err}") from last_err

    df = util.df(bars).rename(columns={
        "date": "Date", "open": "Open", "high": "High",
        "low": "Low",  "close": "Close", "volume": "Volume",
    })
    df["Date"] = pd.to_datetime(df["Date"])
    if df["Date"].dt.tz is not None:
        df["Date"] = df["Date"].dt.tz_localize(None)
    df = df.set_index("Date")[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df = df.iloc[:-1]  # 去掉可能未收盘的最后一根

    log.info(f"  拉取 {len(df)} 根 Bar  ({df.index[0].date()} ~ {df.index[-1].date()})")
    return df


# ══════════════════════════════════════════════════════════════════════
# 回测重放
# ══════════════════════════════════════════════════════════════════════

_capture: dict = {"stage": 0, "dir": 0, "entry_price": 0.0, "entry_atr": 0.0}


class _CaptureStrategy(ConfluenceStrategy):
    """回测结束后，通过类变量暴露策略最后的持仓状态。"""
    def next(self):
        super().next()
        _capture["stage"]       = self._stage
        _capture["dir"]         = self._entry_dir
        _capture["entry_price"] = self._entry_price
        _capture["entry_atr"]   = self._entry_atr


def _set_strategy_params(params: dict, n_contracts: int, multiplier: int):
    _CaptureStrategy.min_score           = int(params["min_score"])
    _CaptureStrategy.adx_threshold       = float(params.get("adx_threshold", 20.0))
    _CaptureStrategy.use_adx             = bool(params.get("use_adx", True))
    _CaptureStrategy.vol_mult            = float(params.get("vol_mult", 1.0))
    _CaptureStrategy.use_vol             = bool(params.get("use_vol", True))
    _CaptureStrategy.allow_short         = bool(params.get("allow_short", True))
    _CaptureStrategy.reversal_score      = int(params.get("reversal_score", 2))
    _CaptureStrategy.allow_reversal_flip = bool(params.get("allow_reversal_flip", True))
    _CaptureStrategy.conflict_threshold  = int(params.get("conflict_threshold", 2))
    _CaptureStrategy.use_bbmc_dir        = bool(params.get("use_bbmc_dir", True))
    _CaptureStrategy.use_squeeze_mr      = bool(params.get("use_squeeze_mr", False))
    _CaptureStrategy.rsi_mr_ob           = float(params.get("rsi_mr_ob", 65.0))
    _CaptureStrategy.rsi_mr_os           = float(params.get("rsi_mr_os", 35.0))
    # use_atr_exit / atr_sl_mult 已废弃：staged_tp=True 时止损用 utTS，以下两行无效
    # _CaptureStrategy.use_atr_exit        = bool(params.get("use_atr_exit", False))
    # _CaptureStrategy.atr_sl_mult         = float(params.get("atr_sl_mult", 1.5))
    _CaptureStrategy.use_trend_filter    = bool(params.get("use_trend_filter", False))
    _CaptureStrategy.n_contracts         = n_contracts
    _CaptureStrategy.contract_size       = multiplier
    _CaptureStrategy.use_staged_tp       = bool(params.get("use_staged_tp", True))
    _CaptureStrategy.atr_tp1_mult        = float(params.get("atr_tp1_mult", 1.0))
    _CaptureStrategy.atr_tp2_mult        = float(params.get("atr_tp2_mult", 2.0))
    _CaptureStrategy.tp1_portion         = float(params.get("tp1_portion", 0.34))


def _stage_to_contracts(stage: int, n_contracts: int, tp1_portion: float) -> int:
    if stage == 0:
        return 0
    elif stage == 1:
        return n_contracts
    elif stage == 2:
        return max(1, round(n_contracts * (1 - tp1_portion)))
    else:  # stage == 3
        return max(1, round(n_contracts * (1 - tp1_portion) * 0.5))


# ══════════════════════════════════════════════════════════════════════
# 下单
# ══════════════════════════════════════════════════════════════════════

TRADES_CSV = LOG_DIR / "trades.csv"


def _log_trade(action: str, qty: int, sym: str, fill_price, instrument: str, tf: str):
    """追加一行到 trades.csv，静默失败，不影响交易逻辑。"""
    try:
        write_header = not TRADES_CSV.exists()
        with open(TRADES_CSV, "a") as f:
            if write_header:
                f.write("time,instrument,tf,action,qty,price\n")
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts},{instrument},{tf},{action},{qty},{fill_price}\n")
    except Exception:
        pass


def place_order(ib: IB, contract, delta: int, dry_run: bool = False,
                instrument: str = "", tf: str = ""):
    if delta == 0:
        return
    action = "BUY" if delta > 0 else "SELL"
    qty    = abs(delta)
    sym    = contract.symbol

    if dry_run:
        log.info(f"  [DRY-RUN] {action} {qty} {sym}（不实际下单）")
        return

    order = MarketOrder(action, qty)
    trade = ib.placeOrder(contract, order)
    ib.sleep(3)
    fill_price = trade.orderStatus.avgFillPrice or "待成交"
    log.info(f"  ✅ {action} {qty} {sym}  |  "
             f"状态: {trade.orderStatus.status}  "
             f"成交均价: {fill_price}")
    tg_alert(f"✅ 下单成功: {action} {qty} {sym} @ {fill_price}")
    _log_trade(action, qty, sym, fill_price, instrument, tf)
    return trade


# ══════════════════════════════════════════════════════════════════════
# 单周期处理：拉数据 → 计算 ATR → 定仓 → 回测 → 对比 → 下单
# ══════════════════════════════════════════════════════════════════════

def process_tf(ib: IB, contract, instrument: str, tf: str,
               tf_params: dict, state: dict,
               equity: float, risk_pct: float, dry_run: bool = False):

    multiplier = INSTRUMENTS[instrument]["multiplier"]
    inst_state = state[instrument]

    log.info(f"\n── {instrument}/{tf.upper()} 信号处理 {'─' * 36}")

    # 1. 拉取数据（先等 10s 避免 IB pacing 限速）
    ib.sleep(10)
    try:
        df = fetch_bars(ib, contract, tf)
    except Exception as e:
        log.error(f"  [{instrument}/{tf}] 数据拉取失败: {e}")
        raise RuntimeError(f"数据拉取失败，触发重连: {e}") from e

    # 2. 计算指标，读取当前 ATR
    params = deepcopy(tf_params)
    try:
        df_sig = compute_signals(df, params)
    except Exception as e:
        log.error(f"  [{instrument}/{tf}] 指标计算失败: {e}", exc_info=True)
        return

    last_atr = float(df_sig["atrVal"].iloc[-1]) if "atrVal" in df_sig.columns else df["Close"].iloc[-1] * 0.01
    if last_atr != last_atr or last_atr <= 0:
        last_atr = df["Close"].iloc[-1] * 0.01

    # 3. 动态计算合约数
    sl_mult = float(params.get("atr_sl_mult", 1.5))
    n = calc_n_contracts(equity, risk_pct, last_atr, sl_mult, multiplier)
    stop_usd = last_atr * sl_mult * multiplier
    log.info(f"  ATR={last_atr:.1f}pt  止损≈{last_atr*sl_mult:.0f}pt  "
             f"${stop_usd:.0f}/手  → {n} 手（风险 ${stop_usd*n:.0f}）")

    # 4. 回测重放，读取信号
    params["n_contracts"]   = n
    params["contract_size"] = multiplier
    _set_strategy_params(params, n, multiplier)

    try:
        bt = Backtest(
            df_sig, _CaptureStrategy,
            cash=500_000,
            commission=float(params.get("commission", 0.00002)),
            margin=float(params.get("margin", 0.05)),
            exclusive_orders=True,
        )
        bt.run()
    except Exception as e:
        log.error(f"  [{instrument}/{tf}] 回测重放失败: {e}", exc_info=True)
        return

    stage = _capture["stage"]
    d     = _capture["dir"]

    if d == 0 or stage == 0:
        new_signed = 0
    else:
        tp1_portion = float(params.get("tp1_portion", 0.34))
        new_signed  = d * _stage_to_contracts(stage, n, tp1_portion)

    # 4.5 幻象信号过滤：当状态文件空仓时，必须当前bar指标也满足入场条件
    old_signed_pre = inst_state[tf]['signed_contracts']
    if old_signed_pre == 0 and new_signed != 0:
        lb       = df_sig.iloc[-1]
        min_sc   = int(params.get('min_score', 4))
        conflict = int(params.get('conflict_threshold', 6))
        adx_thr  = float(params.get('adx_threshold', 20.0))
        ok_adx   = (not params.get('use_adx', True)) or (float(lb.get('adx', 0)) >= adx_thr)
        ok_vol   = (not params.get('use_vol', True)) or bool(lb.get('isHighVol', False))
        use_bbmc = bool(params.get('use_bbmc_dir', False))
        if new_signed > 0:
            valid = (lb['bullScore'] >= min_sc and lb['bearScore'] <= conflict
                     and not bool(lb['isChoppy']) and ok_adx and ok_vol
                     and (not use_bbmc or lb['bbmcDir'] >= 0))
        else:
            valid = (lb['bearScore'] >= min_sc and lb['bullScore'] <= conflict
                     and not bool(lb['isChoppy']) and ok_adx and ok_vol
                     and (not use_bbmc or lb['bbmcDir'] <= 0))
        if not valid:
            log.warning(f'  ⚠ 回测末态与当前bar指标不符，忽略（幻象信号）')
            log.info(f'    bull={lb["bullScore"]:.0f} bear={lb["bearScore"]:.0f} '
                     f'choppy={bool(lb["isChoppy"])} adx={float(lb.get("adx",0)):.1f} '
                     f'bbmcDir={lb["bbmcDir"]:.0f} → 需要 bull>={min_sc}, bear<={conflict}')
            new_signed = 0

    # 5. 对比状态，计算差额
    old_signed = inst_state[tf]['signed_contracts']
    delta      = new_signed - old_signed

    def dir_str(s):
        if s > 0:  return f"多头 {abs(s)} 手"
        if s < 0:  return f"空头 {abs(s)} 手"
        return "空仓"

    log.info(f"  策略: {dir_str(new_signed)}  |  "
             f"上次: {dir_str(old_signed)}  |  差额: {delta:+d}")

    # 6. 下单
    if delta != 0:
        place_order(ib, contract, delta, dry_run=dry_run, instrument=instrument, tf=tf)
        if not dry_run:
            inst_state[tf]["signed_contracts"] = new_signed
            inst_state[tf]["last_update"]      = datetime.now(ET).isoformat()
            save_state(state)
            log.info(f"  [{instrument}/{tf}] 状态已保存: {dir_str(new_signed)}")
    else:
        log.info(f"  [{instrument}/{tf}] 仓位无变化，跳过")


# ══════════════════════════════════════════════════════════════════════
# Bar 收盘触发检测
# ══════════════════════════════════════════════════════════════════════

def is_bar_close(tf: str, now_et: datetime) -> bool:
    h, m, s = now_et.hour, now_et.minute, now_et.second
    if tf == "1h":
        return m == 0 and s <= BAR_CLOSE_DELAY
    elif tf == "4h":
        return h % 4 == 0 and m == 0 and s <= BAR_CLOSE_DELAY
    elif tf == "1d":
        return h == 16 and m == 0 and s <= BAR_CLOSE_DELAY
    return False


def is_market_open(now_et: datetime) -> bool:
    """排除周末和 CME 每日维护期（17:00-18:00 ET）。"""
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
    parser = argparse.ArgumentParser(description="NQ+ES 多周期实盘/模拟盘执行引擎")
    parser.add_argument("--config",     default=str(BASE_DIR / "config.yaml"))
    parser.add_argument("--instrument", default=None, help="只跑指定品种 NQ/ES")
    parser.add_argument("--tf",         default=None, help="只跑指定周期 1h/4h/1d")
    parser.add_argument("--port",       type=int, default=7497,
                        help="IB TWS 端口（模拟盘=7497，实盘=7496）")
    parser.add_argument("--host",       default="127.0.0.1")
    parser.add_argument("--run-now",    action="store_true",
                        help="立即运行一次（测试用）")
    parser.add_argument("--dry-run",    action="store_true",
                        help="只打印信号，不实际下单")
    parser.add_argument("--client-id",  type=int, default=20,
                        help="IB clientId（干跑用不同ID避免踢掉主bot，如 99）")
    args = parser.parse_args()

    # ── 加载配置 ────────────────────────────────────────────────────
    with open(args.config) as f:
        config = yaml.safe_load(f)

    # ── Telegram 告警配置 ──────────────────────────────────────────────
    tg_cfg = config.get("telegram", {})
    _TG["token"]   = tg_cfg.get("token")   or os.environ.get("TG_TOKEN",   "")
    _TG["chat_id"] = tg_cfg.get("chat_id") or os.environ.get("TG_CHAT_ID", "")
    if _TG["token"]:
        log.info("  Telegram 告警: 已启用")
    else:
        log.info("  Telegram 告警: 未配置（config.yaml telegram.token 或 TG_TOKEN 环境变量）")

    allow_short      = config.get("allow_short", True)
    risk_pct         = float(config.get("risk_pct", 1.0))
    equity_threshold = float(config.get("equity_threshold", 60000))

    # 各品种的 TF 参数来源
    inst_tf_configs = {
        "NQ": config.get("timeframes", {}),
        "ES": config.get("es_timeframes", {}),
    }

    # 确定要运行的品种（只跑有配置的）
    if args.instrument:
        active_instruments = [args.instrument]
    else:
        active_instruments = [i for i in INSTRUMENTS if inst_tf_configs.get(i)]

    log.info("═" * 62)
    log.info(f"  多品种实盘引擎  |  品种: {active_instruments}")
    log.info(f"  风险/笔: {risk_pct}%  |  净值阈值: ${equity_threshold:,.0f}  |  "
             f"{'📄 模拟盘' if args.port == 7497 else '⚠️  实  盘'}")
    if args.dry_run:
        log.info("  dry-run 模式：只打印信号，不下单")
    log.info("═" * 62)

    # ── 初始化 ───────────────────────────────────────────────────────
    import time as _time
    ib        = IB()
    equity    = 0.0
    state     = load_state(active_instruments)
    contracts = {}
    _last_gw_restart = [0.0]   # Gateway 上次重启时间（防止频繁重启）
    _got_err_326    = [False]  # Error 326 (clientId冲突) 标志，不计入重启计数
    _err_326_count  = [0]      # 连续 326 次数，超 30 次发 Telegram 告警

    def _restart_gateway():
        """连续连接失败后，kill Gateway 并通过 IBC 重启（自动登录）。"""
        import subprocess
        now = _time.time()
        if now - _last_gw_restart[0] < 7200:   # 2小时内不重复重启
            log.warning(f"  Gateway 重启冷却中（距上次 {now - _last_gw_restart[0]:.0f}s），跳过")
            return
        _last_gw_restart[0] = now
        script = BASE_DIR / "restart_gateway.sh"
        log.warning("  kill Gateway + IBC，等待 IBC 重新自动登录...")
        tg_alert("⚠️ 连续连接失败，正在重启 IB Gateway（IBC 自动登录）...")
        try:
            with open("/tmp/ibc_restart.log", "a") as lf:
                subprocess.Popen(["/bin/bash", str(script)], stdout=lf, stderr=subprocess.STDOUT)
        except Exception as e:
            log.error(f"  Gateway 重启失败: {e}")

    def do_connect():
        """带重试的连接，连上后同步持仓/净值/合约"""
        nonlocal equity, contracts
        _conn_fail_alerted = [False]
        _fail_count = 0
        while True:
            try:
                if ib.isConnected():
                    try: ib.disconnect()
                    except Exception: pass
                log.info(f"连接 IB Gateway {args.host}:{args.port}...")
                ib.connect(args.host, args.port, clientId=args.client_id, timeout=30, readonly=False)
                log.info(f"✅ IB 已连接  账户: {ib.wrapper.accounts}")
                equity = get_account_equity(ib)
                contracts.update({inst: get_contract(ib, inst) for inst in active_instruments})

                # 检查并修正持仓一致性（以 IB 为准）
                mismatch_lines = []
                for inst in active_instruments:
                    ib_pos = get_ib_position(ib, inst)
                    st_pos = sum(state[inst][tf]["signed_contracts"] for tf in ["1h","4h","1d"])
                    if ib_pos != st_pos:
                        fixed = reconcile_state(state, inst, ib_pos)
                        if fixed:
                            save_state(state)
                            new_dist = {tf: state[inst][tf]["signed_contracts"] for tf in ["1h","4h","1d"]}
                            msg = (f"{inst} 持仓已强制对齐 IB 实际 {ib_pos:+d}手"
                                   f"（原状态文件 {st_pos:+d}手，新分配: {new_dist}）")
                            log.warning(f"  ⚠️ {msg}")
                            mismatch_lines.append(msg)
                    else:
                        log.info(f"  {inst} IB持仓: {ib_pos:+d}手  状态文件: {st_pos:+d}手  ✅")

                # 拉取未平仓单（日志记录）
                try:
                    open_orders = ib.reqAllOpenOrders()
                    ib.sleep(1)
                    if open_orders:
                        log.warning(f"  ⚠️ 发现 {len(open_orders)} 笔未平仓单，请手动核查！")
                        for o in open_orders:
                            log.warning(f"    {o.contract.symbol} {o.order.action} "
                                        f"{o.order.totalQuantity} {o.orderStatus.status}")
                    else:
                        log.info("  未平仓单: 无")
                except Exception as e:
                    log.warning(f"  拉取未平仓单失败: {e}")

                # 更新 VIX CSV（yfinance 优先，IB 备用）
                try:
                    update_vix_csv(ib)
                except Exception as e:
                    log.warning(f"VIX 更新失败（非致命）: {e}")

                # Telegram 通知
                alert_parts = [f"✅ ib-bot（趋势 NQ+ES）已连接  账户净值: ${equity:,.0f}"]
                if mismatch_lines:
                    alert_parts.append("⚠️ 持仓不一致，请手动核查 live_state.json！")
                    alert_parts.extend(mismatch_lines)
                alert_parts.append(_mr_status())
                tg_alert("\n".join(alert_parts))
                return
            except Exception as exc:
                if _got_err_326[0]:
                    # Error 326 = clientId 已被占用，Gateway 本身正常，不计入重启计数
                    _got_err_326[0] = False
                    _err_326_count[0] += 1
                    log.error(f"  clientId 冲突（Error 326，第{_err_326_count[0]}次），Gateway正常，10秒后重试")
                    if _err_326_count[0] == 30:
                        import os as _os, subprocess as _sp
                        my_pid = _os.getpid()
                        result = _sp.run(
                            f"pgrep -f 'live_engine.py' | grep -v {my_pid} | xargs kill -9",
                            shell=True, capture_output=True)
                        log.warning(f"⚠️ clientId 冲突持续5分钟，已自动 kill 冲突进程（自身PID={my_pid}）")
                        tg_alert("⚠️ clientId 20 被占用持续5分钟，已自动 kill 冲突进程，继续重连...")
                    try: ib.disconnect()
                    except Exception: pass
                    _time.sleep(10)
                    continue
                _err_326_count[0] = 0  # 成功连接或其他错误时重置
                _fail_count += 1
                log.error(f"  连接失败 ({_fail_count}次): {exc}，10秒后重试")
                if not _conn_fail_alerted[0]:
                    tg_alert(f"❌ IB Gateway 连接失败，持续重试中...\n{exc}")
                    _conn_fail_alerted[0] = True
                try: ib.disconnect()
                except Exception: pass
                # 失败超过30次（5分钟）才重启 Gateway
                if _fail_count % 30 == 0:
                    log.warning(f"⚠️ 已连续失败 {_fail_count} 次（约{_fail_count//6}分钟），重启 Gateway...")
                    _restart_gateway()
                    _time.sleep(90)   # 等 Gateway 启动 + IBC 自动登录
                else:
                    _time.sleep(10)

    # Error 1100/1101/2105 → 触发重连；2110 只记日志不重连
    needs_reconnect    = [False]
    _last_reconnect_time = [0.0]
    _last_tg_error_time  = [0.0]   # Telegram 告警冷却（5分钟）
    def on_error(reqId, errorCode, errorString, contract):
        import time as _t
        if errorCode in (1100, 1101):
            needs_reconnect[0] = True
            log.warning(f"⚠️  Error {errorCode}: IB连接异常，将触发重连")
            # Telegram 5分钟冷却，避免反复刷屏
            if _t.time() - _last_tg_error_time[0] > 300:
                _last_tg_error_time[0] = _t.time()
                tg_alert(f"⚠️ IB 连接异常 (Error {errorCode})，正在重连...")
        elif errorCode == 326:
            _got_err_326[0] = True
        elif errorCode == 2110:
            # TWS→IBKR 链路中断，会自动恢复，不主动重连
            # （重连后立刻收到 2110 → 再重连 → 死循环，故移除）
            log.warning("⚠️  Error 2110: TWS→IBKR 断连，等待自动恢复（不主动重连）")
        elif errorCode == 2105:
            # HMDS ushmds 断连 → 只记日志，不主动重连
            # 原因：Gateway 与 IBKR 断连期间 2105 必然出现，此时 TCP 连接仍在；
            # fetch_bars 有 3 次重试 + RuntimeError 机制，会在真正需要时触发重连
            log.warning("⚠️  Error 2105: HMDS ushmds 断连（等待自动恢复）")
    ib.errorEvent += on_error

    do_connect()

    def build_params(inst, tf):
        p = inst_tf_configs[inst][tf].copy()
        p.setdefault("allow_short", allow_short)
        return p

    def _get_tf_risk_pct(params: dict, eq: float) -> float:
        """根据净值阈值选择该TF的风险比例（统一 or 金字塔）。"""
        if eq >= equity_threshold and "risk_pct_pyramid" in params:
            return float(params["risk_pct_pyramid"])
        return risk_pct

    def run_all():
        nonlocal equity
        equity = _refresh_equity(ib, state, active_instruments, equity)
        use_pyramid = equity >= equity_threshold
        log.info(f"  净值基准: ${equity:,.2f}  "
                 f"{'【金字塔模式】' if use_pyramid else '【统一仓位模式 < $' + f'{equity_threshold:,.0f}】'}")

        for inst in active_instruments:
            tfs = inst_tf_configs[inst]
            active_tfs = [args.tf] if args.tf else list(tfs.keys())
            active_tfs = [tf for tf in active_tfs if tf in tfs]
            for tf in active_tfs:
                params = build_params(inst, tf)
                process_tf(ib, contracts[inst], inst, tf,
                           params, state,
                           equity, _get_tf_risk_pct(params, equity), args.dry_run)

    # ── 立即运行模式 ─────────────────────────────────────────────────
    if args.run_now:
        log.info("\n▶ --run-now: 立即处理所有品种和周期\n")
        run_all()
        log.info("\n✅ --run-now 完成")
        ib.disconnect()
        return

    def send_daily_recap():
        """每周五收市（14:05 ET）调用 weekly_review.py 发送完整复盘报告。"""
        import subprocess as _sp, sys as _sys
        review_script = Path(__file__).parent / "weekly_review.py"
        tg_token   = _TG.get("token", "")
        tg_chat_id = _TG.get("chat_id", "")
        env = {**__import__("os").environ,
               "TG_TOKEN": tg_token, "TG_CHAT_ID": tg_chat_id}
        _sp.Popen(
            [_sys.executable, str(review_script), f"--port={args.port}", "--send"],
            env=env,
        )

    # ── 定时主循环 ───────────────────────────────────────────────────
    log.info("\n▶ 等待 Bar 收盘触发...\n")
    triggered: set = set()

    all_tfs = set()
    for inst in active_instruments:
        all_tfs |= set(inst_tf_configs[inst].keys())
    if args.tf:
        all_tfs = {args.tf}

    try:
        while True:
            try:
                ib.sleep(10)

                if needs_reconnect[0] or not ib.isConnected():
                    needs_reconnect[0] = False
                    log.warning("⚠️  连接断开，重连中...")
                    try: ib.disconnect()
                    except Exception: pass
                    _time.sleep(10)
                    do_connect()
                    continue

                now_et = datetime.now(ET)
                if not is_market_open(now_et):
                    continue

                # 每周复盘（周五 14:05 ET 收市时）
                if now_et.weekday() == 4 and now_et.hour == 14 and now_et.minute == 5 and now_et.second <= 15:
                    recap_key = f"recap-{now_et.strftime('%Y%m%d')}"
                    if recap_key not in triggered:
                        triggered.add(recap_key)
                        send_daily_recap()

                for tf in all_tfs:
                    if not is_bar_close(tf, now_et):
                        continue
                    key = f"{tf}-{now_et.strftime('%Y%m%d-%H-%M')}"
                    if key in triggered:
                        continue
                    triggered.add(key)
                    log.info(f"\n🕐 {now_et.strftime('%Y-%m-%d %H:%M:%S ET')}  "
                             f"{tf.upper()} Bar 收盘触发")
                    log.info(f"  等待 {BAR_CLOSE_DELAY}s，确保 IB 数据更新完毕...")
                    ib.sleep(BAR_CLOSE_DELAY)

                    equity = _refresh_equity(ib, state, active_instruments, equity)
                    use_pyramid = equity >= equity_threshold
                    log.info(f"  净值基准: ${equity:,.2f}  "
                             f"{'【金字塔模式】' if use_pyramid else '【统一仓位模式】'}")

                    for inst in active_instruments:
                        if tf not in inst_tf_configs[inst]:
                            continue
                        params = build_params(inst, tf)
                        process_tf(ib, contracts[inst], inst, tf,
                                   params, state,
                                   equity, _get_tf_risk_pct(params, equity), args.dry_run)

                # 整点心跳：联合播报两个 bot 状态
                hr_key = f"heartbeat-{now_et.strftime('%Y%m%d-%H')}"
                if hr_key not in triggered and now_et.minute == 0 and now_et.second <= 30:
                    triggered.add(hr_key)
                    nq_pos = {tf: state.get("NQ", {}).get(tf, {}).get("signed_contracts", 0)
                              for tf in ["1h", "4h", "1d"]}
                    es_pos = {tf: state.get("ES", {}).get(tf, {}).get("signed_contracts", 0)
                              for tf in ["4h", "1d"]}
                    nq_net = sum(nq_pos.values())
                    es_net = sum(es_pos.values())
                    hb_msg = (
                        f"💓 整点心跳 {now_et.strftime('%m-%d %H:%M')} ET\n"
                        f"💰 账户净值: ${equity:,.0f}\n"
                        f"─────────────────\n"
                        f"✅ ib-bot（趋势 NQ+ES）\n"
                        f"  NQ: 1H{nq_pos['1h']:+d} / 4H{nq_pos['4h']:+d} / 1D{nq_pos['1d']:+d}  净仓{nq_net:+d}手\n"
                        f"  ES: 4H{es_pos['4h']:+d} / 1D{es_pos['1d']:+d}  净仓{es_net:+d}手\n"
                        f"{_mr_status()}"
                    )
                    tg_alert(hb_msg)

                cutoff_h = datetime.now(ET).strftime('%Y%m%d-%H')
                triggered = {k for k in triggered if k[:13] == cutoff_h}

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.error(f"💥 主循环错误: {exc}，断线重连中...")
                try: ib.disconnect()
                except Exception: pass
                _time.sleep(10)
                do_connect()

    except KeyboardInterrupt:
        log.info("\n⛔ 用户中断")
    finally:
        try: ib.disconnect()
        except Exception: pass
        log.info("IB 已断开连接")


if __name__ == "__main__":
    import time
    while True:
        try:
            main()
            break  # 正常退出（KeyboardInterrupt）则停止
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"💥 引擎崩溃: {e}，30秒后自动重启...")
            tg_alert(f"💥 引擎崩溃: {e}\n30秒后自动重启...")
            time.sleep(30)
