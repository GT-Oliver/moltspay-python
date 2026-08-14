"""Alipay AI Pay server facilitator with RSA2/OpenAPI support."""

from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

import httpx

from .base import BaseFacilitator, HealthCheckResult, SettleResult, VerifyResult


ALIPAY_NETWORK = "alipay"
ALIPAY_SCHEME = "alipay-aipay"
SIGNING_FIELDS = ["amount", "currency", "goods_name", "out_trade_no", "pay_before", "resource_id", "seller_id", "service_id"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_b64url(value: str) -> Dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


def normalize_cny_amount(value: str) -> str:
    """Validate a CNY amount and normalize it to two decimal places."""
    amount = Decimal(str(value))
    fen = amount * 100
    if not amount.is_finite() or amount < Decimal("0.01") or fen != fen.to_integral_value():
        raise ValueError("price_cny must be at least 0.01 with no more than 2 decimal places")
    return f"{amount:.2f}"


def verify_alipay_response_signature(
    signed_content: str,
    signature: str,
    platform_public_key_pem: str,
) -> bool:
    """Verify an Alipay OpenAPI RSA2 response signature."""
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        key = serialization.load_pem_public_key(platform_public_key_pem.encode())
        key.verify(
            base64.b64decode(signature),
            signed_content.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def _extract_signed_response_content(raw_response: str, response_key: str) -> str:
    """Return the exact JSON value covered by an Alipay response signature."""
    match = re.search(rf'"{re.escape(response_key)}"\s*:\s*', raw_response)
    if not match:
        raise RuntimeError(f"Alipay API response is missing {response_key}")
    start = match.end()
    try:
        _, length = json.JSONDecoder().raw_decode(raw_response[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("Alipay API returned malformed signed JSON") from exc
    return raw_response[start:start + length]


def _read_key(
    config: Dict[str, Any],
    inline_name: str,
    path_name: str,
) -> str:
    inline = config.get(inline_name)
    if inline:
        return str(inline)
    path = config.get(path_name)
    return Path(path).read_text(encoding="utf-8") if path else ""


def _app_key_pair_matches(private_key_pem: str, app_public_key_pem: str) -> bool:
    try:
        from cryptography.hazmat.primitives import serialization

        private_key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
        expected = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        actual = serialization.load_pem_public_key(app_public_key_pem.encode()).public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return expected == actual
    except Exception:
        return False


class AlipayFacilitator(BaseFacilitator):
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.private_key_pem = _read_key(self.config, "private_key_pem", "private_key_path")
        self.app_public_key_pem = _read_key(self.config, "app_public_key_pem", "app_public_key_path")
        self.platform_public_key_pem = (
            _read_key(self.config, "platform_public_key_pem", "platform_public_key_path")
            or _read_key(self.config, "alipay_public_key_pem", "alipay_public_key_path")
        )
        # Kept for callers that used the old attribute name. It now has the
        # standard Alipay meaning: the platform key used for response checks.
        self.public_key_pem = self.platform_public_key_pem
        if not self.private_key_pem:
            raise ValueError("Alipay application private key is required")
        if self.app_public_key_pem and not _app_key_pair_matches(
            self.private_key_pem, self.app_public_key_pem,
        ):
            raise ValueError("Alipay application public key does not match the private key")
        self.gateway_url = self.config.get("gateway_url", "https://openapi.alipay.com/gateway.do")

    @property
    def name(self) -> str:
        return "alipay"

    @property
    def display_name(self) -> str:
        return "Alipay AI Pay"

    @property
    def supported_networks(self) -> List[str]:
        return [ALIPAY_NETWORK]

    def _sign(self, message: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise RuntimeError("Alipay server support requires: pip install moltspay[fiat]") from exc
        key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        return base64.b64encode(key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())).decode()

    def create_payment_requirements(self, service_id: str, price_cny: str, goods_name: str, resource_id: str) -> Dict[str, Any]:
        price_cny = normalize_cny_amount(price_cny)
        out_trade_no = "VID" + _b64url(secrets.token_bytes(22))[:29]
        pay_before = (datetime.now() + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
        fields = {
            "amount": price_cny, "currency": "CNY", "goods_name": goods_name,
            "out_trade_no": out_trade_no, "pay_before": pay_before,
            "resource_id": resource_id, "seller_id": self.config["seller_id"],
            "service_id": service_id or self.config["service_id_default"],
        }
        signature = self._sign("&".join(f"{key}={fields[key]}" for key in SIGNING_FIELDS))
        challenge = {
            "protocol": {
                "out_trade_no": out_trade_no, "amount": price_cny, "currency": "CNY",
                "resource_id": resource_id, "pay_before": pay_before,
                "seller_signature": signature, "seller_sign_type": "RSA2",
                "seller_unique_id": self.config["seller_id"],
            },
            "method": {
                "seller_name": self.config["seller_name"], "seller_id": self.config["seller_id"],
                "seller_app_id": self.config["app_id"], "goods_name": goods_name,
                "seller_unique_id_key": "seller_id", "service_id": fields["service_id"],
            },
        }
        header = _b64url(json.dumps(challenge, separators=(",", ":"), ensure_ascii=False).encode())
        return {
            "requirement": {
                "scheme": ALIPAY_SCHEME, "network": ALIPAY_NETWORK, "asset": "CNY",
                "amount": price_cny, "payTo": self.config["seller_id"], "maxTimeoutSeconds": 1800,
                "extra": {"payment_needed_header": header, "out_trade_no": out_trade_no, "pay_before": pay_before, "service_id": fields["service_id"]},
            },
            "payment_needed_header": header,
        }

    @staticmethod
    def _proof(payment_payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = payment_payload.get("payload")
        if isinstance(raw, dict):
            raw = raw.get("paymentProof") or raw.get("proofHeader") or raw.get("payment_proof")
        if not isinstance(raw, str) or not raw:
            raise ValueError("Alipay payment payload is missing Payment-Proof")
        return _decode_b64url(raw)

    def _openapi(self, method: str, business: Dict[str, Any]) -> Dict[str, Any]:
        if not self.platform_public_key_pem:
            raise RuntimeError(
                "Alipay platform public key is required to verify OpenAPI responses"
            )
        params = {
            "app_id": self.config["app_id"], "method": method, "format": "JSON",
            "charset": "utf-8", "sign_type": "RSA2",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "version": "1.0",
            "biz_content": json.dumps(business, separators=(",", ":"), ensure_ascii=False),
        }
        canonical = "&".join(f"{key}={params[key]}" for key in sorted(params) if params[key] not in (None, ""))
        params["sign"] = self._sign(canonical)
        response = httpx.post(self.gateway_url, data=params, timeout=30.0)
        response.raise_for_status()
        raw_response = response.text
        data = response.json()
        wrapper = method.replace(".", "_") + "_response"
        response_key = wrapper if wrapper in data else "error_response" if "error_response" in data else wrapper
        signature = data.get("sign")
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("Alipay API response is missing sign")
        signed_content = _extract_signed_response_content(raw_response, response_key)
        if not verify_alipay_response_signature(
            signed_content, signature, self.platform_public_key_pem,
        ):
            raise RuntimeError("Alipay API response signature verification failed")
        return data[response_key]

    async def verify(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> VerifyResult:
        try:
            proof = self._proof(payment_payload)
            protocol, method_data = proof["protocol"], proof["method"]
            result = self._openapi("alipay.aipay.agent.payment.verify", {
                "payment_proof": protocol["payment_proof"], "trade_no": protocol["trade_no"],
                "client_session": method_data["client_session"],
            })
            if str(result.get("code")) != "10000":
                return VerifyResult(valid=False, error=f"alipay verify {result.get('code')}: {result.get('sub_msg') or result.get('msg')}", details=result)
            return VerifyResult(valid=True, details=result)
        except Exception as exc:
            return VerifyResult(valid=False, error=str(exc))

    async def settle(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> SettleResult:
        try:
            trade_no = self._proof(payment_payload)["protocol"]["trade_no"]
            result = self._openapi("alipay.aipay.agent.fulfillment.confirm", {"trade_no": trade_no})
            if str(result.get("code")) != "10000":
                return SettleResult(success=False, transaction=trade_no, status="fulfillment_failed", error=str(result.get("sub_msg") or result.get("msg")))
            return SettleResult(success=True, transaction=trade_no, status="fulfilled")
        except Exception as exc:
            return SettleResult(success=False, error=str(exc))

    async def health_check(self) -> HealthCheckResult:
        try:
            self._sign("health-check")
            if not self.platform_public_key_pem:
                raise RuntimeError("Alipay platform public key is not configured")
            from cryptography.hazmat.primitives import serialization
            serialization.load_pem_public_key(self.platform_public_key_pem.encode())
            return HealthCheckResult(healthy=True)
        except Exception as exc:
            return HealthCheckResult(healthy=False, error=str(exc))
