# Python Design Document Index

This is the Python counterpart to the Node.js design-document set. Node.js
documents are protocol references; these documents describe Python APIs,
optional dependencies, persistence, and current implementation status.

## Capability map

| Area | Implementation | Status | Design |
|---|---|---|---|
| Core client and x402 | `client.py`, `x402.py` | Implemented | [DESIGN.md](DESIGN.md) |
| Wallet/security | `wallet.py`, `wallet_solana.py`, security modules | Implemented | [MODULES-DESIGN.md](MODULES-DESIGN.md), [SECURITY-DESIGN.md](SECURITY-DESIGN.md) |
| Multi-chain protocols | `chains.py`, `facilitators/` | Implemented | [MULTI-CHAIN-DESIGN.md](MULTI-CHAIN-DESIGN.md) |
| Balance rail | `balance.py`, server facilitator | Implemented | [BALANCE-RAIL-DESIGN.md](BALANCE-RAIL-DESIGN.md) |
| WeChat rail | `wechat.py`, `server/facilitators/wechat.py` | Implemented | [WECHAT-RAIL-DESIGN.md](WECHAT-RAIL-DESIGN.md) |
| Alipay rail | `alipay.py`, `server/facilitators/alipay.py` | Implemented | [ALIPAY-RAIL-DESIGN.md](ALIPAY-RAIL-DESIGN.md) |
| Provider runtime and CLI | `server/`, `cli.py` | Implemented | [MODULES-DESIGN.md](MODULES-DESIGN.md) |
| MCP adapter | `mcp/` | Implemented; optional | [MCP-SERVER-DESIGN.md](MCP-SERVER-DESIGN.md) |

## New tracks compared with the original Python design

1. Multi-chain protocol routing: x402, MPP/Tempo, BNB intents, and Solana SPL.
2. Balance, WeChat Native v3, and Alipay payment rails.
3. Provider-side Python runtime with a facilitator registry.
4. MCP tools for discovery, status, payment, and configuration.
5. Security primitives: compatible encrypted wallets, limits, permits,
   whitelists, and hash-chained audit records.

For a new chain, update the registry, facilitator mapping, models, tests,
README support matrix, and the multi-chain design. For a new rail, also update
manifest validation and `MoltsPay.pay()` routing.
