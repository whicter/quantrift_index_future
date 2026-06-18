# TASK.md — 待办事项

## 已完成

| 任务 | 完成日期 |
|---|---|
| Telegram 配置（bot @quantrift_index_future_bot） | 2026-06-15 |
| 连接稳定性修复（2105重连、fetch_bars重试、连接失败告警） | 2026-06-17 |
| 每小时心跳 Telegram（状态 + 持仓 + 实时净值） | 2026-06-17 |
| IBC 自动登录（Gateway 重启后自动填账号密码，免手动操作） | 2026-06-17 |

---

## 待办

### 2. open orders 处理（优先级：中）

**目标**：重连时发现在途订单应暂停该品种信号，等订单结束后再继续。

现状：重连时调用 `reqAllOpenOrders()` 打日志，有未平仓单会告警，但不自动处理。

---

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

---

### 4. ES 参数优化（优先级：低）

目前 ES 用 NQ 参数，未经专项优化。
