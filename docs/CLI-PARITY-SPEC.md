# CLI parity specification

The Python CLI follows the Node.js `moltspay` CLI at version `2.4.0`.
The reference is the Node repository's `src/cli/` command tree, pinned to the
release commit used for this SDK release. This document is the review checklist
for future CLI changes.

## Top-level commands

```text
init
config
fund <amount>
approve
faucet
status
list
pay <server> <service> [params]
balance
wechat
services [url]
validate <path>
transfer <to> <amount>
server start <paths...>
server stop
```

## Global rules

- `--version` reports the package version.
- `--config-dir <dir>` uses the same wallet and session directory for every
  command that exposes the option.
- `--json` is the explicit machine-readable output mode where Node exposes it.
- Human-readable output, QR rendering, and progress messages go to stderr when
  JSON output is requested; stdout remains valid JSON.
- Command names and option names are Node-compatible. Python-only aliases must
  not be added to the public parser.

## Parity review

When the Node CLI changes, update the pinned reference and compare:

1. top-level commands;
2. nested commands;
3. positional argument order and optionality;
4. short and long option names;
5. defaults and choices;
6. stdout, stderr, and exit codes.

The comparison must be covered by parser/help tests before release.
