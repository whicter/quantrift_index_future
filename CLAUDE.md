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
- **干跑（--dry-run）绝对不能用 clientId 20**（主bot用20，会踢掉主bot）。干跑必须用不同 clientId，例如 `--client-id 99`

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

**最终策略配置（NQ）**：
- NQ 1D：`use_pattern_exit:false`，`use_vix_filter:true`，`use_vix_entry:true`
- NQ 4H/1H：`use_pattern_exit:true`（仅空头），`use_vix_filter:false`
- VIX > 40 触发：1D 空头止盈 + pattern 信号确认后抄底做多（需 ≥2 信号共振）

**最终策略配置（ES）**：
- ES 1D：`use_pattern_exit:true`（仅空头），`use_vix_filter:true`，`use_vix_entry:true`（VIX>40 平空+抄底）
- ES 4H：`use_pattern_exit:false`，`use_vix_filter:false`（基准 Sharpe=1.064 已最优，不干预）
- ES 1D 配置效果：2025年 +$31,164 vs 无VIX +$8,478；整体 Sharpe 0.637→0.670，MaxDD -9.77%→-8.21%

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

## Bug 修复（2026-07-01）

**全面审查发现并修复 6 个 bug**（commits 1686f61、84688c6、d9c1e6c）：

**🔴 Spread 双触发**（spread_engine.py）：
- `is_bar_close()` 窗口 30s，处理仅需 ~20s，处理完后主循环再次触发同一根 bar
- 修复：加 `_last_bar_time` 追踪，每根 bar 只处理一次
- 顺带：空仓期每 :30 分调用 `save_state()`，防健康检查 stale 误报

**🔴 Gap bracket order parentId=0**（gap_engine.py）：
- `MarketOrder` 初始 `orderId=0`，TP/SL 在父单 `placeOrder` 之前设置 `parentId` → 永远是 0
- 结果：TP/SL 是独立悬空单，不与父单绑定，入场后 TP/SL 无法正常保护
- 修复：先 `placeOrder(parent)`，`ib.sleep(1)` 等待 orderId 分配，再设置子单 `parentId`

**🔴 MR/Range/Gap 持仓相互干扰**（nq_mr_engine.py、nq_range_engine.py）：
- 三个 bot 共用同一合约，`get_ib_position()` 返回账户所有 MNQ 持仓之和，无法区分谁的
- 重连 reconcile 时：NQ MR 有 +2 → NQ Range 重连看到 +2，强制将自己状态改为 +2，然后策略判断"应空仓"→ SELL 2 MNQ 关掉 NQ MR 的仓位
- 修复1：Gap 合约改为**第三季**（MNQH7，Mar 2027），与 MR/Range 的 MNQZ6 物理隔离
- 修复2：reconcile 逻辑单向化：只在 `state>0 且 IB=0`（持仓确实丢失）时自动修正；IB>state 时仅告警不修改

**🟡 Gap Telegram 失效**（gap_engine.py）：
- 从 `config.yaml` 读 token（值为空），其他 bot 均从环境变量 `TG_TOKEN` 读
- 修复：env var 优先，config.yaml 作回退

**🟡 Error 322 reqAccountSummary 噪音**（nq_mr/nq_range/gap_engine.py）：
- `get_account_equity()` 每次调用 `reqAccountSummary()` 但从不取消，产生 Error 322
- ib_insync 连接后自动维护 `accountValues()` 缓存，无需重复订阅
- 修复：去掉 `reqAccountSummary()` + `ib.sleep(2)`，直接读 `accountValues()` 缓存

**合约隔离现状**：

| Bot | 合约 |
|-----|------|
| ib-bot（趋势）| MNQU6 / MESU6（当季，ContFuture）|
| ib-bot-mr（ES MR）| MESZ6（次季）|
| ib-bot-nq-mr（NQ MR）| MNQZ6（次季）|
| ib-bot-nq-range（NQ Range）| MNQZ6（次季）|
| ib-bot-gap（Gap）| **MNQH7（第三季）** |
| ib-bot-spread（Spread）| MNQZ6 + MESZ6（次季）|

