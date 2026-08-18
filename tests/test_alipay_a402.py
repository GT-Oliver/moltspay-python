"""Focused A402 protocol and persistence tests."""

import asyncio
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from moltspay.alipay import (
    AlipayBuyerClient,
    decode_a402_json,
    encode_a402_json,
    parse_alipay_cli_output,
)
from moltspay.client import MoltsPay
from moltspay.exceptions import (
    AlipayRequestContextInvalid,
    AlipayRequestContextMissing,
    AlipayWalletNotReady,
)
from moltspay.server.alipay_store import AlipayOrderStore
from moltspay.server.facilitators.alipay import AlipayFacilitator, normalize_cny_amount
from moltspay.x402 import _service_from_dict


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
    assert decoded["protocol"]["service_id"] == "svc"
    assert decoded["protocol"]["seller_sign_type"] == "RSA2"


def test_order_store_claim_and_replay_are_idempotent():
    store = AlipayOrderStore(":memory:")
    order = store.create_order(
        request_id="req-1", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="", service_id="API_SVC",
    )
    assert store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", resource_id=order["resource_id"])["state"] == "claimed"
    store.complete(order["out_trade_no"], {"result": "ok"})
    replay = store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", resource_id=order["resource_id"])
    assert replay["state"] == "completed"
    assert replay["order"]["result"] == {"result": "ok"}


def test_order_store_rejects_service_change_for_same_idempotency_key():
    store = AlipayOrderStore(":memory:")
    store.create_order(
        request_id="req-1", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="", service_id="API_ONE",
    )
    try:
        store.create_order(
            request_id="req-1", kind="service", amount_fen=10,
            resource_id="/execute?service=svc", goods_name="AI", pay_before="", service_id="API_TWO",
        )
    except ValueError as exc:
        assert "idempotency key conflicts" in str(exc)
    else:
        raise AssertionError("service_id changes must not reuse an existing order")


def test_facilitator_rejects_verified_service_mismatch():
    private, public = _keys()
    facilitator = AlipayFacilitator({
        "app_id": "app", "seller_id": "seller", "seller_name": "Provider",
        "private_key_pem": private, "alipay_public_key_pem": public,
        "allow_insecure_gateway": True,
    })
    facilitator.verify_payment = lambda proof: {
        "code": "10000", "active": True, "amount": "0.10",
        "trade_no": proof["trade_no"], "out_trade_no": "MPA1",
        "service_id": "API_OTHER", "resource_id": "/execute?service=svc",
    }
    result = asyncio.run(facilitator.verify(
        {"payload": {"trade_no": "trade-1"}},
        {
            "amount": "0.10",
            "extra": {
                "out_trade_no": "MPA1", "service_id": "API_SVC",
                "resource_id": "/execute?service=svc",
            },
        },
    ))
    assert not result.valid
    assert result.error == "alipay_service_mismatch"


def test_buyer_session_persists_and_resumes_without_proof(tmp_path):
    calls = []
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        calls.append(list(args))
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [
                "请使用支付宝完成支付",
                "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/"
                f"payment_{out_shake_no}.png",
            ]
        if args[0] == "402-query-payment-status":
            return ['{"status":"completed","tradeNo":"trade-1","result":{"ok":true}}']
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider.test/execute", encode_a402_json({"protocol": {"out_trade_no": "MPA1"}}),
        intent_summary="Buy svc", request_id="req-1", request_body=json.dumps({"service": "svc"}),
        business_session_id="d52e3b71-d00e-4a51-bc16-169cba465bc9",
    )
    assert session.status == "pending"
    assert session.out_shake_no == out_shake_no
    assert session.media_paths == [
        "/tmp/openclaw/alipay-bot-cli/qrcode/"
        f"payment_{out_shake_no}.png"
    ]
    completed = client.resume(session.payment_session_id)
    assert completed.status == "completed"
    query = next(call for call in calls if call[0] == "402-query-payment-status")
    assert query[query.index("--out-shake-no") + 1] == out_shake_no
    persisted = (tmp_path / "alipay-sessions" / f"{session.payment_session_id}.json").read_text()
    assert "Payment-Proof" not in persisted
    assert all("payment_proof" not in " ".join(call) for call in calls)
    assert all(
        "mpay_alipay_" not in call[call.index("--session-id") + 1]
        for call in calls if "--session-id" in call
    )


@pytest.mark.parametrize("label", ["订单号", "查询单号"])
@pytest.mark.parametrize("family", ["8282", "8283"])
def test_cli_output_parses_labeled_recovery_number(label, family):
    out_shake_no = f"1234567890{family}123456789012345678"

    parsed = parse_alipay_cli_output([
        f"- {label}：{out_shake_no}",
        "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/payment_ignored.png",
    ])

    assert parsed["out_shake_no"] == out_shake_no


