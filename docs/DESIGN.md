# MoltsPay Python SDK Design

> Status: current implementation reference
>
> Version baseline: `2.4.0` (`src/moltspay/__init__.py`)
>
> This document describes the architecture that exists in the repository. It is
> not a roadmap. Historical implementation plans are kept separately and must
> not be read as a list of currently missing features.

This is the single consolidated design document for the Python SDK. Node.js
remains the protocol reference where wire compatibility is required; this
document describes the Python entry points, persistence, optional-dependency
boundaries and payment rails.

## 1. Scope and design goals

MoltsPay is a Python SDK and service runtime for agent-to-agent payments. The
primary client workflow is:

1. Discover a provider's services.
2. Call a service over HTTP.
3. Interpret `402 Payment Required` (or select an explicitly requested rail).
4. Create the chain- or rail-specific payment proof.
5. Retry the request and return a normalized `PaymentResult`.

The design goals are a single Python API across payment networks, lazy loading
of optional blockchain dependencies, local spending controls, and protocol
compatibility with the Node SDK where the protocol permits it.

## 2. Package boundaries

```text
MoltsPay / AsyncMoltsPay
        |
        +-- x402.py ---------------- HTTP discovery, 402 parsing, retry flow
        |       +-- facilitators/tempo.py   MPP / Tempo
        |       +-- facilitators/bnb.py     BNB approval + intent
        |       +-- facilitators/solana.py  SPL transfer + fee payer
        |
        +-- chains.py -------------------- network and token registry
        +-- wallet.py --------------------- EVM wallet, limits, transfers
        +-- wallet_solana.py -------------- optional Solana keypair
        +-- balance.py -------------------- balance rail client + SQLite ledger
        +-- wechat.py ---------------------- fiat rail client
        |
        +-- cli.py ------------------------- command-line adapter
        +-- server/ ------------------------ provider runtime and facilitators
```

`models.py` contains the public normalized data models. `exceptions.py` is the
public error vocabulary. Integrations such as LangChain and CLI call the same
client APIs rather than implementing payment protocols themselves.

## 3. Network and protocol model

The canonical registry is `src/moltspay/chains.py`. Each chain declares its
network identity, RPC, explorer, VM type, protocol family, and supported token
metadata.

| Chain | VM | Protocol | Environment | Wallet/dependency |
|---|---|---|---|---|
| `base` | EVM | x402 | mainnet | EVM wallet |
| `polygon` | EVM | x402 | mainnet | EVM wallet |
| `base_sepolia` | EVM | x402 | testnet | EVM wallet |
| `tempo_moderato` | EVM/Tempo | MPP | testnet | EVM wallet |
| `bnb` | EVM | BNB facilitator | mainnet | EVM wallet |
| `bnb_testnet` | EVM | BNB facilitator | testnet | EVM wallet |
| `solana` | SVM | x402 + Solana facilitator | mainnet | `solders`/`solana` |
| `solana_devnet` | SVM | x402 + Solana facilitator | testnet | `solders`/`solana` |

The EVM address is shared across EVM chains. Solana uses a separate ed25519
keypair stored in `~/.moltspay/wallet-solana.json`; it must not be treated as an
EVM private key.

## 4. Payment routing

`MoltsPay.pay()` accepts `chain`, `token`, `rail`, and payment parameters. If a
rail is explicitly selected, it takes precedence over direct crypto payment:

- `balance`: calls the provider's balance API and uses a buyer account.
- `wechat`: creates or continues a WeChat Native payment session.
- no rail: performs the protocol flow selected by the chain and the provider's
  402 response.

For direct crypto payments, `X402Client.pay_and_call()` is the protocol router:

1. Send the service request.
2. Parse payment requirements from the response headers/body.
3. Match the requested chain and token.
4. Build the correct signed payload or transaction.
5. Retry with the protocol-specific header.
6. Normalize the response into `PaymentResult`.

Protocol-specific behavior:

- **Base, Polygon, Base Sepolia:** sign an EIP-3009 authorization; the
  facilitator verifies and settles it.
- **Tempo Moderato:** parse `WWW-Authenticate: Payment`, execute a TIP-20
  transfer, then retry with an MPP `Authorization: Payment` credential.
