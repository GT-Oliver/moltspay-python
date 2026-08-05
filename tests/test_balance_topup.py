from pathlib import Path

from moltspay.client import MoltsPay


class FakeBalanceClient:
    def __init__(self):
        self.buyer_id = "buyer-1"
        self.calls = []

    def create_topup_order(self, server_url, pack=None, context=None):
        self.calls.append(("order", server_url, pack, context))
        return {
            "code_url": "weixin://pay",
            "out_trade_no": "WX-test-1",
            "pack": pack or "2.00",
            "max_timeout_seconds": 300,
        }

    def confirm_topup(self, server_url, out_trade_no):
        self.calls.append(("confirm", server_url, out_trade_no))
        return {"credited": True, "balance": "2.00", "tx_id": "btx-1"}


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

    result = client.confirm_balance_topup("WX-test-1")
    assert result["credited"] is True
    assert client.get_balance_topup_session("WX-test-1").status == "credited"
    assert [call[0] for call in fake.calls] == ["order", "confirm"]
