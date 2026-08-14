"""Alipay AI Pay facilitator signing and response-verification tests."""

import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from moltspay.server.facilitators.alipay import (
    SIGNING_FIELDS,
    AlipayFacilitator,
    normalize_cny_amount,
    verify_alipay_response_signature,
)


def _keys():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_key, private_pem, public_pem


def _config(private_pem, app_public_pem, platform_public_pem=None):
    config = {
        "seller_id": "seller-1",
        "app_id": "app-1",
        "seller_name": "Seller",
        "service_id_default": "service-1",
        "private_key_pem": private_pem,
        "app_public_key_pem": app_public_pem,
    }
    if platform_public_pem is not None:
        config["platform_public_key_pem"] = platform_public_pem
    return config


def _sign(private_key, content):
    return base64.b64encode(
        private_key.sign(content.encode(), padding.PKCS1v15(), hashes.SHA256())
    ).decode()


def _api_response(method, payload, platform_private_key, *, tamper=False):
    wrapper = method.replace(".", "_") + "_response"
    signed_content = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signature = _sign(platform_private_key, signed_content)
    if tamper:
        signed_content = signed_content.replace("10000", "40004")
    body = json.dumps(
        {wrapper: json.loads(signed_content), "sign": signature},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return httpx.Response(
        200,
        content=body.encode(),
        request=httpx.Request("POST", "https://openapi.alipay.test/gateway.do"),
    )


def _proof_header():
    proof = {
        "protocol": {"payment_proof": "proof", "trade_no": "1" * 32},
        "method": {"client_session": "session-1"},
    }
    return base64.urlsafe_b64encode(
        json.dumps(proof, separators=(",", ":")).encode()
    ).decode().rstrip("=")


def test_verify_alipay_response_signature_accepts_valid_content():
    private_key, _, public_pem = _keys()
    content = '{"code":"10000","msg":"Success"}'

    assert verify_alipay_response_signature(content, _sign(private_key, content), public_pem)


def test_verify_alipay_response_signature_rejects_tampered_content():
    private_key, _, public_pem = _keys()
    signature = _sign(private_key, '{"code":"10000"}')

    assert not verify_alipay_response_signature('{"code":"40004"}', signature, public_pem)


def test_openapi_verifies_response_before_returning(monkeypatch):
    _, app_private_pem, app_public_pem = _keys()
    platform_private, _, platform_public_pem = _keys()
    facilitator = AlipayFacilitator(
        _config(app_private_pem, app_public_pem, platform_public_pem)
    )
    method = "alipay.aipay.agent.payment.verify"
    response = _api_response(method, {"code": "10000", "msg": "Success"}, platform_private)
    monkeypatch.setattr(
        "moltspay.server.facilitators.alipay.httpx.post", lambda *args, **kwargs: response
    )

    assert facilitator._openapi(method, {"trade_no": "1" * 32})["code"] == "10000"


def test_openapi_rejects_tampered_or_unsigned_response(monkeypatch):
    _, app_private_pem, app_public_pem = _keys()
    platform_private, _, platform_public_pem = _keys()
    facilitator = AlipayFacilitator(
        _config(app_private_pem, app_public_pem, platform_public_pem)
    )
    method = "alipay.aipay.agent.payment.verify"
    response = _api_response(
        method, {"code": "10000", "msg": "Success"}, platform_private, tamper=True
    )
    monkeypatch.setattr(
        "moltspay.server.facilitators.alipay.httpx.post", lambda *args, **kwargs: response
    )
    with pytest.raises(RuntimeError, match="signature verification failed"):
        facilitator._openapi(method, {})

    wrapper = method.replace(".", "_") + "_response"
    unsigned = httpx.Response(
        200,
        json={wrapper: {"code": "10000"}},
        request=httpx.Request("POST", "https://openapi.alipay.test/gateway.do"),
    )
    monkeypatch.setattr(
        "moltspay.server.facilitators.alipay.httpx.post", lambda *args, **kwargs: unsigned
    )
    with pytest.raises(RuntimeError, match="missing sign"):
        facilitator._openapi(method, {})


def test_application_public_key_must_match_private_key():
    _, private_pem, _ = _keys()
    _, _, unrelated_public_pem = _keys()

    with pytest.raises(ValueError, match="does not match"):
        AlipayFacilitator(_config(private_pem, unrelated_public_pem))


def test_platform_public_key_is_required_for_openapi_and_health():
    _, private_pem, public_pem = _keys()
    facilitator = AlipayFacilitator(_config(private_pem, public_pem))

    with pytest.raises(RuntimeError, match="platform public key is required"):
        facilitator._openapi("alipay.test", {})
    health = asyncio.run(facilitator.health_check())
    assert not health.healthy
    assert "platform public key" in health.error


def test_requirement_is_normalized_and_signed_by_application_key():
    _, private_pem, public_pem = _keys()
    _, _, platform_public_pem = _keys()
    facilitator = AlipayFacilitator(
        _config(private_pem, public_pem, platform_public_pem)
    )

    built = facilitator.create_payment_requirements("", "1", "Ping", "/execute?service=ping")
    challenge = json.loads(
        base64.urlsafe_b64decode(built["payment_needed_header"] + "==")
    )
    fields = {
        "amount": challenge["protocol"]["amount"],
        "currency": challenge["protocol"]["currency"],
        "goods_name": challenge["method"]["goods_name"],
        "out_trade_no": challenge["protocol"]["out_trade_no"],
        "pay_before": challenge["protocol"]["pay_before"],
        "resource_id": challenge["protocol"]["resource_id"],
        "seller_id": challenge["method"]["seller_id"],
        "service_id": challenge["method"]["service_id"],
    }
    signed_content = "&".join(f"{key}={fields[key]}" for key in SIGNING_FIELDS)
    public_key = serialization.load_pem_public_key(public_pem.encode())
    public_key.verify(
        base64.b64decode(challenge["protocol"]["seller_signature"]),
        signed_content.encode(),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    assert built["requirement"]["amount"] == "1.00"
    assert challenge["protocol"]["amount"] == "1.00"


@pytest.mark.parametrize("value", ["0", "-1", "0.001", "NaN", "Infinity"])
def test_invalid_cny_amounts_are_rejected(value):
    with pytest.raises(ValueError, match="price_cny"):
        normalize_cny_amount(value)


def test_verify_and_settle_call_expected_openapi_methods(monkeypatch):
    _, private_pem, public_pem = _keys()
    _, _, platform_public_pem = _keys()
    facilitator = AlipayFacilitator(
        _config(private_pem, public_pem, platform_public_pem)
    )
    calls = []

    def fake_openapi(method, business):
        calls.append((method, business))
        return {"code": "10000"}

    monkeypatch.setattr(facilitator, "_openapi", fake_openapi)
    payment = {"payload": {"paymentProof": _proof_header()}}

    verified = asyncio.run(facilitator.verify(payment, {}))
    settled = asyncio.run(facilitator.settle(payment, {}))

    assert verified.valid
    assert settled.success
    assert settled.transaction == "1" * 32
    assert calls == [
        (
            "alipay.aipay.agent.payment.verify",
            {"payment_proof": "proof", "trade_no": "1" * 32, "client_session": "session-1"},
        ),
        ("alipay.aipay.agent.fulfillment.confirm", {"trade_no": "1" * 32}),
    ]
