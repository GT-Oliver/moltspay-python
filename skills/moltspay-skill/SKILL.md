---
name: moltspay-skill
description: Use MoltsPay to discover and purchase paid AI services, inspect or fund wallets, manage provider balances, and recover MoltsPay WeChat or Alipay sessions. Trigger when the user names MoltsPay, supplies a MoltsPay provider/service, chooses MoltsPay for an x402 purchase, or continues a MoltsPay payment or recharge. Do not use for unrelated generic Alipay or WeChat payments.
metadata:
  openclaw:
    emoji: 💸
    homepage: https://moltspay.com/docs
    requires:
      bins: [moltspay]
---

# MoltsPay Client

Use the installed Python MoltsPay SDK through its registered console command.

## Command contract

`pyproject.toml` registers these executables:

- `moltspay` — buyer CLI; use this for the workflows in this skill.
- `moltspay-mcp` — MCP server process; it is infrastructure, not a buyer command.
- `moltspay-server` — provider runtime; do not start it for an ordinary purchase.

Invoke commands as `moltspay ...`. Do not use `python3 -m moltspay.cli`, `npx moltspay`, or a bundled Node.js binary in this skill.

Before the first operation in a session, verify the selected executable:

```bash
command -v moltspay
moltspay --version
moltspay --help
```

If it is missing, install the Python project, then retry the registered command:

```bash
python3 -m pip install -e /Users/vnet/WorkPlace/GitHub/moltspay-python
moltspay --version
```

Do not switch between CLI and MCP halfway through a WeChat, Alipay, or balance top-up lifecycle. Their persisted session stores may differ. If the user explicitly requests MCP, follow the same state machines with the corresponding `moltspay_*` tools and keep that transport for the whole lifecycle.

## Operating rules

1. Resolve the provider URL. The Python CLI does not provide a universal registry when no URL is supplied.
2. Run `moltspay services <provider-url> --json` before a new purchase. Use the returned service ID, parameters, price, currency, chains, tokens, and advertised payment rails; do not rely on remembered examples.
3. Respect an explicitly requested rail. Without a rail preference, prefer an already-funded provider balance when available; otherwise present the advertised viable choices instead of silently creating a top-up or payment order.
4. A request such as “buy this service” authorizes that described purchase only after its current quote is known. Ask before spending when the service, price, currency, rail, chain, or amount is ambiguous or changed.
5. Never create a second order merely because a command timed out or the user says “已支付”. Recover the saved session or order first.
6. Never print wallet files, private keys, balance signing keys, Alipay tokens, payment proofs, or raw credentials.
7. Treat local spending limits as policy controls. Never raise them unless the user explicitly requests the new limits.

Read-only commands such as `services`, `status`, balance queries, transaction lists, and local session status may run without payment confirmation. Commands that pay, transfer, approve, create an external order, fulfill a paid order, or change limits are side-effectful.

## Route by task

| User intent | First action | Detailed procedure |
|---|---|---|
| Discover or buy a service | `moltspay services <url> --json` | Read [service-payments.md](references/service-payments.md) |
| Balance purchase or recharge | Discover, then `moltspay balance query <url> --json` | Read [service-payments.md](references/service-payments.md) and [chat-lifecycles.md](references/chat-lifecycles.md) |
| WeChat or Alipay purchase | Discover the rail-specific CNY quote | Read [chat-lifecycles.md](references/chat-lifecycles.md) |
| User says paid/done/continue | Recover the current conversation's saved session | Read [chat-lifecycles.md](references/chat-lifecycles.md) |
| Wallet balance, funding, faucet, transfer, limits | Start with `moltspay status --json` when relevant | Read [wallet-operations.md](references/wallet-operations.md) |

Load only the references needed for the current request.

## Current Python SDK scope

Supported chain identifiers are:

```text
base, polygon, base_sepolia, bnb, bnb_testnet,
solana, solana_devnet, tempo_moderato
```

`opbnb` is not supported by the current Python CLI. Base, Polygon, Base Sepolia, BNB, BNB Testnet, and Tempo use the EVM wallet; Solana uses a separate wallet. Provider support is narrower than SDK support, so discovery remains authoritative.

## Result handling

- Report the actual amount, currency/token, rail, network, transaction/order ID, and service result when returned.
- Do not call a payment successful from a QR, a locally cached `pending` status, or a user statement alone.
- For balance deductions, report `refunded` when service execution fails.
- For unknown or ambiguous remote payment state, preserve identifiers and stop; do not create a replacement order automatically.
