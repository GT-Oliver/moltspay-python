"""Balance-rail ledger and facilitator regression tests."""

import asyncio
import base64
import json

import pytest
import time
from eth_account import Account
from eth_account.messages import encode_defunct

from moltspay.balance import BalanceClient, BalanceLedger, build_deduct_message, from_sat, to_sat
from moltspay.exceptions import InsufficientBalance
from moltspay.server.facilitators.balance import BalanceFacilitator
from moltspay.server.facilitators.registry import FacilitatorRegistry


def test_amount_conversion_is_exact():
    assert to_sat("3.99") == 399
    assert from_sat(399) == "3.99"
    with pytest.raises(ValueError):
        to_sat("0.001")
    with pytest.raises(ValueError):
        to_sat("not-a-number")


def test_topup_deduct_replay_and_refund_are_idempotent():
    with BalanceLedger(":memory:", default_single_limit_sat=1000, default_daily_limit_sat=2000) as ledger:
        topup = ledger.topup("buyer-1", 1000, "external-1")
        replayed_topup = ledger.topup("buyer-1", 1000, "external-1")
        assert topup["replayed"] is False
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
        assert len(ledger.list_transactions("buyer-1")) == 3
        assert ledger.integrity_ok() is True


def test_limits_are_enforced():
    with BalanceLedger(":memory:", default_single_limit_sat=500, default_daily_limit_sat=600) as ledger:
        assert ledger.check_deduct("missing", 1)["error"] == "buyer_not_found"
        ledger.topup("buyer", 1000, "fund")
        assert ledger.deduct("buyer", 501)["error"] == "exceeds_single_limit"
        assert ledger.deduct("buyer", 400, request_id="one")["success"]
        assert ledger.deduct("buyer", 300, request_id="two")["error"] == "exceeds_daily_limit"
        ledger.db.execute("UPDATE buyers SET status='disabled' WHERE buyer_id='buyer'")
        assert ledger.check_deduct("buyer", 1)["error"] == "buyer_not_active"
        with pytest.raises(ValueError):
            ledger.deduct("buyer", 0)
        with pytest.raises(ValueError):
            ledger.topup("buyer", 0, "bad")
        assert ledger.refund("missing")["error"] == "tx_not_found"
        assert ledger.refund(ledger.list_transactions("buyer")[-1]["id"])["error"] == "not_a_deduct"


def test_ledger_bindings_and_currency_guard(tmp_path):
    path = tmp_path / "ledger.db"
    with BalanceLedger(path) as ledger:
        assert ledger.bind_signer("buyer", "0xABC")["bound"] is True
        assert ledger.bind_signer("buyer", "0xDEF")["conflict"] is True
        assert ledger.bind_openid("buyer", "openid-1")["bound"] is True
        assert ledger.bind_openid("buyer", "openid-2")["conflict"] is True
    with pytest.raises(ValueError, match="currency mismatch"):
        BalanceLedger(path, currency="CNY")


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
    facilitator.ledger.close()


def test_registry_accepts_node_balance_payload_without_accepted_object():
    facilitator = BalanceFacilitator(":memory:", single_limit="10.00", daily_limit="20.00")
    facilitator.ledger.topup("node-buyer", 500, "fund")
    registry = object.__new__(FacilitatorRegistry)
    registry._facilitators = {"balance": facilitator}
    requirements = facilitator.create_requirements("0.01", "ping")
    payment = {
        "accepted": None,
        "scheme": "balance",
        "network": "balance",
        "payload": {"buyer_id": "node-buyer", "request_id": "node-request"},
    }

    verified = asyncio.run(registry.verify(payment, requirements))
    settled = asyncio.run(registry.settle(payment, requirements))

    assert verified.valid is True
    assert settled.success is True
    assert facilitator.ledger.get_buyer("node-buyer")["balance_sat"] == 499
    facilitator.ledger.close()


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
    facilitator.ledger.close()


class FakeBalanceHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def http_response(status, body):
    import httpx
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://provider.test"))


