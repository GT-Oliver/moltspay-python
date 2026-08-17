import asyncio
import base64
from types import SimpleNamespace

import pytest

from moltspay.exceptions import InsufficientBalance
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
        return {
            "outTradeNo": "ORDER1", "codeUrl": "weixin://pay/topup-1",
            "status": "pending", "expiresAt": "2099-01-01T00:00:00Z",
        }

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

    def start_alipay_payment(self, *args, **kwargs):
        self.calls.append(("alipay_start", args, kwargs))
        return SimpleNamespace(
            payment_session_id="mpay_alipay_1",
            business_session_id=kwargs["business_session_id"],
            amount="1.00", currency="CNY", status="pending",
            request_id=kwargs.get("request_id") or "req-1",
            resource_url="https://provider.test/execute", method="POST",
            out_shake_no=None, trade_no=None, out_trade_no="MPA1",
            created_at="now", updated_at="now", expires_at="later",
            last_error_code=None, last_error=None, result=None,
        )

    def check_alipay_wallet(self):
        return {"ready": True, "bound": True}

    def get_alipay_payment_status(self, identifier):
        return self.start_alipay_payment(
            "https://provider.test", "svc", business_session_id="runtime-session"
        )

    def resume_alipay_payment(self, identifier):
        session = self.get_alipay_payment_status(identifier)
        session.status = "completed"
        return session

    def list_alipay_payment_sessions(self, **kwargs):
        return [self.get_alipay_payment_status("mpay_alipay_1")]

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
    schema = by_name["moltspay_balance_transactions"].inputSchema
    assert schema["properties"]["limit"]["maximum"] == 100
    assert schema["properties"]["offset"]["minimum"] == 0

    result = asyncio.run(server.call_tool("moltspay_wechat_start", {
        "serverUrl": "https://provider.test", "service": "svc", "confirmed": True,
    }))
    assert any(getattr(item, "type", None) == "image" for item in result.content)
    assert result.structuredContent["ok"] is True


def test_alipay_start_forwards_real_business_session_and_returns_quote():
    client = FakeClient()
    result = MoltsPayMCP(client).alipay_start(
        "https://provider.test", "pong",
        "d52e3b71-d00e-4a51-bc16-169cba465bc9",
        confirmed=True,
    )

    assert result["ok"] is True
    assert result["data"]["businessSessionId"] == "d52e3b71-d00e-4a51-bc16-169cba465bc9"
    assert result["data"]["amount"] == "1.00"
    assert result["data"]["currency"] == "CNY"
    call = next(call for call in client.calls if call[0] == "alipay_start")
    assert call[2]["business_session_id"] == "d52e3b71-d00e-4a51-bc16-169cba465bc9"


def test_balance_topup_order_returns_qr_image_content():
    pytest.importorskip("mcp")
    from moltspay.mcp import create_mcp_server

    server = create_mcp_server(FakeClient())
    result = asyncio.run(server.call_tool("moltspay_balance_topup_order", {
        "serverUrl": "https://provider.test", "confirmed": True,
    }))

    image = next(item for item in result.content if getattr(item, "type", None) == "image")
    assert result.structuredContent["data"]["codeUrl"] == "weixin://pay/topup-1"
    qr = result.structuredContent["data"]["qrCode"]
    assert qr["mimeType"] == "image/png"
    assert base64.b64decode(qr["data"]).startswith(b"\x89PNG")
    assert image.data == qr["data"]


def test_fastmcp_registration_documents_optional_parameters_options_and_ranges():
    pytest.importorskip("mcp")
    from moltspay.mcp import create_mcp_server

    server = create_mcp_server(FakeClient())
    tools = asyncio.run(server.list_tools())
    by_name = {tool.name: tool for tool in tools}

    def has_description(schema):
        if isinstance(schema, list):
            return any(has_description(value) for value in schema)
        if not isinstance(schema, dict):
            return False
        if schema.get("description"):
            return True
        return any(has_description(value) for value in schema.values())

    for tool in tools:
        assert tool.description
        assert tool.outputSchema is not None
        assert tool.outputSchema["type"] == "object"
        assert {"ok", "requestId", "retried"}.issubset(tool.outputSchema["required"])
        assert {"data", "error"}.issubset(tool.outputSchema["properties"])
        for name, schema in tool.inputSchema.get("properties", {}).items():
            assert has_description(schema), f"{tool.name}.{name} has no schema description"

    pay = by_name["moltspay_pay"].inputSchema["properties"]
    assert pay["token"]["enum"] == ["USDC", "USDT"]
    assert pay["rail"]["anyOf"][0]["const"] == "balance"
    assert pay["chain"]["anyOf"][0]["enum"] == [
        "base", "polygon", "base_sepolia", "bnb", "bnb_testnet",
        "tempo_moderato", "solana", "solana_devnet",
    ]
    assert pay["confirmed"]["default"] is False
    assert pay["dryRun"]["default"] is False

    transactions = by_name["moltspay_balance_transactions"].inputSchema["properties"]
    assert transactions["limit"]["minimum"] == 1
    assert transactions["limit"]["maximum"] == 100
    assert transactions["offset"]["minimum"] == 0

    topup = by_name["moltspay_balance_topup_order"].inputSchema["properties"]
    assert topup["pack"]["anyOf"][0]["maxLength"] == 64
    assert "pattern" in topup["pack"]["anyOf"][0]

    wechat_list = by_name["moltspay_wechat_list"].inputSchema["properties"]
    assert wechat_list["status"]["anyOf"][0]["enum"] == [
        "pending", "paid", "completed", "expired", "cancelled", "failed", "unknown",
    ]

    alipay_start = by_name["moltspay_alipay_start"]
    assert "sessionId" in alipay_start.inputSchema["required"]
    assert "real current framework" in alipay_start.inputSchema["properties"]["sessionId"]["description"].lower()
    assert "mpay_alipay_*" in alipay_start.description


