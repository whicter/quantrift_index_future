# NQ MR — 详细设计文档

## 策略原理

均值回归假设：价格短期偏离均值后大概率回归。
NQ 相比 ES 的特点：
- 波动性更高（ATR 约 200-400pt/天 vs ES 约 40-80pt）
- 科技股集中，情绪驱动更强，急跌后反弹更快
- 日内成交量分布更集中在开盘/收盘

## 指标复用（来自 es_mr/indicators_mr.py）

| 指标 | 函数 | 用途 |
|------|------|------|
| 布林带 | `compute_bb` | 识别价格极端偏离 |
| RSI | `compute_rsi` | 超卖信号 |
| VWAP | `compute_vwap` | 均值锚点，价格偏离参考 |
| ATR | `compute_atr` | 动态止盈止损计算 |
| ADX | `compute_adx` | 趋势强度过滤 |
| CI | `compute_ci` | 备选：震荡市过滤（已知与MR信号互斥）|

## 信号评分机制

```
bull_score = 0
if RSI < rsi_os:          bull_score += 1   # 超卖
if close <= BB_lower:     bull_score += 1   # 价格在下轨以下
if close < VWAP - k*ATR:  bull_score += 1   # 价格大幅低于 VWAP

if ADX < adx_threshold and bull_score >= min_score:
    → 入场做多
```

## 出场逻辑（按优先级）

1. **止损**：close < entry - sl_mult × ATR（由 backtesting.py SL 参数处理）
2. **时间止损**：持仓 > max_bars 根 bar 强平
3. **ATR 止盈**：close >= entry + tp_atr_mult × ATR
4. **VWAP 止盈**：close >= VWAP（价格回归均值）

## 与 ES MR 的架构差异

ES MR 只做多（牛市背景），NQ MR 待回测确认是否双向：
- 若 NQ 空头 MR 胜率 > 50%，可加空头方向
- 需要独立的 `bear_score` 评分 + `rsi_ob` 超买阈值

## 隔离架构

- clientId 独立（与主 bot、es_mr 不同）
- 状态文件：`nq_mr/mr_state.json`
- 日志文件：`logs/nq_mr_YYYYMMDD.log`
- pm2 进程名：`ib-bot-nq-mr`
