# quantrift_index_future — CLAUDE.md

> **语言规则**：只用中文或英文回复，绝对不能出现韩语或其他语言。

> Claude Code 在本机（cohan）运行，所有 IB/策略命令必须 SSH 到 Mac Studio 执行。

## 环境

| 项目 | 值 |
|---|---|
| Mac Studio hostname | `mac-studio`（用户 congrenhan） |
| 项目路径（Mac Studio） | `/Users/congrenhan/Documents/quantrift_index_future` |
| 项目路径（本机） | `/Users/cohan/Documents/quantrift_index_future` |
| Python（Mac Studio） | `/opt/homebrew/bin/python3.11` |
| IB Gateway 实盘端口 | 4001（只在 Mac Studio，本机连不上） |
| IB Gateway 模拟盘端口 | 4002 |
| GitHub remote | `git@github.com:whicter/quantrift_index_future.git`（SSH） |
| 实盘账户 | U17682857，净值约 $32,365 |

## 常用命令

```bash
# dry-run 测试
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 live_engine.py --port 4001 --run-now --dry-run"

# 查看引擎状态
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 status"

# 查看日志
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 logs ib-bot --lines 50"

# 重启引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 restart ib-bot"

# 停止引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 stop ib-bot"

# 手动启动（绕过 pm2）
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && nohup /opt/homebrew/bin/python3.11 live_engine.py --port 4001 >> logs/live_cron.log 2>&1 &"

# 同步文件到 Mac Studio
scp /tmp/xxx.py mac-studio:/Users/congrenhan/Documents/quantrift_index_future/

# git commit（在 Mac Studio 上）
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && git add -A && git commit -m '...'"

# git push（必须用 -A 转发 SSH agent）
ssh -A mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && git push"

# 本机同步
cd /Users/cohan/Documents/quantrift_index_future && git pull origin master
```

**绝对不要做：**
- 不要在本机直接连 127.0.0.1:4001/4002
- git push 必须用 `ssh -A`，否则没有 GitHub 权限

## 策略架构

### 系统一：趋势引擎（ib-bot，clientId=20）

- **核心文件**：`live_engine.py`、`strategy.py`、`indicators.py`、`config.yaml`
- **品种**：NQ（MNQ，$2/点）+ ES（MES，$5/点），CME，用 `ContFuture` 自动滚近月
- **当前合约**：MNQU6 / MESU6（9月，到期 2026-09-18）
- **周期**：NQ 1h/4h/1d + ES 4h/1d，三周期独立信号，仓位可叠加
- **开市时间**：周日 18:00 ET（北京时间周一 06:00）

**仓位模式：**
- 净值 < $60k：统一 1% 风险/笔
- 净值 ≥ $60k：多TF叠加模式 NQ（1h:0.72%，4h:2.4%，1d:6.0%）/ ES（4h:1.5%，1d:4.5%）
- NQ $100k 典型：1H=3手 / 4H=4手 / 1D=5手（周期越长手数越多），全顺势最大12手MNQ
- 止损：ATR × 1.5 × 合约乘数；$30k 下各 TF 均为 1 手（最小）

**仓位叠加：**
- 三个 TF 独立持仓，net 仓位 = 三 TF signed_contracts 之和
- `live_state.json` 记录每个 TF 的 signed_contracts（正=多，负=空）

**复利 & 动态仓位：**
- `reference_equity`：全部空仓时从 IB 查净值并锁定，持仓期间用锁定值定仓
- 盈利全平后基准上调（复利），亏损全平后基准下调（去杠杆）

**分批止盈（Staged TP）：**
- TP1：ATR × 1.0，平掉 34% 仓位
- TP2：ATR × 2.0，平掉剩余

**幻象信号过滤：**
- `live_engine.py` 第 4.5 节，空仓时回测末态有仓 → 验证当前 bar 指标是否满足入场条件，不满足则忽略（commit ab2e504）

---

### 系统二：ES MR 引擎（ib-bot-mr，clientId=21）

- **核心文件**：`es_mr/mr_engine.py`、`es_mr/strategy_mr.py`、`es_mr/config_mr.yaml`
- **品种**：MES 1H only，**次季合约**（deferred，跳过当期前月，避免临近交割流动性风险）
- **策略**：超卖均值回归，只做多（RSI<28 + BB下轨 + VWAP-2ATR，三合一）
- **仓位**：ATR 动态定仓，固定 1% 风险/笔（无金字塔，单 TF）
  - 手数 = max(1, round(净值 × 1% / (ATR × 1.0 × $5)))
  - $32k → 4手 | $60k → 8手 | $100k → 13手
