# Python Multi-Chain and Protocol Design

## Decisions

| Decision | Python behavior |
|---|---|
| Chain selection | Client or provider 402 requirements |
| Cross-chain payment | Not supported; one payment stays on one network |
| EVM address | Shared across Base, Polygon, Tempo, and BNB |
| Solana wallet | Separate ed25519 keypair and address |
| Protocol routing | Registry-driven |

## Supported matrix

| Chain | Protocol | Token model | Gas model |
|---|---|---|---|
| `base`, `polygon`, `base_sepolia` | x402/CDP | EIP-3009 USDC/USDT | facilitator |
| `tempo_moderato` | MPP | TIP-20 | direct Tempo flow |
| `bnb`, `bnb_testnet` | BNB intent | USDC/USDT approval | first approval needs BNB/tBNB |
| `solana`, `solana_devnet` | Solana x402 | SPL USDC | provider fee payer may apply |

`balance`, `wechat`, and `alipay` are rails rather than blockchain chains.

## Payment flow

```text
pay -> select rail/chain -> parse 402 -> build signed payment
    -> retry with protocol header -> normalize PaymentResult
```

## Chain addition contract

1. Add `ChainConfig` and token metadata in `chains.py`.
2. Add facilitator and server-registry routing.
3. Add payment-header and response parsing rules.
4. Add models, success/failure tests, README and design-matrix entries.
5. Keep optional imports lazy and provide an actionable install error.

`MULTI-CHAIN-PLAN.md` records delivery history; this document is the current
implementation design.
