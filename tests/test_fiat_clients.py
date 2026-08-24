"""Recoverable fiat client tests without live gateways."""

import json

import httpx
import pytest

from moltspay.wechat import WechatClient


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(status: int, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://provider/execute"))


def test_wechat_http_session_persists_queries_and_completes(tmp_path):
    fake = FakeHttp([
        response(200, {"status": "pending"}),
        response(200, {"status": "paid"}),
        response(200, {"result": {"ok": True}}),
    ])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "http://127.0.0.1:8402/execute",
        {"scheme": "wechatpay-native", "network": "wechat", "extra": {"code_url": "weixin://pay", "out_trade_no": "WX1"}},
        data=json.dumps({"service": "demo", "params": {}}),
    )
    assert client.status(session.payment_session_id).status == "pending"
    paid = client.status("WX1")
    assert paid.status == "paid"
    assert fake.calls[0][0][0] == "GET"
    assert fake.calls[0][0][1] == "http://127.0.0.1:8402/payments/wechat/WX1"
    completed = client.fulfill("WX1")
    assert completed.status == "completed"
    assert fake.calls[-1][0][0] == "POST"
    assert fake.calls[-1][0][1] == "http://127.0.0.1:8402/execute"
    assert "X-Payment" in fake.calls[-1][1]["headers"]
    assert json.loads(completed.result_body)["result"]["ok"] is True
    assert WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([])).list_sessions()[0].out_trade_no == "WX1"


def test_wechat_requires_native_order_fields(tmp_path):
    client = WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([]))
    with pytest.raises(Exception, match="code_url"):
        client.start_402("https://provider/execute", {"extra": {}})


def test_wechat_reads_node_session_files_for_all_session_commands(tmp_path):
    session = {
        "paymentSessionId": "mpay_sess_node",
        "status": "pending",
        "resourceUrl": "https://provider/execute",
        "method": "POST",
        "data": json.dumps({"service": "demo", "params": {}}),
        "requirement": {
            "scheme": "wechatpay-native",
            "network": "wechat",
            "extra": {"code_url": "weixin://pay", "out_trade_no": "WX-node"},
        },
        "codeUrl": "weixin://pay",
        "outTradeNo": "WX-node",
        "createdAt": "2026-08-05T00:00:00.000Z",
        "updatedAt": "2026-08-05T00:00:00.000Z",
        "expiresAt": "2099-01-01T00:00:00.000Z",
    }
    session_dir = tmp_path / "wechat-sessions"
    session_dir.mkdir()
    (session_dir / "mpay_sess_node.json").write_text(json.dumps(session), encoding="utf-8")

    assert WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([])).list_sessions()[0].out_trade_no == "WX-node"
    assert WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([response(200, {"status": "pending"})])).status("WX-node").status == "pending"
    with pytest.raises(Exception, match="must be paid"):
        WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([])).fulfill("mpay_sess_node")
    assert WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([])).cancel("mpay_sess_node").status == "cancelled"


def test_wechat_status_network_failure_is_unknown_and_does_not_fulfill(tmp_path):
    fake = FakeHttp([response(503, {"error": "gateway unavailable"})])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"code_url": "weixin://pay", "out_trade_no": "WX2"}},
    )

    observed = client.status(session.payment_session_id)

    assert observed.status == "unknown"
    assert len(fake.calls) == 1
    assert fake.calls[0][0][0] == "GET"


def test_wechat_rejects_unsafe_identifier_and_terminal_cancel(tmp_path):
    client = WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([response(200, {"status": "paid"})]))
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"code_url": "weixin://pay", "out_trade_no": "WX3"}},
    )
    assert client.status(session.payment_session_id).status == "paid"
    with pytest.raises(Exception, match="Cannot cancel"):
        client.cancel(session.payment_session_id)
    with pytest.raises(Exception, match="Invalid WeChat"):
        client.status("../wallet")


def test_wechat_status_and_fulfillment_failure_branches(tmp_path):
    fake = FakeHttp([
        RuntimeError("offline"),
        httpx.Response(200, text="not-json", request=httpx.Request("GET", "https://provider/status")),
        response(200, {"status": "paid"}),
        RuntimeError("ambiguous"),
    ])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"code_url": "weixin://pay", "out_trade_no": "WX4"}},
    )
    assert client.status(session.payment_session_id).status == "unknown"
    assert client.status(session.payment_session_id).status == "unknown"
    assert client.status(session.payment_session_id).status == "paid"
    assert client.fulfill(session.payment_session_id).status == "unknown"


@pytest.mark.parametrize("http_status, expected", [(402, "pending"), (500, "failed")])
def test_wechat_fulfillment_http_outcomes(tmp_path, http_status, expected):
    fake = FakeHttp([response(200, {"status": "paid"}), response(http_status, {"error": "no"})])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"code_url": "weixin://pay", "out_trade_no": f"WX{http_status}"}},
    )
    client.status(session.payment_session_id)
    assert client.fulfill(session.payment_session_id).status == expected


def test_wechat_expiration_custom_status_url_and_corrupt_file(tmp_path):
    now = [1000.0]
    fake = FakeHttp([])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake, now=lambda: now[0])
    callback = []
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"code_url": "weixin://pay", "out_trade_no": "WX5"}},
        context={"status_url": "https://status.test/WX5"}, timeout=1,
        on_payment_pending=callback.append,
    )
    assert callback[0]["out_trade_no"] == "WX5"
    assert client._status_url(session) == "https://status.test/WX5"
    (client.session_dir / "broken.json").write_text("{", encoding="utf-8")
    now[0] = 1002.0
    assert client.status(session.payment_session_id).status == "expired"
    assert len(client.list_sessions()) == 1
    with pytest.raises(Exception, match="not found"):
        client.status("missing")


def test_wechat_blocking_wrapper_and_owned_client_close(tmp_path, monkeypatch):
    fake = FakeHttp([response(200, {"status": "paid"}), response(200, {"result": "ok"})])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    result = client.pay_402(
        resource_url="https://provider/execute",
        requirement={"extra": {"code_url": "weixin://pay", "out_trade_no": "WX6"}},
        poll_interval=0.01,
    )
    assert result["body"]["result"] == "ok"

    owned = WechatClient(config_dir=str(tmp_path))
    monkeypatch.setattr(owned.http, "close", lambda: setattr(owned, "closed", True))
    owned.close()
    assert owned.closed is True
