# Python Balance Rail Design

The balance rail lets an agent pay from a provider-owned account without an
on-chain transaction for every service call. It is selected explicitly with
`client.pay(..., rail="balance")`.

## Components

- `balance.py`: integer minor-unit ledger, top-ups, deductions, and refunds.
- `server/facilitators/balance.py`: provider verification and settlement.
- `client.py` and `models.py`: routing and normalized results.

Money is stored as integer minor units. Mutations are idempotent by
`external_ref`, `request_id`, and deduction transaction ID. Optional
`auth_mode="enforce"` requires an EIP-191 buyer signature and TOFU-binds the
account to that signer.

## State flow

```text
top-up request -> pending -> settled | failed
payment        -> reserved -> deducted | released
service error  -> refund (idempotent)
```

The provider API covers balance query, atomic deduction, top-up, refund, and
transaction history. Operator mutations require the configured admin bearer
token. Tests must cover duplicate mutations, insufficient funds, limits,
refunds, and authentication failures.
