"""Alipay AI Pay server facilitator with RSA2/OpenAPI support."""

from __future__ import annotations

import base64
import json
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlencode

import httpx

from .base import BaseFacilitator, HealthCheckResult, SettleResult, VerifyResult


ALIPAY_NETWORK = "alipay"
ALIPAY_SCHEME = "alipay-aipay"
SIGNING_FIELDS = ["amount", "currency", "goods_name", "out_trade_no", "pay_before", "resource_id", "seller_id", "service_id"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _decode_b64url(value: str) -> Dict[str, Any]:
    return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))


class AlipayFacilitator(BaseFacilitator):
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.private_key_pem = self.config.get("private_key_pem") or Path(self.config["private_key_path"]).read_text(encoding="utf-8")
        self.public_key_pem = self.config.get("alipay_public_key_pem") or Path(self.config["alipay_public_key_path"]).read_text(encoding="utf-8")
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
        data = response.json()
        wrapper = method.replace(".", "_") + "_response"
        return data.get(wrapper, data)

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
            return HealthCheckResult(healthy=True)
        except Exception as exc:
            return HealthCheckResult(healthy=False, error=str(exc))
