# MoltsPay Python SDK 法币支付与 MCP 模块重构实施报告

> 实施日期：2026-08-11  
> 实施方案：方案 2——重构本地仓库中的 `moltspay-python`  
> 限定范围：Balance、WeChat Pay、MCP
> 验收状态：已完成

## 1. 结论

本项目不需要完全重新实现，也不需要基于 GitHub 上游仓库另起一套代码。

本地仓库已经具备可复用的链上支付、钱包、链配置、公开 API 和测试基础。现有问题主要集中在法币支付生命周期、会话恢复、MCP 工具边界及资源管理，因此采用局部架构重构，可以保留稳定能力，同时避免扩大改动风险。

本次重构已经执行，并达到以下结果：

- 全量测试：`89 passed, 4 skipped`。
- 目标模块行覆盖率均高于计划要求的 `85%`。
- 冻结的链上模块与 facilitator 文件无内容变化。
- `git diff --check` 通过。
- 测试不再出现项目代码导致的 SQLite 或 HTTP 客户端未关闭警告。
- 现有同步 SDK 入口继续保留，作为新生命周期 API 的兼容包装器。

## 2. 为什么选择重构本地仓库

三个方案的判断如下：

| 方案 | 判断 | 依据 |
|---|---|---|
| 基于上游仓库重新实现现有功能 | 不推荐 | 需要重新合并本地差异，且无法直接继承当前仓库已经存在的集成与测试成果。 |
| 重构本地仓库 | 已采用 | 可以冻结稳定的链上模块，只修复法币和 MCP 边界，风险与迁移成本最低。 |
| 完全重新实现全部代码 | 不推荐 | 会把钱包、链配置、签名、结算和 facilitator 等非问题模块一并置于回归风险中。 |

重构对象不是整个 SDK，而是以下边界：

1. 支付创建、状态查询和服务履约之间的职责边界。
2. Pending、Paid、Unknown、Expired 等状态的持久化与恢复。
3. MCP 工具的输入约束、确认策略和敏感字段输出边界。
4. Balance buyer、幂等键和资源生命周期的确定性。

## 3. 问题来自设计文档，还是原有架构

两者都有责任，但产生问题的方式不同：

- 原有架构造成了直接的运行时和可维护性问题。
- 设计文档缺少可执行的语义约束，使这些问题没有在实现和测试阶段被拦截。

| 问题 | 直接成因 | 设计文档缺口 | 主要归因 |
|---|---|---|---|
| `status` 可能触发服务执行 | 查询、支付确认和 `/execute` 耦合在阻塞流程中 | 未明确规定 `status` 必须只读 | 原有架构 |
| 网络超时后可能重复支付 | 缺少 `unknown` 状态与可恢复会话 | 未定义不确定支付结果的查询和重试规则 | 架构与文档共同造成 |
| Balance buyer 选择不确定 | 显式 `buyer_id` 没有完整贯穿 top-up 调用链 | 未明确显式参数与默认配置的优先级 | 实现为主，文档助长 |
| MCP 通用支付入口职责过宽 | 交互式 rail 与非交互支付共用 `pay` | 确认矩阵、dry-run 和输出白名单不完整 | 设计文档 |
| SQLite 连接警告 | 测试和初始化失败路径没有确定性关闭连接 | 未设置资源生命周期验收门槛 | 工程实现 |

因此，设计文档不是错误到必须推倒重写；真正需要修复的是法币 rail 的状态机与 MCP 适配层边界。设计文档则需要把这些边界写成可测试的规则。

## 4. 实施范围

### 4.1 已改动模块

- Balance。
- WeChat Pay。
- MCP。

为完成上述模块集成，允许并已经进行了以下最小改动：

- `src/moltspay/client.py`。
- `src/moltspay/exceptions.py`。
- `src/moltspay/server/server.py` 中的法币路由。
- `src/moltspay/__init__.py`。
- `pyproject.toml`。
- README、设计文档及测试。

### 4.2 明确冻结的模块

以下链上模块未发生内容变化：

