# MoltsPay Python SDK Design

> Status: current implementation reference
>
> Version baseline: `2.4.0` (`src/moltspay/__init__.py`)
>
> This document describes the architecture that exists in the repository. It is
> not a roadmap. Historical implementation plans are kept separately and must
> not be read as a list of currently missing features.

Detailed module and payment-rail designs are indexed in
[DESIGN-INDEX.md](DESIGN-INDEX.md). Node.js remains the protocol reference
where wire compatibility is required; these documents describe the Python
entry points, persistence, and optional-dependency boundaries.

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
        +-- wechat.py / alipay.py ---------- fiat rail clients
        |
        +-- cli.py ------------------------- command-line adapter
        +-- mcp/server.py ------------------ MCP adapter
        +-- server/ ------------------------ provider runtime and facilitators
```

`models.py` contains the public normalized data models. `exceptions.py` is the
public error vocabulary. Integrations such as LangChain, CLI, and MCP call the
same client APIs rather than implementing payment protocols themselves.

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
- `alipay`: delegates to the configured Alipay client/provider flow.
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
select CDP, BNB, Solana, balance, WeChat, or Alipay settlement behavior.

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
not installed. LangChain and MCP integrations are similarly optional. A
missing optional dependency must not prevent importing `moltspay` for users who
only need EVM or non-MCP functionality.

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
`balance`, `wechat`, and `alipay` rails.

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
`docs/TESTNET-SUPPORT-PLAN.md` describe earlier delivery stages; the current
design is documented by the files indexed in `docs/DESIGN-INDEX.md`.
