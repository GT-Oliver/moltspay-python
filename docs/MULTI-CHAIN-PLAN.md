# moltspay-python Multi-Chain Implementation Plan (Historical)

> This document records the original delivery plan. The listed chains and
> facilitators are now implemented. Read [MULTI-CHAIN-DESIGN.md](MULTI-CHAIN-DESIGN.md)
> for the current architecture and [DESIGN-INDEX.md](DESIGN-INDEX.md) for the
> complete Python design-document set.

## Current State at the time of writing

The following snapshot is historical and is intentionally retained to explain
the phase order. It is not the current Python capability matrix.

**Node.js SDK has:**
- base, polygon, base_sepolia (x402)
- tempo_moderato (MPP protocol)
- bnb, bnb_testnet (EVM + approval flow)
- solana, solana_devnet (ed25519, SPL tokens)

**Python SDK has:**
- base, polygon, base_sepolia (x402) ✅
- faucet() for base_sepolia ✅

**Python SDK missing at that time:**
- tempo_moderato (MPP)
- bnb, bnb_testnet
- solana, solana_devnet

---

## Phase 1: Chain Config & Infrastructure

**Files to modify:** `src/moltspay/chains.py` (create new)

1. Add chain configurations:
   ```python
   CHAINS = {
     'base': { 'rpc': '...', 'chainId': 8453, 'usdc': '0x...', 'decimals': 6, 'explorer': '...' },
     'polygon': { ... },
     'base_sepolia': { ... },
     'tempo_moderato': { 'chainId': 42431, 'rpc': 'https://rpc.moderato.tempo.xyz', 'pathUSD': '0x20c0...', ... },
     'bnb': { ... },
     'bnb_testnet': { ... },
     'solana': { 'rpc': 'https://api.mainnet-beta.solana.com', 'usdcMint': '...', ... },
     'solana_devnet': { ... }
   }
   ```

2. Add protocol detection:
   - x402 → base, polygon, base_sepolia
   - MPP → tempo_moderato
   - BNB → bnb, bnb_testnet (EVM but custom flow)
   - Solana → solana, solana_devnet

---

## Phase 2: Tempo/MPP Support

**New file:** `src/moltspay/facilitators/tempo.py`

1. Implement `TempoFacilitator`:
   - Direct on-chain payment (no CDP)
   - Sign EIP-712 typed data for MPP
   - Verify via on-chain receipt

2. Update `client.pay()`:
   - Detect MPP from 402 response (`paymentRequirements.mpp`)
   - Route to TempoFacilitator

**Key differences from x402:**
- No CDP facilitator - direct transfer
- Uses pathUSD token (TIP-20)
- EIP-712 domain: `{ name: "Tempo", chainId: 42431 }`

---

## Phase 3: BNB Support

**New file:** `src/moltspay/facilitators/bnb.py`

1. Implement `BNBFacilitator`:
   - Read `bnbSpender` from 402 response
   - Check/create approval for USDC/USDT
   - Sign EIP-712 intent
   - Server executes transferFrom

2. Handle gas requirements:
   - Check BNB balance before approval
   - Helpful error messages if insufficient gas

**Key differences:**
- User needs small BNB for first approval tx (~$0.0001)
- After approval, payments are gasless
- BNB tokens don't support EIP-2612 permit

---

## Phase 4: Solana Support

**New files:**
- `src/moltspay/wallet_solana.py`
- `src/moltspay/facilitators/solana.py`

1. **Wallet management:**
   - Generate ed25519 keypair (separate from EVM)
   - Store in `wallet-solana.json`
   - Use `solders` or `solana-py` library

2. **Implement `SolanaFacilitator`:**
   - Create SPL token transfer instruction
   - Server pays tx fees (gasless for client)
   - Verify via Solana tx signature

3. **Key differences:**
   - Completely different crypto (ed25519 vs secp256k1)
   - SPL tokens instead of ERC-20
   - Associated Token Accounts (ATA)
   - Server pays all fees

**Dependencies:**
```
solders>=0.20.0    # Lightweight Solana library
# OR solana-py>=0.30.0
```

---

## Phase 5: CLI Updates

**File:** `src/moltspay/cli.py`

