"""WeChat Pay v3 response-signature tests."""

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from moltspay.server.facilitators.wechat import (
    WechatFacilitator,
    verify_wechat_response_signature,
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


def _response_signature(private_key, timestamp, nonce, body):
    message = f"{timestamp}\n{nonce}\n{body}\n".encode()
    return base64.b64encode(
        private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    ).decode()


def test_verify_wechat_response_signature_accepts_valid_body():
    private_key, _, public_pem = _keys()
    body = '{"trade_state":"SUCCESS"}'
    signature = _response_signature(private_key, "100", "nonce", body)

    assert verify_wechat_response_signature("100", "nonce", body, signature, public_pem)


def test_verify_wechat_response_signature_rejects_tampered_body():
    private_key, _, public_pem = _keys()
    signature = _response_signature(private_key, "100", "nonce", "original")

    assert not verify_wechat_response_signature(
        "100", "nonce", "tampered", signature, public_pem
    )


def test_call_verifies_raw_response_before_parsing(monkeypatch):
    private_key, private_pem, public_pem = _keys()
    body = json.dumps({"trade_state": "SUCCESS"}, separators=(",", ":"))
    headers = {
        "Wechatpay-Timestamp": "100",
        "Wechatpay-Nonce": "nonce",
        "Wechatpay-Signature": _response_signature(private_key, "100", "nonce", body),
    }
    response = httpx.Response(
        200,
        content=body.encode(),
        headers=headers,
        request=httpx.Request("GET", "https://api.mch.weixin.qq.com/test"),
    )
    monkeypatch.setattr("moltspay.server.facilitators.wechat.httpx.request", lambda *a, **k: response)
    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
        "platform_public_key_pem": public_pem,
    })

    assert facilitator._call("GET", "/test") == {"trade_state": "SUCCESS"}


def test_call_allows_non_empty_response_without_platform_public_key(monkeypatch):
    private_key, private_pem, _ = _keys()
    body = json.dumps({"trade_state": "SUCCESS"}, separators=(",", ":"))
    response = httpx.Response(
        200,
        content=body.encode(),
        headers={
            "Wechatpay-Timestamp": "100",
            "Wechatpay-Nonce": "nonce",
            "Wechatpay-Signature": _response_signature(private_key, "100", "nonce", body),
        },
        request=httpx.Request("GET", "https://api.mch.weixin.qq.com/test"),
    )
    monkeypatch.setattr("moltspay.server.facilitators.wechat.httpx.request", lambda *a, **k: response)
    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
    })

    assert facilitator._call("GET", "/test") == {"trade_state": "SUCCESS"}


def test_call_rejects_missing_response_signature_headers(monkeypatch):
    _, private_pem, public_pem = _keys()
    response = httpx.Response(
        200,
        json={"trade_state": "SUCCESS"},
        request=httpx.Request("GET", "https://api.mch.weixin.qq.com/test"),
    )
    monkeypatch.setattr("moltspay.server.facilitators.wechat.httpx.request", lambda *a, **k: response)
    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
        "platform_public_key_pem": public_pem,
    })

    with pytest.raises(RuntimeError, match="missing signature headers"):
        facilitator._call("GET", "/test")


def test_call_rejects_invalid_response_signature(monkeypatch):
    _, private_pem, public_pem = _keys()
    body = json.dumps({"trade_state": "SUCCESS"}, separators=(",", ":"))
    response = httpx.Response(
        200,
        content=body.encode(),
        headers={
            "Wechatpay-Timestamp": "100",
            "Wechatpay-Nonce": "nonce",
            "Wechatpay-Signature": base64.b64encode(b"invalid signature").decode(),
        },
        request=httpx.Request("GET", "https://api.mch.weixin.qq.com/test"),
    )
    monkeypatch.setattr("moltspay.server.facilitators.wechat.httpx.request", lambda *a, **k: response)
    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
        "platform_public_key_pem": public_pem,
    })

    with pytest.raises(RuntimeError, match="signature verification failed"):
        facilitator._call("GET", "/test")


def test_call_rejects_tampered_response_body(monkeypatch):
    private_key, private_pem, public_pem = _keys()
    original_body = json.dumps({"trade_state": "SUCCESS"}, separators=(",", ":"))
    response = httpx.Response(
        200,
        content=json.dumps({"trade_state": "REFUND"}, separators=(",", ":")).encode(),
        headers={
            "Wechatpay-Timestamp": "100",
            "Wechatpay-Nonce": "nonce",
            "Wechatpay-Signature": _response_signature(private_key, "100", "nonce", original_body),
        },
        request=httpx.Request("GET", "https://api.mch.weixin.qq.com/test"),
    )
    monkeypatch.setattr("moltspay.server.facilitators.wechat.httpx.request", lambda *a, **k: response)
    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
        "platform_public_key_pem": public_pem,
    })

    with pytest.raises(RuntimeError, match="signature verification failed"):
        facilitator._call("GET", "/test")


def test_platform_public_key_path_is_loaded(tmp_path):
    _, private_pem, public_pem = _keys()
    key_path = tmp_path / "wechat-platform.pem"
    key_path.write_text(public_pem, encoding="utf-8")

    facilitator = WechatFacilitator({
        "mchid": "mchid",
        "serial_no": "serial",
        "private_key_pem": private_pem,
        "platform_public_key_path": str(key_path),
    })

    assert facilitator.platform_public_key_pem == public_pem


def test_invalid_configured_platform_public_key_is_rejected_at_construction():
    _, private_pem, _ = _keys()

    with pytest.raises(RuntimeError, match="platform public key is invalid"):
        WechatFacilitator({
            "mchid": "mchid",
            "serial_no": "serial",
            "private_key_pem": private_pem,
            "platform_public_key_pem": "not a PEM public key",
        })
