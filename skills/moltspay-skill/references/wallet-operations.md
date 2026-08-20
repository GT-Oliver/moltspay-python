# Wallet operations

Use this reference for wallet status, funding, faucets, transfers, and limits.

## Status and identities

```bash
moltspay status --json
```

Crypto wallet balances and provider custodial balances are different:

```bash
moltspay balance query <provider-url> --buyer <buyer-id> --json
moltspay balance whoami <provider-url> --buyer <buyer-id> --json
```

Never print or read the contents of wallet key files or `balance-identity.key`. A shared agent's balance signer may control multiple provider accounts, so keep channel users and buyer accounts separated.

## Initialize

```bash
moltspay init --chain <chain> --max-per-tx <amount> --max-per-day <amount>
```

Creating a wallet is a local mutation. Base, Polygon, Base Sepolia, BNB, BNB Testnet, and Tempo share the EVM wallet; Solana uses a separate Ed25519 wallet.

## Mainnet funding link

```bash
moltspay fund <usd-amount> --chain <chain>
```

The current client accepts `base`, `polygon`, `bnb`, `solana`, and `tempo_moderato` for hosted funding, subject to provider availability, and enforces a USD 5 minimum. Explain that this is wallet funding, not provider-balance recharge. Do not claim arrival until `status` observes the funds.

## Testnet faucet

```bash
moltspay faucet --chain <base_sepolia-or-bnb_testnet-or-solana_devnet-or-tempo_moderato>
```

Faucets are rate-limited. Report `already_claimed` rather than retrying repeatedly.

## Spending limits

Read configuration:

```bash
moltspay config
```

Change only values explicitly requested by the user:

```bash
moltspay config --max-per-tx <amount> --max-per-day <amount>
```

These are local SDK controls, not on-chain allowances or provider-balance limits.

## Transfer or withdrawal

Transfers are ordinary on-chain transactions, not gasless service payments. Before execution, confirm all four values with the user:

- destination address;
- chain/network;
- token;
- amount.

The destination's deposit network must match the selected chain. Recommend a small test transfer first for a new exchange address.

```bash
moltspay transfer <address> <amount> \
  --token <USDC-or-USDT> --chain <evm-chain> --yes --json
```

Use `--yes` only after confirmation. The current transfer implementation is EVM-only and requires native gas. Do not use it for Solana or Tempo despite those values appearing in the generic CLI chain choices.

Return the transaction hash and explorer URL. Never retry an unknown transfer automatically; check the transaction first.

