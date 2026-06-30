# NQ/ES Statistical Arbitrage — 详细设计文档

## 策略原理

NQ 和 ES 长期协整（cointegrated），价差存在均值回归特性。
利用这一特性：当价差偏离历史均值时，建立对冲仓位，等待回归后平仓。

## 价差计算

### 方法一：简单比值
```python
spread = NQ_price / ES_price
```
优点：直觉简单；缺点：不考虑历史关系变化。

### 方法二：OLS 线性回归（推荐）
```python
# 滚动窗口（如 120h = 5天）
hedge_ratio = OLS(NQ ~ ES).coef  # 每次重新计算
spread = NQ - hedge_ratio * ES
```
优点：动态调整，适应市场变化；缺点：计算复杂。

## Z-score 信号

```python
z = (spread - spread.rolling(window).mean()) / spread.rolling(window).std()

if z > z_entry:   # 入场：价差高 → NQ 贵，做空 NQ + 做多 ES
if z < -z_entry:  # 入场：价差低 → NQ 便宜，做多 NQ + 做空 ES
if abs(z) < z_exit:  # 出场：价差回归
```

## 仓位对冲（美元中性）

```
MNQ 点值 = $2/点；NQ 约 30000pt → 1手 MNQ = $60,000
MES 点值 = $5/点；ES 约 6000pt  → 1手 MES = $30,000

做空 1手 MNQ + 做多 2手 MES → 约等值对冲（$60k vs $60k）
```
注意：hedge_ratio 变化时需动态调整对冲比。

## 协整检验

```python
from statsmodels.tsa.stattools import adfuller, coint

# 检验价差是否平稳
result = adfuller(spread)
p_value = result[1]
# p < 0.05 → 价差平稳 → 均值回归有统计支撑
```

## 关键参数（待优化）

| 参数 | 说明 | 初始值 |
|------|------|--------|
| lookback_window | 计算均值/标准差的回望窗口 | 120h（5天）|
| z_entry | 入场 Z-score 阈值 | ±2.0 |
| z_exit | 出场 Z-score 阈值 | 0 或 ±0.5 |
| hedge_window | OLS 回归窗口 | 120h |
| max_bars | 时间止损 | 24h |

## 执行注意事项

1. 两条腿需同时提交（用 IB 的 combo order 或分别提交并接受滑点）
2. 单腿失败需立即平掉成交的一条腿，避免单腿敞口
3. clientId 分配：建议用一个 clientId 管理两个品种