1. Update commands:
   - `init` → support `--chain` for Solana wallet
   - `status` → show balances for all chains
   - `faucet` → add tempo_moderato, bnb_testnet, solana_devnet
   - `pay` → route to correct facilitator based on chain

2. Add `approve` command for BNB chains:
   ```
   moltspay approve --chain bnb_testnet --spender 0x...
   ```

3. Chain detection in `pay`:
   ```python
   if chain == 'tempo_moderato':
       return tempo_facilitator.pay(...)
   elif chain in ['bnb', 'bnb_testnet']:
       return bnb_facilitator.pay(...)
   elif chain in ['solana', 'solana_devnet']:
       return solana_facilitator.pay(...)
   else:
       return x402_facilitator.pay(...)  # default
   ```

---

## Phase 6: Testing

1. **Unit tests for each facilitator:**
   - `tests/test_tempo_facilitator.py`
   - `tests/test_bnb_facilitator.py`
   - `tests/test_solana_facilitator.py`

2. **E2E tests:**
   ```bash
   # Tempo
   moltspay pay https://moltspay.com/a/zen7 text-to-video --chain tempo_moderato --prompt "test"
   
   # BNB
   moltspay pay https://moltspay.com/a/zen7 text-to-video --chain bnb_testnet --prompt "test"
   
   # Solana
   moltspay pay https://moltspay.com/a/zen7 text-to-video --chain solana_devnet --prompt "test"
   ```

---

## Dependencies to Add

```
# requirements.txt additions
web3>=6.0.0           # EVM chains (may already exist)
eth-account>=0.10.0   # EIP-712 signing
solders>=0.20.0       # Solana support (lightweight)
```

---

## File Structure After Implementation

```
src/moltspay/
├── __init__.py
├── client.py              # Main MoltsPay class
├── chains.py              # NEW: Chain configurations
├── wallet.py              # EVM wallet management
├── wallet_solana.py       # NEW: Solana wallet management
├── facilitators/
│   ├── __init__.py
│   ├── x402.py            # Base/Polygon (existing)
│   ├── tempo.py           # NEW: Tempo/MPP
│   ├── bnb.py             # NEW: BNB chains
│   └── solana.py          # NEW: Solana chains
└── cli.py                 # CLI commands
```

---

## Estimated Effort

| Phase | Effort | Priority |
|-------|--------|----------|
| 1. Chain Config | 2 hours | High |
| 2. Tempo/MPP | 4 hours | High |
| 3. BNB | 4 hours | Medium |
| 4. Solana | 6 hours | Medium |
| 5. CLI | 2 hours | High |
| 6. Testing | 4 hours | High |

**Total: ~22 hours**

---

## Implementation Order

1. **Phase 1 + 2** (Config + Tempo) → Test on Playground
2. **Phase 3** (BNB) → E2E test
3. **Phase 4** (Solana) → E2E test
4. **Phase 5 + 6** (CLI + Tests)

---

## Reference: Node.js Implementation

Key files to reference in `~/clawd/projects/payment-agent/`:
- `src/chains/index.ts` - Chain configurations
- `src/facilitators/tempo.ts` - Tempo/MPP implementation
- `src/facilitators/bnb.ts` - BNB implementation
- `src/wallet/solana.ts` - Solana wallet
- `src/facilitators/solana.ts` - Solana facilitator
- `src/cli/index.ts` - CLI commands

---

---

## Future at the time of writing: MCP Server

The MCP track has since been implemented. See [MCP-SERVER-DESIGN.md](MCP-SERVER-DESIGN.md).

**Goal:** Universal LLM access without framework lock-in

**Files to create:**
- `src/moltspay/mcp/__init__.py`
- `src/moltspay/mcp/server.py`
- `src/moltspay/mcp/tools.py`

**Entry point:** `moltspay-mcp` (stdio transport)

**Tools to expose:**
| Tool | Description |
|------|-------------|
| `moltspay_discover` | Discover services from provider URL |
| `moltspay_pay` | Pay for a service |
| `moltspay_balance` | Check wallet balance |
| `moltspay_status` | Get wallet address and limits |

**Priority:** Low (nice-to-have for broader adoption)

---

*Created: 2026-03-23*
*Updated: 2026-03-23 - Added MCP roadmap*
