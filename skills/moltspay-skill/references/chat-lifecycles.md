# Chat payment lifecycles

Use non-blocking, recoverable flows in Feishu, Discord, webchat, and other turn-based channels. Keep the provider, service, parameters, channel user, conversation, local session ID, external order ID, and expiry together. Never recover an order from another user or conversation.

## Balance recharge

Balance recharge is paid through WeChat only.

### Amount-selection gate

Before creating an order:

1. Read `topupPacks`/`topup_packs` and `customTopupMax`/`custom_topup_max` from a fresh `insufficient_balance` response or:

   ```bash
   moltspay balance query <provider-url> --buyer <buyer-id> --json
   ```

2. Present only provider-returned packs, preserving server order. Offer a custom amount only when a custom maximum is returned.
3. If the user already supplied an amount, still validate it against this fresh policy.
4. Compare decimal amounts exactly, not with binary floating point. A custom amount must be positive, have at most two decimal places, and not exceed the returned maximum.
5. Do not infer an amount from a service price, missing balance, previous purchase, or old policy.

Use a native selection card when the channel supports one; otherwise use numbered text. Do not create an order until the user explicitly selects a valid amount.

### Create, show, confirm, retry

```bash
moltspay balance topup-order <provider-url> \
  --buyer <buyer-id> --pack <selected-amount> --json
```

The current CLI returns `code_url` and `out_trade_no`; it does not write a PNG for this command. Render `code_url` as a QR image with the channel's QR/image facility. If needed, use the bundled deterministic helper:

```bash
python3 <skill-dir>/scripts/render_qr.py '<code_url>' '<output.png>'
```

Send the image, save `out_trade_no`, and end the turn. When the same user later says “已支付”, confirm the same order:

```bash
moltspay balance topup-confirm <out_trade_no> --json
```

- `credited`: retry the original balance purchase once.
- `pending`: tell the user it is not confirmed; keep the same order.
- `expired`: explain that the QR expired. Create a new order only after renewed user approval.

`topup-confirm` is idempotent. Never use Alipay for this flow and never create a replacement order merely because the user says paid.

## Direct WeChat service purchase

Use only when the provider advertises direct WeChat and the user chooses it. If both direct WeChat and balance funding are possible, do not silently reinterpret the user's preference.

```bash
moltspay wechat start <provider-url> <service-id> '<params-json>' --json
```

`wechat start` prints a terminal QR to stderr and returns the session as JSON. For chat delivery, render the returned `code_url` as a PNG if the channel does not capture the terminal QR. Save `payment_session_id` and `out_trade_no`, then end the turn.

On the user's later continuation:

```bash
moltspay wechat status <payment_session_id-or-out_trade_no> --json
moltspay wechat fulfill <payment_session_id-or-out_trade_no> --json
```

Run `fulfill` only after status is paid. If pending, wait. If expired or cancelled, require approval before starting a new order. Do not use blocking `moltspay pay ... --rail wechat` in a chat channel.

## Direct Alipay A402 purchase

Use the Alipay-specific CNY quote returned by discovery. A real current OpenClaw business session ID is mandatory; never invent one and never pass a local `mpay_alipay_*` payment session as the business session.

Start once:

```bash
moltspay alipay start <provider-url> <service-id> '<params-json>' \
  --session-id '<current-openclaw-session-id>' \
  --framework openclaw \
  --intent-summary '<non-sensitive purchase purpose>' \
  --timeout 1800 --json
```

Send only the current result's trusted `MEDIA:` PNG, save `payment_session_id`, and end the turn. Do not immediately poll or resume.

When the same user continues or says paid:

```bash
moltspay alipay resume <payment_session_id> --json
```

Interpret results conservatively:

- `pending`/`processing`: wait; retain the same session.
- `paid`/`fulfilling`: payment is confirmed, delivery is not complete.
- `completed`: return the service result.
- `unknown`: preserve the session and stop. Do not create another payment.

`moltspay alipay status` reads local recovery state only; it cannot prove that a newly completed remote payment succeeded. Do not use blocking `moltspay pay ... --rail alipay` in OpenClaw chat channels.

## Recovery lookup

When the current conversation lost an identifier, inspect local sessions without creating anything:

```bash
moltspay balance topup-list --json
moltspay wechat list --json
moltspay alipay list --json
```

Match provider, service/order, channel user, conversation, time, and status. If the match is ambiguous, ask rather than resuming the wrong payment.