- **每日限制**：最多 1 笔入场（防同日连续亏损）
- **状态文件**：`es_mr/mr_state.json`

**启动命令：**
```bash
# dry-run 测试
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 es_mr/mr_engine.py --port 4001 --run-now --dry-run"

# pm2 启动（首次）
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 start /opt/homebrew/bin/python3.11 --name ib-bot-mr -- /Users/congrenhan/Documents/quantrift_index_future/es_mr/mr_engine.py --port 4001"
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 save"

# 日常管理
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 logs ib-bot-mr --lines 50"
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 restart ib-bot-mr"
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 stop ib-bot-mr"
```

## 持续运行架构（commit e89a774）

四层守护，由外到内：

```
launchd          周日 15:00 PST 开市 / 周五 14:00 PST 收市
  └─ pm2         进程崩了自动拉起，Mac Studio 重启后自动恢复
       └─ __main__ crash loop   main() 崩溃 30s 后重启
            └─ do_connect() reconnect loop   断线自动重连
                 └─ Error 1100/1101/2110 事件   提前触发重连
```

launchd 配置文件（Mac Studio）：
- `~/Library/LaunchAgents/com.quantrift.start.plist`（周日开市）
- `~/Library/LaunchAgents/com.quantrift.stop.plist`（周五收市）
- `~/Library/LaunchAgents/pm2.congrenhan.plist`（pm2 开机自启）

IB Gateway 设置：Auto-Restart ON（每周日 1AM ET 自动重启）；Auto-Logoff OFF

## 回测结论

- `staged_tp=True` vs False：True 碾压（+146% vs +4.5%），必须保持
- 多TF叠加 1h 0.4%→1.0%：全历史 $100k 从 +$146k→+$183k（commit dd39dc0）

**NQ 三TF叠加组合最终结果（2024-03 ~ 2026-06，公共区间）**：

| 档位 | 合约分配 | Sharpe | MaxDD | 总PnL | 年化 |
|------|---------|--------|-------|-------|------|
| $32k | 各1手 | **2.226** | -18.09% | **+$18,470** | **+22.50%** |
| $100k | 1D=5/4H=4/1H=3 | **4.263** | -12.88% | **+$135,748** | **+46.32%** |

策略叠加演进（$100k 档 Sharpe）：
- Baseline（无 pattern exit）：2.945
- + Pattern exit（1D关/4H1H开）：3.174
- + VIX filter（1D：exit+entry）：**4.263**

**最终策略配置**：
- NQ 1D：`use_pattern_exit:false`，`use_vix_filter:true`，`use_vix_entry:true`
- NQ 4H/1H：`use_pattern_exit:true`（仅空头），`use_vix_filter:false`
- VIX > 40 触发：1D 空头止盈 + pattern 信号确认后抄底做多（需 ≥2 信号共振）

**复利 + 动态仓位回测（年度再平衡，2024-03 ~ 2026-06）**：

| 起始资金 | 期末净值 | 年化 | Sharpe | MaxDD |
|---------|---------|------|--------|-------|
| $30k | $90,945 | **+63.8%** | 4.223 | -16.8% |
| $100k | $289,350 | **+60.4%** | 4.541 | -15.7% |
| $200k | $588,425 | **+61.6%** | 4.471 | -16.0% |

定仓公式：`n = equity × risk% / (ATR × 1.5 × $2)`，risk%：1D=6% / 4H=2.4% / 1H=0.72%
$100k 档手数演进：2024（5/4/3）→ 2025（6/5/4）→ 2026（9/7/6），全顺势最大净仓 12→15→22手

**NQ 1h 参数优化结论（2026-06-20）**：
- ADX threshold：20 / 25 / 30 三档对比，**ADX=30 最优**（Sharpe 0.80，MaxDD -2.7%），不要降低
- TP 目标（atr_tp1_mult / atr_tp2_mult）：放大 TP 目标会降低胜率，Sharpe 反而下降，**保持 ×1.0 / ×2.0**
- tp1_portion：对回测结果无影响（n_contracts=1 时分批平仓整数取整为 0），**保持 34%**
- ut_key：1.0→3.0 虽然胜率和总收益提升，但 MaxDD 翻倍（-5.6%），**保持 ut_key=1.0**
- **结论：当前参数已是最优，不改任何参数**
- 当前 Sharpe=0.80，胜率 62.1%，Profit Factor 2.44，MaxDD -2.7%

