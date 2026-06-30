# NQ Mean Reversion — 开发任务追踪

## 状态说明
- ✅ 已完成
- 🔄 进行中
- ⏳ 待开始
- ❌ 已取消/放弃

---

## Phase 1：文档与规划

| 状态 | 任务 | 说明 |
|------|------|------|
| ✅ | 创建 `nq_mr/` 文件夹 | 与趋势引擎、es_mr 完全隔离 |
| ✅ | 写 README.md | 策略概述、入场出场条件、文件结构 |
| ✅ | 写 WIKI.md | 详细设计文档 |
| ✅ | 写 TASK.md | 本文件 |
| ✅ | 写 LEARNING.md | 初始化，待填充回测结果 |

---

## Phase 2：数据准备

| 状态 | 任务 | 说明 |
|------|------|------|
| ⏳ | 下载 NQ 1h 历史数据 | 复用 `data/NQF_1h_*.csv` 或从 IB 重新拉取 |
| ⏳ | 确认数据时间范围 | 至少 2 年（2024-2026）|
| ⏳ | 确认 Volume 数据完整性 | VWAP 计算需要 Volume |

---

## Phase 3：策略开发

| 状态 | 任务 | 文件 | 说明 |
|------|------|------|------|
| ⏳ | 复用 es_mr 指标库 | `es_mr/indicators_mr.py` | BB/RSI/VWAP/ADX/CI 均可复用 |
| ⏳ | 写策略类 | `strategy_nq_mr.py` | 继承或复制 MeanReversionStrategy |
| ⏳ | 初始回测 | `backtest_nq_mr.py` | 用 ES 参数作为基线 |
| ⏳ | 参数优化 | — | 针对 NQ 波动性调整（重点：rsi_os、max_bars）|

### 优化目标
- Sharpe Ratio > 0.5
- Win Rate > 50%
- Profit Factor > 1.5
- 交易笔数 ≥ 20（约 1 笔/月）
- MaxDD < 20%

### 重点关注参数（NQ vs ES 差异）
| 参数 | ES MR（当前） | NQ MR（待优化） |
|------|------------|--------------|
| rsi_os | 28 | 待定（NQ 超卖程度更深）|
| max_bars | 8 | 待定（NQ 反弹可能更快）|
| adx_threshold | 25 | 待定 |
| vwap_atr_mult | 2.0 | 待定 |

---

## Phase 4：实盘引擎

| 状态 | 任务 | 说明 |
|------|------|------|
| ⏳ | 写实盘引擎 | `nq_mr_engine.py`，参考 `es_mr/mr_engine.py` |
| ⏳ | 分配 clientId | 当前已用：20（主bot）、21（es_mr）、2（stock-alert）|
| ⏳ | 注册 pm2 进程 | `pm2 start nq_mr_engine.py --name ib-bot-nq-mr` |
| ⏳ | 上线前纸交易验证 | --dry-run 模式运行至少 2 周 |
