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

---

## 待办

### 1. Pattern Exit 参数调优（优先级：高）

**状态**：代码已实现，但全历史回测发现对 NQ 长周期有显著负面影响，需调整参数。

**NQ 全历史回测对比（$100k，2020-2026）**：

| 周期 | Sharpe 关闭→开启 | PnL 关闭→开启 | 结论 |
|------|----------------|-------------|------|
| NQ 1D | 0.855 → 0.317 | +$117k → +$53k | ❌ 严重有害 |
| NQ 4H | 0.662 → 0.540 | +$27k → +$16k | ❌ 有害 |
| NQ 1H | 0.718 → 0.796 | +$12k → +$14k | ✅ 轻微有利 |
| NQ 组合 | Sharpe 3.845 → 2.140 | +$156k → +$82k | ❌ 整体有害 |

**根因**：NQ 2020-2026 单边牛市，`pin_wick_ratio=1.5` + `lookback=5` 假阳性太多，
把正常回调的插针误判为反转顶，提前平掉大量趋势赢家。

**待完成**：
- [ ] NQ 1D/4H：关闭 `use_pattern_exit: false` 或 `pattern_exit_score: 3`
- [ ] 测试 `pin_wick_ratio: 2.0` 能否降低假阳性
- [ ] 单独评估 ES TF 的 pattern exit 效果（ES 非单边，可能更适合）
- [ ] 确定哪些 TF + 信号组合有净正收益后再考虑实盘开启

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