def test_cli_output_parses_recovery_number_from_official_current_media_path():
    out_shake_no = "12345678908283123456789012345678"
    parsed = parse_alipay_cli_output([
        "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/"
        f"payment_{out_shake_no}.png",
    ])

    assert parsed["out_shake_no"] == out_shake_no


def test_cli_output_does_not_infer_recovery_number_from_arbitrary_media_path():
    parsed = parse_alipay_cli_output([
        "MEDIA: /tmp/untrusted/payment_12345678908282123456789012345678.png",
    ])

    assert "out_shake_no" not in parsed


def test_wallet_code_200_applied_unbound_is_not_ready():
    client = AlipayBuyerClient(
        runner=lambda _: ['{"code":200,"status":"applied_unbound","message":"已申请开通"}']
    )

    with pytest.raises(AlipayWalletNotReady) as error:
        client.check_wallet()

    assert error.value.details == {
        "status": "applied_unbound", "reason": "waiting_for_authorization",
    }


def test_wallet_requires_explicit_bound_signal():
    ambiguous = AlipayBuyerClient(runner=lambda _: ['{"code":200,"success":true}'])
    with pytest.raises(AlipayWalletNotReady):
        ambiguous.check_wallet()

    bound = AlipayBuyerClient(
        runner=lambda _: ['{"code":200,"message":"已开启支付宝支付功能"}']
    )
    assert bound.check_wallet()["bound"] is True


def test_wallet_parses_pretty_printed_json_with_diagnostic_lines():
    bound = AlipayBuyerClient(runner=lambda _: [
        "checking wallet...",
        "{",
        '  "code": 200,',
        '  "message": "已开启支付宝支付功能"',
        "}",
        "done",
    ])
    assert bound.check_wallet()["status"] == "bound"

    not_opened = AlipayBuyerClient(runner=lambda _: [
        "{",
        '  "code": 500,',
        '  "message": "未开通",',
        '  "reason": "当前尚未开启支付宝支付能力"',
        "}",
    ])
    with pytest.raises(AlipayWalletNotReady) as error:
        not_opened.check_wallet()
    assert error.value.details == {"status": "not_opened"}


def test_business_session_uses_runtime_context_and_rejects_local_session(monkeypatch):
    monkeypatch.setenv("AIPAY_SESSION_ID", "d52e3b71-d00e-4a51-bc16-169cba465bc9")
    monkeypatch.setenv("AIPAY_FRAMEWORK", "openclaw")
    client = AlipayBuyerClient(framework="moltspay")

    assert client._business_session_id(None) == "d52e3b71-d00e-4a51-bc16-169cba465bc9"
    assert client.framework == "openclaw"
    with pytest.raises(AlipayRequestContextInvalid):
        client._business_session_id("mpay_alipay_deadbeef")


def test_missing_business_session_fails_before_provider_request(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPAY_SESSION_ID", raising=False)
    called = False

    def unexpected_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider request must not run")

    monkeypatch.setattr("moltspay.client.httpx.post", unexpected_post)
    client = MoltsPay(
        config_dir=str(tmp_path), wallet_path=str(tmp_path / "wallet.json"),
        solana_wallet_path=str(tmp_path / "wallet-solana.json"),
    )

    with pytest.raises(AlipayRequestContextMissing):
        client.start_alipay_payment("https://provider.test", "pong")

    assert called is False


def test_service_discovery_and_alipay_pay_use_cny_rail_quote(tmp_path):
    service = _service_from_dict({
        "id": "pong", "name": "Pong", "price": 1.0, "currency": "USDC",
        "paymentRails": {"alipay": {"amount": "1.00", "currency": "CNY"}},
    })
    assert service.payment_rails["alipay"] == {"amount": "1.00", "currency": "CNY"}

    client = MoltsPay(
        config_dir=str(tmp_path), wallet_path=str(tmp_path / "wallet.json"),
        solana_wallet_path=str(tmp_path / "wallet-solana.json"),
    )
    client.discover = lambda _: [service]
    observed = {}

    def pay_alipay(url, service_id, params, amount, currency, **options):
        observed.update(amount=amount, currency=currency, options=options)
        return "paid"

    client._pay_alipay = pay_alipay
    result = client.pay(
        "https://provider.test", "pong", rail="alipay",
        rail_options={"business_session_id": "d52e3b71-d00e-4a51-bc16-169cba465bc9"},
    )

    assert result == "paid"
    assert observed == {
        "amount": 1.0,
        "currency": "CNY",
        "options": {"business_session_id": "d52e3b71-d00e-4a51-bc16-169cba465bc9"},
    }
