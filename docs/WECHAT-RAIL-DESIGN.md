# WeChat Pay Rail Design for Python

## 1. Scope

This rail implements WeChat Pay v3 Native payments using the shared x402-style
402 handshake. It has two sides:

- Buyer: `moltspay.wechat.WechatClient` creates and resumes local payment
  sessions.
- Provider: `server.facilitators.wechat.WechatFacilitator` creates Native
  orders, verifies payment status, and returns the WeChat transaction ID.

The rail identifier is `wechat`; the scheme is `wechatpay-native`.

## 2. Provider configuration

The provider manifest enables the rail with a `wechat` provider block and adds
`wechat` to the provider chains. The facilitator requires the merchant app ID,
merchant ID, certificate serial number, API private key, and notification URL.
The API base defaults to `https://api.mch.weixin.qq.com` and can be overridden
for testing.

Private key material is loaded from `private_key_pem` or
`private_key_path`; it must not be placed in `moltspay.services.json` when that
file is distributed.

## 3. Payment requirement creation

`WechatFacilitator.create_payment_requirements()`:

1. Converts the CNY price to integer fen using `Decimal`.
2. Rejects negative values, more than two decimal places, and amounts below
   0.01 CNY.
3. Creates `/v3/pay/transactions/native` with a unique `out_trade_no`.
4. Returns `code_url` and the trade number in `extra`.

The resulting requirement is shaped as:

```json
{
  "scheme": "wechatpay-native",
  "network": "wechat",
  "asset": "CNY",
  "amount": "2.00",
  "extra": {"code_url": "weixin://pay/...", "out_trade_no": "WX..."}
}
```

Merchant API calls use `WECHATPAY2-SHA256-RSA2048` authorization. The signed
message includes HTTP method, path, timestamp, nonce, and exact request body.

## 4. Buyer session lifecycle

`WechatClient.start_402()` requires `extra.code_url` and
`extra.out_trade_no`, creates a session, and persists it under:

```text
~/.moltspay/wechat-sessions/<payment_session_id>.json
```

The session stores the resource URL, original method/body, requirement,
expiration, code URL, trade number, last HTTP status/error, and result body.
Both snake_case and Node-compatible camelCase field names are accepted when
loading sessions.

```text
start_402 -> show code_url/QR -> status poll
                              ├─ HTTP 402 -> pending
                              ├─ HTTP 200 -> completed
                              ├─ expiry/timeout -> expired
                              └─ other HTTP -> failed
```

The buyer payment header is base64-encoded JSON with `x402Version: 2`, the
WeChat scheme/network, and `out_trade_no`. `pay_402()` is the blocking helper;
`status()`, `poll_session()`, `fulfill()`, `cancel()`, and `list_sessions()`
support recoverable agent/channel workflows.

## 5. Server verification and settlement

The provider facilitator extracts the trade number from the payment payload or
requirement, queries:

```text
GET /v3/pay/transactions/out-trade-no/{out_trade_no}?mchid={mchid}
```

Verification requires `trade_state == SUCCESS` and checks that payer total (or
total) is at least the required fen amount. Settlement is idempotent at the
provider flow level: it returns the WeChat `transaction_id` when available and
does not create a second order.

## 6. CLI and error behavior

The CLI should display the code URL/QR and expose recoverable session commands.
Missing order fields raise `PaymentError`; expired sessions become `expired`,
and non-402/non-200 execution responses become `failed` with a bounded error
message. A user-selected `rail="wechat"` must not silently fall back to crypto.

## 7. Test matrix

- fen conversion and minimum amount validation;
- Native order requirement shape and missing `code_url`;
- RSA request signing and API error handling;
- pending, completed, expired, failed, and cancelled sessions;
- restart/resume from both snake_case and Node camelCase JSON;
- duplicate status/fulfillment and unknown session IDs;
- correct `out_trade_no` extraction and insufficient paid amount.
