# MoltsPay Python SDK

[![PyPI version](https://img.shields.io/pypi/v/moltspay.svg)](https://pypi.org/project/moltspay/)
[![Python versions](https://img.shields.io/pypi/pyversions/moltspay.svg)](https://pypi.org/project/moltspay/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Python payment SDK and provider runtime for AI agents.

MoltsPay lets an agent discover a provider's services, handle an HTTP `402 Payment Required` response, pay through a supported chain or payment rail, and receive the service result through one API.

## Features

- Discover priced services exposed by a MoltsPay provider.
- Pay with USDC or USDT through x402 and chain-specific facilitators.
- Use Base, Polygon, BNB Chain, Solana, and Tempo networks.
- Use provider balances, WeChat Pay, or Alipay when the provider supports them.
- Auto-create EVM and Solana wallets and enforce local spending limits.
- Run through Python, the CLI, MCP, LangChain, or the provider server.
- Share the EVM wallet format with the Node.js MoltsPay SDK.

Ethereum mainnet is not supported.

## Requirements

- Python 3.9 or newer
- Network access to the selected provider, RPC, and facilitator
- A funded wallet for mainnet payments, or faucet tokens on a supported testnet

## Installation

Install the EVM SDK and CLI:

```bash
pip install moltspay
```

The base package includes `qrcode`, so `moltspay fund` can display a funding QR code in the terminal.

Install only the optional integrations you need:

```bash
pip install "moltspay[solana]"    # Solana wallets and payments
pip install "moltspay[mcp]" pillow # MCP server with PNG QR output
pip install "moltspay[langchain]" # LangChain tools
pip install "moltspay[server]"    # Coinbase CDP server settlement
```

Extras can be combined, for example:

```bash
pip install "moltspay[mcp,solana]" pillow
```

## Quick start

The client creates `~/.moltspay/wallet.json` on first use.

```python
from moltspay import MoltsPay

provider = "https://moltspay.com/a/zen7"
client = MoltsPay(chain="base", timeout=180.0)

print(f"Wallet: {client.address}")

# Always discover first: service IDs, prices, inputs, and supported chains
# are controlled by the provider.
services = client.discover(provider)
for service in services:
    print(service.id, service.name, service.price, service.currency, service.chains)

result = client.pay(
    provider,
    "b23c6959-605f-49ff-98de-aea28705d386",
    prompt="a cat dancing in the rain",
)

if result.success:
    print(f"Paid: {result.amount} {result.token}")
    print(f"Transaction: {result.tx_hash}")
    print(f"Result: {result.result}")
else:
    print(f"Payment failed: {result.error}")

client.close()
```

The service UUID above is an example from the Zen7 provider. Call `discover()` instead of hard-coding a service ID in production.

### Testnet

Base Sepolia is the simplest way to try the SDK without real funds:

```python
from moltspay import MoltsPay

with MoltsPay(chain="base_sepolia", timeout=180.0) as client:
    faucet = client.faucet()
    if not faucet.success:
        raise RuntimeError(faucet.error)

    result = client.pay(
        "https://moltspay.com/a/zen7",
        "b23c6959-605f-49ff-98de-aea28705d386",
        prompt="a robot dancing in the rain",
    )
    print(result.result if result.success else result.error)
```

Faucets are rate-limited. Inspect `FaucetResult.success` and `FaucetResult.error` before using the returned amount.

## Networks and protocols

The chain name passed to `MoltsPay(chain=...)` must be one of the following:

| Chain | Environment | VM | Payment protocol | Notes |
|---|---|---|---|---|
| `base` | Mainnet | EVM | x402 / EIP-3009 | Default; gasless USDC authorization |
| `polygon` | Mainnet | EVM | x402 / EIP-3009 | Gasless USDC authorization |
| `base_sepolia` | Testnet | EVM | x402 / EIP-3009 | Faucet available |
| `bnb` | Mainnet | EVM | x402 / EIP-712 intent | Token approval requires BNB once |
| `bnb_testnet` | Testnet | EVM | x402 / EIP-712 intent | Faucet available; approval requires tBNB |
| `solana` | Mainnet | SVM | x402 / SPL transfer | Requires the `solana` extra |
| `solana_devnet` | Testnet | SVM | x402 / SPL transfer | Requires the `solana` extra; faucet available |
| `tempo_moderato` | Testnet | EVM/Tempo | MPP / TIP-20 | Native gas-free flow; faucet available |

The provider must advertise the selected chain and token. Base, Polygon, and Base Sepolia use the same EVM address. Solana uses a separate ed25519 wallet.

“Gasless” refers to the service-payment flow supported by the relevant facilitator. BNB token approval and ordinary EVM transfers are separate on-chain transactions and require the chain's native gas token.

## Wallets and funding

Default local files:

| File | Purpose |
|---|---|
| `~/.moltspay/wallet.json` | EVM private key, limits, and daily spending |
| `~/.moltspay/wallet-solana.json` | Solana keypair, created lazily |
| `~/.moltspay/config.json` | Buyer ID and payment preferences |

Wallet files contain signing keys. Back them up, keep them out of source control, and do not print or send their contents.

### Check balances

```python
client = MoltsPay(chain="base")

balance = client.balance()
print(balance.usdc, balance.usdt, balance.native)

for chain, amounts in client.get_all_balances().items():
    print(chain, amounts)
```

### Fund a mainnet wallet

`fund()` returns a hosted onramp URL. `fund_qr()` also renders it as a terminal QR code. The minimum amount is USD 5.

```python
result = client.fund_qr(amount=10, chain="base")
if not result.success:
    print(result.error)
```

You can also transfer the correct token directly to `client.address` on the selected network. Always verify the chain and token contract before sending.

### Spending limits

```python
client.set_limits(max_per_tx=10, max_per_day=100)

limits = client.limits()
print(limits.max_per_tx)
print(limits.max_per_day)
print(limits.spent_today)
print(limits.remaining_daily)
```

These are local SDK policy controls, not on-chain allowances. They protect calls made through this wallet file but cannot prevent the key from being used elsewhere.

## Payment rails

`MoltsPay.pay()` uses an on-chain payment when `rail` is omitted. A provider may also advertise additional rails.

| Rail | Select with | Behavior |
|---|---|---|
| Crypto | Omit `rail` | Uses `chain` and `token` to complete the provider's 402 challenge |
| Provider balance | `rail="balance"` | Deducts from a provider-managed buyer balance; supports recoverable top-ups |
| WeChat Pay | `rail="wechat"` | Creates a Native payment QR session, polls it, then fulfills the service |
| Alipay | `rail="alipay"` | Uses the official `alipay-bot` buyer flow and resumes fulfillment |

Example using a provider balance:

```python
client = MoltsPay(buyer_id="buyer-123")

result = client.pay(
    "https://provider.example",
    "service-id",
    rail="balance",
    payment_params={"prompt": "hello"},
    rail_options={
        "topup_mode": "manual",
        "topup_pack": "10.00",
    },
)
```

Interactive WeChat and Alipay flows persist recoverable sessions locally. See [Node.js compatibility and fiat rails](docs/NODE-PARITY.md) for the session and provider contracts.

## Core API

```python
client = MoltsPay(
    wallet_path=None,
    private_key=None,
    chain="base",
    timeout=180.0,
    config_dir=None,
    rail_preference=None,
    buyer_id=None,
)
```

| API | Description |
|---|---|
| `discover(provider_url)` | Return the provider's available `Service` objects |
| `get_services(provider_url)` | Return provider metadata and services |
| `pay(provider_url, service_id, ...)` | Pay for and execute a service |
| `balance(chain=None)` | Read one wallet balance |
| `get_all_balances()` | Read balances across configured chains |
| `transfer(to, amount, token="USDC", chain=None)` | Send an EVM token transfer |
| `limits()` / `set_limits(...)` | Read or update local spending limits |
| `fund(amount, chain=None)` / `fund_qr(...)` | Create a mainnet funding link |
| `faucet()` | Request tokens for the client's testnet |
| `get_buyer_balance(...)` | Query a provider balance account |
| `create_balance_topup_order(...)` | Create a recoverable provider-balance top-up |

`PaymentResult` normalizes payment and service output through `success`, `amount`, `token`, `service_id`, `tx_hash`, `result`, `error`, `explorer_url`, `network`, `facilitator`, and `payment`.

## Async client

```python
import asyncio

from moltspay import AsyncMoltsPay


async def main():
    async with AsyncMoltsPay(chain="base", timeout=180.0) as client:
        services = await client.discover("https://moltspay.com/a/zen7")
        service_id = services[0].id
        result = await client.pay(
            "https://moltspay.com/a/zen7",
            service_id,
            prompt="a cat playing piano",
        )
        print(result.result if result.success else result.error)


asyncio.run(main())
```

## Error handling

Methods may return an unsuccessful result or raise a typed exception, depending on whether the failure happened before or during the payment flow.

```python
from moltspay import (
    InsufficientBalance,
    InsufficientFunds,
    LimitExceeded,
    MoltsPay,
    PaymentError,
    UnsupportedRail,
)

try:
    result = MoltsPay().pay("https://provider.example", "service-id")
    if not result.success:
        print(result.error)
except InsufficientBalance as exc:
    print(exc.details.get("topupPacks", []))
except InsufficientFunds as exc:
    print(f"Need {exc.required}, have {exc.balance}")
except LimitExceeded as exc:
    print(f"Exceeded {exc.limit_type} limit")
except (UnsupportedRail, PaymentError) as exc:
    print(exc)
```

## CLI

All normal command results are emitted as JSON on stdout. QR codes and prompts use stderr so the JSON remains machine-readable.

```bash
moltspay --help
moltspay init --chain base
moltspay services https://moltspay.com/a/zen7
moltspay faucet --chain base_sepolia
moltspay fund 10 --chain base
moltspay status
```

Pay for a service:

```bash
moltspay pay \
  https://moltspay.com/a/zen7 \
  b23c6959-605f-49ff-98de-aea28705d386 \
  --chain base_sepolia \
  --prompt "a cat dancing in the rain"
```

The CLI also exposes `transfer`, `approve`, `config`, `balance`, `wechat`, `alipay`, `validate`, and `server` commands. Run `moltspay <command> --help` or read the [CLI reference](docs/CLI.md) for details.

## MCP server

Install and start the stdio MCP server:

```bash
pip install "moltspay[mcp]" pillow
moltspay-mcp
```

Pillow is required for the MCP server's PNG QR image output. The MCP adapter exposes namespaced tools such as `moltspay_status`, `moltspay_pay`, balance top-up tools, and recoverable WeChat/Alipay session tools. Tool results use a stable envelope with `ok`, `data` or `error`, `requestId`, and `retried`.

Require explicit confirmation for money-moving or fulfillment tools:

```bash
export MOLTSPAY_MCP_REQUIRE_CONFIRM=1
moltspay-mcp
```

The unified `moltspay_pay` tool supports on-chain and provider-balance payments. WeChat and Alipay use their dedicated start/status/fulfill tools.

## LangChain

```python
from moltspay.integrations.langchain import get_moltspay_tools

tools = get_moltspay_tools(chain="base")
```

This returns payment and discovery tools backed by the same `MoltsPay` client. Agent framework setup depends on the LangChain version in your application.

## Run a provider

A provider skill directory contains:

```text
my_skill/
├── __init__.py
└── moltspay.services.json
```

Start one or more skill directories:

```bash
pip install "moltspay[server]"
moltspay-server ./my_skill --host 0.0.0.0 --port 8402
```

The server exposes `/services`, `/.well-known/agent-services.json`, `/execute`, and `/health`. Provider manifests define services, prices, functions, wallets, networks, and optional balance or fiat rails. See the [server guide](docs/SERVER.md) and [architecture reference](docs/DESIGN.md).

## Protocol flow reference

The following diagrams summarize the chain-specific payment flows. Application code normally does not need to implement these steps directly; `MoltsPay.pay()` selects the flow from the requested chain and the provider's `402` response.

### x402 with EIP-3009 — Base, Polygon, and Base Sepolia

```text
Client                         Provider                    Facilitator
  | POST /execute                |                              |
  |----------------------------->|                              |
  | 402 + payment requirements   |                              |
  |<-----------------------------|                              |
  | sign EIP-3009 authorization  |                              |
  | POST /execute + payment      |                              |
  |----------------------------->| verify and settle            |
  |                              |----------------------------->|
  | 200 + service result         |                              |
  |<-----------------------------|                              |
```

The client signs a USDC transfer authorization without submitting a gas-paying transaction. The facilitator verifies and settles the payment.

### x402 with a Solana fee payer

```text
Client                         Provider / fee payer        Solana
  | POST /execute                |                              |
  |----------------------------->|                              |
  | 402 + fee payer details      |                              |
  |<-----------------------------|                              |
  | partially sign SPL transfer  |                              |
  | POST /execute + payment      |                              |
  |----------------------------->| add fee-payer signature      |
  |                              |----------------------------->|
  | 200 + service result         |                              |
  |<-----------------------------|                              |
```

The Solana wallet signs the SPL token transfer. The provider-supplied fee payer completes and submits the transaction.

### x402 with a BNB payment intent

```text
Client                         Provider                    BNB Chain
  | POST /execute                |                              |
  |----------------------------->|                              |
  | 402 + spender details        |                              |
  |<-----------------------------|                              |
  | sign EIP-712 payment intent  |                              |
  | POST /execute + payment      |                              |
  |----------------------------->| execute transferFrom         |
  |                              |----------------------------->|
  | 200 + service result         |                              |
  |<-----------------------------|                              |
```

The service payment uses a signed EIP-712 intent and server-sponsored execution. The buyer must first approve the advertised spender; that approval is an on-chain transaction requiring BNB or tBNB.

### MPP — Tempo Moderato

```text
Client                         Provider                    Tempo
  | POST /execute                |                              |
  |----------------------------->|                              |
  | 402 + WWW-Authenticate       |                              |
  |<-----------------------------|                              |
  | execute TIP-20 transfer      |----------------------------->|
  | POST + Authorization         |                              |
  |----------------------------->| verify payment               |
  | 200 + service result         |                              |
  |<-----------------------------|                              |
```

Tempo uses Machine Payments Protocol credentials and a TIP-20 transfer. Tempo Moderato provides the native gas-free execution model used by this flow.

Across all four flows, service discovery happens first, payment requirements come from the provider, and the service result is returned only after the payment proof has been accepted. See [the architecture reference](docs/DESIGN.md) for parsing, routing, facilitator, and settlement details.

## Documentation

- [Architecture and protocol routing](docs/DESIGN.md)
- [CLI reference](docs/CLI.md)
- [Provider server guide](docs/SERVER.md)
- [Node.js compatibility and fiat rails](docs/NODE-PARITY.md)
- [OpenClaw + Feishu balance purchase flow](docs/OPENCLAW-FEISHU-BALANCE-PURCHASE.md)
- [Whitepaper](docs/WHITEPAPER.md)
- [Hosted documentation](https://moltspay.com/docs)
- [LLM-readable documentation](https://moltspay.com/llms.txt)

## Development

```bash
git clone https://github.com/Yaqing2023/moltspay-python.git
cd moltspay-python
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

Network and provider integration tests may require RPC access, credentials, or funded wallets. Unit tests under `tests/` mock external payment operations where possible.

## Related projects and support

- [MoltsPay website](https://moltspay.com)
- [MoltsPay on PyPI](https://pypi.org/project/moltspay/)
- [MoltsPay Node.js SDK](https://github.com/Yaqing2023/moltspay)
- [MoltsPay on npm](https://www.npmjs.com/package/moltspay)
- [x402 protocol](https://www.x402.org/)
- [Discord](https://discord.gg/QwCJgVBxVK)

## License

[MIT](LICENSE)
