# MoltsPay Client 身份与支付宝 AI 钱包权限设计

> 状态：Design Draft
>
> 最后更新：2026-08-18
>
> 适用范围：支付宝 A402 服务购买

## 1. 范围

本设计只解决 A402 服务购买中的 Client principal、runtime 请求授权和 AI 钱包归属。

Provider 余额充值不支持支付宝，因此余额充值不进入 AI 钱包，不使用 `AlipayBuyerClient`、`PaymentRequestContext`、`Payment-Needed` 或 `Payment-Proof`。余额充值的 `buyer_id` 也不能用于推导或授权 AI 钱包。

```text
A402 服务购买
Client identity -> runtime authorization -> AI wallet -> Payment-Proof

Provider 余额充值
buyer_id -> WeChat top-up order -> query/confirm -> ledger credit
```

## 2. 身份模型

```text
MoltsPay Client Identity -> 决定 AI 钱包归属
PaymentRequestContext    -> 决定本次服务请求能否使用钱包
A402 business session   -> 恢复本次服务购买
```

稳定 principal 由独立身份公钥派生：

```text
principal_id = "mpid_" + base64url(sha256(identity_public_key_der))
```

`agent_id` 只是显示标签。channel、临时 session、MCP request、模型名称和进程链都不能改变 principal。

## 3. session 与 channel

session 和 channel 只参与 A402 服务购买授权：

- session 关联支付意图、审计与恢复；
- channel 标识请求入口；
- sender 标识谁请求代表当前 Client 付款；
- runtime adapter 对上下文签名。

| 场景 | AI 钱包处理 |
|---|---|
| 同一 Client，新 session | 复用钱包 |
| 同一 Client，切换已授权 channel | 复用钱包 |
| 同名 Agent、身份密钥不同 | 新 principal，不复用 |
| 身份密钥轮换 | 显式迁移或重新授权 |

## 4. 凭据边界

MoltsPay 不保存或输出：

- 支付宝账号 token、授权码或支付密码；
- `Payment-Proof` 原文；
- 支付宝应用私钥；
- `alipay-bot` 私有钱包凭据；
- runtime attestation 或临时请求 token。

Client identity 密钥与支付宝商户应用密钥、用户钱包凭据和链上钱包密钥相互独立。

## 5. 信任模型

| 身份 | 所有者 | 作用 |
|---|---|---|
| Client Identity | MoltsPay Client | 确定 AI 钱包归属 |
| Runtime Adapter Identity | 接入适配器 | 证明 session、channel、sender 来源 |
| Provider Identity | Provider | 签署与验证 A402 服务请求 |

模型可以提出支付意图，但不能自行提供并让系统信任 principal、wallet fingerprint、sender、channel、runtime attestation 或临时请求 token。

## 6. 请求上下文

```python
class PaymentRequestContext(BaseModel):
    adapter_id: str
    session_id: str
    channel: str
    sender_id: str
    issued_at: int
    expires_at: int
    nonce: str
    attestation: str
```

上下文必须绑定当前 principal、A402 服务、金额、Provider、资源、请求 ID、有效期和 nonce。

权限判定顺序：

1. Client identity 可用；
2. runtime attestation 有效；
3. channel grant 有效；
4. sender scope 允许付款；
5. AI 钱包属于当前 principal；
6. 金额限额允许；
7. seller、service 和 resource 在允许范围；
8. request ID 幂等；
9. 执行 A402 支付；
10. 验证 `Payment-Proof` 并履约。

任何身份、权限或绑定字段缺失都停止支付，不自动切换或新建钱包。

## 7. CLI 与 MCP

AI 钱包操作包括：

- `alipay start`；
- `alipay resume`；
- `pay --rail alipay`。

余额充值命令和 MCP balance top-up 工具不属于 AI 钱包操作，也不提供 Alipay rail。

MCP 参数不能允许模型覆盖 principal、wallet fingerprint 或钱包 token。响应序列化前必须删除 attestation、临时 token 和 `Payment-Proof`。

## 8. 验收

- 同一 identity 重启后 principal 不变；
- 相同 `agent_id`、不同身份密钥产生不同 principal；
- 换 session 不触发钱包申请；
- 未授权 channel 拒绝，授权后复用钱包；
- 模型参数不能覆盖 principal；
- attestation 过期、篡改或重放时拒绝；
- 敏感凭据与 `Payment-Proof` 不持久化；
- A402 服务购买可完成 start、resume、验付与履约；
- 余额充值路径无法启动 Alipay AI 钱包。
