import asyncio
import base64
from types import SimpleNamespace

import pytest

from moltspay.mcp import MoltsPayMCP


class FakeClient:
    address = "0xabc"

    def __init__(self):
        self.calls = []

    def get_config(self):
        return {"chain": "base", "limits": {"maxPerTx": 10}, "buyerId": "buyer"}

    def get_all_balances(self):
        return {"base": {"USDC": 1.0}}

    def get_buyer_balance(self, server_url, buyer_id=None):
        self.calls.append(("balance", server_url, buyer_id))
        if "down" in server_url:
            raise RuntimeError("provider down")
        return {"buyer_id": buyer_id or "buyer", "balance": "1.00"}

    def list_balance_transactions(self, server_url, buyer_id=None, limit=20, offset=0):
        self.calls.append(("transactions", server_url, buyer_id, limit, offset))
        return [{"id": "btx-1"}]

    def update_config(self, **kwargs):
        return {"buyerId": kwargs.get("buyer_id")}

    def create_balance_topup_order(self, *args):
        self.calls.append(("topup", *args))
        return {"outTradeNo": "ORDER1", "status": "pending", "expiresAt": "2099-01-01T00:00:00Z"}

    def confirm_balance_topup(self, *args):
        self.calls.append(("topup_confirm", *args))
        return {"credited": True}

    def get_balance_topup_session(self, identifier):
        if identifier == "missing":
            return None
        return SimpleNamespace(out_trade_no=identifier, status="pending")

    def list_balance_topup_sessions(self):
        return [SimpleNamespace(out_trade_no="A", status="pending"), SimpleNamespace(out_trade_no="B", status="expired")]

    def start_wechat_payment(self, *args):
        self.calls.append(("wechat_start", *args))
        return SimpleNamespace(
            payment_session_id="mpay_sess_1", status="pending", code_url="weixin://pay/1",
            out_trade_no="WX1", created_at="now", updated_at="now", expires_at="later",
            last_http_status=None, last_error=None, result_body=None,
            requirement={"secret": "must-not-leak"}, data="sensitive params",
        )

    def get_wechat_payment_status(self, identifier):
        self.calls.append(("wechat_status", identifier))
        return self.start_wechat_payment()

    def fulfill_wechat_payment(self, identifier):
        session = self.start_wechat_payment()
        session.status = "completed"
        return session

    def cancel_wechat_payment(self, identifier):
        session = self.start_wechat_payment()
        session.status = "cancelled"
        return session

    def list_wechat_payment_sessions(self):
        pending = self.start_wechat_payment()
        expired = self.start_wechat_payment()
        expired.status = "expired"
        return [pending, expired]

    def start_alipay_payment(self, *args):
        self.calls.append(("alipay_start", *args))
        return SimpleNamespace(
            payment_session_id="mpay_1", status="pending", trade_no="1" * 32,
            out_trade_no="ORDER2", payment_url="https://pay.test/2", created_at="now",
            updated_at="now", expires_at="later", result=None, last_error=None,
            data="sensitive params", resource_url="https://provider/execute",
        )

    def check_alipay_wallet(self):
        self.calls.append(("alipay_wallet",))

    def get_alipay_payment_status(self, identifier):
        return self.start_alipay_payment()

    def fulfill_alipay_payment(self, identifier):
        session = self.start_alipay_payment()
        session.status = "completed"
        return session

    def list_alipay_payment_sessions(self):
        return [self.start_alipay_payment(), self.start_alipay_payment()]

    def pay(self, *args, **kwargs):
        self.calls.append(("pay", args, kwargs))
        return {"success": True}


def test_status_preserves_partial_result_when_provider_is_down():
    result = MoltsPayMCP(FakeClient()).status(serverUrl="https://down.test")

    assert result["ok"] is True
    assert result["data"]["fiatBalance"] is None
    assert result["data"]["warnings"][0]["code"] == "fiat_balance_unavailable"


def test_dry_run_is_side_effect_free_and_does_not_require_confirmation(monkeypatch):
    monkeypatch.setenv("MOLTSPAY_MCP_REQUIRE_CONFIRM", "1")
    client = FakeClient()

    result = MoltsPayMCP(client).balance_topup_order("https://provider.test", dryRun=True)

    assert result["ok"] is True
    assert result["data"]["intent"] == "create_balance_topup_order"
    assert client.calls == []


