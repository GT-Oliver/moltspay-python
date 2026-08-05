"""Security wallet, audit and invoice tests."""

import json

from moltspay.audit import AuditLog
from moltspay.invoice import PaymentAgent
from moltspay.models import Limits, SecurityLimits, TransferResult
from moltspay.secure_wallet import SecureWallet
from moltspay.wallet import create_wallet, load_wallet
from moltspay.exceptions import WalletError
import pytest


class FakeWallet:
    def __init__(self, root):
        self._wallet_path = root / "wallet.json"
        self.chain = "base"
        self.limits = Limits(max_per_tx=100, max_per_day=100, spent_today=0)
        self.spent = 0

    def transfer(self, to, amount, token):
        return TransferResult(success=True, tx_hash="0x1", to_address=to, amount=amount, token=token, chain=self.chain)

    def record_spend(self, amount):
        self.spent += amount


def test_secure_wallet_approval_flow(tmp_path):
    wallet = FakeWallet(tmp_path)
    secure = SecureWallet(
        wallet=wallet,
        limits=SecurityLimits(single_max=5, daily_max=20, require_whitelist=True),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    queued = secure.transfer("0x0000000000000000000000000000000000000001", 6)
    request_id = queued.error.split(":")[1]
    assert len(secure.get_pending_transfers()) == 1
    executed = secure.approve(request_id, "admin")
    assert executed.success is True
    assert wallet.spent == 6
    assert secure.audit.verify() is True


def test_audit_detects_tampering(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.jsonl"))
    audit.append("limit_change", max=10)
    assert audit.verify()
    path = tmp_path / "audit.jsonl"
    path.write_text(path.read_text().replace('"max": 10', '"max": 11'))
    assert audit.verify() is False


def test_payment_agent_invoice():
    agent = PaymentAgent(chain="base", wallet_address="0x0000000000000000000000000000000000000001")
    invoice = agent.create_invoice("order-1", 1.25, "demo")
    assert invoice.order_id == "order-1"
    assert invoice.chain_id == 8453
    assert invoice.deep_link.startswith("moltspay://pay?")


def test_encrypted_wallet_is_node_compatible_shape(tmp_path):
    path = tmp_path / "wallet.json"
    created = create_wallet(str(path), password="correct horse", label="agent")
    raw = json.loads(path.read_text())
    assert raw["encrypted"] is True
    assert raw["iv"] and raw["salt"]
    assert load_wallet(str(path), password="correct horse").address == created.address
    with pytest.raises(WalletError):
        load_wallet(str(path), password="wrong")
