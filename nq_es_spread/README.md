# NQ/ES Statistical Arbitrage (nq_es_spread)

NQ 与 ES 配对统计套利策略。利用两者价差的均值回归特性，价差偏离历史均值时建仓，回归时平仓。

## 策略概述

- **品种**：MNQ（$2/点）vs MES（$5/点）
- **周期**：1h
- **逻辑**：价差均值回归（市场中性，方向性风险小）
- **信号**：价差偏离 Z-score > N 个标准差
- **状态**：⏳ 待开发

## 核心逻辑

NQ 和 ES 高度相关（相关系数通常 > 0.95）。
当 NQ/ES 价差偏离历史均值：
- 价差过高（NQ 相对 ES 偏贵）→ 做空 NQ + 做多 ES
- 价差过低（NQ 相对 ES 偏便宜）→ 做多 NQ + 做空 ES

价差回归均值后平仓，赚取价差回归的利润。

## 价差定义

```
spread = NQ_price / ES_price   # 比值
或
spread = NQ_price - hedge_ratio × ES_price  # 线性价差
```

hedge_ratio 通过 OLS 回归计算：`NQ = hedge_ratio × ES + intercept`

## 入场条件（草案）

| 条件 | 说明 |
|------|------|
| Z-score > +2 | 价差高于均值 2 个标准差 → 做空 NQ、做多 ES |
| Z-score < -2 | 价差低于均值 2 个标准差 → 做多 NQ、做空 ES |
| 协整检验 | ADF test p-value < 0.05（确认价差均值回归特性）|

## 风险控制

- **市场中性**：同时持有 NQ 多 + ES 空（或反向），对冲方向性风险
- **去相关风险**：当市场极端波动时，NQ/ES 相关性可能短暂崩溃
- **仓位对冲比**：需根据 DV01 或美元价值对冲

## 文件结构

```
nq_es_spread/
├── README.md          本文件
├── WIKI.md            详细设计文档
├── TASK.md            开发任务追踪
├── LEARNING.md        回测结果与参数学习
├── config_spread.yaml
├── strategy_spread.py
├── backtest_spread.py
└── spread_engine.py（后期）
```
