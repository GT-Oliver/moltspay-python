"""Server facilitator for the custodial balance rail."""

from typing import Any, Dict, List, Optional

import time

from eth_account import Account
from eth_account.messages import encode_defunct

from ...balance import (
    BALANCE_AUTH_MAX_SKEW_SECONDS, BalanceLedger, build_deduct_message,
    from_sat, to_sat,
)
from .base import BaseFacilitator, HealthCheckResult, SettleResult, VerifyResult


class BalanceFacilitator(BaseFacilitator):
    def __init__(
        self,
        db_path: str,
        currency: str = "USD",
        single_limit: str = "5.00",
        daily_limit: str = "10.00",
        auth_mode: str = "off",
    ):
        self.currency = currency
        self.auth_mode = auth_mode
        self.ledger = BalanceLedger(
            db_path=db_path, currency=currency,
            default_single_limit_sat=to_sat(single_limit),
            default_daily_limit_sat=to_sat(daily_limit),
        )

    @property
    def name(self) -> str:
        return "balance"

    @property
    def display_name(self) -> str:
        return "Custodial Balance"

    @property
    def supported_networks(self) -> List[str]:
        return ["balance"]

    def create_requirements(self, price: str, service_id: Optional[str] = None) -> Dict[str, Any]:
        to_sat(price)
        return {
            "scheme": "balance", "network": "balance", "asset": self.currency,
            "amount": price, "payTo": "custodial", "maxTimeoutSeconds": 30,
            "extra": {"service_id": service_id} if service_id else {},
        }

    @staticmethod
    def _payload(payment_payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = payment_payload.get("payload") or {}
        return payload if isinstance(payload.get("buyer_id"), str) and payload["buyer_id"] else None

    async def verify(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> VerifyResult:
        payload = self._payload(payment_payload)
        if not payload:
            return VerifyResult(valid=False, error="Missing buyer_id in balance payment payload")
        auth_result = self._verify_auth(payload, requirements)
        if self.auth_mode == "enforce" and not auth_result["ok"]:
            return VerifyResult(valid=False, error=f"balance auth failed: {auth_result['reason']}", details=auth_result)
        try:
            result = self.ledger.check_deduct(payload["buyer_id"], to_sat(requirements["amount"]))
        except ValueError as exc:
            return VerifyResult(valid=False, error=str(exc))
        if not result["success"]:
            return VerifyResult(valid=False, error=result["error"], details=result)
        return VerifyResult(valid=True, details={"balance": from_sat(result["balance_sat"]), "auth": auth_result})

    def _verify_auth(self, payload: Dict[str, Any], requirements: Dict[str, Any]) -> Dict[str, Any]:
        if self.auth_mode == "off":
            return {"ok": True, "reason": "disabled"}
        auth = payload.get("auth")
        if not isinstance(auth, dict) or not auth.get("signature") or not isinstance(auth.get("timestamp"), int):
            return {"ok": False, "reason": "no_signature"}
        timestamp = auth["timestamp"]
        if abs(int(time.time()) - timestamp) > BALANCE_AUTH_MAX_SKEW_SECONDS:
            return {"ok": False, "reason": "timestamp_skew"}
        request_id = payload.get("request_id")
        service = (requirements.get("extra") or {}).get("service_id")
        if not request_id or not service:
            return {"ok": False, "reason": "malformed"}
        try:
            message = build_deduct_message(payload["buyer_id"], request_id, service, timestamp)
            recovered = Account.recover_message(encode_defunct(text=message), signature=auth["signature"]).lower()
        except Exception:
            return {"ok": False, "reason": "bad_signature"}
        binding = self.ledger.bind_signer(payload["buyer_id"], recovered)
        if binding["conflict"]:
            return {"ok": False, "reason": "signer_mismatch", "recovered": recovered, "existing": binding["existing"]}
        return {"ok": True, "recovered": recovered}

    async def settle(self, payment_payload: Dict[str, Any], requirements: Dict[str, Any]) -> SettleResult:
        payload = self._payload(payment_payload)
        if not payload:
            return SettleResult(success=False, error="Missing buyer_id in balance payment payload")
        result = self.ledger.deduct(
            buyer_id=payload["buyer_id"], amount_sat=to_sat(requirements["amount"]),
            request_id=payload.get("request_id"), service=(requirements.get("extra") or {}).get("service_id"),
        )
        if not result["success"]:
            return SettleResult(success=False, status=result.get("error"), error=result.get("error"))
        return SettleResult(
            success=True, transaction=result["tx_id"],
            status="replayed" if result.get("replayed") else "deducted",
        )

    def refund(self, deduct_tx_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        return self.ledger.refund(deduct_tx_id, reason)

    async def health_check(self) -> HealthCheckResult:
        try:
            return HealthCheckResult(healthy=self.ledger.integrity_ok())
        except Exception as exc:
            return HealthCheckResult(healthy=False, error=str(exc))
