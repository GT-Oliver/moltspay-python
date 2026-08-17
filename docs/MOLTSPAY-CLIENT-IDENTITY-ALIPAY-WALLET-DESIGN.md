# MoltsPay Client 身份与支付宝钱包权限设计

> 状态：Design Draft  
> 最后更新：2026-08-17  
> 适用范围：MoltsPay Python SDK、CLI、MCP、支付宝 A402 买方流程、Provider 余额充值  
> 关联设计：[ALIPAY-A402-DESIGN.md](ALIPAY-A402-DESIGN.md)

## 1. 背景

当前支付宝买方调用链可能是：

```text
Feishu / Discord
  -> OpenClaw
    -> MoltsPay CLI / MCP
      -> Python SDK
        -> alipay-bot
```

`alipay-bot` 在最内层子进程运行时，只能观察到 `python -> node` 一类进程链。如果上层 Agent 身份没有显式传递，CLI 可能把调用识别为未知 Agent，并选择错误的钱包上下文。由此会产生以下问题：

- 同一 Agent 因 session 或调用路径变化被识别成多个钱包；
- 已经绑定钱包，支付时仍被判定为未绑定；
- Feishu、Discord 或本地 CLI 之间的钱包归属不清晰；
- 将临时 `session_id` 当成钱包身份会导致每个新 session 都可能要求重新绑定；
- 仅修改某个 Agent 框架的 hook 只能修复单一运行时，不能形成 SDK 级通用契约。

本设计将“钱包归属”和“本次请求来源”拆开：

```text
MoltsPay Client Identity  -> 决定钱包归属
Request Context           -> 决定本次请求是否有权使用钱包
Provider buyer_id         -> 决定充值或消费对应的余额账户
```

## 2. 核心决策

### 2.1 钱包绑定对象

支付宝 AI 钱包绑定到稳定的 MoltsPay Client principal，不绑定到以下临时对象：

- channel；
- session；
- 单次 MCP request；
- 模型名称；
- `alipay-bot` 观察到的进程链。

稳定 principal 由独立身份公钥派生：

```text
principal_id = "mpid_" + base64url(sha256(identity_public_key_der))
```

`agent_id` 是便于展示和管理的标签，不能单独作为安全身份。两个 Client 即使都叫 `main`，只要身份密钥不同，就属于不同 principal。

### 2.2 session 与 channel 的作用

session 和 channel 只参与请求授权：

- session 用于支付意图、审计和恢复当前业务请求；
- channel 用于判断请求入口是否已授权；
- sender 用于判断谁可以代表当前 Client 发起付款；
- 它们都不能改变 Client principal 或选择其他钱包。

因此：

| 场景 | 钱包处理 |
|---|---|
| 同一 Client，新 session | 复用现有钱包，不重新绑定 |
| 同一 Client，从 Feishu 换到 Discord | channel 授权后复用现有钱包 |
| 同一 Client，重启运行时 | 从 Client identity 恢复现有钱包 |
| 相同 `agent_id`，但身份密钥不同 | 视为新 Client，不允许复用 |
| 身份密钥轮换 | 显式迁移或重新授权，禁止静默切换 |

### 2.3 凭据边界

MoltsPay 不保存或复制以下材料：

- 支付宝账号 token；
- 用户授权码、支付密码；
- `Payment-Proof` 原文；
- 支付宝应用私钥；
- `alipay-bot` 钱包凭据。

MoltsPay 只保存支付宝官方 CLI 返回的安全钱包指纹和绑定状态。Client identity 密钥是 MoltsPay 本地身份密钥，与支付宝应用密钥、商户签名密钥和链上支付钱包密钥相互独立。

## 3. 信任模型

### 3.1 三类身份

| 身份 | 所有者 | 作用 |
|---|---|---|
| Client Identity | MoltsPay Client | 确定钱包归属并签署 Provider 请求 |
| Runtime Adapter Identity | OpenClaw/Codex/其他接入适配器 | 证明 session、channel、sender 来源真实 |
| Provider Identity | Provider | 签署 `Payment-Needed`，验付并履约 |

### 3.2 不信任模型生成的身份字段

模型可以提出支付意图，但下列字段不能由模型自由填写后直接信任：

