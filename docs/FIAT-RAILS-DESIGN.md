# Python Fiat Rail Overview: WeChat and Alipay

Detailed specifications are maintained separately:

- [WECHAT-RAIL-DESIGN.md](WECHAT-RAIL-DESIGN.md)
- [ALIPAY-RAIL-DESIGN.md](ALIPAY-RAIL-DESIGN.md)

Fiat rails are explicit alternatives to direct crypto and balance payments.
They create or resume a recoverable external payment session, wait for
settlement, and then return the paid service result. Credentials stay in
environment variables or the external integration, not service manifests.

## WeChat Pay

`wechat.py` uses WeChat Native v3 orders. Sessions persist under
`~/.moltspay/wechat-sessions` and can be listed, queried, fulfilled, or
cancelled after restart.

```text
create order -> display code/QR -> poll -> fulfill -> result
                         \-> timeout -> close/cancel
```

`server/facilitators/wechat.py` owns provider signing and settlement. Preserve
Node-compatible camelCase session fields during interoperability.

## Alipay

`alipay.py` integrates the buyer-side `alipay-bot` CLI; the provider facilitator
uses RSA2/OpenAPI. Amounts are yuan, not fen. The flow preserves the external
trade number across payment status, fulfillment, and cancellation.

```text
intent -> wallet check -> cashier URL -> user confirms -> poll -> acknowledge
```

## Routing and tests

`rail="wechat"` or `rail="alipay"` takes precedence over crypto and must not
silently fall back. Test pending, success, timeout, cancellation, malformed
responses, restart/resume, duplicate fulfillment, and missing credentials.