---

## 最近实现（2026-06-23）

**mr_engine.py 断连修复**（commit eff0131）：
- `except Exception` 分支缺少 `needs_reconnect[0] = True`，导致 `ConnectionError: Not connected` 不触发重连
- 进程存活但 IB 连接已死，bot 持续运行但每小时失败一次，状态文件 2.4 天未更新
- 任何未预期错误现在都触发重连
- **教训：进程存活 ≠ 连接健康**。`_mr_status()` 用 pgrep 判断 ✅ 是错的，需要同时检查状态文件新鲜度

**Gateway 重连逻辑重构**（live_engine.py，commit 4ceb8d8）：

- **Error 326（clientId 冲突）不计入 fail_count**：326 = Gateway 正常只是 clientId 被占，计入会误杀好的 Gateway
  - 连续 30 次（5分钟）→ 自动 `kill -9` 所有其他 live_engine.py 进程，发 Telegram 通知
- **Gateway 重启阈值：5 → 30 次（50秒 → 5分钟）**：减少误重启，避免短暂断连被当 Gateway 故障
- **2小时冷却**：两次 restart_gateway.sh 调用之间最少间隔 7200s
- **新增 `--client-id` 参数**：干跑必须用 `--client-id 99`，避免踢掉主 bot

**restart_gateway.sh 修复**（commit 4ceb8d8）：
- `pkill -f "IbcGateway"` → `pkill -9 -f "IbcGateway"`（SIGKILL）：SIGTERM 会删 autorestart 文件导致每次重启要 2FA
- **移除末尾 `exec ibcstart.sh`**：这是 23:29 和 23:48 两次二次断线的真实根因
  - ibcstart.sh loop 与 launchd IBC 并发，每 18 分钟争抢 Gateway GUI 一次
  - 正确做法：只 kill Gateway，让 launchd `com.quantrift.ibc.plist` 自动重启

**Gateway 架构澄清**（2026-06-23 排查中发现）：
- `com.quantrift.ibc.plist`（`KeepAlive: true`）由 launchd 管理，Gateway 退出后自动拉起
- 不要在 `restart_gateway.sh` 里 `exec ibcstart.sh`——会产生两个并发 IBC 争抢 Gateway GUI，导致 Gateway 不稳定
- 正确做法：只 kill Gateway，让 launchd 的 `com.quantrift.ibc.plist` 自动重启

**周日夜间三次重启根因（2026-06-22 23:11 PDT）**：
- 有人跑了 `live_engine.py --dry-run` 未指定 `--client-id`，默认使用 clientId 20
- 干跑因 Error 326（clientId 占用）连续失败 5 次，旧代码触发 restart_gateway.sh，杀掉了正常运行的 Gateway
- autorestart 文件被 SIGTERM 删除，后续每次重启都需要 2FA
- restart_gateway.sh 的 ibcstart.sh 循环与 launchd IBC 并发，每隔 18 分钟干扰一次 Gateway → 23:29、23:48 两次再断线

**mr_engine.py 监控完善**（commit 740e396）：
- 每次 bar 处理成功（`else` 分支）后调用 `save_state()`，更新状态文件 mtime
- 空仓期间 `save_state()` 从不被调用 → 状态文件永远 stale → `_mr_status()` 健康检查形同虚设
- 修复后：stale > 2h 才真正意味着 bot 断连，不再误报

**`_mr_status()` pgrep 路径修复**（commit 2f76f52）：
- `subprocess.run(["pgrep", ...])` → `subprocess.run(["/usr/bin/pgrep", ...])`
- pm2 启动进程不走 login shell，PATH 受限，`/usr/bin` 不在其中
- 导致每次心跳/连接消息都抛 `FileNotFoundError`，ib-bot-mr 状态一直显示"状态读取失败"
- **教训：pm2 环境内调系统命令必须用绝对路径**，不能依赖 PATH