- **BNB:** require the spender allowance to be pre-approved, sign an EIP-712
  payment intent, and let the server execute `transferFrom`. The approval
  transaction itself requires native BNB/tBNB.
- **Solana:** create and partially sign an SPL USDC transfer. When the provider
  supplies `solanaFeePayer`, the server adds the fee-payer signature and submits
  the transaction.

The provider server mirrors this model. `server.MoltsPayServer` loads skill
manifests, advertises supported networks, and uses `FacilitatorRegistry` to
select CDP, BNB, Solana, balance, or WeChat settlement behavior.

## 5. Wallets, limits, and persistence

The default EVM wallet is `~/.moltspay/wallet.json`. `Wallet` owns key loading,
signing, transfers, spending limits, and local spend accounting. Limits are
checked before payment and recorded only after a successful payment result.

Configuration is stored in `config.json` beside the wallet when a config
directory is used. It contains the default chain, `maxPerTx`, `maxPerDay`, rail
preference, and buyer ID. Secrets must remain in the wallet file or environment
variables; provider credentials are not part of service manifests.

Security-related components are deliberately separate:

- `SecureWallet` adds approval/whitelist workflow for transfers.
- `PermitWallet` and `AllowanceWallet` manage permit/allowance-based spending.
- `AuditLog` provides hash-chained local audit records.
- `balance.py` stores monetary values as integer minor units and makes top-ups,
  deductions, and refunds idempotent.

## 6. Optional dependencies and failure behavior

The base installation supports EVM functionality. Solana modules are imported
lazily and produce an actionable installation error when `moltspay[solana]` is
not installed. LangChain integration is similarly optional. A missing optional
dependency must not prevent importing `moltspay` for users who only need EVM
functionality.

Network errors, malformed 402 responses, unsupported chains, insufficient
balance, missing BNB allowance/gas, and exceeded limits are surfaced as
structured results or MoltsPay exception types; callers should inspect
`PaymentResult.success` and `PaymentResult.error` before using `result`.

## 7. Provider and service contract

Providers publish `moltspay.services.json` and Python skill functions. A
manifest declares provider identity, wallet/chains, service IDs, prices,
accepted currencies, input schema, output schema, and the function to execute.
The server loads one or more skill directories and exposes discovery plus
paid execution over HTTP. Provider manifests may additionally configure
`balance` and `wechat` rails.

## 8. Verification and testing strategy

Pure parsing, signing payload construction, limit enforcement, wallet behavior,
CLI behavior, balance idempotency, and fiat-session state transitions are
covered by the test suite under `tests/`. On-chain and provider integration
tests should remain opt-in because they require RPC access, funded wallets, or
provider credentials. Changes to a protocol adapter should include a focused
regression test for both the success path and its most important failure path.

## 9. Source of truth and maintenance rules

- Network metadata: `src/moltspay/chains.py`.
- Public API: `src/moltspay/__init__.py`, `client.py`, and `models.py`.
- Protocol wire behavior: `src/moltspay/x402.py` and `src/moltspay/facilitators/`.
- Provider behavior: `src/moltspay/server/`.
- User-facing command behavior: `src/moltspay/cli.py` and `docs/CLI.md`.

When adding a chain or rail, update the registry, routing logic, normalized
result fields, tests, README support matrix, and the relevant design document
in the same change. `docs/MULTI-CHAIN-PLAN.md` and
`docs/TESTNET-SUPPORT-PLAN.md` describe earlier delivery stages and are not
part of the current design reference.

## 10. Module boundaries and extension rules

`MoltsPay` is the synchronous facade and `AsyncMoltsPay` exposes the same
workflow with async HTTP. `x402.py` owns discovery, 402 parsing,
payment-header construction, retry, and response normalization; it does not
own wallet persistence or provider business logic.

`chains.py` is the registry for chain IDs, RPCs, explorers, tokens, protocol
families, and testnet flags. `facilitators/` contains protocol-specific
signing and settlement behavior. `server/` maps network identifiers to
facilitators and loads provider skill manifests. `cli.py` is an adapter over
public client APIs and must not duplicate payment logic.

For a new module, define public models and errors first, keep network I/O
behind the client/facilitator boundary, add focused success and failure tests,
and document any optional dependency. For a new chain, update the registry,
facilitator mapping, models, tests, README support matrix, and this document.

