# Alipay AI Pay Rail Design for Python

## 1. Scope

This rail implements the Alipay AI Pay 402 handshake while delegating the
buyer-side wallet interaction to the official `alipay-bot` CLI.

- Buyer: `moltspay.alipay.AlipayClient` runs and parses `alipay-bot`.
- Provider: `server.facilitators.alipay.AlipayFacilitator` creates the signed
  payment challenge and calls Alipay RSA2/OpenAPI for verification and
  fulfillment.

The rail identifier is `alipay`; the scheme is `alipay-aipay`.

## 2. Provider configuration

The provider manifest enables `alipay` and supplies seller ID, app ID, seller
name, default service ID, merchant private key, and Alipay public key. Keys may
be supplied as PEM strings or paths. The gateway defaults to
`https://openapi.alipay.com/gateway.do`.

The amount is CNY yuan, not fen. The provider creates a 30-minute default
payment window and signs these fields in this exact order:

```text
amount, currency, goods_name, out_trade_no,
pay_before, resource_id, seller_id, service_id
```

## 3. Payment-needed challenge

`AlipayFacilitator.create_payment_requirements()` creates an `out_trade_no`,
signs the protocol fields with RSA2, and returns a Base64URL-encoded
`payment_needed_header` in `extra`:

```json
{
  "scheme": "alipay-aipay",
  "network": "alipay",
  "asset": "CNY",
  "amount": "2.00",
  "extra": {
    "payment_needed_header": "...",
    "out_trade_no": "VID...",
    "pay_before": "2026-08-05 12:30:00",
    "service_id": "..."
  }
}
```

The client writes the challenge to
`~/.moltspay/alipay/402_<request_id>.txt`, allowing the CLI flow to be
recovered or inspected without putting the challenge in process arguments.

## 4. Buyer flow

`AlipayClient.pay_402()` performs:

```text
payment-intent -> check-wallet -> 402-buyer-pay
               -> parse tradeNo/payment URL
               -> 402-query-payment-status polling
               -> extract resource body
               -> 402-buyer-fulfillment-ack
```

The client requires a pure 32-digit `tradeNo`, preserves the cashier URL, and
accepts both JSON and human-readable CLI output. Status normalization is:

| CLI marker | State |
|---|---|
| success / trade success / resource response 200 | `paid` |
| unpaid / wait / pending / process | `pending` |
| closed / cancel / fail / reject / timeout | `rejected` |

The fulfillment acknowledgement is best-effort after the resource has been
received; failure to acknowledge does not discard the successful result.

## 5. Server verification and fulfillment

The provider decodes the Base64URL `Payment-Proof`, then calls:

```text
alipay.aipay.agent.payment.verify
alipay.aipay.agent.fulfillment.confirm
```

Verification requires Alipay response code `10000`. Settlement confirms the
trade number and returns it as the transaction identifier. RSA2 signatures are
generated over the sorted OpenAPI request parameters and the configured public
key is used by the external protocol.

## 6. CLI, errors, and safety

The buyer must have `alipay-bot` installed and an opened/bound wallet. Missing
CLI, unopened wallet, missing payment challenge, malformed trade number, and
rejected/timeout statuses map to dedicated `Alipay*` exceptions. A selected
`rail="alipay"` must never silently fall back to crypto.

Do not modify machine-readable CLI output assumptions: URL parsing trims
trailing Markdown delimiters, and Payment-Proof decoding restores Base64URL
padding before JSON parsing.

## 7. Test matrix

- trade number parsing for JSON, labeled text, invalid length, and bare values;
- payment URL parsing including `alipays://` and Markdown punctuation;
- pending, paid, rejected, and unknown status normalization;
- wallet-not-ready and missing executable errors;
- challenge persistence, timeout, rejection, result-body extraction, and
  fulfillment acknowledgement;
- seller signature field order, Base64URL challenge/proof, verify failure, and
  fulfillment failure.