def test_money_operation_requires_confirmation_when_enabled(monkeypatch):
    monkeypatch.setenv("MOLTSPAY_MCP_REQUIRE_CONFIRM", "1")
    result = MoltsPayMCP(FakeClient()).balance_topup_order("https://provider.test")

    assert result["ok"] is False
    assert result["error"]["code"] == "payment_error"


def test_wechat_start_returns_png_without_serializing_private_session_fields():
    result = MoltsPayMCP(FakeClient()).wechat_start("https://provider.test", "svc", confirmed=True)

    assert result["ok"] is True
    assert base64.b64decode(result["data"]["qrCode"]["data"]).startswith(b"\x89PNG")
    assert "requirement" not in result["data"]
    assert "data" not in result["data"]


def test_interactive_rail_is_rejected_by_unified_pay():
    client = FakeClient()
    result = MoltsPayMCP(client).pay("https://provider.test", "svc", {}, rail="wechat", confirmed=True)

    assert result["ok"] is False
    assert not any(call[0] == "pay" for call in client.calls)


def test_fastmcp_registration_exposes_constrained_tools():
    pytest.importorskip("mcp")
    from moltspay.mcp import create_mcp_server

    server = create_mcp_server(FakeClient())
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    assert "moltspay_wechat_start" in by_name
    assert "moltspay_alipay_start" in by_name
    schema = by_name["moltspay_balance_transactions"].inputSchema
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["offset"]["minimum"] == 0

    content = asyncio.run(server.call_tool("moltspay_wechat_start", {
        "serverUrl": "https://provider.test", "service": "svc", "confirmed": True,
    }))
    assert any(getattr(item, "type", None) == "image" for item in content)


def test_adapter_exposes_all_read_and_lifecycle_paths(monkeypatch):
    monkeypatch.delenv("MOLTSPAY_MCP_REQUIRE_CONFIRM", raising=False)
    client = FakeClient()
    adapter = MoltsPayMCP(client)

    assert adapter.status(requestId="req")["requestId"] == "req"
    assert adapter.balance_query("https://provider.test")["ok"]
    assert adapter.balance_transactions("https://provider.test", limit=5)["data"]["limit"] == 5
    assert adapter.balance_set_buyer("new-buyer")["ok"]
    assert adapter.balance_topup_order("https://provider.test", confirmed=True)["ok"]
    assert adapter.balance_topup_confirm("ORDER1", confirmed=True)["data"]["credited"]
    assert adapter.balance_topup_status("ORDER1")["ok"]
    assert adapter.balance_topup_status("missing")["ok"] is False
    assert len(adapter.balance_topup_list(status="pending")["data"]["sessions"]) == 1

    assert adapter.wechat_start("https://provider.test", "svc", dryRun=True)["data"]["intent"] == "start_wechat_payment"
    assert adapter.wechat_status("WX1")["ok"]
    assert adapter.wechat_fulfill("WX1", confirmed=True)["data"]["status"] == "completed"
    assert adapter.wechat_cancel("WX1")["data"]["status"] == "cancelled"
    assert len(adapter.wechat_list(includeExpired=False)["data"]["sessions"]) == 1

    assert adapter.alipay_check_wallet()["data"]["ready"] is True
    assert adapter.alipay_start("https://provider.test", "svc", dryRun=True)["data"]["intent"] == "start_alipay_payment"
    assert adapter.alipay_start("https://provider.test", "svc", confirmed=True)["ok"]
    assert adapter.alipay_status("ORDER2")["ok"]
    assert adapter.alipay_fulfill("ORDER2", confirmed=True)["data"]["status"] == "completed"
    assert len(adapter.alipay_list(limit=1)["data"]["sessions"]) == 1

    assert adapter.pay("https://provider.test", "svc", {}, dryRun=True)["data"]["intent"] == "pay"
    assert adapter.pay("https://provider.test", "svc", {}, rail="balance", confirmed=True)["ok"]
    assert adapter.config()["data"]["chain"] == "base"
    assert adapter.config(maxPerTx=2)["ok"]


def test_adapter_error_classification():
    import httpx
    adapter = MoltsPayMCP(FakeClient())
    assert adapter._fail(httpx.ReadTimeout("slow"), None)["error"] == {
        "code": "timeout", "message": "slow", "retryable": True, "details": {},
    }
    assert adapter._fail(ValueError("bad"), None)["error"]["code"] == "invalid_request"
    assert adapter._fail(RuntimeError("boom"), None)["error"]["code"] == "internal_error"
