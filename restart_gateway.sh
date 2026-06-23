#!/bin/bash
# restart_gateway.sh — 终止 IB Gateway，由 launchd com.quantrift.ibc.plist 自动重启
# 由 live_engine.py 在连续连接失败时自动调用
#
# 注意：不要在这里调用 ibcstart.sh！
# launchd 的 com.quantrift.ibc.plist（KeepAlive=true）负责管理 Gateway 生命周期。
# 只 kill Gateway，launchd 会自动拉起。两个并发 IBC 会争抢 Gateway GUI 导致不稳定。

echo "[$(date)] === Gateway 重启开始 ==="

# 先终止所有 ibcstart.sh（避免孤儿进程）
echo "[$(date)] 终止 ibcstart.sh 和 IbcGateway..."
pkill -f "ibcstart.sh" 2>/dev/null || true

# SIGKILL 强杀 Gateway（保留 autorestart 文件，IBC 重启后无需 2FA）
# 注意：SIGTERM 允许 Gateway 清理 → 删除 autorestart 文件 → 每次重启都要 2FA
pkill -9 -f "IbcGateway" 2>/dev/null || true

echo "[$(date)] Gateway 已终止，等待 launchd (com.quantrift.ibc.plist) 自动重启..."
# launchd KeepAlive=true 会在 Gateway 退出后自动重启，无需手动启动
