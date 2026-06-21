# TASK.md — 待办事项

## 已完成

| 任务 | 完成日期 |
|---|---|
| Telegram 配置（bot @quantrift_index_future_bot） | 2026-06-15 |
| 连接稳定性修复（2105重连、fetch_bars重试、连接失败告警） | 2026-06-17 |
| 每小时心跳 Telegram（状态 + 持仓 + 实时净值） | 2026-06-17 |
| IBC 自动登录（Gateway 重启后自动填账号密码，免手动操作） | 2026-06-17 |
| ES 参数专项优化（ES 4H Sharpe=1.025，ES 1D Sharpe=0.657） | 2026-06-20 |
| 移除 ES 1H 趋势策略（Sharpe=-0.445，永久不适用） | 2026-06-20 |
| 建立 es_mr/ 均值回归引擎（文档+代码+回测） | 2026-06-20 |
| ES MR 策略回测优化（Sharpe=0.723，PF=2.585，MaxDD=-13.4%） | 2026-06-20 |
| ES MR 实盘引擎（mr_engine.py，clientId=21，次季合约，ATR定仓） | 2026-06-20 |
| ES MR 每日最多 1 单限制（today_trades 计数，防同日连续亏损） | 2026-06-20 |
| Pattern + 背离信号实现（SMI/RSI背离、Pin Bar、双底/双顶，indicators.py + strategy.py） | 2026-06-21 |
| Pattern exit 调优：仅空头模式（多头不干预，NQ 全历史 4H/1H 超越基准） | 2026-06-21 |

---

## 待办

### 1. Pattern Exit 调优历程（已完成）

**v1（双向）**：同时对多空仓触发，对 NQ 全历史有害（1D Sharpe 0.855→0.317，PnL -55%）。

**v2（仅空头，最终版）**：`strategy.py` 中 pattern exit 仅在 `d == -1`（空仓）时触发，
多头完全不受影响。NQ 全历史验证：
- 4H Sharpe 0.662 → **0.832**（超越基准）
- 1H Sharpe 0.718 → **0.849**（超越基准）
- 1D Sharpe 0.855 → 0.764（轻微损失，可接受）
- 2025 NQ 1D 年度亏损：-$882 → **-$184**（April 空单止盈效果）

**待评估**：
- [ ] ES TF 的 pattern exit 效果（ES 多空均衡，双向可能有用）

---

### 2. open orders 处理（优先级：中）

**目标**：重连时发现在途订单应暂停该品种信号，等订单结束后再继续。

现状：重连时调用 `reqAllOpenOrders()` 打日志，有未平仓单会告警，但不自动处理。

---

### 3. Docker 化（优先级：低，稳定后再做）

IBC + Gateway + Bot 统一 Docker 管理，消除每周手动登录。

```bash
docker run -d --name ib-gateway --restart always \
  -p 4001:4001 \
  -e TWS_USERID=账号 -e TWS_PASSWORD=密码 \
  -e TRADING_MODE=live \
  ghcr.io/gnzsnz/ib-gateway:stable
```