- `src/moltspay/x402.py`。
- `src/moltspay/wallet.py`。
- `src/moltspay/chains.py`。
- BNB facilitator。
- Solana facilitator。
- Tempo facilitator。
- CDP facilitator。

本次没有修改链上路由、签名、结算、钱包行为或链配置。

## 5. 目标支付生命周期

```text
start -> pending -> paid -> fulfill -> completed
           |          |
           +-> unknown+-> failed
           +-> expired
           +-> cancelled
```

各阶段规则如下：

1. `start` 只创建支付意图，并持久化恢复所需的最小信息。
2. `status` 只能读取本地状态或查询支付订单，不允许执行付费服务。
3. `fulfill` 是唯一允许提交已支付凭证并执行 provider 服务的阶段。
4. 网络结果不确定时进入 `unknown`，禁止自动创建第二笔支付。
5. `unknown` 必须通过查询已有订单来消解。
6. 旧的阻塞式方法继续保留，但内部由新生命周期组合实现。

## 6. WeChat Pay 重构结果

### 6.1 原有问题

- 状态检查和服务执行边界不清晰。
- 查询支付状态可能再次调用 `/execute`。
- 网络错误无法区分“未支付”和“结果未知”。
- 终态、过期状态和不安全标识符处理不完整。

### 6.2 已实施改动

- `status()` 改为调用只读的 `GET /payments/wechat/{tradeNo}`。
- 服务端新增对应查询路由，并复用已有 `WechatFacilitator.query_order()`。
- 只有 `fulfill()` 可以携带 `X-Payment` 调用 `/execute`。
- 增加 `unknown` 状态。
- 增加 `completed`、`cancelled`、`expired`、`failed` 等终态保护。
- 增加会话过期处理和标识符安全校验。
- 兼容 Node.js 使用 camelCase 持久化的会话文件。
- 保留 `pay_402()` 阻塞式兼容入口。

### 6.3 结果

状态查询现在是真正只读的。网络失败不会自动重新支付；系统会保存 `unknown` 状态，等待后续查询或人工判断。

## 7. Balance 重构结果

### 7.1 原有问题

- 调用方显式传入的 `buyer_id` 没有完整下传到 top-up order。
- top-up 返回信息不足，进程重启后难以恢复。
- 过期状态和标识符校验不一致。
- SQLite 连接生命周期依赖调用方自行处理。

### 7.2 已实施改动

- 显式 `buyer_id` 优先于本地默认配置。
- `buyer_id` 正确传递到 `BalanceClient.create_topup_order()`。
- top-up 会话持久化以下信息：
  - buyer。
  - server URL。
  - `out_trade_no`。
  - 金额包。
  - 当前状态。
  - 创建、更新时间和过期时间。
- top-up 查询和确认支持进程重启恢复。
- 增加安全标识符校验和自动过期。
- `BalanceLedger` 支持上下文管理器。
- 初始化失败时关闭已经打开的 SQLite 连接。
- 保留扣款、充值和退款的幂等语义。

### 7.3 结果

Balance buyer 的选择变得确定，top-up 订单可以恢复，SQLite 资源也能在正常退出和初始化失败路径中确定性释放。

## 8. MCP 重构结果

### 8.1 原有问题

- MCP 工具输入缺少严格 schema。
- 交互式支付与非交互式支付混用同一个工具。
- dry-run、确认和真实执行的边界不明确。
- 会话序列化可能暴露 provider requirement 或请求数据。
- 二维码只作为字符串返回，没有标准 MCP 图片内容。

### 8.2 已实施改动

- 在 `src/moltspay/mcp/` 下重新建立 MCP 包。
- 使用受约束的 Pydantic 参数类型：
  - HTTP URL。
  - 非空字符串。
  - 安全标识符。
  - 分页上限和偏移。
  - 非负金额和正数超时。
- 使用稳定的成功/错误信封。
- 对产生外部副作用的工具增加显式确认。
- `dryRun=true` 完全无副作用，也不要求支付确认。
- 会话输出采用字段白名单，不序列化 requirement、原始请求数据等敏感字段。
- WeChat QR 同时返回：
  - `codeUrl` fallback。
  - Base64 PNG。
  - FastMCP `ImageContent`。
