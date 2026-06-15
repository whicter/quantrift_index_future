# 实盘连接操作手册

## 1. IB 账户要求

### 必须开通的权限
| 权限 | 申请路径 |
|------|---------|
| Futures Trading（期货交易）| Account Management → Trading Permissions → Futures |
| CME Market Data（行情数据）| Account Management → Market Data → CME Real-Time |
| HMDS Historical Data | 随 CME 数据包自动开通 |

### 合约规格
| 品种 | 代码 | 交易所 | 乘数 | 每点价值 | 当前合约 |
|------|------|--------|------|---------|---------|
| Micro NQ | MNQ | CME | 2 | $2 | MNQU6（到期 2026-09-19）|
| Micro ES | MES | CME | 5 | $5 | MESU6（到期 2026-09-19）|

> ⚠️ **合约到期自动滚动**：live_engine.py 使用 `ContFuture`（前月连续），到期前 IB 自动切换到下一个合约，无需手动操作。但持仓会自动平仓再开仓，会产生一笔滑点。建议在到期前 3 天手动滚仓。

---

## 2. 软件安装

### 选项 A：TWS（适合同时手动操作）
- 下载：https://www.interactivebrokers.com/en/trading/tws.php
- 模拟盘端口：**7497**
- 实盘端口：**7496**

### 选项 B：IB Gateway（推荐用于纯自动化）
- 下载：https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
- 模拟盘端口：**4002**
- 实盘端口：**4001**
- 资源占用约 100MB RAM，适合 24 小时运行

---

## 3. TWS / IB Gateway API 设置

打开 TWS → Edit → Global Configuration → API → Settings：

```
☑  Enable ActiveX and Socket Clients          ← 必须勾选
☐  Read-Only API                               ← 必须取消勾选（否则无法下单）
☑  Allow connections from localhost only        ← 保持勾选（安全）
   Socket port: 7497                           ← 模拟盘；实盘改为 7496
   Master API client ID: 0
☑  Expose entire trading schedule visible in TWS
```

---

## 4. 当前实盘配置

现在使用 **IB Gateway**（非 TWS），运行于 Mac Studio，实盘端口 **4001**。
账户：U17682857，净值约 $32,365（2026-06-14）。

> ⚠️ **切换模拟盘时**：
> - 改为 `--port 4002`，确认 IB Gateway 已切换到 paper trading 模式
> - `live_state.json` 中的 signed_contracts 须与账户持仓一致
> - 如不一致，先运行 `--run-now --dry-run` 确认信号，再手动同步持仓

---

## 5. 仓位大小与风险模式

引擎使用 ATR 风险定仓，公式：

```
手数 = (账户净值 × risk_pct%) / (ATR × atr_sl_mult × 合约乘数)
最小 1 手
```

**两种模式（自动切换）：**

| 净值 | 模式 | NQ risk | ES risk |
|------|------|---------|---------|
| < $60,000 | 统一模式 | 1.0% | 1.0% |
| ≥ $60,000 | Pyramid 模式 | 1h:1.0% / 4h:1.5% / 1d:4.5% | 1h:1.0% / 4h:1.5% / 1d:4.5% |

**当前账户（~$32,365）典型仓位（统一 1.0%）：**

| 品种 | 周期 | 典型ATR | 止损距离 | 止损金额 | 手数 |
|------|------|---------|---------|---------|------|
| MNQ | 1H | 80pt | 120pt | $240 | 1手 |
| MNQ | 4H | 200pt | 300pt | $600 | 1手 |
| MNQ | 1D | 400pt | 600pt | $1,200 | 1手 |
| MES | 1H | 20pt | 30pt | $150 | 2手 |
| MES | 4H | 50pt | 75pt | $375 | 1手 |
| MES | 1D | 100pt | 150pt | $750 | 1手 |

**分批止盈（Staged TP，已启用）：**
- TP1：ATR × 1.0，平掉 34% 仓位
- TP2：ATR × 2.0，平掉剩余仓位

---

## 6. 启动命令

引擎由 pm2 管理，通常无需手动启动。常用操作：

```bash
# 查看引擎状态
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 status"

# 查看日志（实时）
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 logs ib-bot --lines 50"

# 重启引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 restart ib-bot"

# 停止引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 stop ib-bot"

# 干跑测试（不下单，直接运行，绕过 pm2）
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 live_engine.py --port 4001 --run-now --dry-run"

# 只测 NQ
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 live_engine.py --port 4001 --run-now --dry-run --instrument NQ"
```

---

## 7. 每日维护窗口

IB 服务器每天约 **23:00–23:45 ET** 重启，连接会断开。

**现在已全自动处理，无需手动干预：**
- IB Gateway 设置了 **Auto-Restart ON**，维护后自动重启 Gateway
- 引擎内 `do_connect()` 检测到 Error 1100/1101 后自动重连
- pm2 负责引擎进程崩溃后自动拉起
- launchd 负责 pm2 开机自启

**IB Gateway Auto-Restart 配置**：
`Configure → Settings → Auto Restart → Enable`（每周日 1AM ET 重启一次，需手动重登录）

