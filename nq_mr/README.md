# NQ Mean Reversion (nq_mr)

NQ（MNQ）均值回归策略，移植自 es_mr，针对 NQ 特性重新优化。

## 状态：✅ 引擎已完成，待干跑验证

## 策略概述

- **品种**：MNQ（微型纳斯达克 100 期货，$2/点）
- **周期**：1h
- **逻辑**：均值回归（只做多，不做空）
- **信号**：RSI + 布林带 + VWAP 三合一（三个全中才入场）
- **过滤**：ADX < 25（非趋势市场）
- **clientId**：22
- **pm2 进程**：ib-bot-nq-mr

## 优化后参数（2026-06-30 回测确认）

| 参数 | NQ MR | ES MR | 差异说明 |
|------|-------|-------|---------|
| rsi_os | **30** | 28 | NQ 波动大，更高阈值捕获更多信号 |
| tp_atr_mult | **1.5** | 2.0 | 快进快出，防 NQ 剧烈波动吃掉盈利 |
| max_bars | **12** | 8 | NQ 均值回归更慢，需要更多时间 |
| adx_threshold | 25 | 25 | 相同 |
| vwap_atr_mult | 2.0 | 2.0 | 相同 |
| bb_mult | 2.0 | 2.0 | 相同 |
| sl_mult | 1.0 | 1.0 | 相同 |
| long_only | true | true | 空头 WR 仅 26.3%，不做空 |

## 回测结果（2024-03 ~ 2026-06）

| 指标 | 值 |
|------|---|
| 交易笔数 | 33（约 1.2笔/月）|
| 胜率 | 48.5% |
| Sharpe | 1.015 |
| Sortino | 1.876 |
| MaxDD | -0.36% |
| Profit Factor | 1.81 |
| 平均持仓 | 6h |

### IS vs OOS

| | 笔数 | 胜率 | Sharpe | MaxDD | PF |
|---|---|---|---|---|---|
| IS（2024-03~2025-06）| 15 | 46.7% | 1.052 | -0.24% | 2.07 |
| OOS（2025-07~2026-06）| 18 | 50.0% | 1.000 | -0.36% | 1.60 |

无过拟合，OOS 表现稳定。

## 实盘启动命令

```bash
# 干跑测试
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 nq_mr/nq_mr_engine.py --port 4001 --run-now --dry-run"

# pm2 启动
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH TG_TOKEN='...' TG_CHAT_ID='...' pm2 start /opt/homebrew/bin/python3.11 --name ib-bot-nq-mr -- /Users/congrenhan/Documents/quantrift_index_future/nq_mr/nq_mr_engine.py --port 4001 && pm2 save"
```

## 文件结构

```
nq_mr/
├── README.md           本文件
├── WIKI.md             详细设计文档
├── TASK.md             开发任务追踪
├── LEARNING.md         回测结论与参数学习
├── config_nq_mr.yaml   策略参数配置
├── backtest_nq_mr.py   回测入口
└── nq_mr_engine.py     实盘引擎（clientId=22）
```
