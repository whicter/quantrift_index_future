# NQ Mean Reversion (nq_mr)

NQ（MNQ）均值回归策略，移植自 es_mr，针对 NQ 特性重新优化。

## 策略概述

- **品种**：MNQ（微型纳斯达克 100 期货，$2/点）
- **周期**：1h
- **逻辑**：均值回归（只做多，不做空）
- **信号**：RSI + 布林带 + VWAP 三合一
- **过滤**：ADX < adx_threshold（非趋势市场）
- **状态**：⏳ 待开发

> **与 ES MR 的主要区别**：
> NQ 波动性更高（ATR 约为 ES 的 5 倍），参数需重新优化。
> NQ 科技股成分更集中，急跌后反弹速度更快，max_bars 可能更短。

## 入场条件（待回测确认）

| 信号 | 条件（初始值，待优化） |
|------|------|
| RSI | < 30（超卖） |
| 布林带 | 价格 ≤ 下轨（20期，2.0σ） |
| VWAP | 价格 < VWAP − 2.0 × ATR |
| 市场过滤 | ADX < 25（非趋势行情） |
| 要求 | 三个信号全部满足 |

## 出场条件（待回测确认）

| 优先级 | 出场方式 | 触发条件 |
|--------|---------|---------|
| 1 | 止损 | 价格跌破 entry − 1.0 × ATR |
| 2 | 时间止损 | 持仓超过 max_bars 根 1h bar |
| 3 | ATR 止盈 | 价格升至 entry + 2.0 × ATR |
| 4 | VWAP 止盈 | 价格回到 VWAP |

## 与 ES MR 的关系

| 项目 | ES MR | NQ MR |
|------|-------|-------|
| 品种 | MES（$5/点） | MNQ（$2/点） |
| 方向 | 只做多 | 待定（可能双向） |
| clientId | 21 | 待分配（22 或 23）|
| 状态文件 | `es_mr/mr_state.json` | `nq_mr/mr_state.json` |
| pm2 进程 | `ib-bot-mr` | `ib-bot-nq-mr`（待创建）|

## 文件结构

```
nq_mr/
├── README.md          本文件
├── WIKI.md            详细设计文档
├── TASK.md            开发任务追踪
├── LEARNING.md        回测结果与参数学习
├── config_nq_mr.yaml  策略参数配置
├── strategy_nq_mr.py  策略类
├── backtest_nq_mr.py  回测入口
└── nq_mr_engine.py    实盘引擎（后期）
```