**Telegram 告警**（需配置 token/chat_id）：
- IB 连接成功 / 持仓不一致 / 自动对齐
- Error 1100/1101（连接断开）
- 每笔下单成功
- 引擎崩溃重启
- 每周五 14:05 ET 发送周度复盘（净值 + 持仓汇总）

配置方式：编辑 `config.yaml` 的 `telegram.token` 和 `telegram.chat_id`，或设置环境变量 `TG_TOKEN`/`TG_CHAT_ID`。

---

## 8. 合约滚动（每季度）

MNQ/MES 季度到期（3月、6月、9月、12月第三个周五）。

**操作流程**：
1. 到期前 3 天，手动在 TWS 平仓当前合约
2. 同时将 `live_state.json` 中对应 `signed_contracts` 清零
3. 引擎下次触发时会以新合约重新开仓

或等 ContFuture 自动滚动（有一笔额外滑点）。

---

## 9. 状态文件说明

`live_state.json` 记录各品种各周期的持仓意图：

```json
{
  "NQ": {
    "1h": {"signed_contracts": 0},
    "4h": {"signed_contracts": -1, "last_update": "2026-06-08T20:57:17"},
    "1d": {"signed_contracts": 0}
  },
  "ES": {
    "1h": {"signed_contracts": 0},
    "4h": {"signed_contracts": 0},
    "1d": {"signed_contracts": 0}
  }
}
```

- 正数 = 多头手数，负数 = 空头手数
- 如果手动在 TWS 修改了持仓，必须同步修改此文件，否则引擎会下错方向的订单

**重连后自动对齐**：引擎重连时会对比 IB 实际净仓和状态文件之和。如有不一致，自动以 IB 为准修正状态文件，并发 Telegram 告警。IB=0 时全清所有 TF；IB≠0 时绝对值最大的 TF 吸收差额。

---

## 10. 紧急平仓

如需立即平掉所有仓位：
1. 在 IB Gateway / TWS 中手动平仓（最快最安全）
2. 手动编辑 `live_state.json`，将所有 `signed_contracts` 改为 0
3. 停止引擎：`ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 stop ib-bot"`

---

## 11. Mac Studio 专机部署架构

### 架构说明

```
开发 Mac（cohan）             GitHub              Mac Studio（24小时运行）
────────────────────────    ─────────────────   ──────────────────────────
改代码 → git push ──────>   仓库（代码）──────>  git pull → pm2 restart
Claude Code（远程管理）                           IB Gateway（Auto-Restart ON）
SSH via Cloudflare Tunnel ──────────────────>   pm2 → live_engine.py
                                                  └─ reconnect loop（自动重连）
```

**守护层次（由外到内）：**
1. launchd — 周日 15:00 PST 开市 / 周五 14:00 PST 收市
2. pm2 — 引擎崩溃自动拉起，Mac Studio 重启后自动恢复
3. `__main__` crash loop — `main()` 崩溃 30s 后重启
4. `do_connect()` reconnect loop — 断线无限重试
5. Error 1100/1101/2110 事件 — 提前触发重连

**两台电脑不能同时跑引擎**：IB 账户只允许一个 Gateway 实例，第二台登录会把第一台踢下线。

---

### 第零步：Cloudflare Tunnel 网络打通（一次性配置）

Cloudflare Tunnel 通过 HTTPS 443 端口建立隧道，适合公司电脑无法使用 Tailscale 的场景。

**前提条件：**
- 一个域名（在 Cloudflare Registrar 购买，DNS 自动由 Cloudflare 管理）
- 本例使用 `quantrift.io`，子域名 `mac-studio.quantrift.io`

**Mac Studio 上安装配置（一次性）：**

```bash
# 1. 安装 cloudflared
brew install cloudflare/cloudflare/cloudflared

# 2. 登录 Cloudflare（浏览器弹出，选你的域名 zone）
cloudflared tunnel login

# 3. 创建隧道
cloudflared tunnel create mac-studio-ssh

# 4. 绑定子域名（自动在 Cloudflare DNS 创建 CNAME 记录）
cloudflared tunnel route dns mac-studio-ssh mac-studio.quantrift.io

# 5. 创建配置文件（tunnel ID 替换为实际值）
cat > ~/.cloudflared/config.yml << 'EOF'
tunnel: mac-studio-ssh
credentials-file: /Users/congrenhan/.cloudflared/c70b3203-4afe-4b33-899b-cb35224d8d16.json

ingress:
  - hostname: mac-studio.quantrift.io
    service: ssh://localhost:22
  - service: http_status:404
EOF

# 6. 安装为系统服务（开机自启）
sudo cloudflared service install
sudo launchctl start com.cloudflare.cloudflared
```

**Cloudflare 控制台配置：**

1. 登录 cloudflare.com → Zero Trust → Access → Applications → Add application → Self-hosted
2. 填写：Name = `Mac Studio SSH`，Subdomain = `mac-studio`，Domain = `quantrift.io`
3. 创建 Policy：Action = **Bypass**（跳过认证，SSH key 负责安全）
4. Save application

