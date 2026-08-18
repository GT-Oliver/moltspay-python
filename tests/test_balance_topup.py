from pathlib import Path

import pytest

from moltspay.client import MoltsPay
from moltspay.exceptions import InsufficientBalance, UnsupportedRail
from moltspay.models import PaymentResult, Service


class FakeBalanceClient:
    def __init__(self):
        self.buyer_id = "buyer-1"
        self.calls = []

    def create_topup_order(self, server_url, pack=None, context=None, buyer_id=None):
        self.calls.append(("order", server_url, pack, context, buyer_id))
        return {
            "code_url": "weixin://pay",
            "out_trade_no": "WX-test-1",
            "pack": pack or "2.00",
            "max_timeout_seconds": 300,
        }

    def confirm_topup(self, server_url, out_trade_no):
        self.calls.append(("confirm", server_url, out_trade_no))
        return {"credited": True, "balance": "2.00", "tx_id": "btx-1"}


class TimestampedBalanceClient(FakeBalanceClient):
    def create_topup_order(self, server_url, pack=None, context=None, buyer_id=None):
        result = super().create_topup_order(server_url, pack, context, buyer_id)
        return {
            **result,
            "created_at": "2026-08-13T02:42:16Z",
            "expires_at": "2026-08-13T02:47:16Z",
        }


def test_balance_topup_order_is_persisted_and_recovered(tmp_path: Path):
    client = MoltsPay(private_key="0x" + "11" * 32, config_dir=str(tmp_path))
    fake = FakeBalanceClient()
    client._balance_client = fake

    order = client.create_balance_topup_order("http://server.test", pack="2.00")

    assert order["outTradeNo"] == "WX-test-1"
    session = client.get_balance_topup_session("WX-test-1")
    assert session is not None
    assert session.server_url == "http://server.test"
    assert session.status == "pending"
    assert order["expiresAt"] == session.expires_at

    result = client.confirm_balance_topup("WX-test-1")
    assert result["credited"] is True
    assert client.get_balance_topup_session("WX-test-1").status == "credited"
    assert [call[0] for call in fake.calls] == ["order", "confirm"]


def test_balance_topup_explicit_buyer_is_forwarded(tmp_path: Path):
    client = MoltsPay(private_key="0x" + "11" * 32, config_dir=str(tmp_path), buyer_id="default-buyer")
    fake = FakeBalanceClient()
    client._balance_client = fake

    order = client.create_balance_topup_order("http://server.test", buyer_id="explicit-buyer")

    assert fake.calls[0][-1] == "explicit-buyer"
    assert order["buyerId"] == "explicit-buyer"
    assert client.get_balance_topup_session(order["outTradeNo"]).buyer_id == "explicit-buyer"


def test_balance_topup_uses_provider_order_timestamps(tmp_path: Path):
    client = MoltsPay(private_key="0x" + "11" * 32, config_dir=str(tmp_path))
    client._balance_client = TimestampedBalanceClient()

    order = client.create_balance_topup_order("http://server.test", pack="2.00")

    assert order["createdAt"] == "2026-08-13T02:42:16Z"
    assert order["expiresAt"] == "2026-08-13T02:47:16Z"


def test_alipay_is_not_available_for_balance_topups(tmp_path: Path):
    client = MoltsPay(
        private_key="0x" + "11" * 32,
        config_dir=str(tmp_path),
        buyer_id="buyer",
    )
    fake = FakeBalanceClient()
    client._balance_client = fake

    with pytest.raises(UnsupportedRail, match="only supported for A402 service purchases"):
        client.create_balance_topup_order(
            "https://provider.test", pack="10.00", rail="alipay"
        )
    with pytest.raises(UnsupportedRail, match="only supported for A402 service purchases"):
        client.topup_balance(
            "https://provider.test", "10.00", "alipay", out_trade_no="trade-1"
        )

    assert fake.calls == []


def test_balance_topup_rejects_unsafe_identifier(tmp_path: Path):
    client = MoltsPay(private_key="0x" + "11" * 32, config_dir=str(tmp_path), buyer_id="buyer")

    with pytest.raises(Exception, match="Invalid balance"):
        client.get_balance_topup_session("../wallet")


class RetryingBalanceClient(FakeBalanceClient):
    def __init__(self, required):
        super().__init__()
        self.required = required
        self.balance = 0
        self.pay_calls = 0
        self.orders = {}

    def pay(self, server_url, service_id, params, amount=0.0, buyer_id=None):
        self.pay_calls += 1
        if self.balance < self.required:
            raise InsufficientBalance(required=str(self.required), balance=str(self.balance))
        self.balance -= self.required
        return PaymentResult(
            success=True, amount=amount, token="BALANCE", service_id=service_id,
            result={"ok": True},
        )

    def create_topup_order(self, server_url, pack=None, context=None, buyer_id=None):
        result = super().create_topup_order(server_url, pack, context, buyer_id)
        result["out_trade_no"] = f"WX-test-{len(self.orders) + 1}"
        self.orders[result["out_trade_no"]] = int(float(result["pack"]))
        return result

    def confirm_topup(self, server_url, out_trade_no):
        self.calls.append(("confirm", server_url, out_trade_no))
        self.balance += self.orders[out_trade_no]
        return {"credited": True, "balance": str(self.balance), "tx_id": f"btx-{out_trade_no}"}


def _retrying_client(tmp_path, required):
    client = MoltsPay(
        private_key="0x" + "11" * 32, config_dir=str(tmp_path), buyer_id="feishu-user"
    )
    balance = RetryingBalanceClient(required)
    client._balance_client = balance
    client.discover = lambda _: [Service(id="ping", name="Ping", price=float(required), currency="USDC")]
    return client, balance


def test_balance_pay_repeats_topup_and_shows_each_qr_until_sufficient(tmp_path: Path):
    client, balance = _retrying_client(tmp_path, required=25)
    shown = []

    result = client.pay(
        "https://provider.test", "ping", rail="balance",
        rail_options={
            "topup_pack": "10", "topup_poll_interval": 0.01,
            "max_topup_attempts": 3,
            "on_topup_required": lambda pack, url: shown.append((pack, url)),
        },
    )

    assert result.success is True
    assert balance.pay_calls == 4
    assert [pack for pack, _ in shown] == ["10", "10", "10"]
    assert all(url.startswith("weixin://pay") for _, url in shown)


def test_balance_pay_stops_after_configured_topup_attempts(tmp_path: Path):
    client, balance = _retrying_client(tmp_path, required=100)

    with pytest.raises(InsufficientBalance):
        client.pay(
            "https://provider.test", "ping", rail="balance",
            rail_options={
                "topup_pack": "10", "topup_poll_interval": 0.01,
                "max_topup_attempts": 2,
            },
        )

    assert balance.pay_calls == 3
    assert balance.balance == 20
