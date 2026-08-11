# Fiat Rails and MCP Refactor Plan

## Scope

This refactor is limited to Alipay, provider balance, WeChat Pay, and MCP.
The existing on-chain x402 implementation, wallets, chain registry, and the
BNB, Solana, Tempo, and CDP facilitators are frozen.

Minimal integration changes are allowed in `client.py`, `models.py`,
`exceptions.py`, and the fiat HTTP routes in `server.py`. Those changes must
not alter on-chain routing, signing, settlement, or wallet behavior.

## Goals

1. Separate payment creation, read-only status checks, and fulfillment.
2. Represent pending, paid, completed, cancelled, expired, failed, and unknown
   outcomes explicitly.
3. Make provider balance buyer selection and top-up confirmation deterministic
   and idempotent.
4. Make Alipay payments recoverable instead of requiring one blocking call.
5. Expose the resulting public APIs through strict MCP tool contracts.
6. Preserve existing synchronous SDK entry points as compatibility wrappers.

## Target lifecycle

```text
start -> pending -> paid -> fulfill -> completed
           |          |
           +-> unknown+-> failed
           +-> expired
           +-> cancelled
```

`status` is read-only. Only `fulfill` may send a stored payment credential to
the provider service. An unknown network outcome must be queried; it must not
cause a second payment automatically.

## Delivery stages

### Stage 1: WeChat Pay

- Query the provider's WeChat order-status endpoint without calling `/execute`.
- Keep fulfillment as the only operation that submits `X-Payment`.
- Enforce terminal-state transitions and safe identifiers.
- Preserve the existing blocking helper as a compatibility wrapper.

### Stage 2: Provider balance

- Give an explicit `buyer_id` precedence over configured defaults.
- Return complete, recoverable top-up sessions.
- Compute expiry consistently and distinguish pending, unknown, and failed.
- Validate identifiers before using them as file names.
- Close owned HTTP and SQLite resources deterministically.

### Stage 3: Alipay

- Split payment start, one-shot resume/query, and blocking compatibility flows.
- Persist the minimum information required to resume by trade number.
- Return payment URLs immediately instead of hiding them behind polling.
- Restrict executable selection to server configuration.

### Stage 4: MCP

- Register tools with constrained Pydantic input models.
- Use a stable success/error envelope and an explicit confirmation matrix.
- Keep dry-run side-effect free and independent of payment confirmation.
- Return WeChat QR data as MCP image content where supported, with `codeUrl`
  retained as a fallback.
- Test real FastMCP registration when the optional dependency is installed.

## Test gates

```text
tool input
  -> schema validation
  -> dry-run / confirmation
  -> rail API
       -> completed
       -> action required
       -> pending
       -> unknown
       -> failed
```

Every branch above requires a unit or contract test. Tests must also cover
duplicate fulfillment, duplicate balance deduction, expired sessions, corrupt
session files, timeouts, network ambiguity, and process restart recovery.

Completion gates:

- The existing test suite remains green.
- Touched fiat and MCP modules reach at least 85% line coverage.
- Tests emit no unclosed SQLite or HTTP-client resource warnings.
- Frozen on-chain files have no content changes.
- MCP registration schemas match their documented snapshots.

## Frozen files

- `src/moltspay/x402.py`
- `src/moltspay/wallet.py`
- `src/moltspay/chains.py`
- `src/moltspay/facilitators/bnb.py`
- `src/moltspay/facilitators/solana.py`
- `src/moltspay/facilitators/tempo.py`
- `src/moltspay/server/facilitators/cdp.py`
- `src/moltspay/server/facilitators/bnb.py`
- `src/moltspay/server/facilitators/solana.py`
- `src/moltspay/server/facilitators/tempo.py`

## Explicitly deferred

- Global migration of all monetary values to `Decimal`.
- Redesign of `AsyncMoltsPay` beyond thin wrappers for the new fiat APIs.
- Replacement of the HTTP server or SQLite ledger technology.
- Refactoring of any on-chain payment or settlement implementation.
- Credential-backed live gateway tests in the default unit-test suite.

## Implementation result (2026-08-11)

The four delivery stages are implemented. The final verification results are:

- Full test suite: 87 passed.
- Target-module line coverage: 94% overall (Alipay 91%, Balance 93%, MCP server 97%, WeChat Pay 96%).
- Resource lifecycle: no unclosed SQLite or HTTP-client warnings from project code.
- Frozen on-chain files and facilitators: no content changes from `HEAD`.
- Diff hygiene: `git diff --check` passes.

Two dependency warnings remain outside this refactor's ownership: the installed
`websockets` legacy-package deprecation warning and FastMCP's unresolved
`lifespan` forward-reference warning.