- `principal_id`；
- `agent_id`；
- `wallet_fingerprint`；
- `sender_id`；
- `channel`；
- runtime attestation；
- 临时请求 token。

运行时适配器必须在模型生成工具调用之后注入并签署请求上下文；MoltsPay 必须验证签名。无有效 attestation 时，涉及支付宝钱包的写操作返回 `alipay_request_context_missing` 或 `alipay_request_context_invalid`。

## 4. 总体架构

```text
Channel user
  |
  v
Runtime adapter
  |  signed PaymentRequestContext
  v
MoltsPay Client
  |  fixed MoltsPayClientIdentity
  |  ChannelPolicy authorization
  v
AlipayBuyerClient
  |  stable wallet principal + real business session
  v
official alipay-bot
  |
  v
Alipay AI wallet

MoltsPay Client
  |  signed principal request
  v
Provider /balance/topup/alipay
  |  Payment-Needed / Payment-Proof
  v
Balance ledger
```

设计不要求 OpenClaw 成为核心依赖。OpenClaw、Codex、Claude Code 或其他 Agent 只需实现同一份 `PaymentRequestContext` 适配契约。

## 5. Client Identity 模型

### 5.1 新模块

新增 `src/moltspay/identity.py`：

```python
class MoltsPayClientIdentity(BaseModel):
    version: int = 1
    client_id: str
    agent_id: str
    principal_id: str
    public_key: str
    created_at: str
    rotated_from: str | None = None
```

私钥不进入 Pydantic 序列化模型，由 `IdentityKeyStore` 管理：

```python
class IdentityKeyStore:
    def load_or_create(self, identity_path: Path, agent_id: str) -> MoltsPayClientIdentity: ...
    def sign(self, principal_id: str, payload: bytes) -> str: ...
    def rotate(self, principal_id: str) -> MoltsPayClientIdentity: ...
```

建议算法为 Ed25519；如果基础安装不引入新加密依赖，则使用项目现有 `cryptography` 能力并调整依赖分组。不得复用支付宝应用私钥。

### 5.2 本地存储

默认公开身份文件：

```text
~/.moltspay/agent-identity.json
```

建议内容：

```json
{
  "version": 1,
  "clientId": "mpclient_...",
  "agentId": "main",
  "principalId": "mpid_...",
  "publicKey": "...",
  "createdAt": "2026-08-17T05:00:00Z"
}
```

私钥优先保存在系统 Keychain；不支持 Keychain 时使用独立 `0600` 文件。写入必须使用临时文件、`fsync` 和原子替换，禁止跟随符号链接。

### 5.3 `MoltsPay` 构造函数

修改 `src/moltspay/client.py`：

```python
def __init__(
    self,
    ...,
    agent_id: str = "default",
    identity_path: str | None = None,
    channel_policy_path: str | None = None,
): ...
```

新增只读属性：

```python
client.identity
client.principal_id
client.agent_id
```

新增内部函数：

```python
def _get_identity(self) -> MoltsPayClientIdentity: ...
def _sign_principal_request(self, canonical_request: bytes) -> PrincipalProof: ...
def _get_channel_policy(self) -> ChannelPolicy: ...
```

一个 `config_dir` 默认只对应一个 Client identity。检测到同目录下 identity 与钱包绑定 principal 不一致时必须停止，不得自动覆盖。

## 6. 请求上下文与 Channel 授权

### 6.1 `PaymentRequestContext`

新增 `src/moltspay/request_context.py`：

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

约束：

- `session_id` 是当前框架真实业务会话 ID，不是 `mpay_alipay_*` 本地支付会话 ID；
- `expires_at` 与 `issued_at` 的差值建议不超过 5 分钟；
- `nonce` 在有效期内只允许使用一次；
- `attestation` 由 runtime adapter 对其余字段的规范 JSON 签名；
- 请求上下文不包含 `principal_id`，避免请求调用方切换钱包身份。

### 6.2 Channel grant

新增 `src/moltspay/channel_policy.py`：

```python
class ChannelGrant(BaseModel):
    grant_id: str
    principal_id: str
    adapter_id: str
    adapter_public_key: str
    channel: str
    sender_id: str
    scopes: list[str]
    status: Literal["active", "revoked"]
    created_at: str
    expires_at: str | None = None
```

