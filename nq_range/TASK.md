# NQ Range Trading — 开发任务追踪

## Phase 1：文档与规划
| 状态 | 任务 |
|------|------|
| ✅ | 创建 nq_range/ 文件夹与文档 |
| ✅ | 确认策略设计（CI+ADX+BB）|

## Phase 2：数据准备
| 状态 | 任务 |
|------|------|
| ✅ | 使用 NQF_1h_2024-03-01_2026-06-24.csv |
| ✅ | 确认 CI/ADX 指标可复用 es_mr/indicators_mr.py |

## Phase 3：策略回测
| 状态 | 任务 |
|------|------|
| ✅ | 策略类设计（NQRangeStrategy）|
| ✅ | 基线回测：ci=55, adx=25, rsi=45 → Sharpe 1.098 |
| ✅ | 参数扫描（ci × adx × rsi_os）|
| ✅ | IS/OOS 验证（OOS 胜率反升至 67.9%）|
| ✅ | 逐年分析（三年均盈利）|
| ✅ | 逐月分析（8/27 月亏损，符合预期）|

## Phase 4：实盘引擎
| 状态 | 任务 |
|------|------|
| ⏳ | 写 config_nq_range.yaml |
| ⏳ | 写 backtest_nq_range.py（整理现有回测代码）|
| ⏳ | 写 nq_range_engine.py（参考 nq_mr_engine.py）|
| ⏳ | 分配 clientId（候选：23）|
| ⏳ | 干跑测试 |
| ⏳ | pm2 注册 |