## 11. Payment rail details

### Balance rail

The balance rail is selected explicitly with `rail="balance"`. `balance.py`
stores monetary values as integer minor units and provides balance queries,
top-ups, atomic deductions, refunds, and transaction history. Mutations are
idempotent by `external_ref`, `request_id`, and deduction transaction ID.
Optional `auth_mode="enforce"` requires an EIP-191 buyer signature and
TOFU-binds the account to that signer. Operator mutations require the admin
bearer token. The state flow is:

```text
top-up request -> pending -> settled | failed
payment        -> reserved -> deducted | released
service error  -> refund (idempotent)
```

### WeChat Native rail

The `wechat` rail uses `wechatpay-native` and WeChat Pay v3 Native orders.
The provider converts CNY to integer fen with `Decimal`, rejects negative or
sub-cent amounts, creates `/v3/pay/transactions/native`, and returns
`code_url` plus `out_trade_no`. Merchant requests use
`WECHATPAY2-SHA256-RSA2048` authorization.

`WechatClient` persists recoverable sessions under
`~/.moltspay/wechat-sessions/<payment_session_id>.json`. A session stores the
resource request, requirement, expiration, code URL, trade number, last
status/error, and result body; both snake_case and Node-compatible camelCase
fields are accepted. The lifecycle is `start_402 -> scan -> status poll ->
fulfill`, with `pending`, `completed`, `expired`, `cancelled`, and `failed`
states. A selected WeChat rail never silently falls back to crypto.

The provider verifies `trade_state == SUCCESS`, checks that the paid amount is
at least the required fen amount, and returns the WeChat transaction ID.
Repeated status and fulfillment operations are safe and must not create a
second order.

## 12. Security and persistence

The EVM wallet uses the Node-compatible scrypt plus AES-256-CBC format. Private
keys must never be logged or placed in provider manifests. `SecureWallet`
provides whitelist and approval workflow; `PermitWallet` and
`AllowanceWallet` isolate delegated-spend operations; `AuditLog` writes
hash-chained local records. Limits are checked before payment and spending is
recorded only after a successful result.

Security controls include matching chain/token requirements against the
registry, nonces and request IDs for replay protection, explicit BNB spender
and allowance checks, and environment/config separation for credentials. Every
new rail must
document authentication, replay protection, amount precision, persistence,
and failure recovery before `pay()` exposes it.

## 13. MCP adapter and tool contracts

The MCP module is a thin stdio adapter over `MoltsPay` public methods. It owns
tool schemas, input validation, confirmation gates, error envelopes, and
serialization; it does not implement x402, ledger, WeChat, polling, or
QR encoding. The source of truth for the detailed mapping is
[`MCP-FIAT-BALANCE-TOOLS-DESIGN.md`](MCP-FIAT-BALANCE-TOOLS-DESIGN.md).

Every tool specification must document its inputs, outputs, delegated client
method, and payment rail. `moltspay_wechat_start` returns both the WeChat
`codeUrl` and a Base64-encoded PNG QR image generated inside the tool; a second
QR tool is not required for WeChat payment. Balance top-up still returns its
`codeUrl` for the host/UI to render. The CLI may render a terminal QR code. The
MCP adapter does not return private keys, signatures, or payment credentials.

The implemented tool groups are wallet/status, read-only provider service
discovery, balance, WeChat Native, unified payment, and configuration. A tool
must not be documented as implemented unless it is registered by
`mcp/server.py`. `moltspay_services` delegates to `MoltsPay.get_services()` and
must not create an order or initiate payment while discovering services.

## 14. Reliability and verification

Pure parsing, signing payload construction, limits, wallet behavior, CLI
behavior, balance idempotency, and fiat session transitions belong in the test
suite. On-chain, provider, and credential-backed
tests remain opt-in. Network retries use bounded exponential backoff, respect
`Retry-After`, never automatically retry non-idempotent POSTs, and never
extend the original business deadline. Payment creation and fulfillment must
use stable idempotency keys (`paymentSessionId`, `outTradeNo`, `tradeNo`, or
server `externalRef`).

Acceptance coverage must include missing optional dependencies, malformed 402
responses, unsupported chains, insufficient funds, limits, duplicate
mutations, 429/5xx polling, and timeout recovery.