**Mac Studio SSH 远程访问权限：**

```
System Settings → General → Sharing → Remote Login → 点 i 按钮
→ 开启 "Allow full disk access for remote users"
```

**公司电脑配置（一次性）：**

```bash
# 安装 cloudflared
brew install cloudflare/cloudflare/cloudflared
```

编辑 `~/.ssh/config`，加入：

```
Host mac-studio
  HostName mac-studio.quantrift.io
  User congrenhan
  IdentityFile ~/.ssh/id_ed25519
  IdentitiesOnly yes
  ProxyCommand /opt/homebrew/bin/cloudflared access ssh --hostname %h
```

**免密登录配置（一次性）：**

```bash
# 把公司电脑公钥复制到 Mac Studio
ssh-copy-id mac-studio
```

---

### 第一步：初始化 Git 仓库（开发 Mac 上操作）

```bash
cd /Users/cohan/Documents/confluence_backtest

# 初始化（如果还没有）
git init
git remote add origin https://github.com/你的用户名/confluence_backtest.git

# 创建 .gitignore（重要！不上传运行时文件）
cat > .gitignore << 'GITIGNORE'
# 运行时状态（机器独立，不上传）
live_state.json
logs/

# 大文件（可选，如果 data/ 超过 100MB）
data/

# Python 缓存
__pycache__/
*.pyc
.pyenv/
*.egg-info/

# 回测结果
results/
GITIGNORE

git add .
git commit -m "初始化交易系统"
git push -u origin main
```

---

### 第二步：Mac Studio 初次部署（从开发 Mac 远程操作）

Cloudflare Tunnel 打通后，所有操作可以直接在开发 Mac 上执行，无需坐到 Mac Studio 前。

**方式 A：通过 Git 克隆（推荐）**

```bash
ssh mac-studio << 'EOF'
cd ~
git clone https://github.com/你的用户名/confluence_backtest.git
cd confluence_backtest
pip install backtesting ib_insync pandas pyyaml
mkdir -p logs
EOF
```

**方式 B：直接复制整个项目（含 data/ 目录）**

```bash
# 从开发 Mac 把整个项目 scp 到 Mac Studio
scp -r /Users/cohan/Documents/confluence_backtest mac-studio:~/

# 单独复制 data/ 目录（如果较大，Git 里没有）
scp -r /Users/cohan/Documents/confluence_backtest/data/ mac-studio:~/confluence_backtest/
```

**验证部署：**

```bash
# 确认文件已到位
ssh mac-studio "ls ~/confluence_backtest/"

# 确认 Python 环境
ssh mac-studio "cd ~/confluence_backtest && python -c 'import backtesting, ib_insync; print(\"OK\")'"
```

---

### 第三步：代码更新流程（日常操作）

**开发 Mac 端（改完代码后）：**
```bash
cd /Users/cohan/Documents/quantrift_index_future
git add strategy.py indicators.py live_engine.py config.yaml
git commit -m "调整参数: 4H min_score 改为 6"
ssh -A mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && git pull"
```

**部署更新（pm2 管理）：**
```bash
# 查看当前持仓（更新前确认）
ssh mac-studio "cat /Users/congrenhan/Documents/quantrift_index_future/live_state.json"

# 重启引擎（pm2 自动用新代码）
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 restart ib-bot"
```

> ⚠️ **安全建议**：尽量在两个 Bar 收盘之间的空窗期更新，避免在信号即将触发时重启。

---

### 第四步：Mac Studio 常驻配置（已完成）

以下配置均已完成，无需重复操作：

| 组件 | 配置文件 | 作用 |
|------|---------|------|
| IB Gateway Auto-Restart | IB Gateway GUI | 每周日 1AM ET 自动重启 |
| Cloudflare Tunnel | `com.cloudflare.cloudflared`（launchd） | SSH 隧道开机自启 |
| pm2 开机自启 | `~/Library/LaunchAgents/pm2.congrenhan.plist` | Mac Studio 重启后自动拉起 pm2 |
| 引擎开市 | `~/Library/LaunchAgents/com.quantrift.start.plist` | 周日 15:00 PST 自动启动 |
| 引擎收市 | `~/Library/LaunchAgents/com.quantrift.stop.plist` | 周五 14:00 PST 自动停止 |

---

### 远程监控与管理

所有命令在**开发 Mac（cohan）** 上执行：

```bash
# 查看引擎状态
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 status"

# 查看实时日志
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 logs ib-bot --lines 100"

# 查看当前持仓
ssh mac-studio "cat /Users/congrenhan/Documents/quantrift_index_future/live_state.json"

# 重启引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 restart ib-bot"

# 停止引擎
ssh mac-studio "PATH=/opt/homebrew/bin:$PATH pm2 stop ib-bot"

# 干跑测试（不下单）
ssh mac-studio "cd /Users/congrenhan/Documents/quantrift_index_future && /opt/homebrew/bin/python3.11 live_engine.py --port 4001 --run-now --dry-run"
```

