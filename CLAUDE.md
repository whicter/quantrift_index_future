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

- **核心文件**：`live_engine.py`（实盘引擎）、`strategy.py`（ConfluenceStrategy）、`indicators.py`（compute_signals）、`config.yaml`（参数）
- **品种**：NQ（MNQ，$2/点）+ ES（MES，$5/点），CME，用 `ContFuture` 自动滚近月
- **当前合约**：MNQU6 / MESU6（9月，到期 2026-09-18）
- **周期**：1h / 4h / 1d，三周期独立信号，仓位可叠加
- **开市时间**：周日 18:00 ET（北京时间周一 06:00）

**仓位模式：**
- 净值 < $60k：统一 1% 风险/笔
- 净值 ≥ $60k：pyramid 模式（1h:1.0%，4h:1.5%，1d:4.5%）
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
- Pyramid 1h 0.4%→1.0%：全历史 $100k 从 +$146k→+$183k（commit dd39dc0）

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

### 0. ~~Telegram 配置~~（已完成 2026-06-15）

bot `@quantrift_index_future_bot` 已配置并验证，config.yaml 中已填入 token/chat_id。

### 1. IBC 自动登录（优先级：中）

**目标**：彻底消除每周手动登录 IB Gateway 的需求。

```bash
# Mac Studio 上安装
curl -L https://github.com/IbcAlpha/IBC/releases/latest/download/IBCMacos-3.x.x.zip -o IBC.zip
unzip IBC.zip -d ~/IBC
cp ~/IBC/config.ini.example ~/IBC/config.ini
# 编辑 config.ini：填 IbLoginId、IbPassword、TradingMode=live
```

更新 `com.quantrift.start.plist` 改为用 IBC 启动 Gateway（替代手动启动）。

### 3. Docker 化（优先级：低，稳定后再做）

**第一步**：只 Docker 化 IB Gateway + IBC（使用 `ghcr.io/gnzsnz/ib-gateway:stable`）

```bash
docker run -d --name ib-gateway --restart always \
  -p 4001:4001 -p 5900:5900 \
  -e TWS_USERID=账号 -e TWS_PASSWORD=密码 \
  -e TRADING_MODE=live \
  ghcr.io/gnzsnz/ib-gateway:stable
```

**第二步**：bot 也 Docker 化，用 docker-compose 统一管理：

```yaml
services:
  ib-gateway:
    image: ghcr.io/gnzsnz/ib-gateway:stable
    restart: always
    environment:
      TWS_USERID: "${IB_USER}"
      TWS_PASSWORD: "${IB_PASSWORD}"
      TRADING_MODE: live
    ports:
      - "4001:4001"
      - "5900:5900"

  bot:
    build: .
    restart: always
    depends_on: [ib-gateway]
    environment:
      IB_HOST: ib-gateway
      IB_PORT: 4001
      TELEGRAM_TOKEN: "${TELEGRAM_TOKEN}"
      TELEGRAM_CHAT_ID: "${TELEGRAM_CHAT_ID}"
    volumes:
      - ./live_state.json:/app/live_state.json
      - ./logs:/app/logs
```