## 分析工具

均支持 `--since` / `--config` 参数，在 `/tmp/` 目录下：

| 工具 | 用途 |
|---|---|
| `risk_sizing.py` | ATR 定仓重算各 TF 仓位和 PnL |
| `combined_portfolio.py` | 三周期叠加组合分析 |
| `recent_trades.py` | 各 TF 交易明细 |
| `compare_staged_tp.py` | staged_tp True/False 三场景对比 |
| `compare_pyramid_risk.py` | pyramid 风险参数对比 |
| `pyramid_sizing.py` | 全历史复利回测（$100k/$200k 月度调仓） |

## 最近实现（2026-06-20）

**死代码清理**（commit 52fd766）：
- `use_atr_exit` / `atr_sl_mult` / `atr_tp_mult` 在 `staged_tp=True` 模式下完全无效
- `staged_tp=True` 时止损走 `utTS`（UT Bot 动态追踪线），固定 ATR 止损路径从未执行
- 已注释掉 `strategy.py` 中的相关类变量和所有 `if self.use_atr_exit:` 块
- `live_engine.py` 中注释掉 `_CaptureStrategy.use_atr_exit` / `.atr_sl_mult` 赋值
- `atr_sl_mult` 在 `live_engine.py` 中仍用于仓位大小计算（`calc_n_contracts`），保留

**实盘交易日志**（commit 3eb7ee5）：
- 每次实盘成交自动追加一行到 `logs/trades.csv`
- 格式：`time,instrument,tf,action,qty,price`
- 纯旁路，静默失败，不影响任何交易逻辑

**weekly_review.py**（standalone 复盘脚本）：
- 每周五 14:05 ET 自动触发，连接 IB 拉历史数据，运行回测，提取近 7 天成交
- 合并 staged TP 拆分记录（同 EntryTime + 方向 → 一笔），正确计算胜率
- 检测 near-miss（bull 够但 ADX 拦截、bull 差 1 分）
- 自动生成优化建议（如多次 ADX 拦截则建议回测降低阈值）
- 用法：`python weekly_review.py --port 4001 --send`

## 最近实现（2026-06-17）

**连接稳定性修复**：
- `do_connect()` 连接失败立刻发 Telegram 告警（首次失败时，不重复刷屏）
- Error 2105（HMDS ushmds 断连）加入重连触发列表，60s 冷却防止循环
- `fetch_bars()` 加 3 次重试，失败后抛 `RuntimeError` 触发外层重连

**每小时心跳 Telegram**：
- 整点发送：连接状态 + 各品种净仓 + 账户净值
- 确保系统异常能在 ≤1 小时内被发现

## 最近实现（2026-06-15）

**Telegram 配置完成**：
- bot: `@quantrift_index_future_bot`
- token 存储：pm2 环境变量（`/Users/congrenhan/.pm2/dump.pm2`），不进 git，Mac Studio 重启后自动恢复
- config.yaml 里 token/chat_id 保持空，repo 公开安全
- 配置命令：`PATH=/opt/homebrew/bin:$PATH TG_TOKEN='...' TG_CHAT_ID='...' pm2 start ib-bot --update-env && pm2 save`
- 已验证：连接成功、下单告警均可正常发送

## 最近实现（2026-06-14）

**Telegram 告警**（commit ebbca09）：
- `tg_alert()` 非阻塞线程发送，静默失败
- 触发时机：IB 连接成功、持仓不一致、Error 1100、下单成功、引擎崩溃
- 配置：`config.yaml` 的 `telegram.token/chat_id`，或环境变量 `TG_TOKEN`/`TG_CHAT_ID`

**重连后状态对齐**（commit dea0de7）：
- `reconcile_state()`：IB=0 时全清所有 TF；IB≠0 时绝对值最大的 TF 吸收差额
- `do_connect()` 检测不一致时自动修正状态文件并发 Telegram 告警
- 同时新增 `reqAllOpenOrders()` 日志检查

**周度复盘**（commit 699b191）：
- 每周五 14:05 ET 自动发 Telegram，内容：净值 + 各 TF 持仓 + 净仓汇总

## TODO

详见 `TASK.md`。