推荐 scopes：

```text
alipay:wallet:read
alipay:payment:create
alipay:payment:resume
balance:topup:create
balance:topup:credit
```

核心接口：

```python
def authorize_channel(grant: ChannelGrant) -> None: ...
def revoke_channel(grant_id: str) -> None: ...
def verify_request_context(context: PaymentRequestContext, scope: str) -> None: ...
def list_channel_grants() -> list[ChannelGrant]: ...
```

换 channel 时只新增或确认 `ChannelGrant`。不得调用支付宝钱包开通或绑定流程。

## 7. 支付宝钱包绑定模型

### 7.1 本地安全元数据

在 `src/moltspay/alipay.py` 新增：

```python
class AlipayWalletBinding(BaseModel):
    version: int = 1
    principal_id: str
    wallet_fingerprint: str
    status: Literal["unbound", "bound", "revoked", "migration_required"]
    scopes: list[str]
    max_per_tx: str | None = None
    max_per_day: str | None = None
    bound_at: str | None = None
    updated_at: str
```

默认文件：

```text
~/.moltspay/alipay-wallet-binding.json
```

只允许保存安全指纹和权限元数据。支付宝账号 token 或完整钱包凭据仍由官方 CLI 管理。

### 7.2 `AlipayBuyerClient`

构造函数调整为：

```python
def __init__(
    self,
    config_dir: str | None = None,
    executable: str = "alipay-bot",
    identity: MoltsPayClientIdentity | None = None,
    channel_policy: ChannelPolicy | None = None,
    runner: Callable | None = None,
): ...
```

新增函数：

```python
def get_wallet_binding(self) -> AlipayWalletBinding: ...
def adopt_current_wallet(self, safe_wallet_fingerprint: str) -> AlipayWalletBinding: ...
def assert_wallet_principal(self) -> None: ...
def _authorize(self, context: PaymentRequestContext, scope: str) -> None: ...
def _build_cli_environment(self, context: PaymentRequestContext) -> dict[str, str]: ...
```

修改：

```python
def start_402(..., request_context: PaymentRequestContext) -> AlipayPaymentSession: ...
def resume(identifier: str, request_context: PaymentRequestContext) -> AlipayPaymentSession: ...
```

执行顺序必须是：

1. 验证 runtime attestation；
2. 验证 channel grant 和 scope；
3. 验证本地 wallet binding 属于当前 principal；
4. 构造官方 CLI 上下文；
5. 执行 `payment-intent`、钱包检查和 `402-buyer-pay`；
6. 保存安全恢复字段。

缺少任何身份材料时不得自动调用 `apply-wallet`。

### 7.3 本地支付会话字段

`AlipayPaymentSession` 增加：

```python
principal_id: str
agent_id: str
adapter_id: str | None
channel: str | None
session_fingerprint: str | None
execution_mode: Literal["nested", "delegated"] = "nested"
```

不得持久化：

- runtime attestation；
- request token；
- sender 原始凭据；
- `Payment-Proof`；
- 完整支付宝钱包 token。

`session_fingerprint` 只用于检测恢复请求是否来自同一业务上下文，不参与钱包选择。

## 8. `alipay-bot` 身份契约

安全实现需要官方 CLI 同时接收两个不同维度：

```text
稳定钱包维度：MoltsPay Client principal
临时请求维度：真实 Agent business session
```

建议新增或确认官方支持的稳定选择器，例如：

```text
AIPAY_CLIENT_PRINCIPAL=<principal_id>
```

临时上下文继续使用现有概念：

```text
AIPAY_FRAMEWORK=<runtime framework>
AIPAY_SESSION_ID=<real business session UUID>
AIPAY_REQUEST_TOKEN=<ephemeral request token>
```

关键约束：

- 不得把 `principal_id` 填进 `AIPAY_SESSION_ID`；
- 不得把本地 `mpay_alipay_*` 支付会话 ID 填进真实业务 session 字段；
- 不得根据进程链猜测 principal；
- CLI 必须返回安全钱包指纹，供 MoltsPay 验证绑定，不得要求 MoltsPay读取 CLI 私有凭据文件。