## 最近实现（2026-06-21，续）

**VIX 数据自动更新**（commit 8929c80）：
- `live_engine.py` 新增 `update_vix_csv(ib)`，每次连接 IB 成功后自动更新
  - 主链路：`yfinance ^VIX`（免费，无需 IB 订阅）
  - 备用：IB VIX IND 合约（需 CBOE 延迟/实时权限）
  - 两者均失败：静默，沿用旧数据，不影响引擎
- `indicators.py`：VIX 路径改为 `data/VIX_1d.csv`（通用名），旧带日期文件名作回退
- 初始化：`data/VIX_1d.csv`（1878行，至 2026-06-19）

**重连逻辑修复**（commit 6a29271）：
- 修复 Error 2110/2105 触发重连导致的死循环：
  - 2110（TWS→IBKR 断连，会自动恢复）：移除重连触发，仅记日志
  - 2105（HMDS 断连）：移除重连触发，fetch_bars 3次重试兜底
  - 1100/1101：保留，新增5分钟 Telegram 冷却防刷屏
- 原死循环：1100 → 重连 → 收到 2110 → 立刻再重连 → 无限循环

**Gateway 自动重启**（commit e6de53c）：
- `live_engine.py`：连续5次 do_connect 失败 → 调用 `_restart_gateway()`
  - 15分钟冷却防止频繁重启
  - kill Gateway + IBC → 重启 IBC（自动登录）→ 等90秒
- `restart_gateway.sh`：kill ibcstart.sh + IbcGateway，执行 ibcstart.sh 重启

**注意**：周末 IB 有时强制2FA，IBC 存了账号密码但拦不住 IBKR 服务器主动触发的验证，需手动处理一次。

## 最近实现（2026-06-21）

**ES 子系统策略更新**（commit 350d98e）：
- ES 1D：加 VIX 过滤（`use_vix_filter/entry: true`，VIX>40 平空+抄底），Sharpe 0.637→0.670，2025年 +$8k→+$31k
- ES 4H：关闭 pattern exit（`use_pattern_exit: false`），保持基准 Sharpe=1.064（pattern exit 在 2025年把好空头砍掉 -$4,746）
- ES 4H pattern exit 有害的原因：ES 4H 趋势质量本身极高，不需要额外过滤

**联合 bot 状态 Telegram 通知**（commit 47c4e02）：
- `live_engine.py` 新增 `_mr_status()` 辅助函数，读 `es_mr/mr_state.json` + 检查进程存活
- 连接消息：改为"ib-bot（趋势 NQ+ES）已连接"，附 ib-bot-mr 状态一行
- 整点心跳：每小时发送两个 bot 联合状态（连接状态 + NQ/ES 各TF持仓 + MR 持仓 + 净值）
- 心跳格式示例：
  ```
  💓 整点心跳 06-21 21:00 ET
  💰 账户净值: $33,154
  ─────────────────
  ✅ ib-bot（趋势 NQ+ES）
    NQ: 1H+0 / 4H+0 / 1D+0  净仓+0手
    ES: 4H+0 / 1D+0  净仓+0手
  ib-bot-mr（ES MR）✅  MESZ6 空仓
  ```

**ib-bot-mr 重新启动**：
- 之前因昨晚 dry-run 进程未退出占用 clientId 21，已清理并重启
- 当前合约：MESZ6（12月，次季合约），空仓待机

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


---

## 新策略回测结论（2026-06-30）

### NQ MR（nq_mr）— ✅ 已上线实盘

**策略**：MNQ 1H 均值回归，只做多，RSI+BB+VWAP 三合一
**clientId**：22，pm2 进程：ib-bot-nq-mr
**最优参数**：rsi_os=30, tp_atr_mult=1.5, max_bars=12, adx_threshold=25

| 指标 | 值 |
|------|---|
| 笔数（2年）| 33（约1.2笔/月）|
| 胜率 | 48.5% |
| Sharpe | 1.015 |
| MaxDD | -0.36% |
| Profit Factor | 1.81 |