def test_balance_client_query_topup_confirm_and_transactions():
    http = FakeBalanceHttp([
        http_response(404, {}),
        http_response(200, {
            "balance": "3.00", "spent_today": "1.00",
            "topupPacks": ["0.01", "10.00", "20.00"],
            "customTopupMax": "100.00",
        }),
        http_response(200, {"transactions": [{"id": "tx1"}]}),
        http_response(200, {"out_trade_no": "WX1"}),
        http_response(400, {"error": "not paid"}),
        http_response(200, {"credited": True}),
    ])
    client = BalanceClient(buyer_id="buyer", http_client=http)
    balance = client.get_balance("https://provider.test/")
    assert balance.balance == "3.00"
    assert balance.topup_packs == ["0.01", "10.00", "20.00"]
    assert balance.custom_topup_max == "100.00"
    assert client.list_transactions("https://provider.test") == [{"id": "tx1"}]
    assert client.create_topup_order("https://provider.test", pack="2.00")["out_trade_no"] == "WX1"
    assert client.confirm_topup("https://provider.test", "WX1")["credited"] is False
    assert client.confirm_topup("https://provider.test", "WX1")["credited"] is True
    with pytest.raises(Exception, match="buyer_id"):
        BalanceClient(http_client=http)._buyer(None)


def test_balance_client_external_topup_and_payment_paths():
    http = FakeBalanceHttp([
        http_response(200, {"credited": True}),
        http_response(400, {"error": "duplicate"}),
        http_response(200, {"result": {"cached": True}}),
        http_response(402, {"accepts": []}),
        http_response(200, {"result": {"ok": True}, "transaction": "btx-1"}),
        http_response(402, {}),
        http_response(500, {"error": "failed"}),
    ])
    client = BalanceClient(buyer_id="buyer", http_client=http)
    assert client.topup_balance("https://provider.test", "2.00", "wechat", out_trade_no="WX1")["credited"]
    with pytest.raises(Exception, match="duplicate"):
        client.topup_balance("https://provider.test", "2.00", "wechat", out_trade_no="WX1")
    assert client.pay("https://provider.test", "svc", {}).success is True
    paid = client.pay("https://provider.test", "svc", {"x": 1}, amount=1.5)
    assert paid.success is True and paid.tx_hash == "btx-1"
    with pytest.raises(Exception, match="Balance payment failed"):
        client.pay("https://provider.test", "svc", {})


def test_balance_client_preserves_structured_insufficient_balance_details():
    http = FakeBalanceHttp([
        http_response(402, {"accepts": []}),
        http_response(402, {
            "code": "insufficient_balance",
            "error": "insufficient_balance",
            "details": {
                "required": "25.00", "balance": "5.00", "currency": "CNY",
                "topupPacks": ["10.00", "20.00", "50.00", "100.00"],
                "customTopupMax": "100.00",
            },
        }),
    ])
    client = BalanceClient(buyer_id="feishu-user", http_client=http)

    with pytest.raises(InsufficientBalance) as captured:
        client.pay("https://provider.test", "ping", {})

    assert captured.value.code == "INSUFFICIENT_BALANCE"
    assert captured.value.details["required"] == "25.00"
    assert captured.value.details["topupPacks"] == ["10.00", "20.00", "50.00", "100.00"]


def test_balance_client_uses_caller_request_id_in_payment_payload():
    http = FakeBalanceHttp([
        http_response(402, {"accepts": []}),
        http_response(200, {"result": {"ok": True}, "transaction": "btx-1"}),
    ])
    client = BalanceClient(buyer_id="feishu-user", http_client=http)

    assert client.pay(
        "https://provider.test", "ping", {}, request_id="feishu-message-123"
    ).success

    payment = http.calls[1][2]["headers"]["X-Payment"]
    decoded = json.loads(base64.b64decode(payment).decode())
    assert decoded["payload"]["request_id"] == "feishu-message-123"
