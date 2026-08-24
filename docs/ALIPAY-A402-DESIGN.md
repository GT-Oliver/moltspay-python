# MoltsPay 支付宝 A402 服务购买设计

> 状态：Implemented
>
> 最后更新：2026-08-24
>
> 范围：使用支付宝 AI 按量付费（A402）购买 Provider 服务

## 1. 产品边界

支付宝只用于直接购买 AI 服务：

```python
result = client.pay(
    "https://provider.example",
    "service-id",
    rail="alipay",
    payment_params={"prompt": "hello"},
    rail_options={"business_session_id": "runtime-session-id"},
)
```

Provider 余额充值不支持支付宝。以下能力已经移除：

- `POST /balance/topup/alipay`；
- `balance_topup_service_id`；
- `create_balance_topup_order(..., rail="alipay")`；
- `topup_balance_pack(..., rail="alipay")`；
- CLI `--topup-rail alipay` 和余额充值命令的 `--rail alipay`；
- MCP balance top-up 工具中的 Alipay 选项；
- `balance_topup` 类型的 A402 订单与账本入账逻辑。

Provider 余额充值只走现有微信 `topup-order -> query/confirm -> credit` 生命周期。不得把充值包装成 AI 服务，也不得把 A402 `Payment-Proof` 用于余额入账。

## 2. A402 服务购买架构

```text
MoltsPay.pay(rail="alipay")
  -> discover service-specific CNY quote
  -> POST /execute
  -> HTTP 402 + signed Payment-Needed
  -> AlipayBuyerClient.start_402()
  -> official alipay-bot / AI wallet
  -> Payment-Proof
  -> POST /execute
  -> AlipayFacilitator verifies payment
  -> idempotent service execution
  -> Payment-Validation
```

职责分离：

| 组件 | 职责 |
|---|---|
| `AlipayBuyerClient` | 创建、持久化和恢复 A402 买方支付会话 |
| `AlipayFacilitator` | 生成 `Payment-Needed`，验签和调用支付宝查询接口 |
| `AlipayOrderStore` | 保存 service 类型订单、防止 proof/trade 重放、维护履约 outbox |
| `MoltsPayServer` | `/execute` 的 A402 challenge、验付和服务履约 |

## 3. 服务报价与挑战

服务发现必须包含支付宝专属报价：

```json
{
  "paymentRails": {
    "alipay": {
      "amount": "1.00",
      "currency": "CNY",
      "serviceId": "ALIPAY_SERVICE_ID",
      "resourceId": "/execute?service=service-id",
      "maxTimeoutSeconds": 1800
    }
  }
}
```

客户端不能用 USDC 服务价格推导人民币金额。没有完整 Alipay 报价时，`rail="alipay"` 返回 unsupported rail。

SDK 将发现结果固化为不可变的 `AlipayPaymentIntent`，内容包括 Provider origin、
MoltsPay `skill_id`、支付宝 `service_id`、`resource_id`、金额、币种和最大有效期。
后续收到的 `Payment-Needed` 必须逐项匹配该意图；商户订单号由 Provider 在创建挑战时生成，
因此不在发现阶段预先指定，但必须是合法格式并保存到同一个本地会话中。

Provider 为服务请求创建 `kind="service"` 的 A402 订单，并将下列字段绑定到签名挑战：

- `out_trade_no`；
- `amount` 与 `currency=CNY`；
- `seller_id` 与 `service_id`；
- `resource_id`；
- `goods_name`；
- `pay_before`。

其中 `service_id` 是支付宝侧服务标识，不是 MoltsPay 用于选择 handler 的
`ServiceConfig.id`。Provider 必须在本地订单中额外保存后者作为 `skill_id`；
`skill_id` 不进入支付宝协议，但它将支付订单绑定到唯一的本地执行目标。

金额使用规范的两位小数字符串，禁止浮点计算和指数表示。

## 4. 买方会话

`AlipayBuyerClient.start_402()` 只接收真实的 A402 服务挑战，并强制要求完整的
`AlipayPaymentIntent`。它在启动官方 `alipay-bot` 前检查 Provider origin、金额、币种、
支付宝 `service_id`、`resource_id`、商户订单号格式及带时区的 `pay_before`；账单必须尚未
过期，且不得超过发现时公布的最大有效期。公开的 `MoltsPay.start_alipay_payment()` 在未传入
意图时会先执行服务发现并创建意图。它还需要当前 runtime 的业务 session ID。

本地 A402 会话只保存恢复所需的安全元数据，例如本地 payment session ID、商户订单号、查询单号、状态、媒体路径和时间戳。不得保存：

- `Payment-Proof` 原文；
- 支付宝用户 token；
- 授权码或支付密码；
- 应用私钥；
- runtime attestation。

本地 `mpay_alipay_*` ID 不能冒充上层业务 session ID。

