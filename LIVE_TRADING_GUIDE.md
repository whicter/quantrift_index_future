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
| Micro NQ | MNQ | CME | 2 | $2 | MNQM6（到期 2026-06-18）|
| Micro ES | MES | CME | 5 | $5 | MESM6（到期 2026-06-20）|

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

## 4. 切换到实盘

### 步骤
1. 确认 TWS 已登录**实盘账户**（不是 paper trading）
2. 在 TWS 中确认端口为 **7496**
3. 修改启动命令，加 `--port 7496`：

```bash
# 模拟盘（当前）
python live_engine.py

# 实盘（切换后）
python live_engine.py --port 7496
```

> ⚠️ **切换前必须检查**：
> - `live_state.json` 中的 signed_contracts 是否与实盘账户当前持仓一致
> - 如不一致，先运行 `--run-now --dry-run` 确认信号，再手动同步持仓

---

## 5. 仓位大小（$25,000 账户）

引擎使用 ATR 风险定仓，公式：

```
手数 = (账户净值 × risk_pct%) / (ATR × atr_sl_mult × 合约乘数)
```

**当前配置（risk_pct = 1.0%）的典型仓位：**

| 品种 | 周期 | 典型ATR | 止损距离 | 止损金额 | 手数 |
|------|------|---------|---------|---------|------|
| MNQ | 1H | 80pt | 120pt | $240 | 1手 |
| MNQ | 4H | 200pt | 300pt | $600 | 1手 |
| MNQ | 1D | 400pt | 600pt | $1,200 | 1手 |
| MES | 1H | 20pt | 30pt | $150 | 2手 |
| MES | 4H | 50pt | 75pt | $375 | 1手 |
| MES | 1D | 100pt | 150pt | $750 | 1手 |

**最大总风险**（NQ+ES 全开仓）：约 $5,000（账户的 20%）

如需更保守，将 `config.yaml` 中 `risk_pct: 1.0` 改为 `0.5`。

---

## 6. 启动命令

```bash
# 测试（干跑，不下单）
python live_engine.py --run-now --dry-run

# 只跑 NQ 测试
python live_engine.py --run-now --dry-run --instrument NQ

# 实盘启动（后台运行，日志写文件）
nohup python live_engine.py --port 7496 >> logs/live_$(date +%Y%m%d).log 2>&1 &

# 查看日志
tail -f logs/live_$(date +%Y%m%d).log

# 停止引擎
pkill -f live_engine.py
```

---

## 7. 每日维护窗口

IB 服务器每天约 **23:00–23:45 ET** 重启，连接会断开。

**临时方案**：维护后手动重启引擎：
```bash
pkill -f live_engine.py
sleep 60
nohup python live_engine.py --port 7496 >> logs/live_$(date +%Y%m%d).log 2>&1 &
```

**自动方案（macOS launchd）**：后续可配置 plist 在维护后自动重启。

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

---

## 10. 紧急平仓

如需立即平掉所有仓位：
1. 在 TWS 中手动平仓（最快最安全）
2. 手动编辑 `live_state.json`，将所有 `signed_contracts` 改为 0
3. 停止引擎：`pkill -f live_engine.py`

---

## 11. Mac Studio 专机部署架构

### 架构说明

```
开发 Mac（公司机器）          GitHub              Mac Studio（24小时运行）
────────────────────────    ─────────────────   ──────────────────────────
改代码 → git push ──────>   仓库（代码）──────>  git pull → 重启引擎
Claude Code（远程管理）                           IB Gateway（一直开着）
SSH via Cloudflare Tunnel ──────────────────>   live_engine.py（一直运行）
```

**两台电脑不能同时跑引擎**：IB 账户只允许一个 TWS/IB Gateway 实例登录，第二台登录会把第一台踢下线，且两个引擎会产生重复订单。

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
cd /Users/cohan/Documents/confluence_backtest
git add strategy.py indicators.py live_engine.py config.yaml  # 明确指定，不要 git add .
git commit -m "调整参数: 4H min_score 改为 6"
git push
```

**Mac Studio 端（部署更新）：**
```bash
cd ~/confluence_backtest

# 1. 拉取最新代码
git pull origin main

# 2. 查看当前持仓状态（更新前确认）
cat live_state.json

# 3. 停止当前引擎
pkill -f live_engine.py
echo "引擎已停止"

# 4. 等待 5 秒确保干净退出
sleep 5

# 5. 重启引擎（实盘）
nohup python live_engine.py --port 7496 >> logs/live_$(date +%Y%m%d).log 2>&1 &
echo "引擎已重启，PID: $!"
```

> ⚠️ **安全建议**：尽量在两个 Bar 收盘之间的空窗期更新，避免在信号即将触发时重启。

---

### 第四步：Mac Studio 常驻配置

**开机自动启动 IB Gateway（macOS launchd）：**

IB Gateway 本身支持设置为"开机自动登录"，在 IB Gateway 界面：
`Configure → Settings → Auto Restart → Enable`

**Cloudflare Tunnel 已配置为开机自启**（第零步已完成）：
```bash
sudo cloudflared service install  # 已执行过，无需重复
```

**引擎自动重启脚本（维护窗口后自动恢复）：**

创建 `~/confluence_backtest/restart_engine.sh`：
```bash
#!/bin/bash
cd ~/confluence_backtest
pkill -f live_engine.py 2>/dev/null
sleep 5
nohup python live_engine.py --port 4001 >> logs/live_$(date +%Y%m%d).log 2>&1 &
echo "$(date): 引擎已重启 PID=$!" >> logs/restart.log
```

设置每天 23:50 ET（IB 维护结束后）自动重启：
```bash
# crontab -e 添加
50 23 * * * /bin/bash ~/confluence_backtest/restart_engine.sh
```

---

### 远程监控与管理（从开发 Mac 通过 Cloudflare Tunnel SSH）

所有命令在**开发 Mac** 上执行，通过 SSH 操作 Mac Studio：

```bash
# 查看实时日志
ssh mac-studio "tail -f ~/confluence_backtest/logs/live_$(date +%Y%m%d).log"

# 查看当前持仓状态
ssh mac-studio "cat ~/confluence_backtest/live_state.json"

# 查看引擎是否在运行
ssh mac-studio "pgrep -a python"

# 停止引擎
ssh mac-studio "pkill -f live_engine.py"

# 重启引擎
ssh mac-studio "cd ~/confluence_backtest && pkill -f live_engine.py; sleep 5; nohup python live_engine.py --port 4001 >> logs/live_\$(date +%Y%m%d).log 2>&1 &"

# 干跑测试（不下单）
ssh mac-studio "cd ~/confluence_backtest && python live_engine.py --run-now --dry-run --port 4001"
```

**通过 Claude Code 管理（在开发 Mac 的 Claude 会话里）：**

Claude Code 的 Bash 工具可以直接执行上面所有 SSH 命令，等同于远程控制 Mac Studio。例如在 Claude 会话里说"重启引擎"，Claude 就会执行对应的 SSH 命令。

