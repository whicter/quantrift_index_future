# 策略说明文档

**策略名称**: Confluence 多指标趋势跟踪
**标的**: NQ（MNQ）+ ES（MES），CME 微型期货
**周期**: 1H / 4H / 1D 三周期独立运行

---

## 策略逻辑概述

核心思想：**多个独立指标同方向共振时才入场**，单一指标不足以触发信号。

每根 Bar 收盘时，计算各指标方向，累加"牛市分" (bullScore) 和"熊市分" (bearScore)。
当牛市分 ≥ `min_score` 且无拥挤/矛盾信号时，开多；反之开空。

---

## 参与评分的指标

### 1. UT Bot（趋势跟踪止损）
- 基于 ATR 的自适应追踪止损线
- 价格突破上方止损线 → +1 牛；突破下方 → +1 熊
- 参数：`ut_key`（ATR 倍数，越大越不敏感）

### 2. SSL Channel（趋势通道）
- 基于 EMA 的上下轨，判断价格在通道内的位置
- 收盘价在上轨上方 → +1 牛；下轨下方 → +1 熊
- 参数：`ssl_len`（EMA 周期）

### 3. MACD（动量）
- 标准 MACD（12/26/9）
- MACD 线 > 信号线 → +1 牛；反之 → +1 熊

### 4. RSI（动量过滤）
- RSI > 50 → +1 牛；RSI < 50 → +1 熊
- 参数：`rsi_period`（默认 14）

### 5. ADX（趋势强度过滤，可选）
- ADX < `adx_threshold` 时过滤掉信号（震荡市不入场）
- 参数：`use_adx`, `adx_threshold`（当前 4H/1D 约 30，1H 约 20）

### 6. 成交量过滤（可选）
- 当前成交量 > 均量 × `vol_mult` 时才允许入场
- 参数：`use_vol`, `vol_mult`

### 7. BBMC（Bollinger Band + MACD Color）
- 布林带中轨方向作为大趋势过滤
- `bbmc_dir` ≥ 0 → 允许做多；< 0 → 允许做空（当 `use_bbmc_dir=True`）

### 8. Squeeze Momentum（均值回归，可选）
- 布林带收缩 + 动量方向，用于均值回归信号
- 参数：`use_squeeze_mr`（当前全部关闭，趋势策略不用均值回归）

---

## 入场条件（多头示例）

```
bullScore >= min_score          # 足够多的指标方向一致
bearScore <= conflict_threshold # 反向指标不能太多（拥挤过滤）
not isChoppy                    # 非震荡市
ADX >= adx_threshold            # 趋势足够强（如启用）
volume > vol_mult × avg_vol     # 成交量放大（如启用）
bbmc_dir >= 0                   # 大趋势向上（如启用）
```

空头条件对称。

---

## 退出条件

### 止损（ATR 止损）
- 止损距离 = ATR × `atr_sl_mult`（当前 1.5）
- 以入场价计算止损位，触及则全平

### 分批止盈（Staged TP，已启用）
- **TP1**：价格达到 入场价 + ATR × `atr_tp1_mult`（当前 1.0）→ 平掉 34% 仓位
- **TP2**：价格达到 入场价 + ATR × `atr_tp2_mult`（当前 2.0）→ 平掉剩余仓位

### 反向信号翻转（`allow_reversal_flip`）
- 持多仓期间出现强烈空头信号（bearScore ≥ `reversal_score`）→ 平多开空
- 参数：`reversal_score`（当前约 2-3）

---

## 仓位管理

### 定仓公式

```
风险金额 = 账户净值 × risk_pct%
止损金额 = ATR × atr_sl_mult × 合约乘数
手数     = 风险金额 / 止损金额（最小 1 手）
```

### 两种风险模式

| 净值 | 模式 | 说明 |
|------|------|------|
| < $60,000 | 统一模式 | 所有周期统一 1% 风险/笔 |
| ≥ $60,000 | Pyramid 模式 | 1H:1.0% / 4H:1.5% / 1D:4.5% |

Pyramid 模式下高周期仓位更重，与趋势持续时间正相关。

### 参考净值锁定

- 全部空仓时：从 IB 拉取最新净值作为 `reference_equity`
- 持仓期间：使用锁定净值定仓（避免浮盈自动放大仓位）
- 效果：亏损后自动缩仓（去杠杆），盈利全平后自动加仓（复利）

---

## 三周期独立运行与仓位叠加

三个周期（1H/4H/1D）各自独立计算信号、独立持仓，互不干扰。

```
净仓位 = 1H signed_contracts + 4H signed_contracts + 1D signed_contracts
```

叠加示例：
- 4H 空 -1 + 1H 多 +1 = 净仓 0（对冲，实际风险降低）
- 4H 多 +1 + 1H 多 +1 + 1D 多 +1 = 净仓 +3（顺势叠加）

---

## 当前参数（config.yaml）

### NQ 1H
| 参数 | 值 |
|------|---|
| min_score | 5 |
| adx_threshold | 30.0 |
| use_squeeze_mr | false |
| atr_sl_mult | 1.5 |
| risk_pct_pyramid | 1.0% |

### NQ 4H
| 参数 | 值 |
|------|---|
| min_score | 5 |
| adx_threshold | 30.0 |
| ssl_len | 54 |
| atr_sl_mult | 1.5 |
| risk_pct_pyramid | 1.5% |

### NQ 1D
| 参数 | 值 |
|------|---|
| min_score | 4 |
| adx_threshold | 20.0 |
| use_trend_filter | true |
| atr_sl_mult | 1.5 |
| risk_pct_pyramid | 4.5% |

---

## 幻象信号过滤

**问题**：回测引擎用 ContFuture（当前合约）数据，与历史 CSV（旧合约）数据不同，可能产生虚假入场信号。

**解决**（`live_engine.py` 第 4.5 节）：当状态文件显示空仓但回测末态显示有仓时，额外验证当前 Bar 的指标是否真正满足入场条件。不满足则忽略该信号（标记为"幻象信号"）。

---

## 回放法执行原理

引擎不在盘中实时订阅 tick 数据，而是在每根 Bar 收盘后：

1. 从 IB 拉取最新历史数据（含已收盘的 Bar）
2. 运行完整回测，读取策略最终持仓状态
3. 对比 IB 实际持仓与状态文件，计算差额
4. 按差额下单（市价单）

优点：逻辑与回测完全一致，不存在"实盘和回测信号不同"的问题。
