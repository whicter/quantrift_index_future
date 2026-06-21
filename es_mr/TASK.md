# ES Mean Reversion — 开发任务追踪

## 状态说明
- ✅ 已完成
- 🔄 进行中
- ⏳ 待开始
- ❌ 已取消/放弃

---

## Phase 1：文档与规划

| 状态 | 任务 | 说明 |
|------|------|------|
| ✅ | 创建 `es_mr/` 文件夹 | 与趋势引擎完全隔离 |
| ✅ | 写 README.md | 策略概述、入场出场条件、文件结构 |
| ✅ | 写 WIKI.md | 详细设计：指标、评分机制、出场逻辑、隔离架构 |
| ✅ | 写 TASK.md | 本文件（持续更新） |
| ✅ | 写 LEARNING.md | 回测结果与参数学习日志（已完成 3 轮回测记录） |

---

## Phase 2：基础代码

| 状态 | 任务 | 文件 | 说明 |
|------|------|------|------|
| ✅ | 写配置文件 | `config_mr.yaml` | 已优化：vwap=2.0, tp=2.0, score=3, long_only |
| ✅ | 写指标计算 | `indicators_mr.py` | BB / RSI / VWAP / ADX / CI 全部实现 |
| ✅ | 写策略类 | `strategy_mr.py` | 只做多，3/3 信号全中，ATR止盈2.0，SL1.0 |
| ✅ | 写回测入口 | `backtest_mr.py` | 已验证：Sharpe=0.723，Return=+72.5% |

### indicators_mr.py 需要实现的函数
- `compute_bb(close, length, mult)` → lower, mid, upper
- `compute_rsi(close, length)` → rsi series
- `compute_vwap(high, low, close, volume)` → daily-reset VWAP
- `compute_atr(high, low, close, length)` → ATR series
- `compute_adx(high, low, close, length)` → ADX series
- `compute_ci(high, low, close, length)` → Choppiness Index
- `compute_mr_signals(df, params)` → 汇总所有信号，返回 bull_score / bear_score

### strategy_mr.py 需要实现
- `class MeanReversionStrategy(Strategy)` (backtesting.py)
- 入场：bull_score ≥ min_score 且 ADX < adx_threshold 且 CI > ci_threshold
- 出场（优先级）：止损 → BB中轨 → VWAP → 时间止损(max_bars)

---

## Phase 3：回测与优化

| 状态 | 任务 | 文件 | 说明 |
|------|------|------|------|
| ✅ | 初始回测 | `backtest_mr.py` | 完成 3 轮优化，最终 Sharpe=0.723 |
| ⏳ | 参数优化器 | `optimizer_mr.py` | 针对 MR 策略的启发式优化（待开发） |
| ✅ | 记录结果 | `LEARNING.md` | 已记录 3 轮回测结论及每笔交易明细 |

### 优化目标
- 主指标：Sharpe Ratio > 0.5
- 次指标：Win Rate > 55%，Profit Factor > 1.5
- 约束：交易笔数 ≥ 30，MaxDD < 25%

### 优化参数范围（初始建议）
| 参数 | 范围 | 步长 |
|------|------|------|
| rsi_os | 20–35 | 1 |
| rsi_ob | 65–80 | 1 |
| bb_mult | 1.5–2.5 | 0.1 |
| vwap_atr_mult | 1.0–2.5 | 0.25 |
| sl_mult | 0.5–2.0 | 0.25 |
| max_bars | 3–12 | 1 |
| min_score | 2–3 | 1 |
| adx_threshold | 20–30 | 2 |
| ci_threshold | 50–65 | 2 |

---

## Phase 4：实盘引擎

| 状态 | 任务 | 文件 | 说明 |
|------|------|------|------|
| ⏳ | 写实盘引擎 | `mr_engine.py` | clientId=21，每小时整点运行 |
| ⏳ | 测试 IB 连接 | — | 用 clientId=21 连接 Gateway 4001 |
| ⏳ | 状态文件设计 | `mr_state.json` | 持仓信息、入场价、bar计数 |
| ⏳ | Telegram 告警 | mr_engine.py | 入场/出场/止损 消息推送 |
| ⏳ | 同步到 Mac Studio | — | rsync es_mr/ 到 mac-studio |
| ⏳ | 注册 pm2 进程 | — | `pm2 start mr_engine.py --name ib-bot-mr` |

### mr_state.json 结构设计
```json
{
  "position": 0,          // 1=多, -1=空, 0=空仓
  "entry_price": null,
  "entry_bar": null,      // 入场时的 bar 时间戳
  "bars_held": 0,
  "sl_price": null,
  "tp_mid_price": null,   // BB 中轨出场目标
  "tp_vwap_price": null,  // VWAP 出场目标
  "last_update": null
}
```

---

## Phase 5：监控与维护

| 状态 | 任务 | 说明 |
|------|------|------|
| ⏳ | 上线后第一周人工监控 | 对比实盘信号 vs 回测预期 |
| ⏳ | 2 周后统计实盘胜率 | 如果 < 45% 暂停并检查 |
| ⏳ | 季度参数复审 | 重跑优化器，确认参数是否需要调整 |

---

## 已知风险与注意事项

1. **VWAP 日内重置**：实盘 VWAP 需在每日 00:00 UTC（或 CME 交易日切换时）重置，回测中需模拟这一行为
2. **信号频率**：均值回归信号在趋势市中极少触发（ADX≥25 过滤掉），预期每周 2-5 笔
3. **与趋势引擎对冲**：两个引擎独立运行，IB 自动合并净仓，对冲属预期行为（不需要干预）
4. **回测数据**：使用 `../data/ESF_1h_2024-06-10_2026-06-08.csv`（约 2 年数据）
