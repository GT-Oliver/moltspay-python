"""Provider-side Alipay AI Pay (A402) facilitator."""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx

from ...alipay import decode_a402_json, encode_a402_json
from ...exceptions import (
    AlipayConfigInvalid,
    AlipayProofMalformed,
    AlipayResponseSignatureInvalid,
    AlipayVerifyUnavailable,
)
from .base import BaseFacilitator, HealthCheckResult, SettleResult, VerifyResult


ALIPAY_NETWORK = "alipay"
ALIPAY_SCHEME = "a402"
VERIFY_METHOD = "alipay.aipay.agent.payment.verify"
FULFILLMENT_METHOD = "alipay.aipay.agent.fulfillment.confirm"
SIGNING_FIELDS = (
    "amount", "currency", "goods_name", "out_trade_no", "pay_before",
    "resource_id", "seller_id", "service_id",
)
AMOUNT_RE = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?$")
SAFE_PATH_RE = re.compile(r"^/?[A-Za-z0-9][A-Za-z0-9._~:/?&=%+\-]{0,511}$")


def normalize_cny_amount(value: Any) -> str:
    """Validate a CNY amount without accepting exponent/NaN spellings."""
    text = str(value).strip()
    if not AMOUNT_RE.fullmatch(text):
        raise ValueError("price_cny must be a decimal with no more than 2 places")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("invalid CNY amount") from exc
    if not amount.is_finite() or amount < Decimal("0.01"):
        raise ValueError("price_cny must be at least 0.01 CNY")
    return f"{amount:.2f}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _read_secure_file(path_value: Any, label: str) -> str:
    if not isinstance(path_value, str) or not path_value:
        raise AlipayConfigInvalid(f"{label} path is required")
    path = Path(path_value).expanduser()
    try:
        if not path.is_file() or path.is_symlink() or not os.access(path, os.R_OK):
            raise AlipayConfigInvalid(f"{label} path is not a readable regular file")
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AlipayConfigInvalid(f"Unable to read {label}") from exc


def _load_private_key(pem: str):
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except Exception as exc:
        raise AlipayConfigInvalid("Alipay private key is invalid") from exc


def _load_public_key(pem: str):
    try:
        from cryptography.hazmat.primitives import serialization
        return serialization.load_pem_public_key(pem.encode("utf-8"))
    except Exception as exc:
        raise AlipayConfigInvalid("Alipay platform public key is invalid") from exc


