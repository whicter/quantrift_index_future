#!/bin/bash
# restart_gateway.sh — 终止并重启 IB Gateway（via IBC 自动登录）
# 由 live_engine.py 在连续连接失败时自动调用

echo "[$(date)] === Gateway 重启开始 ==="

# 终止现有进程
echo "[$(date)] 终止 IBC 和 Gateway..."
pkill -f "ibcstart.sh" 2>/dev/null || true
pkill -f "IbcGateway"  2>/dev/null || true
sleep 10

# 启动新的 Gateway（IBC 负责自动填账号密码）
echo "[$(date)] 启动 IBC → Gateway..."
exec /bin/bash /Users/congrenhan/IBC/scripts/ibcstart.sh 10.45 -g \
  --tws-path=/Users/congrenhan/Applications \
  --tws-settings-path= \
  --ibc-path=/Users/congrenhan/IBC \
  --ibc-ini=/Users/congrenhan/IBC/config.ini \
  --user= --pw= --fix-user= --fix-pw= \
  --java-path= --mode= \
  --on2fatimeout=exit