## 5. Provider 验付与履约

收到 `Payment-Proof` 后，Provider 必须逐项验证：

1. proof 格式和必要字段；
2. 支付宝平台签名或官方查询结果；
3. `active=true`；
4. `out_trade_no` 对应本地 service 订单；
5. `resource_id`；
6. 金额与币种；
7. 有效期；
8. `trade_no` 和 proof hash 未绑定其他订单。
9. 本地订单的 `skill_id`、支付宝 `service_id` 和 `resource_id` 均与当前准备执行的 skill 一致。

`service_id` 在创建订单时保存并受本地幂等约束保护，但官方验付响应契约不保证返回该字段，因此不得用响应中的 `service_id` 拒绝已经通过订单号、资源、金额及防重放校验的付款。
执行授权校验使用本地订单中保存的支付宝 `service_id` 与 `skill_id`，不得使用请求体中的 `service` 代替订单绑定。

验证成功后，`AlipayOrderStore.claim_execution()` 原子取得一次履约权。服务完成后保存结果，并通过 fulfillment outbox 调用支付宝履约确认。重复请求返回原结果，不重复执行服务。

网络失败属于状态未知，不能被当成付款无效，也不能自动创建第二笔订单。

## 6. 订单查询与本地恢复状态

`~/.moltspay/alipay-sessions/*.json` 是买方恢复缓存，不是 Provider 订单数据库。读取本地 session 时只派生有效状态，不写文件；本地结果必须标记 `source=local_cache`、`authoritative=false`，不能据此断言服务未执行、需要退款或 proof 被重放。

Provider 通过 `GET /payments/alipay/{out_trade_no}` 返回单笔安全订单视图，包括订单号、交易号、金额、服务、支付状态、订单状态、履约状态和已保存的业务结果。响应不得包含 Payment-Proof、proof hash、钱包凭据或应用凭据。订单号是高熵 capability identifier；接口不提供未认证的全商户订单枚举。

MCP 查询分为两组：

- `alipay_session_status/list`：只读本地恢复缓存；旧 `alipay_status/list` 是兼容别名；
- `alipay_order_status/list`：查询 Provider 权威状态并与匹配的本地 session 对账；list 只查询本地已知订单。

权威完成态覆盖本地陈旧的 replay/unknown 诊断，写入已保存结果与履约状态，并记录同步来源和时间。任何记忆或对客结论在声称“未交付、需退款、proof 重复”前，都必须取得 Provider 权威状态。

## 7. 配置

```json
{
  "provider": {
    "alipay": {
      "app_id": "APP_ID",
      "seller_id": "SELLER_ID",
      "seller_name": "Provider",
      "private_key_path": "cert/alipay/ALIPAY_PRIVATE_KEY.pem",
      "public_key_path": "cert/alipay/ALIPAY_APP_PUBLIC_KEY.pem",
      "order_db_path": "data/alipay-a402.sqlite"
    }
  }
}
```

配置中不应出现充值 service ID。生产环境必须使用已开通 AI 按量付费产品的真实应用、商户、服务 ID 和密钥。私钥不得提交仓库、写入日志或返回给客户端。

## 8. 错误与恢复原则

| 场景 | 处理 |
|---|---|
| 服务没有 Alipay CNY 报价 | 拒绝选择 Alipay rail |
| 缺少真实业务 session | 拒绝启动 AI 钱包支付 |
| 钱包未开通或未绑定 | 返回钱包状态错误 |
| proof 与订单字段不匹配 | 拒绝履约 |
| 验付网络不可用 | 标记状态未知，允许恢复查询 |
| proof/trade 重放 | 返回原结果或重放冲突，不重复履约 |
| 用户尝试支付宝余额充值 | SDK 拒绝、CLI/MCP 无该选项、旧服务端路径返回 404 |

## 9. 验收标准

- `pay(..., rail="alipay")` 仍读取服务的 CNY 报价并进入 `_pay_alipay()`；
- 服务发现公开 Alipay `serviceId`、`resourceId` 和 `maxTimeoutSeconds`；
- `AlipayPaymentIntent` 不可变，低层 `start_402()` 和 `pay_402()` 缺少意图时拒绝执行；
- 被替换的金额、币种、服务、资源、Provider origin，以及过期或超长有效期挑战均在调用钱包前被拒绝；
- A402 start、resume、proof verification、幂等服务执行和履约确认无回归；
- `AlipayOrderStore` 只接受 `kind="service"`；
- 源码和配置中不存在 `/balance/topup/alipay` 或 `balance_topup_service_id`；
- SDK 的订单式充值和外部入账 API 都拒绝 `rail="alipay"`；
- CLI 与 MCP 的余额充值 rail 只有 `wechat`；
- 微信余额充值保持原行为。
