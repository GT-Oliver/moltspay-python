"""Focused A402 protocol and persistence tests."""

import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from moltspay.alipay import AlipayBuyerClient, decode_a402_json, encode_a402_json
from moltspay.server.alipay_store import AlipayOrderStore
from moltspay.server.facilitators.alipay import AlipayFacilitator, normalize_cny_amount


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def test_cny_and_payment_needed_are_canonical():
    assert normalize_cny_amount("1") == "1.00"
    for invalid in ("0", "1.234", "1e2", "NaN", "Infinity"):
        try:
            normalize_cny_amount(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(invalid)
    private, public = _keys()
    facilitator = AlipayFacilitator({
        "app_id": "app", "seller_id": "seller", "seller_name": "Provider",
        "private_key_pem": private, "alipay_public_key_pem": public,
        "allow_insecure_gateway": True,
    })
    bill = facilitator.create_payment_needed(
        out_trade_no="MPA1", amount="0.10", goods_name="AI service",
        resource_id="/execute?service=svc", service_id="svc",
    )
    decoded = decode_a402_json(bill["header"])
    assert decoded["protocol"]["amount"] == "0.10"
    assert decoded["protocol"]["seller_sign_type"] == "RSA2"


def test_order_store_claim_and_replay_are_idempotent():
    store = AlipayOrderStore(":memory:")
    order = store.create_order(
        request_id="req-1", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="",
    )
    assert store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", resource_id=order["resource_id"])["state"] == "claimed"
    store.complete(order["out_trade_no"], {"result": "ok"})
    replay = store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", resource_id=order["resource_id"])
    assert replay["state"] == "completed"
    assert replay["order"]["result"] == {"result": "ok"}


def test_buyer_session_persists_and_resumes_without_proof(tmp_path):
    calls = []

    def runner(args):
        calls.append(list(args))
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return ['{"status":"pending","tradeNo":"trade-1","outTradeNo":"MPA1"}']
        if args[0] == "402-query-payment-status":
            return ['{"status":"completed","tradeNo":"trade-1","result":{"ok":true}}']
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider.test/execute", encode_a402_json({"protocol": {"out_trade_no": "MPA1"}}),
        intent_summary="Buy svc", request_id="req-1", request_body=json.dumps({"service": "svc"}),
    )
    assert session.status == "pending"
    completed = client.resume(session.payment_session_id)
    assert completed.status == "completed"
    persisted = (tmp_path / "alipay-sessions" / f"{session.payment_session_id}.json").read_text()
    assert "Payment-Proof" not in persisted
    assert all("payment_proof" not in " ".join(call) for call in calls)
