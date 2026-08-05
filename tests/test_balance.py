"""Balance-rail ledger and facilitator regression tests."""

import asyncio

import pytest
import time
from eth_account import Account
from eth_account.messages import encode_defunct

from moltspay.balance import BalanceLedger, build_deduct_message, from_sat, to_sat
from moltspay.server.facilitators.balance import BalanceFacilitator


def test_amount_conversion_is_exact():
    assert to_sat("3.99") == 399
    assert from_sat(399) == "3.99"
    with pytest.raises(ValueError):
        to_sat("0.001")


def test_topup_deduct_replay_and_refund_are_idempotent():
    ledger = BalanceLedger(":memory:", default_single_limit_sat=1000, default_daily_limit_sat=2000)
    topup = ledger.topup("buyer-1", 1000, "external-1")
    replayed_topup = ledger.topup("buyer-1", 1000, "external-1")
    assert replayed_topup["replayed"] is True
    assert ledger.get_buyer("buyer-1")["balance_sat"] == 1000

    deduct = ledger.deduct("buyer-1", 250, request_id="request-1", service="demo")
    replayed_deduct = ledger.deduct("buyer-1", 250, request_id="request-1", service="demo")
    assert deduct["success"] and replayed_deduct["replayed"] is True
    assert ledger.get_buyer("buyer-1")["balance_sat"] == 750

    refund = ledger.refund(deduct["tx_id"], "skill failed")
    replayed_refund = ledger.refund(deduct["tx_id"], "skill failed")
    assert refund["success"] and replayed_refund["replayed"] is True
    assert ledger.get_buyer("buyer-1")["balance_sat"] == 1000


def test_limits_are_enforced():
    ledger = BalanceLedger(":memory:", default_single_limit_sat=500, default_daily_limit_sat=600)
    ledger.topup("buyer", 1000, "fund")
    assert ledger.deduct("buyer", 501)["error"] == "exceeds_single_limit"
    assert ledger.deduct("buyer", 400, request_id="one")["success"]
    assert ledger.deduct("buyer", 300, request_id="two")["error"] == "exceeds_daily_limit"


def test_balance_facilitator_contract():
    facilitator = BalanceFacilitator(":memory:", single_limit="10.00", daily_limit="20.00")
    facilitator.ledger.topup("buyer", 500, "fund")
    requirements = facilitator.create_requirements("2.50", "demo")
    payment = {"payload": {"buyer_id": "buyer", "request_id": "req"}, "network": "balance"}
    verified = asyncio.run(facilitator.verify(payment, requirements))
    settled = asyncio.run(facilitator.settle(payment, requirements))
    assert verified.valid is True
    assert settled.success is True
    assert settled.status == "deducted"


def test_balance_auth_enforce_binds_signer():
    facilitator = BalanceFacilitator(":memory:", single_limit="10.00", daily_limit="20.00", auth_mode="enforce")
    facilitator.ledger.topup("buyer", 500, "fund")
    requirements = facilitator.create_requirements("2.50", "demo")
    account = Account.create()
    timestamp = int(time.time())
    message = build_deduct_message("buyer", "req", "demo", timestamp)
    signature = account.sign_message(encode_defunct(text=message)).signature.hex()
    payment = {"payload": {"buyer_id": "buyer", "request_id": "req", "auth": {"timestamp": timestamp, "signature": signature}}}
    assert asyncio.run(facilitator.verify(payment, requirements)).valid is True
    assert facilitator.ledger.get_buyer("buyer")["signer_address"] == account.address.lower()

    other = Account.create()
    other_signature = other.sign_message(encode_defunct(text=message)).signature.hex()
    payment["payload"]["auth"]["signature"] = other_signature
    rejected = asyncio.run(facilitator.verify(payment, requirements))
    assert rejected.valid is False
    assert "signer_mismatch" in rejected.error
