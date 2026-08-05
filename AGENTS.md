# MoltsPay Python SDK - AI Agent Instructions

MoltsPay enables AI agents to pay for services using USDC. Gasless, multi-chain (Base, Polygon).

## Installation

```bash
pip install moltspay
```

基础安装包含 `qrcode`，用于 `moltspay fund` 在终端显示充值二维码。

## Quick Start

```python
from moltspay import MoltsPay

# Initialize (auto-creates wallet if needed)
client = MoltsPay()                        # default: base
client = MoltsPay(chain="polygon")         # polygon mainnet
client = MoltsPay(chain="base_sepolia")    # testnet
client = MoltsPay(chain="base_sepolia", timeout=180.0)  # custom timeout

# Wallet address (same address works on all chains)
print(client.address)
```

## Wallet Management

```python
# Check balance
balance = client.balance()

# Spending limits
limits = client.limits()
print(limits.max_per_tx)
print(limits.max_per_day)
print(limits.spent_today)

# Set limits
client.set_limits(max_per_tx=10, max_per_day=100)

# Fund via QR code (debit card / Apple Pay, no crypto needed)
result = client.fund_qr(amount=10, chain="base")

# Testnet faucet (base_sepolia only, 1 USDC per 24h)
result = client.faucet()
if result.success:
    print(f"Got {result.amount} USDC! TX: {result.tx_hash}")
```

## Service Discovery

```python
# Discover services from a provider
services = client.discover("https://moltspay.com/a/zen7")

for svc in services:
    print(f"{svc.id}: {svc.name}")
    print(f"  Price: ${svc.price} {svc.currency}")
    print(f"  Chains: {svc.chains}")
```

## Paying for Services

```python
# Pay using service UUID
result = client.pay(
    "https://moltspay.com/a/zen7",
    "b23c6959-605f-49ff-98de-aea28705d386",  # service UUID from discover()
    prompt="a cat dancing in the rain"
)

if result.success:
    print(f"Paid: ${result.amount} USDC")
    print(f"TX: {result.tx_hash}")
    print(f"Result: {result.result}")
else:
    print(f"Error: {result.error}")
```

## Complete Example (Testnet)

```python
from moltspay import MoltsPay

# Use testnet with longer timeout for video generation
client = MoltsPay(chain="base_sepolia", timeout=180.0)
print(f"Wallet: {client.address}")

# Get free testnet USDC
faucet_result = client.faucet()
if faucet_result.success:
    print(f"Got {faucet_result.amount} USDC")

# Discover services
services = client.discover("https://moltspay.com/a/zen7")
for svc in services:
    print(f"{svc.name}: ${svc.price}")

# Pay for video generation
result = client.pay(
    "https://moltspay.com/a/zen7",
    "b23c6959-605f-49ff-98de-aea28705d386",
    prompt="a robot dancing in the rain"
)

if result.success:
    print(f"Video: {result.result}")
```

## Supported Chains

| Chain | Type | Use |
|-------|------|-----|
| base | mainnet | Production |
| polygon | mainnet | Production |
| base_sepolia | testnet | Testing (free USDC via faucet) |

Ethereum NOT supported (gas too expensive).

## Common Errors

| Error | Fix |
|-------|-----|
| insufficient_balance | `client.fund_qr(10, "base")` or `client.faucet()` for testnet |
| already_claimed | Faucet limit - wait 24 hours |
| unsupported_chain | Check `svc.chains` from discover() |
| timeout | Increase timeout: `MoltsPay(timeout=180.0)` |

## Links

- Full docs: https://moltspay.com/llms.txt
- Playground: https://moltspay.com/creators/playground
- npm (Node.js): https://npmjs.com/package/moltspay
- PyPI: https://pypi.org/project/moltspay