如果当前官方 CLI 没有稳定 principal 选择和安全钱包指纹接口，该能力属于外部阻塞项。在官方契约补齐前，不应宣称多 Client 钱包隔离已经完成，也不能通过修改单一框架 hook 或复用 `session_id` 绕过。

## 9. CLI 设计

修改 `src/moltspay/cli.py`，新增：

```text
moltspay identity show
moltspay identity rotate
moltspay alipay wallet-status
moltspay alipay wallet-adopt
moltspay alipay channel-authorize
moltspay alipay channel-revoke
moltspay alipay channel-list
```

输出限制：

- `identity show` 只显示 `client_id`、`agent_id`、`principal_id`；
- `wallet-status` 只显示钱包安全指纹、状态和权限；
- 不显示身份私钥、支付宝 token、attestation 或请求 token；
- `identity rotate` 必须显式确认，并将钱包绑定置为 `migration_required`；
- `wallet-adopt` 只能使用官方 CLI 当前命令真实返回的安全指纹，禁止扫描或解析 CLI 私有钱包文件。

支付命令通过受保护的本地上下文入口接收 `PaymentRequestContext`。普通用户在终端直接发起写操作时，使用 `local-cli` adapter grant；不得伪造 Feishu 或其他 channel。

## 10. MCP 设计

修改 `src/moltspay/mcp/server.py`。

MCP Server 启动时固定加载一个 `MoltsPay` Client identity。单次写操作新增：

```python
class MCPPaymentRequestContext(BaseModel):
    adapterId: str
    sessionId: str
    channel: str
    senderId: str
    issuedAt: int
    expiresAt: int
    nonce: str
    attestation: str
```

适用工具：

- `moltspay_alipay_start`；
- `moltspay_alipay_resume`；
- `moltspay_balance_topup_order` 且 `rail=alipay`；
- `moltspay_balance_topup_resume` 且原订单 rail 为 `alipay`。

禁止把以下字段作为 MCP工具参数：

- `agentId`；
- `principalId`；
- `walletFingerprint`；
- 钱包 token。

Runtime adapter 必须在模型输出工具参数后覆盖注入 `requestContext`。MoltsPay 只接受通过已登记 adapter 公钥验证的上下文；模型直接生成但无法验签的字段必须拒绝。

所有 MCP响应在序列化前删除 attestation 和临时 token。`_alipay_session()` 只返回安全恢复元数据。

## 11. Provider principal 绑定

### 11.1 充值请求字段

`POST /balance/topup/alipay` 请求增加：

```json
{
  "buyer_id": "default-buyer",
  "pack": "0.01",
  "request_id": "req_...",
  "client_principal_id": "mpid_...",
  "client_public_key": "...",
  "principal_timestamp": 1786942800,
  "principal_nonce": "...",
  "principal_signature": "..."
}
```

签名内容使用规范 JSON，至少覆盖：

```text
buyer_id
pack
request_id
resource_id
client_principal_id
principal_timestamp
principal_nonce
```

### 11.2 Provider 校验

`src/moltspay/server/server.py::_handle_balance_topup_alipay()` 在创建 `Payment-Needed` 前执行：

1. 验证 `principal_id` 与公钥摘要一致；
2. 验证 principal 签名；
3. 验证时间窗口；
4. 原子消费 nonce；
5. 验证 `buyer_id` 已绑定当前 principal；
6. 验证金额属于 Provider 允许的充值档位；
7. 验证 `service_id`、`resource_id` 和订单类型；
8. 以 `request_id + principal_id + resource_id` 做幂等查询。

生产环境禁止首次请求自动占用任意已有 `buyer_id`。首次 principal 与 buyer 的绑定必须来自显式开户、管理员授权或已有安全身份迁移。仅本地测试 Provider 可通过明确的测试配置允许自动绑定。

### 11.3 数据库变更

新增：

```sql
CREATE TABLE balance_principal_bindings (
  principal_id TEXT NOT NULL,
  buyer_id TEXT NOT NULL,
  public_key TEXT NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (principal_id, buyer_id)
);

CREATE TABLE principal_nonces (
  principal_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (principal_id, nonce)
);
```

`alipay_orders` 增加：

