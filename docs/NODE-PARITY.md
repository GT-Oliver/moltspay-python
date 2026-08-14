# Node.js 2.4 compatibility

The Python SDK implements the portable Node.js SDK capabilities. Browser-only
signers remain in the TypeScript package because Python does not run in browser
wallet contexts.

## Client APIs

```python
from moltspay import MoltsPay

client = MoltsPay(
    chain="base",
    rail_preference=["base", "balance", "wechat"],
    buyer_id="buyer-123",
)

services = client.get_services("https://provider.example")
result = client.pay(
    "https://provider.example",
    "service-id",
    payment_params={"prompt": "hello"},
    rail="balance",  # balance | wechat; omit for crypto
)
```

`PaymentResult` contains `success`, `tx_hash`, `amount`, `token`, `service_id`,
`result`, `network`, `facilitator`, `payment`, `explorer_url`, and `error`.

Other parity APIs include:

- `client.transfer(to, amount, token, chain)` -> `TransferResult`
- `verify_payment(...)`, `get_transaction_status(...)`, `wait_for_transaction(...)`
- `SecureWallet`, `PermitWallet`, `AllowanceWallet`, `AuditLog`
- `PaymentAgent.create_invoice(...)` -> `Invoice`
- encrypted wallet files compatible with Node's scrypt + AES-256-CBC format

## Fiat and balance rails

Balance accounts use integer minor units in SQLite. Top-ups, deductions and
refunds are idempotent on `external_ref`, `request_id`, and deduct transaction
id respectively. Set `auth_mode` to `enforce` to require an EIP-191 signature
and TOFU-bind the buyer's signer.

Mutating operator endpoints require:

```text
MOLTSPAY_BALANCE_ADMIN_TOKEN=<secret>
Authorization: Bearer <secret>
```

WeChat uses Native v3 orders and persists recoverable sessions under
`~/.moltspay/wechat-sessions`.

Provider manifests can add `balance` and `wechat` to `chains`, plus
the corresponding provider and per-service configuration blocks used by the
Node SDK.

New CLI groups include `services`, `fund`, `transfer`, `config`, `balance`,
and `wechat`.

## Deliberate non-parity

`moltspay/web`, EIP-1193, browser localStorage limits, and Solana wallet-adapter
composition remain TypeScript-only browser features.
