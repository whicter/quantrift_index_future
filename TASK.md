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

---

## 待办

### 1. 信号增强：Pattern + 背离检测（优先级：高）

**背景**：2025-04-09 NQ V形反转复盘显示，策略缺乏图形形态识别能力，导致在明显底部形态
出现时仍持有空仓直至被止损出局。所有新信号仅用于**退出优化**，不改变入场逻辑。

#### 1a. Squeeze Momentum Indicator（SMI）背离检测

- **实现**：`indicators.py` 新增 `compute_smi()` 函数，输出 SMI 动量柱
  - BB（20期，2σ）+ KC（20期，1.5ATR）检测挤压状态（squeeze on/off）
  - 动量值 = 线性回归（close - midpoint，N期）
- **背离信号**：`smi_bull_div`（底背离）/ `smi_bear_div`（顶背离）
  - 底背离：近 lookback 根 bar 内，价格新低但 SMI 动量柱更高
  - 顶背离：近 lookback 根 bar 内，价格新高但 SMI 动量柱更低
- **退出触发**：`strategy.py` 中持仓时检查背离，满足则提前止盈平仓
- **参数**：`smi_len=20`，`smi_lookback=5`

#### 1b. RSI 背离检测

- **实现**：`indicators.py` 新增 `rsi_bull_div` / `rsi_bear_div` 列
  - 底背离：近 lookback 根 bar 内，价格创新低，RSI 未创新低
  - 顶背离：近 lookback 根 bar 内，价格创新高，RSI 未创新高
- **联合使用**：SMI + RSI 同时背离 → 强信号，单独背离 → 弱信号
- **参数**：`divergence_lookback=5`

#### 1c. 4H 插针（Pin Bar）识别

- **实现**：`indicators.py` 新增 `pin_bar_bull`（锤子线）/ `pin_bar_bear`（射击之星）
  - 锤子线：下影线 > 实体 × 2，且下影线 > 上影线 × 2
  - 射击之星：上影线 > 实体 × 2，且上影线 > 下影线 × 2
- **退出触发**：持有空仓时 4H 出现锤子线 → 止盈；持有多仓时出现射击之星 → 止盈
- **注意**：需要在高周期（1D）策略中引用 4H 的 pin bar 信号（跨周期信号）
- **参数**：`pin_wick_ratio=2.0`

#### 1d. 双底 / 双顶形态识别

- **实现**：`indicators.py` 新增 `double_bottom` / `double_top` 列
  - 扫描近 lookback 根 bar 内的两个波段低点
  - 两低点价差 < ATR × tol（价格接近），第二低点后收盘强力收回
- **联合使用**：需同时满足 RSI 或 SMI 底背离才确认信号
- **参数**：`double_bottom_lookback=20`，`double_bottom_atr_tol=0.5`

#### 回测验证计划

实现完成后：
1. 单独回测每个信号的贡献（对比 baseline）
2. 重点验证：2025-04-09 事件是否被任一信号提前捕捉
3. 检查 False Positive 率（正常趋势中误触发止盈的频率）
4. 全历史组合回测，对比加入前后的 Sharpe / MaxDD / 胜率

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
