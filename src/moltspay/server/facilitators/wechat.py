"""WeChat Pay v3 Native server facilitator."""

from __future__ import annotations

import base64
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from .base import BaseFacilitator, HealthCheckResult, SettleResult, VerifyResult


WECHAT_NETWORK = "wechat"
WECHAT_SCHEME = "wechatpay-native"


def cny_to_fen(value: str) -> int:
    amount = Decimal(value)
    fen = amount * 100
    if amount < 0 or fen != fen.to_integral_value():
        raise ValueError("price_cny must be a non-negative decimal with <= 2 places")
    return int(fen)


def generate_out_trade_no() -> str:
    return "WX" + secrets.token_hex(15)


def _load_wechat_platform_public_key(platform_public_key_pem: str):
    try:
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_public_key(platform_public_key_pem.encode("utf-8"))
    except Exception as exc:
        raise RuntimeError("WeChat platform public key is invalid") from exc


def verify_wechat_response_signature(
    timestamp: str,
    nonce: str,
    body: str,
    signature: str,
    platform_public_key_pem: str,
) -> bool:
    """Verify a WeChat Pay v3 API response signature."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        key = _load_wechat_platform_public_key(platform_public_key_pem)
        message = f"{timestamp}\n{nonce}\n{body}\n".encode("utf-8")
        key.verify(
            base64.b64decode(signature, validate=True),
            message,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


class WechatFacilitator(BaseFacilitator):
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        key_path = self.config.get("private_key_path")
        self.private_key_pem = self.config.get("private_key_pem") or (Path(key_path).read_text(encoding="utf-8") if key_path else "")
        platform_key_path = self.config.get("platform_public_key_path")
        self.platform_public_key_pem = self.config.get("platform_public_key_pem") or (
            Path(platform_key_path).read_text(encoding="utf-8")
            if platform_key_path else ""
        )
        if self.platform_public_key_pem:
            _load_wechat_platform_public_key(self.platform_public_key_pem)
        self.api_base = self.config.get("api_base", "https://api.mch.weixin.qq.com").rstrip("/")

    @property
    def name(self) -> str:
        return "wechat"

    @property
    def display_name(self) -> str:
        return "WeChat Pay"

    @property
    def supported_networks(self) -> List[str]:
        return [WECHAT_NETWORK]

    def _sign(self, message: str) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise RuntimeError("WeChat server support requires: pip install moltspay[fiat]") from exc
        key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        signature = key.sign(message.encode(), padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(signature).decode()

    def _call(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
        timestamp = str(int(time.time()))
        nonce = secrets.token_hex(16)
        signature = self._sign(f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_text}\n")
        authorization = (
            'WECHATPAY2-SHA256-RSA2048 '
            f'mchid="{self.config["mchid"]}",nonce_str="{nonce}",'
            f'timestamp="{timestamp}",serial_no="{self.config["serial_no"]}",signature="{signature}"'
        )
        response = httpx.request(
            method, self.api_base + path,
            content=body_text.encode("utf-8") if body is not None else None,
            headers={"Authorization": authorization, "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "moltspay-python"},
            timeout=30.0,
        )
        raw_response = response.text
        # Response verification remains opt-in for compatibility with local
        # test gateways. Production providers should configure the WeChat
        # platform public key so every non-empty API response is authenticated.
        if self.platform_public_key_pem and raw_response:
            timestamp = response.headers.get("Wechatpay-Timestamp")
            nonce = response.headers.get("Wechatpay-Nonce")
            response_signature = response.headers.get("Wechatpay-Signature")
            if not timestamp or not nonce or not response_signature:
                raise RuntimeError("WeChat API response is missing signature headers")
            if not verify_wechat_response_signature(
                timestamp, nonce, raw_response, response_signature,
                self.platform_public_key_pem,
            ):
                raise RuntimeError("WeChat API response signature verification failed")
        if not response.is_success:
            raise RuntimeError(f"WeChat API {response.status_code}: {raw_response[:500]}")
        return json.loads(raw_response) if raw_response else {}

    def create_payment_requirements(
        self, price_cny: str, description: str, out_trade_no: Optional[str] = None,
        expires_seconds: int = 300, attach: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        total = cny_to_fen(price_cny)
        if total < 1:
            raise ValueError("WeChat payment minimum is 0.01 CNY")
        trade_no = out_trade_no or generate_out_trade_no()
        expire = datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)
        body = {
            "appid": self.config["appid"], "mchid": self.config["mchid"],
            "description": description, "out_trade_no": trade_no,
            "notify_url": self.config["notify_url"],
            "time_expire": expire.isoformat(timespec="seconds"),
            "amount": {"total": total, "currency": "CNY"},
        }
        if attach:
            body["attach"] = json.dumps(attach, separators=(",", ":"), ensure_ascii=False)
        result = self._call("POST", "/v3/pay/transactions/native", body)
        code_url = result.get("code_url")
        if not code_url:
            raise RuntimeError("WeChat Native order returned no code_url")
        return {
            "scheme": WECHAT_SCHEME, "network": WECHAT_NETWORK, "asset": "CNY",
            "amount": price_cny, "payTo": self.config["mchid"],
            "maxTimeoutSeconds": expires_seconds,
            "extra": {
                "code_url": code_url, "out_trade_no": trade_no,
                "expires_at": expire.isoformat().replace("+00:00", "Z"),
            },
        }

    @staticmethod
    def _trade_no(payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> str:
        payload = payment_payload.get("payload")
        if isinstance(payload, str) and payload:
            return payload
        if isinstance(payload, dict):
            value = payload.get("out_trade_no") or payload.get("outTradeNo")
            if value:
                return str(value)
        value = (requirements.get("extra") or {}).get("out_trade_no")
        if value:
            return str(value)
        raise ValueError("WeChat payment payload must carry out_trade_no")

    def query_order(self, trade_no: str) -> Dict[str, Any]:
        path = f"/v3/pay/transactions/out-trade-no/{quote(trade_no)}?mchid={quote(self.config['mchid'])}"
        return self._call("GET", path)

    async def verify(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> VerifyResult:
        try:
            trade_no = self._trade_no(payment_payload, requirements)
            result = self.query_order(trade_no)
            state = result.get("trade_state")
            if state != "SUCCESS":
                return VerifyResult(valid=False, error=f"wechat trade_state {state or 'UNKNOWN'}", details=result)
            if str(result.get("out_trade_no") or "") != trade_no:
                return VerifyResult(valid=False, error="wechat out_trade_no mismatch", details=result)
            if str(result.get("appid") or "") != str(self.config.get("appid") or ""):
                return VerifyResult(valid=False, error="wechat appid mismatch", details=result)
            if str(result.get("mchid") or "") != str(self.config.get("mchid") or ""):
                return VerifyResult(valid=False, error="wechat mchid mismatch", details=result)
            expected = cny_to_fen(str(requirements["amount"]))
            amount = result.get("amount") or {}
            if str(amount.get("currency") or "CNY") != "CNY":
                return VerifyResult(valid=False, error="wechat currency mismatch", details=result)
            paid = int(amount.get("payer_total", amount.get("total", 0)))
            if paid < expected:
                return VerifyResult(valid=False, error=f"wechat amount {paid} fen below expected {expected}", details=result)
            return VerifyResult(valid=True, details=result)
        except Exception as exc:
            return VerifyResult(valid=False, error=str(exc))

    async def settle(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> SettleResult:
        try:
            trade_no = self._trade_no(payment_payload, requirements)
            result = self.query_order(trade_no)
            if result.get("trade_state") != "SUCCESS":
                return SettleResult(success=False, status=result.get("trade_state"), error="WeChat order is not paid")
            return SettleResult(success=True, transaction=result.get("transaction_id") or trade_no, status="fulfilled")
        except Exception as exc:
            return SettleResult(success=False, error=str(exc))

    async def health_check(self) -> HealthCheckResult:
        try:
            self._sign("health-check")
            return HealthCheckResult(healthy=True)
        except Exception as exc:
            return HealthCheckResult(healthy=False, error=str(exc))