```sql
ALTER TABLE alipay_orders ADD COLUMN principal_id TEXT;
ALTER TABLE alipay_orders ADD COLUMN wallet_binding_version INTEGER NOT NULL DEFAULT 1;
```

并修复余额充值创建订单时未写入 `service_id` 的问题。`service_id`、`principal_id`、`buyer_id`、金额和 `resource_id` 必须在验付与入账阶段逐项匹配。

## 12. 权限判定顺序

每次支付宝充值或付款按以下顺序执行：

```text
1. Client identity 可用
2. Runtime attestation 有效
3. Channel grant 有效
4. Sender scope 允许当前动作
5. Wallet binding 属于当前 principal
6. 单笔和日限额允许
7. seller_id/service_id/resource_id 在允许范围
8. Provider principal/buyer 绑定有效
9. request_id 与 out_trade_no 幂等
10. 执行支付
11. 验证 Payment-Proof
12. 原子入账并确认履约
```

建议默认拒绝策略：任何身份、权限或绑定字段缺失都停止支付，不自动创建钱包、不自动切换钱包、不自动重试业务失败。

## 13. 错误模型

新增稳定错误：

| 错误码 | 含义 | 是否可重试 |
|---|---|---|
| `agent_identity_missing` | Client identity 不存在或损坏 | 否 |
| `agent_identity_mismatch` | Client identity 与配置/绑定不一致 | 否 |
| `alipay_request_context_missing` | 写操作缺少请求上下文 | 否 |
| `alipay_request_context_invalid` | attestation、时间或 nonce 非法 | 否 |
| `alipay_channel_not_authorized` | channel/sender 无权限 | 否 |
| `alipay_wallet_not_bound` | 当前 principal 尚未绑定钱包 | 否 |
| `alipay_wallet_principal_mismatch` | 钱包属于其他 principal | 否 |
| `alipay_wallet_migration_required` | 身份轮换后需迁移钱包绑定 | 否 |
| `alipay_cli_identity_unsupported` | 官方 CLI 不支持稳定 principal 选择 | 否 |
| `principal_replay_detected` | principal nonce 已使用 | 否 |
| `principal_buyer_binding_invalid` | principal 无权操作该 buyer | 否 |

网络超时和支付宝查询临时不可用可以按现有规则有限重试；身份、权限、绑定和业务失败不得自动重试。

## 14. 现有钱包迁移

当前可能已经存在多个由不同运行时指纹创建的钱包。迁移必须显式且可审计：

1. 加载当前 MoltsPay Client principal；
2. 通过官方 CLI安全接口查询当前钱包状态和安全指纹；
3. 展示脱敏的 Client principal 与钱包指纹；
4. 用户确认归属；
5. 写入 `AlipayWalletBinding`；
6. 查询确认绑定状态；
7. 不调用 `apply-wallet`，不创建新钱包。

禁止直接读取、复制或编辑 `~/.alipay-bot-cli` 的私有钱包 token。若官方 CLI 无法返回安全钱包指纹或无法迁移归属，则保持 `migration_required`，不得宣称迁移成功。

## 15. 代码修改清单

| 模块 | 主要修改 |
|---|---|
| `src/moltspay/identity.py` | 新增 Client identity、密钥存储、签名与轮换 |
| `src/moltspay/request_context.py` | 新增 runtime 请求上下文与 attestation 验证 |
| `src/moltspay/channel_policy.py` | 新增 channel grant、scope 和撤销 |
| `src/moltspay/models.py` | 增加公开安全模型和序列化约束 |
| `src/moltspay/client.py` | Client 持有 identity；支付宝调用透传授权上下文 |
| `src/moltspay/alipay.py` | 钱包绑定、principal 校验、真实 session 与稳定身份分离 |
| `src/moltspay/cli.py` | identity、钱包归属和 channel 授权命令 |
| `src/moltspay/mcp/server.py` | 写操作接收并验证 `requestContext`，禁止覆盖 principal |
| `src/moltspay/server/server.py` | Provider 验证 principal 签名和 buyer 绑定 |
| `src/moltspay/server/alipay_store.py` | 持久化 principal、nonce、service_id 与钱包绑定版本 |
| `src/moltspay/exceptions.py` | 新增稳定身份和权限错误码 |
| `skills/moltspay.services.json` | 修正支付宝平台公钥配置和生产 service ID |

