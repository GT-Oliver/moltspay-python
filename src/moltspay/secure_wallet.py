"""Wallet policy layer with limits, whitelist and approval queue."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

from .audit import AuditLog
from .models import PendingTransfer, SecurityLimits, TransferResult
from .wallet import Wallet


class SecureWallet:
    def __init__(
        self,
        wallet: Optional[Wallet] = None,
        *,
        wallet_path: Optional[str] = None,
        private_key: Optional[str] = None,
        chain: str = "base",
        limits: Optional[SecurityLimits] = None,
        whitelist: Optional[Iterable[str]] = None,
        audit_path: Optional[str] = None,
    ):
        self.wallet = wallet or Wallet(wallet_path=wallet_path, private_key=private_key, chain=chain)
        self.security_limits = limits or SecurityLimits()
        self.whitelist = {item.lower() for item in (whitelist or [])}
        self.pending: Dict[str, PendingTransfer] = {}
        default_audit = self.wallet._wallet_path.parent / "audit.jsonl"
        self.audit = AuditLog(audit_path or str(default_audit))

    def transfer(self, to: str, amount: float, token: str = "USDC", reason: Optional[str] = None, requester: Optional[str] = None) -> TransferResult:
        request_id = str(uuid.uuid4())
        self.audit.append("transfer_request", request_id, to=to, amount=amount, token=token, reason=reason, requester=requester)
        if amount > self.security_limits.single_max:
            return self._queue(request_id, to, amount, token, reason, requester, "Amount exceeds single-transaction limit")
        if self.wallet.limits.spent_today + amount > self.security_limits.daily_max:
            return self._queue(request_id, to, amount, token, reason, requester, "Amount exceeds daily limit")
        if self.security_limits.require_whitelist and to.lower() not in self.whitelist:
            return self._queue(request_id, to, amount, token, reason, requester, "Recipient requires approval")
        result = self.wallet.transfer(to, amount, token)
        self.audit.append("transfer_executed" if result.success else "transfer_failed", request_id, **result.model_dump())
        if result.success:
            self.wallet.record_spend(amount)
        return result

    def _queue(self, request_id: str, to: str, amount: float, token: str, reason: Optional[str], requester: Optional[str], message: str) -> TransferResult:
        self.pending[request_id] = PendingTransfer(
            id=request_id, to=to, amount=amount, token=token, reason=reason,
            requester=requester, created_at=datetime.now(timezone.utc).isoformat(),
        )
        return TransferResult(success=False, to_address=to, amount=amount, token=token, chain=self.wallet.chain, error=f"PENDING_APPROVAL:{request_id}:{message}")

    def approve(self, request_id: str, approver: str) -> TransferResult:
        request = self.pending.get(request_id)
        if not request or request.status != "pending":
            return TransferResult(success=False, error="Pending transfer not found")
        request.status = "approved"
        self.audit.append("transfer_approved", request_id, approver=approver)
        result = self.wallet.transfer(request.to, request.amount, request.token)
        request.status = "executed" if result.success else "approved"
        self.audit.append("transfer_executed" if result.success else "transfer_failed", request_id, **result.model_dump())
        if result.success:
            self.wallet.record_spend(request.amount)
        return result

    def reject(self, request_id: str, rejecter: str, reason: Optional[str] = None) -> None:
        request = self.pending.get(request_id)
        if not request or request.status != "pending":
            raise ValueError("Pending transfer not found")
        request.status = "rejected"
        self.audit.append("transfer_rejected", request_id, rejecter=rejecter, reason=reason)

    def add_to_whitelist(self, address: str, added_by: str) -> None:
        self.whitelist.add(address.lower())
        self.audit.append("whitelist_add", address=address, added_by=added_by)

    def remove_from_whitelist(self, address: str, removed_by: str) -> None:
        self.whitelist.discard(address.lower())
        self.audit.append("whitelist_remove", address=address, removed_by=removed_by)

    def get_pending_transfers(self) -> list[PendingTransfer]:
        return [item for item in self.pending.values() if item.status == "pending"]
