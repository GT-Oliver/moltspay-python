# Python Module Design

## Client and protocol router

`MoltsPay` is the synchronous facade and `AsyncMoltsPay` exposes the same
workflow with async HTTP. `x402.py` owns discovery, 402 parsing, payment-header
construction, retry, and response normalization. It does not own wallet
persistence or provider business logic.

The routing order is:

```text
explicit rail -> balance / wechat / alipay
no rail       -> chain protocol -> x402 / MPP / BNB / Solana
```

## Chain and facilitator modules

`chains.py` is the registry for chain IDs, RPCs, explorers, tokens, protocol
families, and testnet flags. `facilitators/` contains protocol-specific signing
and settlement behavior. The server registry maps networks such as
`eip155:8453` and `solana:devnet` to those facilitators.

## Wallet and security modules

- `wallet.py`: EVM keys, signing, transfers, limits, and Node-compatible files.
- `wallet_solana.py`: separate ed25519 keypair and Solana wallet file.
- `secure_wallet.py`: whitelist and approval controls.
- `permit_wallet.py`: permit/allowance workflows.
- `audit.py`: hash-chained local audit records.

Solana dependencies are imported lazily so EVM-only installations still import
the package.

## Provider runtime, CLI, and integrations

`MoltsPayServer` loads skill manifests and Python handlers, builds 402
requirements, and dispatches verification/settlement through
`FacilitatorRegistry`. `cli.py` and `mcp/` are adapters over public client
APIs; they must not duplicate payment logic.

For a new module, define public models and errors first, keep network I/O behind
the client/facilitator boundary, add focused success and failure tests, and
document any optional dependency.