OpenClaw hook 不属于核心必改模块。它只能作为 OpenClaw runtime adapter 的一种实现，用于签署并注入 `PaymentRequestContext`；其他 Agent 使用相同契约实现各自适配器。

## 16. 测试计划

### 16.1 单元测试

- 相同 `identity_path` 重启后 `principal_id` 不变；
- 相同 `agent_id`、不同身份密钥产生不同 principal；
- identity 文件权限、原子写入和符号链接防护；
- 请求上下文签名、过期、nonce 重放和字段篡改；
- channel grant scope 正确生效；
- `AlipayBuyerClient` 不使用本地 `mpay_alipay_*` 代替业务 session；
- 临时 token、attestation、`Payment-Proof` 不持久化；
- principal 不一致时不调用 `alipay-bot`。

### 16.2 MCP/CLI 测试

- MCP模型参数不能覆盖 principal；
- 无有效 runtime attestation 的支付宝写操作失败；
- CLI只输出安全 principal 和钱包指纹；
- 换 session 不触发钱包申请；
- 换 channel 未授权时拒绝，授权后复用现有钱包。

### 16.3 Provider 测试

- principal 签名有效/无效；
- nonce 原子防重放；
- principal 与 buyer 绑定；
- `service_id` 正确写入订单；
- 金额、资源、订单、principal 任一不一致均拒绝；
- 同一 `request_id` 不创建第二笔订单；
- 同一 `Payment-Proof` 不重复入账。

### 16.4 端到端验收

使用同一个 MoltsPay Client principal：

1. Feishu session A 创建并支付 `0.01 CNY` 充值订单；
2. Feishu session B 不重新绑定钱包，可继续查询；
3. Discord channel 未授权时拒绝；
4. 授权 Discord 后复用同一钱包；
5. 新建另一 Client identity 后不能使用原钱包；
6. Provider 只生成一笔订单；
7. 支付后获得 `trade_no` 和有效 `Payment-Proof`；
8. `default-buyer` 余额只增加 `0.01 CNY`；
9. 全流程没有新增支付宝钱包。

## 17. 实施阶段

### Phase 0：外部契约确认

- 确认或推动 `alipay-bot` 支持稳定 Client principal 选择；
- 确认安全钱包指纹查询/迁移接口；
- 明确这些字段与真实业务 `session_id` 的独立语义。

Phase 0 未完成时，只能完成 MoltsPay 内部身份和授权代码，不能完成安全的钱包选择验收。

### Phase 1：Client Identity

- 实现 identity、密钥存储和轮换；
- `MoltsPay` 初始化时固定 principal；
- 增加配置迁移和单元测试。

### Phase 2：Runtime Authorization

- 实现 `PaymentRequestContext`；
- 实现 adapter attestation、nonce 和 channel grant；
- 接入 CLI、MCP。

### Phase 3：Alipay Wallet Binding

- 实现钱包安全指纹、principal 绑定和迁移状态；
- 修改 `AlipayBuyerClient`；
- 接入官方 CLI稳定身份契约。

### Phase 4：Provider Principal Binding

- 增加 principal 签名请求；
- 增加 buyer 绑定和数据库迁移；
- 修复 `service_id` 持久化和平台公钥配置。

### Phase 5：迁移与真实验收

- 显式认领一个现有钱包；
- 不创建新钱包；
- 完成 Feishu、跨 session、跨 channel 和 `0.01 CNY` 端到端测试。

## 18. 验收标准

全部满足后才能认为设计落地：

- 钱包可以唯一追溯到 MoltsPay Client principal；
- session/channel 变化不会改变钱包归属；
- 新 channel 只需要授权，不需要重新绑定钱包；
- 同名 Agent 不能冒用其他 Client 钱包；
- 模型不能通过工具参数切换 principal；
- MoltsPay 不接触支付宝用户凭据；
- Provider 能验证 principal 与 buyer 的绑定；
- 支付范围同时受 channel scope、金额限额、`seller_id`、`service_id` 和 `resource_id` 约束；
- 身份、权限和业务失败不会触发自动重试或自动开钱包；
- `0.01 CNY` 充值只生成一笔订单、只入账一次且不新增钱包。

