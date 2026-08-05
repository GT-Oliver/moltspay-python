# Python MCP Adapter Design

The Python MCP integration is a local stdio adapter around `MoltsPay`, not a
Cloudflare-hosted custodial wallet. Install `moltspay[mcp]`, initialize the
local wallet, then run `moltspay-mcp`.

| Tool | Behavior |
|---|---|
| `moltspay_status` | Address, default chain, balances, limits |
| `moltspay_services` | Discover and filter services |
| `moltspay_pay` | Pay with chain, token, and rail options |
| `moltspay_config` | Read/update spending limits |

Tools call the public client and return serializable models. `--dry-run` never
signs or sends a payment. `MOLTSPAY_MCP_REQUIRE_CONFIRM=1` requires
`confirmed=true`; normal wallet limits remain enforced.

The MCP process reads `~/.moltspay/wallet.json` under the trusted local user
and never returns private keys. A remote multi-tenant custodial deployment is
out of scope and needs a separate authentication, encryption, and threat model.
