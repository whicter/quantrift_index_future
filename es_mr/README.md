# ES Mean Reversion (es_mr)

ES（MES）1h 均值回归策略，与主趋势引擎完全分离。

## 策略概述

- **品种**：MES（微型 S&P 500 期货，$5/点）
- **周期**：1h
- **逻辑**：均值回归（只做多，不做空）
- **信号**：RSI + 布林带 + VWAP 三合一，**三个全中**才入场
- **过滤**：ADX < 25（非趋势市场）
- **回测**：Sharpe 0.723，年化 +25.6%，MaxDD -13.4%，PF 2.585

> **只做多的原因**：ES 在 2024-2026 牛市背景下，空头 MR 信号胜率仅 38%（多头 53%）。
> 空头 MR 是在趋势中逆势，长期负期望。

## 入场条件（三个全中）

| 信号 | 条件 |
|------|------|
| RSI | < 28（超卖） |
| 布林带 | 价格 ≤ 下轨（20期，2.0σ） |
| VWAP | 价格 < VWAP − 2.0 × ATR |
| 市场过滤 | ADX < 25（非趋势行情） |
| **要求** | **三个信号全部满足** |

## 出场条件（优先级从高到低）

| 优先级 | 出场方式 | 触发条件 |
|--------|---------|---------|
| 1 | 止损 | 价格跌破 entry − 1.0 × ATR |
| 2 | 时间止损 | 持仓超过 8 根 1h bar（8小时）|
| 3 | ATR 止盈 | 价格升至 entry + 2.0 × ATR |
| 4 | VWAP 止盈 | 价格回到 VWAP |

R:R = 2:1（止盈 2 ATR，止损 1 ATR）

## 回测结果（2024-06-10 ~ 2026-06-08）

| 指标 | 值 |
|------|----|
| 总收益率 | **+72.5%** |
| 年化收益率 | 25.6% |
| Sharpe Ratio | **0.723** |
| Max Drawdown | **-13.4%** |
| Profit Factor | **2.585** |
| 胜率 | 53.3% |
| 盈亏比 | 2.02:1 |
| 总笔数 | 15（约 1 笔/月） |
| 平均持仓 | 2 小时 |

## 与主引擎的关系

| 项目 | 趋势引擎 | MR 引擎 |
|------|----------|---------|
| 文件 | `live_engine.py` | `es_mr/mr_engine.py` |
| clientId | 20 | 21 |
| 状态文件 | `live_state.json` | `es_mr/mr_state.json` |
| pm2 进程 | `ib-bot` | `ib-bot-mr` |
| 品种 | NQ + ES（趋势TF） | ES 1h only |

两个引擎独立运行，IB 自动合并净仓（预期行为）。

## 文件结构

```
es_mr/
├── README.md          本文件
├── WIKI.md            详细设计文档
├── TASK.md            开发任务追踪
├── LEARNING.md        回测结果与参数学习
├── config_mr.yaml     MR 策略专用配置（已优化）
├── indicators_mr.py   MR 专用指标（BB/RSI/VWAP/ADX/CI）
├── strategy_mr.py     MeanReversionStrategy 类（只做多）
├── backtest_mr.py     回测入口
├── optimizer_mr.py    参数优化器（待完成）
└── mr_engine.py       实盘引擎（待完成）
```

## 快速开始

```bash
# 回测
cd /Users/cohan/Documents/quantrift_index_future
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && \
  /opt/homebrew/bin/python3.11 es_mr/backtest_mr.py"

# 实盘（mr_engine.py 完成后）
ssh mac-studio "PATH=/opt/homebrew/bin:\$PATH pm2 start \
  /Users/congrenhan/Documents/quantrift_index_future/es_mr/mr_engine.py \
  --name ib-bot-mr"
```
