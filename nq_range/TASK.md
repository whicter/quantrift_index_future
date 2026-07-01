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
| ✅ | 下载 15min NQ 数据（IB HMDS，download_short3.py）→ NQF_15min_2024-06-23_2026-06-30.csv（46581 bars）|

## Phase 3：策略回测
| 状态 | 任务 |
|------|------|
| ✅ | 策略类设计（NQRangeStrategy）|
| ✅ | 基线回测：ci=55, adx=25, rsi=45 → Sharpe 1.197（修正后）|
| ✅ | 参数扫描（ci × adx × rsi_os）|
| ✅ | IS/OOS 验证（IS Sharpe 1.709，OOS 0.656，OOS WR 73.2%）|
| ✅ | 逐年分析（2024 S=2.01，2025 S=1.08，2026上半 S=0.72）|
| ✅ | 逐月分析（8/27 月亏损，符合预期）|
| ✅ | 修正入场信号：Low<BB_lower（非Close）→ 88笔，Sharpe 1.197，MaxDD -0.70% |
| ✅ | 15min 完整回测 → IS Sharpe -1.685，OOS 1.181，不适合（1H策略无需降级）|

## Phase 4：实盘引擎
| 状态 | 任务 |
|------|------|
| ✅ | 写 config_nq_range.yaml（含 risk_pct=1.0）|
| ✅ | 写 backtest_nq_range.py（支持 --all-tf 多周期对比）|
| ✅ | 写 nq_range_engine.py（clientId=23，ATR动态定仓）|
| ✅ | 分配 clientId=23 |
| ⏳ | 干跑测试 |
| ⏳ | pm2 注册 |
