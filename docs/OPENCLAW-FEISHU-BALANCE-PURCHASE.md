# OpenClaw 飞书余额购买流程

本文定义 OpenClaw 飞书 channel 购买 MoltsPay 服务时的可恢复状态机。默认支付方式为 Provider 余额；微信仅用于给余额充值，不直接购买服务。

## MCP 调用顺序

1. 使用飞书事件 ID 作为稳定的 `requestId`，调用：

   ```json
   {
     "tool": "moltspay_pay",
     "arguments": {
       "url": "https://provider.example",
       "service": "ping",
       "params": {},
       "rail": "balance",
       "confirmed": true,
       "requestId": "feishu:<event-id>"
     }
   }
   ```

2. 成功时把 `data.result` 返回给用户，并清理待处理状态。
3. 当 `error.code` 为 `insufficient_balance` 时，读取：
   - `error.details.required`
   - `error.details.balance`
   - `error.details.topupPacks`
   - `error.details.customTopupMax`
4. 发送飞书交互卡片。默认按钮为 10、20、50、100 元，并提供“自定义”。自定义金额必须大于 0、最多两位小数，且不能超过 `customTopupMax`。
5. 用户选择金额后调用 `moltspay_balance_topup_order`：

   ```json
   {
     "serverUrl": "https://provider.example",
     "pack": "20.00",
     "buyerId": "<bound-feishu-buyer-id>",
     "confirmed": true,
     "requestId": "feishu:<event-id>:topup:1"
   }
   ```

   将工具返回的 PNG 图片发送到飞书，并保存 `outTradeNo`、`expiresAt` 和选择的金额。

6. 每 2 秒调用一次 `moltspay_balance_topup_confirm`。`credited=true` 后，使用最初的 `requestId`、服务和参数重新调用 `moltspay_pay`。
7. 如果仍返回 `insufficient_balance`，充值轮次加一，重新发送金额卡片。不要在没有新一次用户选择的情况下自动创建下一笔微信订单。
8. 订单过期时提示用户重新选择充值金额，禁止继续展示或复用过期二维码。

## Channel 持久化状态

每个待处理购买至少保存以下字段：

```json
{
  "requestId": "feishu:<event-id>",
  "openId": "<feishu-open-id>",
  "buyerId": "<provider-buyer-id>",
  "serverUrl": "https://provider.example",
  "service": "ping",
  "params": {},
  "status": "awaiting_amount",
  "topupAttempt": 0,
  "outTradeNo": null,
  "expiresAt": null
}
```

状态依次为：`paying` → `awaiting_amount` → `awaiting_wechat` → `confirming` → `paying` → `completed`。订单过期进入 `awaiting_amount`，用户取消进入 `cancelled`。

## 并发与安全

- `openId` 必须绑定固定的 Provider `buyerId`，不能直接信任卡片回传的 buyer ID。
- 同一 `requestId` 同时只允许一个状态机执行；重复的飞书回调应返回当前状态。
- 所有创建订单、确认充值和支付调用都传 `confirmed=true`，生产环境设置 `MOLTSPAY_MCP_REQUIRE_CONFIRM=1`。
- 充值轮次没有业务上的一次限制，但应设置会话过期时间，避免遗留任务无限轮询。
- 日志不得记录微信支付凭据、私钥或完整 MCP 二维码 Base64 数据。

## CLI 验证

自动充值并显示每一轮微信二维码：

```bash
moltspay pay https://provider.example ping \
  --rail balance --pack 10 \
  --max-topup-attempts 10 --topup-poll-interval 2
```

人工编排模式：

```bash
moltspay pay https://provider.example ping --rail balance --topup-mode manual --pack 20
moltspay balance topup-confirm <out-trade-no>
moltspay pay https://provider.example ping --rail balance --topup-mode manual --pack 20
```

