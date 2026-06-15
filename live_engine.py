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
  手数 = 风险金额 / (ATR × atr_sl_mult × 合约乘数)

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

# ── 路径 / 时区 ────────────────────────────────────────────────────────
BASE_DIR   = Path(__file__).parent
LOG_DIR    = BASE_DIR / "logs"
STATE_FILE = BASE_DIR / "live_state.json"
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

def fetch_bars(ib: IB, contract, tf: str) -> pd.DataFrame:
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
    if not bars:
        raise RuntimeError(f"IB 返回空数据 (tf={tf})")

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
    _CaptureStrategy.use_atr_exit        = bool(params.get("use_atr_exit", False))
    _CaptureStrategy.atr_sl_mult         = float(params.get("atr_sl_mult", 1.5))
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

def place_order(ib: IB, contract, delta: int, dry_run: bool = False):
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
    log.info(f"  ✅ {action} {qty} {sym}  |  "
             f"状态: {trade.orderStatus.status}  "
             f"成交均价: {trade.orderStatus.avgFillPrice or '待成交'}")
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
        return

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
        place_order(ib, contract, delta, dry_run=dry_run)
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
    args = parser.parse_args()

    # ── 加载配置 ────────────────────────────────────────────────────
    with open(args.config) as f:
        config = yaml.safe_load(f)

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

    # ── 连接 IB ─────────────────────────────────────────────────────
    ib       = connect_ib(args.host, args.port)
    equity   = get_account_equity(ib)
    state    = load_state(active_instruments)
    contracts = {inst: get_contract(ib, inst) for inst in active_instruments}

    # 显示当前持仓 vs 状态文件
    for inst in active_instruments:
        ib_pos  = get_ib_position(ib, inst)
        st_pos  = sum(state[inst][tf]["signed_contracts"] for tf in ["1h", "4h", "1d"])
        log.info(f"{inst} IB 持仓: {ib_pos:+d}手  状态文件: {st_pos:+d}手"
                 + ("  ⚠️ 不一致！" if ib_pos != st_pos else ""))

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

    # ── 定时主循环 ───────────────────────────────────────────────────
    log.info("\n▶ 等待 Bar 收盘触发...\n")
    triggered: set = set()

    # 收集所有品种用到的 TF
    all_tfs = set()
    for inst in active_instruments:
        all_tfs |= set(inst_tf_configs[inst].keys())
    if args.tf:
        all_tfs = {args.tf}

    # 用于检测 Error 1100（IB 与服务器断连）
    connectivity_lost = [False]

    def on_error(reqId, errorCode, errorString, contract):
        if errorCode == 1100:
            connectivity_lost[0] = True
            log.warning("⚠️  Error 1100: IB 与服务器断连，将触发重连")

    ib.errorEvent += on_error

    def reconnect():
        connectivity_lost[0] = False
        log.warning("⚠️  IB 连接断开，尝试重连...")
        try:
            ib.disconnect()
        except Exception:
            pass
        import time
        while True:
            try:
                time.sleep(30)
                ib.connect(args.host, args.port, clientId=20, timeout=30, readonly=False)
                contracts.update({inst: get_contract(ib, inst) for inst in active_instruments})
                log.info("✅ IB 重连成功")
                break
            except Exception as exc:
                log.error(f"  重连失败: {exc}，30秒后重试")

    try:
        while True:
            ib.sleep(10)

            if connectivity_lost[0] or not ib.isConnected():
                reconnect()

            now_et = datetime.now(ET)

            if not is_market_open(now_et):
                continue

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

            # 清理超过 1 小时的旧触发记录
            cutoff_h = datetime.now(ET).strftime('%Y%m%d-%H')
            triggered = {k for k in triggered if k[:13] == cutoff_h}

    except KeyboardInterrupt:
        log.info("\n⛔ 用户中断")
    finally:
        ib.disconnect()
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
            time.sleep(30)