def verify_alipay_response_signature(signed_content: str, signature: str, platform_public_key_pem: str) -> bool:
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        _load_public_key(platform_public_key_pem).verify(
            base64.b64decode(signature, validate=True), signed_content.encode("utf-8"),
            padding.PKCS1v15(), hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def _extract_signed_response_content(raw: str, response_key: str) -> str:
    match = re.search(r'"' + re.escape(response_key) + r'"\s*:\s*', raw)
    if not match:
        raise AlipayResponseSignatureInvalid("Alipay response signature wrapper is missing")
    start = match.end()
    try:
        _, length = json.JSONDecoder().raw_decode(raw[start:])
    except json.JSONDecodeError as exc:
        raise AlipayResponseSignatureInvalid("Alipay response wrapper is malformed") from exc
    return raw[start:start + length]


class AlipayFacilitator(BaseFacilitator):
    def __init__(self, config: Dict[str, Any], *, request: Optional[Callable[..., Any]] = None):
        self.config = dict(config or {})
        self.app_id = str(self.config.get("app_id", ""))
        self.seller_id = str(self.config.get("seller_id", ""))
        self.seller_name = str(self.config.get("seller_name", ""))
        if not self.app_id or not self.seller_id or not self.seller_name:
            raise AlipayConfigInvalid("Alipay app_id, seller_id and seller_name are required")
        private = self.config.get("private_key_pem")
        self.private_key_pem = str(private) if private else _read_secure_file(self.config.get("private_key_path"), "private key")
        public = self.config.get("alipay_public_key_pem") or self.config.get("platform_public_key_pem") or self.config.get("public_key_pem")
        self.platform_public_key_pem = str(public) if public else _read_secure_file(
            self.config.get("alipay_public_key_path") or self.config.get("platform_public_key_path") or self.config.get("public_key_path"),
            "Alipay platform public key",
        )
        self._private_key = _load_private_key(self.private_key_pem)
        _load_public_key(self.platform_public_key_pem)
        self.gateway_url = str(self.config.get("gateway_url", "https://openapi.alipay.com/gateway.do"))
        parsed = httpx.URL(self.gateway_url)
        if parsed.scheme != "https" and not bool(self.config.get("allow_insecure_gateway")):
            raise AlipayConfigInvalid("Alipay gateway_url must use HTTPS")
        self.request = request or httpx.request

    @property
    def name(self) -> str:
        return ALIPAY_NETWORK

    @property
    def display_name(self) -> str:
        return "Alipay AI Pay"

    @property
    def supported_networks(self) -> List[str]:
        return [ALIPAY_NETWORK]

    def _sign(self, content: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        return base64.b64encode(self._private_key.sign(content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode("ascii")

    def _payment_signing_content(self, fields: Dict[str, str]) -> str:
        missing = [key for key in SIGNING_FIELDS if not isinstance(fields.get(key), str) or not fields[key]]
        if missing:
            raise ValueError("missing Alipay bill fields: " + ",".join(missing))
        return "&".join(f"{key}={fields[key]}" for key in sorted(SIGNING_FIELDS))

    def create_payment_needed(
        self, *, out_trade_no: Optional[str], amount: str, goods_name: str, resource_id: str,
        service_id: str, timeout_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        amount = normalize_cny_amount(amount)
        if not isinstance(goods_name, str) or not goods_name.strip() or len(goods_name) > 128:
            raise ValueError("goods_name must be non-empty and at most 128 characters")
        if not isinstance(resource_id, str) or not SAFE_PATH_RE.fullmatch(resource_id):
            raise ValueError("resource_id is invalid")
        trade = out_trade_no or "MPA" + secrets.token_hex(14).upper()
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", trade):
            raise ValueError("out_trade_no is invalid")
        service = service_id.strip() if isinstance(service_id, str) else ""
        if not service:
            raise ValueError("service_id is required")
        seconds = int(timeout_seconds or self.config.get("default_timeout_seconds", 1800))
        if seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        pay_before = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        fields = {
            "amount": amount, "currency": "CNY", "goods_name": goods_name.strip(),
            "out_trade_no": trade, "pay_before": pay_before, "resource_id": resource_id,
            "seller_id": self.seller_id, "service_id": str(service),
        }
        protocol = {**fields, "seller_signature": self._sign(self._payment_signing_content(fields)), "seller_sign_type": "RSA2", "seller_unique_id": self.seller_id}
        payload = {
            "protocol": protocol,
            "method": {
                "seller_name": self.seller_name, "seller_id": self.seller_id,
                "seller_app_id": self.app_id, "goods_name": goods_name.strip(),
                "seller_unique_id_key": "seller_id", "service_id": str(service),
            },
        }
        return {
            "header": encode_a402_json(payload), "payload": payload,
            "out_trade_no": trade, "amount": amount, "resource_id": resource_id,
            "pay_before": pay_before, "service_id": str(service), "currency": "CNY",
        }

    def create_payment_requirements(self, service_id: Optional[str] = None, price_cny: Optional[str] = None, goods_name: Optional[str] = None, resource_id: Optional[str] = None, **kwargs: Any) -> Dict[str, Any]:
        # Positional form is retained for callers of the pre-A402 adapter.
        if service_id is not None:
            kwargs.setdefault("service_id", service_id)
        if price_cny is not None:
            kwargs.setdefault("amount", price_cny)
        if goods_name is not None:
            kwargs.setdefault("goods_name", goods_name)
        if resource_id is not None:
            kwargs.setdefault("resource_id", resource_id)
        if "amount" in kwargs and "out_trade_no" not in kwargs:
            kwargs.setdefault("out_trade_no", None)
        bill = self.create_payment_needed(**kwargs)
        result = {
            "scheme": ALIPAY_SCHEME, "network": ALIPAY_NETWORK, "asset": "CNY",
            "amount": bill["amount"], "payTo": self.seller_id,
            "maxTimeoutSeconds": int(self.config.get("default_timeout_seconds", 1800)),
            "extra": {"payment_needed_header": bill["header"], "out_trade_no": bill["out_trade_no"], "resource_id": bill["resource_id"], "service_id": bill["service_id"]},
            "bill": bill,
        }
        result["payment_needed_header"] = bill["header"]
        result["requirement"] = {key: value for key, value in result.items() if key != "requirement"}
        return result

    @staticmethod
    def parse_payment_proof(value: str) -> Dict[str, str]:
        try:
            data = decode_a402_json(value, name="Payment-Proof")
        except Exception as exc:
            raise AlipayProofMalformed("Payment-Proof is malformed") from exc
        protocol = data.get("protocol")
        method = data.get("method")
        if not isinstance(protocol, dict) or not isinstance(method, dict):
            raise AlipayProofMalformed("Payment-Proof has invalid sections")
        values = {"payment_proof": protocol.get("payment_proof"), "trade_no": protocol.get("trade_no"), "client_session": method.get("client_session")}
        if any(not isinstance(item, str) or not item or len(item) > 512 for item in values.values()):
            raise AlipayProofMalformed("Payment-Proof has missing or invalid fields")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", values["trade_no"]):
            raise AlipayProofMalformed("Payment-Proof trade_no is invalid")
        return values  # type: ignore[return-value]

    def _gateway_signing_content(self, params: Dict[str, str]) -> str:
        return "&".join(f"{key}={params[key]}" for key in sorted(params) if key != "sign")

    def _call(self, method: str, biz_content: Dict[str, Any]) -> Dict[str, Any]:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        params: Dict[str, str] = {
            "app_id": self.app_id, "method": method, "format": "JSON", "charset": "utf-8",
            "sign_type": "RSA2", "timestamp": timestamp, "version": "1.0",
            "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
        }
        params["sign"] = self._sign(self._gateway_signing_content(params))
        try:
            response = self.request("POST", self.gateway_url, data=params, timeout=float(self.config.get("api_timeout_seconds", 30)), headers={"Accept": "application/json"})
            raw = response.text
            if not response.is_success:
                raise AlipayVerifyUnavailable("Alipay OpenAPI request failed")
            data = json.loads(raw)
        except AlipayVerifyUnavailable:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise AlipayVerifyUnavailable("Alipay OpenAPI response is unavailable") from exc
        wrapper = f"{method.replace('.', '_')}_response"
        response_obj = data.get(wrapper) if isinstance(data, dict) else None
        if not isinstance(response_obj, dict) or not data.get("sign"):
            raise AlipayResponseSignatureInvalid("Alipay OpenAPI response is missing signed fields")
        if not verify_alipay_response_signature(_extract_signed_response_content(raw, wrapper), str(data["sign"]), self.platform_public_key_pem):
            raise AlipayResponseSignatureInvalid("Alipay OpenAPI response signature is invalid")
        return response_obj

    def verify_payment(self, proof: Dict[str, str]) -> Dict[str, Any]:
        return self._openapi(VERIFY_METHOD, proof)

    def confirm_fulfillment(self, trade_no: str) -> Dict[str, Any]:
        return self._openapi(FULFILLMENT_METHOD, {"trade_no": trade_no})

    def _openapi(self, method: str, business: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility name for the signed gateway call."""
        return self._call(method, business)

    async def verify(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> VerifyResult:
        try:
            raw = payment_payload.get("proof") or payment_payload.get("payment_proof") or payment_payload.get("Payment-Proof")
            proof = self.parse_payment_proof(raw) if isinstance(raw, str) else payment_payload.get("payload")
            if not isinstance(proof, dict):
                raise AlipayProofMalformed("Payment-Proof is missing")
            response = self.verify_payment(proof)
            if str(response.get("code")) != "10000" or response.get("active") is not True:
                return VerifyResult(valid=False, error="alipay_proof_inactive", details={"code": response.get("code")})
            expected = normalize_cny_amount(requirements["amount"])
            if normalize_cny_amount(response.get("amount")) != expected:
                return VerifyResult(valid=False, error="alipay_amount_mismatch", details={"amount": response.get("amount")})
            if str(response.get("trade_no")) != str(proof.get("trade_no")):
                return VerifyResult(valid=False, error="alipay_order_mismatch", details={"trade_no": response.get("trade_no")})
            for key in ("out_trade_no", "service_id", "resource_id", "trade_no"):
                expected_value = (requirements.get("extra") or {}).get(key) or requirements.get(key)
                if expected_value and str(response.get(key)) != str(expected_value):
                    error = {
                        "out_trade_no": "alipay_order_mismatch",
                        "service_id": "alipay_service_mismatch",
                    }.get(key, "alipay_resource_mismatch")
                    return VerifyResult(valid=False, error=error, details={key: response.get(key)})
            return VerifyResult(valid=True, details={**response, "proof": proof})
        except (AlipayProofMalformed, AlipayResponseSignatureInvalid, AlipayVerifyUnavailable) as exc:
            return VerifyResult(valid=False, error=exc.code, details={})
        except Exception:
            return VerifyResult(valid=False, error="alipay_verify_unavailable", details={})

    async def settle(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> SettleResult:
        proof = payment_payload.get("payload") or {}
        trade_no = proof.get("trade_no") if isinstance(proof, dict) else None
        if not trade_no:
            return SettleResult(success=False, error="alipay_proof_malformed")
        return SettleResult(success=True, transaction=str(trade_no), status="verified")

    async def health_check(self) -> HealthCheckResult:
        try:
            _load_private_key(self.private_key_pem)
            _load_public_key(self.platform_public_key_pem)
            return HealthCheckResult(healthy=True)
        except Exception as exc:
            return HealthCheckResult(healthy=False, error="alipay_config_invalid")


__all__ = ["AlipayFacilitator", "ALIPAY_NETWORK", "ALIPAY_SCHEME", "normalize_cny_amount", "verify_alipay_response_signature"]