def test_unified_pay_rejects_configured_interactive_preference():
    client = FakeClient()
    client.get_config = lambda: {
        "chain": "base", "limits": {}, "buyerId": "buyer", "railPreference": ["wechat"],
    }

    result = MoltsPayMCP(client).pay(
        "https://provider.test", "svc", {}, confirmed=True,
    )

    assert result["ok"] is False
    assert "start/status/fulfill" in result["error"]["message"]
    assert not any(call[0] == "pay" for call in client.calls)


def test_balance_pay_forwards_caller_request_id_for_retry_idempotency():
    client = FakeClient()

    result = MoltsPayMCP(client).pay(
        "https://provider.test", "ping", {}, rail="balance",
        confirmed=True, requestId="feishu-message-123",
    )

    assert result["ok"] is True
    pay_call = next(call for call in client.calls if call[0] == "pay")
    assert pay_call[2]["rail_options"] == {
        "auto_topup": False,
        "request_id": "feishu-message-123",
    }


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

    assert adapter.alipay_check_wallet()["ok"]
    assert adapter.alipay_start(
        "https://provider.test", "svc", "runtime-session", dryRun=True,
    )["data"]["sessionId"] == "runtime-session"
    assert adapter.alipay_status("mpay_alipay_1")["ok"]
    assert adapter.alipay_resume("mpay_alipay_1", confirmed=True)["data"]["status"] == "completed"
    assert len(adapter.alipay_list()["data"]["sessions"]) == 1

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


def test_insufficient_balance_error_keeps_actionable_topup_details():
    error = InsufficientBalance(
        required="25.00", balance="0.00", topup_packs=["10.00", "20.00", "50.00", "100.00"]
    )

    result = MoltsPayMCP(FakeClient())._fail(error, "req-balance")

    assert result["error"] == {
        "code": "insufficient_balance",
        "message": "Insufficient provider balance: need 25.00 CNY, have 0.00",
        "retryable": False,
        "details": {
            "required": "25.00",
            "balance": "0.00",
            "currency": "CNY",
            "topupPacks": ["10.00", "20.00", "50.00", "100.00"],
        },
    }


def test_mcp_balance_purchase_can_repeat_topup_until_payment_succeeds():
    pytest.importorskip("mcp")
    from moltspay.mcp import create_mcp_server

    class FlowClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.balance = 0
            self.orders = {}

        def get_config(self):
            return {"chain": "base", "limits": {}, "buyerId": "feishu-user", "railPreference": []}

        def pay(self, *args, **kwargs):
            self.calls.append(("pay", args, kwargs))
            assert kwargs["rail_options"] == {"auto_topup": False}
            if self.balance < 25:
                raise InsufficientBalance(
                    required="25.00", balance=f"{self.balance:.2f}",
                    topup_packs=["10.00", "20.00", "50.00", "100.00"],
                )
            return {"success": True, "result": {"ok": True}}

        def create_balance_topup_order(self, server_url, pack=None, buyer_id=None):
            order = f"ORDER{len(self.orders) + 1}"
            self.orders[order] = int(float(pack))
            return {"outTradeNo": order, "codeUrl": f"weixin://pay/{order}", "pack": pack, "status": "pending"}

        def confirm_balance_topup(self, order, server_url=None):
            self.balance += self.orders[order]
            return {"credited": True, "balance": f"{self.balance:.2f}"}

    server = create_mcp_server(FlowClient())

    async def call(name, arguments):
        result = await server.call_tool(name, arguments)
        if isinstance(result, tuple):
            return result[1]
        return result.structuredContent

    async def flow():
        pay_args = {
            "url": "https://provider.test", "service": "ping", "params": {},
            "rail": "balance", "confirmed": True,
        }
        first = await call("moltspay_pay", pay_args)
        topup_10 = await call("moltspay_balance_topup_order", {
            "serverUrl": "https://provider.test", "pack": "10", "confirmed": True,
        })
        await call("moltspay_balance_topup_confirm", {
            "outTradeNo": topup_10["data"]["outTradeNo"], "confirmed": True,
        })
        second = await call("moltspay_pay", pay_args)
        topup_20 = await call("moltspay_balance_topup_order", {
            "serverUrl": "https://provider.test", "pack": "20", "confirmed": True,
        })
        await call("moltspay_balance_topup_confirm", {
            "outTradeNo": topup_20["data"]["outTradeNo"], "confirmed": True,
        })
        third = await call("moltspay_pay", pay_args)
        return first, second, third

    first, second, third = asyncio.run(flow())
    assert first["error"]["code"] == "insufficient_balance"
    assert second["error"]["details"]["balance"] == "10.00"
    assert third["ok"] is True
