# Python Security Design

The EVM wallet uses the Node-compatible scrypt + AES-256-CBC format. Private
keys must never be logged or placed in provider manifests.

Limits are checked before payment and spending is recorded only after success.
`SecureWallet` adds whitelist/approval workflow; `PermitWallet` and
`AllowanceWallet` isolate delegated-spend operations. `AuditLog` writes
hash-chained local records. Balance mutations use integer minor units and
idempotency keys.

| Risk | Control |
|---|---|
| Wrong chain/token | Match 402 requirements against the registry |
| Replay/double charge | Nonces, request IDs, idempotent ledgers |
| BNB approval abuse | Explicit spender and allowance checks |
| Solana key confusion | Separate wallet file and optional dependency |
| Unintended agent spend | Limits plus MCP confirmation gate |
| Credential leakage | Environment/config separation |

Every new rail must document authentication, replay protection, amount
precision, persistence, and failure recovery before `pay()` exposes it.