- 通用 `pay` 拒绝 WeChat 交互式 rail。
- Balance 通用支付关闭隐式 `auto_topup`。
- 新增 `moltspay-mcp` 命令行入口。
- MCP 作为可选依赖提供。

### 8.3 MCP 交互规则

| 类型 | 调用方式 |
|---|---|
| 链上或 Balance 非交互支付 | 使用通用 `pay` |
| WeChat Pay | `wechat_start → wechat_status → wechat_fulfill` |
| 无副作用预检查 | 使用 `dryRun=true` |

## 9. 测试与验收证据

### 9.1 全量测试

执行命令：

```powershell
pytest -q
```

结果：

```text
89 passed, 4 skipped
```

### 9.2 覆盖率

执行命令：

```powershell
$env:COVERAGE_FILE = Join-Path $env:TEMP "moltspay-fiat-coverage"
pytest -q `
  --cov=moltspay.balance `
  --cov=moltspay.wechat `
  --cov=moltspay.mcp `
  --cov-report=term-missing
```

结果：

| 模块 | 行覆盖率 |
|---|---:|
| Balance | 93% |
| MCP server | 97% |
| WeChat Pay | 96% |

计划要求为至少 85%，当前结果通过。

### 9.3 覆盖的关键分支

- 重复 Balance 扣款、充值和退款。
- 显式 buyer 覆盖默认 buyer。
- Top-up 会话恢复与不安全标识符拒绝。
- WeChat 只读状态查询。
- WeChat HTTP 非 200、非 JSON、网络异常与未知状态。
- WeChat fulfill 的成功、402、失败与网络不确定结果。
- MCP 确认矩阵和无副作用 dry-run。
- MCP 错误分类、字段白名单、真实 FastMCP 注册和图片内容。

### 9.4 冻结文件核验

以下核验通过：

```text
git diff --exit-code HEAD -- <frozen files>
FROZEN_FILES_OK
```

### 9.5 Diff 格式核验

```powershell
git diff --check
```

结果通过。Git 仍提示部分工作区文件在下一次 Git 写入时可能从 LF 转换为 CRLF，这属于现有换行符配置提示，不是 diff 格式错误。

## 10. 已知告警

测试仍然显示两个外部依赖告警：

1. `websockets.legacy` 已弃用。
2. FastMCP/Pydantic Settings 对 `lifespan` 前向引用发出 `IncompleteFieldDefinitionWarning`。

这两个告警来自当前安装的第三方依赖，不影响本次法币/MCP 实现，也不是项目代码产生的资源泄漏。

## 11. 明确延期事项

以下内容不属于本次重构范围：

- 全局把金额类型迁移为 `Decimal`。
- 全面重构 `AsyncMoltsPay`。
- 替换 HTTP server 技术。
- 替换 SQLite ledger 技术。
- 修改任何链上支付、签名、路由、结算或钱包实现。
- 修改 BNB、Solana、Tempo 或 CDP facilitator。
- 在默认测试套件中加入真实微信商户凭据测试。

## 12. 主要文件

| 类别 | 路径 |
|---|---|
| 实施计划 | `docs/FIAT-MCP-REFACTOR-PLAN.md` |
| MCP 设计 | `docs/MCP-FIAT-BALANCE-TOOLS-DESIGN.md` |
| Balance | `src/moltspay/balance.py` |
| WeChat Pay | `src/moltspay/wechat.py` |
| MCP | `src/moltspay/mcp/server.py` |
| SDK 集成 | `src/moltspay/client.py` |
| 服务端法币路由 | `src/moltspay/server/server.py` |
| Balance 测试 | `tests/test_balance.py`、`tests/test_balance_topup.py` |
| 法币客户端测试 | `tests/test_fiat_clients.py` |
| MCP 测试 | `tests/test_mcp.py` |

## 13. 最终状态

本次限定范围重构已经完成并通过验收。当前工作区尚未暂存或提交，可以继续进行人工 diff 审查，再决定提交与发布策略。