IS/OOS：无过拟合（OOS Sharpe 1.000）
**实盘状态**：pm2 ib-bot-nq-mr（id=5）已注册，2026-06-30 上线，clientId=22

---

### NQ Range（nq_range）— ✅ 已上线实盘

**策略**：MNQ 1H 区间交易，CI+ADX 双过滤，Low<BB下轨买入，BB中轨止盈
**策略文件**：nq_range/strategy_nq_range.py
**clientId**：23，pm2 进程：ib-bot-nq-range
**最优参数**：ci_threshold=55, adx_threshold=25, rsi_entry=45, sl_mult=1.5
**入场修正**：用 Low<BB_lower（非Close），捕获bar内极低点

| 指标 | 值 |
|------|---|
| 笔数（2年）| 88（约3.3笔/月）|
| 胜率 | 69.3% |
| Sharpe | 1.197 |
| MaxDD | -0.702% |
| Profit Factor | 1.97 |
| IS Sharpe | 1.709 |
| OOS Sharpe | 0.656（WR反升至73.2%）|

注：OOS Sharpe 偏低（0.656），WR 稳定提升，MaxDD 控制好，值得实盘。
**15min 回测**：IS Sharpe -1.685，不适合（1H策略无需降级）。
**实盘状态**：pm2 ib-bot-nq-range（id=7）已注册，2026-06-30 上线，clientId=23

---

### NQ/ES 价差（nq_es_spread）— ✅ 已上线实盘

**策略**：1MNQ + 2MES 收益率价差 Z-score，市场中性
**注意**：协整检验 p=0.255（不协整），但短期半衰期 0.8 bar 可交易
**clientId**：25，pm2 进程：ib-bot-spread
**最优参数**：z_entry=2.0, z_exit=0.5, z_stop=3.5, window=240h, max_bars=8

| 指标 | 全样本 | IS | OOS |
|------|--------|-----|-----|
| 笔数（2年）| 493（约20笔/月）| 248 | 245 |
| 胜率 | 86.2% | 88.7% | 83.7% |
| 总PnL | $140,652 | $77,952 | $62,700 |
| PF | 14.34 | 16.06 | 12.68 |
| MaxDD | $-2,330 | — | — |
| 盈利月 | 24/24 | — | — |

**实盘状态**：pm2 ib-bot-spread（id=8）已注册，2026-07-01 上线，clientId=25

---

### NQ 隔夜跳空回归（gap_reversion）— ✅ 已上线实盘

**逻辑**：NQ期货每天16:00-17:00 ET有1小时维护中断，重开时若跳空>阈值则反向入场等gap填满
**历史fill率**：100%（gap>10/25/50/75pts均在当日session内填满）
**clientId**：24，pm2 进程：ib-bot-gap
**最优参数**：gap_thresh=75pts, sl_mult=5.0, vix_max=30, max_bars=12

| 指标 | 值 |
|------|----|
| 笔数（2年）| 57（2.4笔/月）|
| 胜率 | 91.2% |
| Sharpe | 2.273 |
| MaxDD | -550pts（MNQ $1,100）|
| IS/OOS | 2.469 / 2.257（8%衰减，极稳定）|

**实盘状态**：pm2 ib-bot-gap（id=6）已注册，2026-06-30 上线，clientId=24

---

### 三策略适用市场

| 市场状态 | 适用策略 |
|---------|---------|
| 趋势行情（ADX>25）| 趋势引擎（ib-bot）|
| 极端超卖（RSI<30+BB+VWAP）| NQ MR（ib-bot-nq-mr）|
| 横盘震荡（CI>55，ADX<25）| NQ 区间交易（ib-bot-nq-range）|
| 每日维护中断跳空（>75pts）| NQ 跳空回归（ib-bot-gap）|
| NQ/ES 短期收益率背离（z>2）| NQ/ES 价差（ib-bot-spread）|
