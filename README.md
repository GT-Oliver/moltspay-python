# MoltsPay Python SDK

[![PyPI version](https://img.shields.io/pypi/v/moltspay.svg)](https://pypi.org/project/moltspay/)
[![Python versions](https://img.shields.io/pypi/pyversions/moltspay.svg)](https://pypi.org/project/moltspay/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**Python SDK for MoltsPay - Agent-to-Agent Payments.**

MoltsPay enables AI agents to pay each other for services using the [x402 protocol](https://www.x402.org/) - HTTP-native payments with USDC stablecoins. No gas fees for clients, no complex wallet management.

## What is MoltsPay?

MoltsPay is blockchain payment infrastructure designed for AI agents. It solves a fundamental problem: **how do autonomous AI agents pay for services?**

- 🤖 **Agent-to-Agent Commerce** - AI agents can autonomously discover, pay for, and use services
- 💨 **Gasless Payments** - Clients never pay gas on any chain
- 🔗 **x402 Protocol** - HTTP 402 Payment Required - payments as native HTTP flow
- 🔒 **Spending Limits** - Set per-transaction and daily limits for safety
- ⛓️ **Multi-Chain** - Base, Polygon, Solana, BNB, Tempo (mainnet & testnet)
- 🌐 **Multi-VM** - EVM chains + Solana (SVM) with unified API
- 🦜 **LangChain Ready** - Drop-in tools for LangChain agents

## Installation

```bash
pip install moltspay
```

For Solana support:
```bash
pip install moltspay[solana]
```

For LangChain integration:
```bash
pip install moltspay[langchain]
```

For everything:
```bash
pip install moltspay[all]
```

## Quick Start

```python
from moltspay import MoltsPay

# Initialize (auto-creates wallet if not exists)
client = MoltsPay()
print(f"Wallet address: {client.address}")

# Discover services from a provider
services = client.discover("https://juai8.com/zen7")
for svc in services:
    print(f"{svc.id}: {svc.price} {svc.currency}")

# Pay for a service
result = client.pay(
    "https://juai8.com/zen7",
    "text-to-video",
    prompt="a cat dancing on the beach"
)
print(result.result)
```

## Supported Chains

MoltsPay supports 8 chains across EVM and Solana (SVM):

| Chain | Network ID | Type | Protocol | Gas Model |
|-------|------------|------|----------|-----------|
| Base | eip155:8453 | Mainnet | x402 + CDP | Gasless (CDP pays) |
| Polygon | eip155:137 | Mainnet | x402 + CDP | Gasless (CDP pays) |
| Solana | solana:mainnet | Mainnet | x402 + SOL Facilitator | Gasless (server pays) |
| BNB | eip155:56 | Mainnet | x402 + BNB Facilitator | Gasless (server pays) |
| Base Sepolia | eip155:84532 | Testnet | x402 + CDP | Gasless (CDP pays) |
| Solana Devnet | solana:devnet | Testnet | x402 + SOL Facilitator | Gasless (server pays) |
| BNB Testnet | eip155:97 | Testnet | x402 + BNB Facilitator | Gasless (server pays) |
| Tempo Moderato | eip155:42431 | Testnet | MPP | Gas-free native |

**Key:** Clients never pay gas on any chain. Different facilitators handle settlement.

## Payment Protocols

MoltsPay uses different protocols optimized for each chain:

### x402 + CDP (Base, Polygon)

Standard x402 flow with Coinbase Developer Platform as facilitator:

```
Client                         Server                      CDP Facilitator
  │ POST /execute                │                              │
  │ ─────────────────────────>   │                              │
  │ 402 + payment requirements   │                              │
  │ <─────────────────────────   │                              │
  │ [Sign EIP-3009 - NO GAS]     │                              │
  │ POST + X-Payment header      │                              │
  │ ─────────────────────────>   │ Verify & settle              │
  │                              │ ─────────────────────────>   │
  │ 200 OK + result              │                              │
  │ <─────────────────────────   │                              │
```

### x402 + SOL Facilitator (Solana)

Solana uses SPL token transfers with server as fee payer:

```
Client                         Server (Fee Payer)          Solana Network
  │ POST /execute                │                              │
  │ ─────────────────────────>   │                              │
  │ 402 + solanaFeePayer         │                              │
  │ <─────────────────────────   │                              │
  │ [Sign SPL Transfer - NO GAS] │                              │
  │ POST + X-Payment header      │                              │
  │ ─────────────────────────>   │ Execute + pay ~$0.001 SOL    │
  │                              │ ─────────────────────────>   │
  │ 200 OK + result              │                              │
  │ <─────────────────────────   │                              │
```

**Key:** Client only signs. Server acts as fee payer and executes transaction.

### x402 + BNB Facilitator (BNB Chain)

BNB uses EIP-712 intent signing with server-sponsored gas:

```
Client                         Server                      BNB Network
  │ POST /execute                │                              │
  │ ─────────────────────────>   │                              │
  │ 402 + bnbSpender             │                              │
  │ <─────────────────────────   │                              │
  │ [Sign EIP-712 Intent-NO GAS] │                              │
  │ POST + X-Payment header      │                              │
  │ ─────────────────────────>   │ Execute + pay ~$0.0001 gas   │
  │                              │ ─────────────────────────>   │
  │ 200 OK + result              │                              │
  │ <─────────────────────────   │                              │
```

**Key:** Client only signs intent. Server executes `transferFrom` and pays gas.

### MPP (Tempo Moderato)

Machine Payments Protocol - client executes directly (gas-free on Tempo):

```
Client                         Server
  │ POST /execute                │
  │ ─────────────────────────>   │
  │ 402 + WWW-Authenticate       │
  │ <─────────────────────────   │
  │ [Execute TIP-20 - NO GAS]    │
  │ POST + Authorization header  │
  │ ─────────────────────────>   │
  │ 200 OK + result              │
  │ <─────────────────────────   │
```

**Key:** Tempo is natively gas-free. Client executes transfer directly.

## Testnet Quick Start

Test without real money using our faucets:

```python
from moltspay import MoltsPay

# === Base Sepolia (x402 + CDP) ===
client = MoltsPay(chain="base_sepolia")
result = client.faucet()  # 1 USDC, once per 24h
print(f"Got {result.amount} USDC!")

# === Solana Devnet (x402 + SOL) ===
client = MoltsPay(chain="solana_devnet")
result = client.faucet()  # 1 USDC
print(f"Got {result.amount} USDC!")

# === BNB Testnet (x402 + BNB) ===
client = MoltsPay(chain="bnb_testnet")
result = client.faucet()  # 1 USDC + 0.001 tBNB for gas
print(f"Got {result.amount} USDC!")

# === Tempo Moderato (MPP) ===
client = MoltsPay(chain="tempo_moderato")
result = client.faucet()  # 1 pathUSD
print(f"Got {result.amount} pathUSD!")
```

**Make test payments:**

```python
# Base Sepolia
result = MoltsPay(chain="base_sepolia").pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="a robot dancing"
)

# Solana Devnet
result = MoltsPay(chain="solana_devnet").pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="a cat playing piano"
)

# BNB Testnet
result = MoltsPay(chain="bnb_testnet").pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="a sunset timelapse"
)

# Tempo Moderato
result = MoltsPay(chain="tempo_moderato").pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="an ocean wave"
)
```

## Features

### Auto Wallet Management

Wallets are automatically created on first run:
- **EVM wallet:** `~/.moltspay/wallet.json` (Base, Polygon, BNB, Tempo)
- **Solana wallet:** `~/.moltspay/wallet-solana.json`

```python
from moltspay import MoltsPay

client = MoltsPay()
print(f"EVM Address: {client.address}")

# Solana address (if initialized)
client_sol = MoltsPay(chain="solana")
print(f"Solana Address: {client_sol.address}")
```

### Funding Your Wallet

Before making payments, you need USDC in your wallet.

#### Option 1: Testnet Faucets (Free)

```python
from moltspay import MoltsPay

# Base Sepolia - 1 USDC (once per 24h)
client = MoltsPay(chain="base_sepolia")
result = client.faucet()

# Solana Devnet - 1 USDC
client = MoltsPay(chain="solana_devnet")
result = client.faucet()

# BNB Testnet - 1 USDC + 0.001 tBNB for gas
client = MoltsPay(chain="bnb_testnet")
result = client.faucet()

# Tempo Moderato - 1 pathUSD
client = MoltsPay(chain="tempo_moderato")
result = client.faucet()
```

#### Option 2: Coinbase Onramp (Mainnet)

Buy USDC with debit card or Apple Pay:

```python
from moltspay import MoltsPay

client = MoltsPay()  # Default: Base mainnet

# Generate funding link
result = client.fund(10)  # $10 minimum
print(f"Open this URL to pay: {result.url}")

# Or print QR code to terminal
client.fund_qr(10)
```

#### Option 3: Direct Transfer (Mainnet)

Send USDC from any wallet:

```python
from moltspay import MoltsPay

client = MoltsPay()
print(f"Send USDC to: {client.address}")
print(f"Chain: Base (chainId: 8453)")
```

⚠️ **Important:** Send USDC on the correct chain!

### Multi-Chain Payments

```python
from moltspay import MoltsPay

# Pay on different chains
result = MoltsPay(chain="base").pay(...)           # Base mainnet
result = MoltsPay(chain="polygon").pay(...)        # Polygon mainnet
result = MoltsPay(chain="solana").pay(...)         # Solana mainnet
result = MoltsPay(chain="bnb").pay(...)            # BNB mainnet
result = MoltsPay(chain="base_sepolia").pay(...)   # Base testnet
result = MoltsPay(chain="solana_devnet").pay(...)  # Solana testnet
result = MoltsPay(chain="bnb_testnet").pay(...)    # BNB testnet
result = MoltsPay(chain="tempo_moderato").pay(...) # Tempo testnet
```

### Spending Limits

Control your agent's spending:

```python
from moltspay import MoltsPay

client = MoltsPay()

# Check current limits
limits = client.limits()
print(f"Max per tx: {limits.max_per_tx}")
print(f"Max per day: {limits.max_per_day}")
print(f"Spent today: {limits.spent_today}")

# Update limits
client.set_limits(max_per_tx=20, max_per_day=200)
```

### Check Balances

```python
from moltspay import MoltsPay

client = MoltsPay()

# Single chain balance
balance = client.balance()
print(f"USDC: {balance.usdc}")
print(f"Chain: {balance.chain}")

# All chain balances
balances = client.all_balances()
for chain, bal in balances.items():
    print(f"{chain}: {bal.get('usdc', 0)} USDC")
```

### BNB Approval Check

BNB requires a one-time approval before first payment:

```python
from moltspay import MoltsPay

client = MoltsPay(chain="bnb")

# Check approval status
approvals = client.check_bnb_approvals()
print(f"USDC approved: {approvals['usdc_approved']}")
print(f"Allowance: {approvals['usdc_allowance']}")
```

**Note:** First BNB payment auto-approves. Approval costs ~$0.0001 in BNB gas (paid by client once).

### Async Support

```python
import asyncio
from moltspay import AsyncMoltsPay

async def main():
    async with AsyncMoltsPay() as client:
        result = await client.pay(
            "https://juai8.com/zen7",
            "text-to-video",
            prompt="a cat dancing"
        )
        print(result.result)

asyncio.run(main())
```

### Error Handling

```python
from moltspay import MoltsPay, InsufficientFunds, LimitExceeded, PaymentError

client = MoltsPay()

try:
    result = client.pay(...)
except InsufficientFunds as e:
    print(f"Need {e.required} USDC, have {e.balance}")
except LimitExceeded as e:
    print(f"Exceeds {e.limit_type} limit: {e.amount} > {e.limit}")
except PaymentError as e:
    print(f"Payment failed: {e}")
```

## API Reference

### Methods

| Method | Description | Returns |
|--------|-------------|---------|
| `pay(url, service_id, **params)` | Pay for and execute a service | `PaymentResult` |
| `discover(url)` | List services from a provider | `List[Service]` |
| `balance(chain=None)` | Get wallet balance | `Balance` |
| `all_balances()` | Get balances on all chains | `Dict[str, Dict]` |
| `limits()` | Get current spending limits | `Limits` |
| `set_limits(max_per_tx, max_per_day)` | Set spending limits | `None` |
| `faucet()` | Get free testnet tokens | `FaucetResult` |
| `fund(amount)` | Open Coinbase funding page | `FundingResult` |
| `fund_qr(amount)` | Print funding QR code | `FundingResult` |
| `check_bnb_approvals(chain="bnb")` | Check BNB approval status | `Dict` |

### Properties

| Property | Description | Type |
|----------|-------------|------|
| `address` | Wallet address (EVM or Solana) | `str` |

### The `.pay()` Method

```python
result = client.pay(
    provider_url: str,         # e.g., "https://juai8.com/zen7"
    service_id: str,           # e.g., "text-to-video"  
    token: str = "USDC",       # "USDC" or "USDT"
    **params                   # Service-specific parameters
)
```

### PaymentResult Object

```python
result.success      # bool - True if payment succeeded
result.amount       # float - Amount paid
result.token        # str - "USDC" or "USDT"
result.tx_hash      # str - Blockchain transaction hash
result.result       # Any - Service result (e.g., video URL)
result.error        # str | None - Error message if failed
result.explorer_url # str | None - Block explorer link
```

### FaucetResult Object

```python
result.success      # bool - True if faucet succeeded
result.amount       # float - Amount received
result.tx_hash      # str - Transaction hash
result.error        # str | None - Error message if failed
```

## LangChain Integration

```python
from langchain.agents import initialize_agent, AgentType
from langchain_openai import ChatOpenAI
from moltspay.integrations.langchain import MoltsPayTool

llm = ChatOpenAI(model="gpt-4")
tools = [MoltsPayTool()]

agent = initialize_agent(
    tools, 
    llm, 
    agent=AgentType.OPENAI_FUNCTIONS,
    verbose=True
)

# Agent can now pay for AI services!
result = agent.run("Generate a video of a cat dancing on the beach")
```

### Available Tools

```python
from moltspay.integrations.langchain import get_moltspay_tools

tools = get_moltspay_tools()  # Returns both tools
```

| Tool | Description |
|------|-------------|
| `MoltsPayTool` | Pay for and execute services |
| `MoltsPayDiscoverTool` | Discover available services and prices |

## Chain-Specific Notes

### Solana

- **Wallet:** Separate ed25519 keypair at `~/.moltspay/wallet-solana.json`
- **Gas:** Server pays (~$0.001 SOL per tx)
- **Token:** Circle USDC SPL token

**USDC Mint Addresses:**
| Network | Address |
|---------|---------|
| Mainnet | `EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` |
| Devnet | `4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU` |

### BNB Chain

- **Decimals:** 18 (not 6 like Base/Polygon)
- **Gas:** Server pays (~$0.0001 per tx)
- **Approval:** First payment requires one-time approval (client pays ~$0.0001)

**Token Addresses:**
| Token | Address |
|-------|---------|
| USDC | `0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d` |
| USDT | `0x55d398326f99059fF775485246999027B3197955` |

### Tempo Moderato

- **Protocol:** MPP (Machine Payments Protocol)
- **Gas:** Native gas-free
- **Explorer:** https://explore.testnet.tempo.xyz

**Stablecoins:**
| Token | Address |
|-------|---------|
| pathUSD (USDC) | `0x20c0000000000000000000000000000000000000` |
| alphaUSD (USDT) | `0x20c0000000000000000000000000000000000001` |

## Live Example: Zen7 Video Generation

Live service at `https://juai8.com/zen7`

**Services:**
- `text-to-video` - $0.01 USDC
- `image-to-video` - $0.01 USDC

**Supported Chains:** Base, Polygon, Solana, BNB, Tempo (mainnet & testnet)

```python
from moltspay import MoltsPay

# Pay on Base (default)
result = MoltsPay().pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="a happy cat"
)

# Pay on Solana
result = MoltsPay(chain="solana_devnet").pay(
    "https://juai8.com/zen7", "text-to-video",
    prompt="a happy cat"
)

print(result.result)  # {"video_url": "https://..."}
```

## CLI Compatibility

Wallet format is fully compatible with the Node.js CLI:

```bash
# Create wallet with Node CLI
npx moltspay init

# Use same wallet in Python
python -c "from moltspay import MoltsPay; print(MoltsPay().address)"
```

## Related Projects

- [moltspay (Node.js)](https://github.com/Yaqing2023/moltspay) - Node.js SDK and CLI
- [x402 Protocol](https://www.x402.org/) - The HTTP payment standard

## Community & Support

- **Discord:** https://discord.gg/QwCJgVBxVK
- **Website:** https://moltspay.com
- **PyPI:** https://pypi.org/project/moltspay/
- **npm (Node.js):** https://www.npmjs.com/package/moltspay
- **GitHub:** https://github.com/Yaqing2023/moltspay-python

## License

MIT
