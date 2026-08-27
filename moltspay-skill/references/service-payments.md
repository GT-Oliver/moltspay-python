# Service purchases

Use this reference for discovery, rail selection, crypto payments, and provider-balance purchases.

## Discover and validate

```bash
moltspay services <provider-url> --json
```

Select a service from the live response. Validate required parameters and use rail-specific quotes. In particular, do not label an Alipay or WeChat CNY amount with a top-level crypto currency.

When several services match, present the relevant choices with service ID, current price/currency, and payment rails. Do not pay until the intended service is unambiguous.

## Rail selection

Apply this order:

1. If the user explicitly requests a supported rail, use it.
2. Otherwise, if the provider advertises `balance`, query it. Use it directly when sufficient.
3. If balance is insufficient, show the provider's current top-up choices and other advertised purchase rails. Do not assume the user wants to recharge.
4. If only one viable rail exists, explain it before creating an order or spending.

Balance top-up is WeChat-only. Alipay is a direct service-purchase rail, not a balance top-up rail.

## Provider balance purchase

Query the account and its current policy:

```bash
moltspay balance query <provider-url> --buyer <buyer-id> --json
```

For chat automation, attempt the purchase without a blocking automatic top-up:

```bash
moltspay pay <provider-url> <service-id> '<params-json>' \
  --rail balance --buyer <buyer-id> --no-auto-topup --json
```

If it succeeds, return the service result. If it returns `insufficient_balance`, follow the amount-selection and top-up state machine in [chat-lifecycles.md](chat-lifecycles.md). A limit failure is not an insufficient-balance failure; report the limit instead of suggesting a top-up.

After a credited top-up, retry the original purchase once with the same provider, service, parameters, buyer ID, and a stable request context. If still insufficient, refresh the provider policy and return to amount selection; never create another order automatically.

## On-chain purchase

```bash
moltspay pay <provider-url> <service-id> '<params-json>' \
  --chain <chain> --token <USDC-or-USDT> --json
```

Use only a chain and token advertised by that service. Current Python chain identifiers:

| Environment | Chains |
|---|---|
| Mainnet | `base`, `polygon`, `bnb`, `solana` |
| Testnet | `base_sepolia`, `bnb_testnet`, `solana_devnet`, `tempo_moderato` |

Tempo uses its advertised TIP-20 token rather than assuming USDC. Solana requires the optional Solana dependencies and a separate wallet.

BNB service payments may require a prior token approval and native BNB/tBNB:

```bash
moltspay approve --chain <bnb-or-bnb_testnet> --spender <provider-advertised-spender>
```

Never guess the spender. Approval is an on-chain side effect and requires explicit authorization.

## Parameters

Prefer a JSON object when a service has more than one input:

```bash
moltspay pay <url> <service> '{"prompt":"a cat dancing","duration":5}' --chain base --json
```

The shortcuts `--prompt`, `--image`, and `--data` are valid when they match the discovered schema. Never invent unsupported fields.

## Payment failures

- `unsupported_chain` or token mismatch: rediscover and choose an advertised combination.
- `insufficient_funds`: fund the selected chain; do not fund a different chain.
- per-transaction or daily limit: report it; change limits only on explicit request.
- timeout or unknown state: inspect returned identifiers and status before retrying. A retry must not create a second interactive order.
- service failure after a balance deduction: report whether the response says it was refunded.

