"""Recoverable fiat client tests without live gateways."""

import json

import httpx
import pytest

from moltspay.alipay import AlipayClient, parse_payment_url, parse_status, parse_trade_no
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


def test_wechat_session_persists_and_completes(tmp_path):
    fake = FakeHttp([
        response(200, {"status": "pending"}),
        response(200, {"status": "paid"}),
        response(200, {"result": {"ok": True}}),
    ])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "https://provider/execute",
        {"scheme": "wechatpay-native", "network": "wechat", "extra": {"code_url": "weixin://pay", "out_trade_no": "WX1"}},
        data=json.dumps({"service": "demo", "params": {}}),
    )
    assert client.status(session.payment_session_id).status == "pending"
    paid = client.status("WX1")
    assert paid.status == "paid"
    assert fake.calls[0][0][0] == "GET"
    assert "/payments/wechat/WX1" in fake.calls[0][0][1]
    completed = client.fulfill("WX1")
    assert completed.status == "completed"
    assert fake.calls[-1][0][0] == "POST"
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


def test_alipay_cli_output_parsers():
    assert parse_trade_no(['{"tradeNo":"20260805001234567890123456789012"}']) == "20260805001234567890123456789012"
    assert parse_trade_no(['{"tradeNo":"20260805001"}']) is None
    assert parse_payment_url(["paymentUrl=https://example.com/pay/1"]) == "https://example.com/pay/1"
    assert parse_payment_url(["url=alipays://platformapi/startapp?appId=1)`"]) == "alipays://platformapi/startapp?appId=1"
    assert parse_status(['{"success":false,"errorCode":"TRADE_STATUS_UNPAID"}']) == "pending"
    assert parse_status(['{"success":true}']) == "paid"
    assert parse_status(["TRADE_CLOSED"]) == "rejected"
    assert parse_status(['{"body":"resource response status 200"}']) == "paid"


def test_alipay_wallet_and_shape_c_result():
    client = AlipayClient(runner=lambda args: ['{"code":200}'])
    client.check_wallet()
    assert AlipayClient._extract_body([
        json.dumps({"body": "resource response: {\"result\": {\"url\": \"video.mp4\"}}"})
    ]) == {"url": "video.mp4"}


def test_alipay_start_persists_and_local_status_is_read_only(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        if args[0] == "payment-intent":
            return ["ok"]
        if args[0] == "check-wallet":
            return ['{"code":200}']
        if args[0] == "402-buyer-pay":
            return ['{"tradeNo":"20260805001234567890123456789012","paymentUrl":"https://pay.example/1"}']
        raise AssertionError(args)

    client = AlipayClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider/execute",
        {"amount": "1.00", "asset": "CNY", "extra": {"payment_needed_header": "challenge", "out_trade_no": "ORDER1"}},
        data='{"service":"demo"}',
    )
    call_count = len(calls)

    recovered = client.get_session(session.payment_session_id)

    assert recovered.status == "pending"
    assert recovered.trade_no == "20260805001234567890123456789012"
    assert recovered.payment_url == "https://pay.example/1"
    assert len(calls) == call_count


def test_alipay_resume_is_explicit_and_idempotent_after_completion(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        command = args[0]
        if command == "payment-intent":
            return ["ok"]
        if command == "check-wallet":
            return ['{"code":200}']
        if command == "402-buyer-pay":
            return ['{"tradeNo":"20260805001234567890123456789012"}']
        if command == "402-query-payment-status":
            return ['{"success":true,"resourceResponse":{"result":{"ok":true}}}']
        if command == "402-buyer-fulfillment-ack":
            return ["ok"]
        raise AssertionError(args)

    client = AlipayClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"payment_needed_header": "challenge", "out_trade_no": "ORDER2"}},
    )
    completed = client.resume(session.payment_session_id)
    calls_after_completion = len(calls)
    replayed = client.resume(session.payment_session_id)

    assert completed.status == "completed"
    assert completed.result == {"ok": True}
    assert replayed.status == "completed"
    assert len(calls) == calls_after_completion


@pytest.mark.parametrize(
    "query_output, expected",
    [
        (["TRADE_STATUS_UNPAID"], "pending"),
        (["unrecognized provider output"], "unknown"),
        (["TRADE_CLOSED"], "rejected"),
    ],
)
def test_alipay_resume_nonterminal_and_rejected_states(tmp_path, query_output, expected):
    trade_no = "2" * 32

    def runner(args):
        if args[0] == "payment-intent":
            return ["ok"]
        if args[0] == "check-wallet":
            return ['{"code":200}']
        if args[0] == "402-buyer-pay":
            return [json.dumps({"tradeNo": trade_no})]
        if args[0] == "402-query-payment-status":
            return query_output
        raise AssertionError(args)

    client = AlipayClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"payment_needed_header": "challenge", "out_trade_no": f"ORDER-{expected}"}},
        data="{}",
    )
    assert client.resume(session.payment_session_id).status == expected


def test_alipay_resume_network_ambiguity_expiration_and_corrupt_file(tmp_path, monkeypatch):
    calls = []

    def runner(args):
        calls.append(args)
        if args[0] == "payment-intent":
            return ["ok"]
        if args[0] == "check-wallet":
            return ["READY"]
        if args[0] == "402-buyer-pay":
            return [json.dumps({"tradeNo": "3" * 32, "paymentUrl": "https://pay.test"})]
        raise RuntimeError("network")

    pending = []
    client = AlipayClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider/execute",
        {"extra": {"payment_needed_header": "challenge", "out_trade_no": "../unsafe"}},
        timeout=60, on_payment_pending=pending.append,
    )
    assert pending[0]["trade_no"] == "3" * 32
    assert client.resume(session.payment_session_id).status == "unknown"
    (client.session_dir / "broken.json").write_text("{", encoding="utf-8")
    assert len(client.list_sessions()) == 1
    with pytest.raises(Exception, match="Invalid Alipay"):
        client.get_session("../wallet")
    with pytest.raises(Exception, match="not found"):
        client.get_session("missing")

    monkeypatch.setattr("moltspay.alipay.time.time", lambda: 9_999_999_999.0)
    assert client.get_session(session.payment_session_id).status == "expired"


def test_alipay_validation_cli_and_extract_body_branches(tmp_path, monkeypatch):
    with pytest.raises(Exception, match="payment_needed_header"):
        AlipayClient(config_dir=str(tmp_path), runner=lambda args: []).start_402("https://provider", {})
    with pytest.raises(Exception, match="not opened"):
        AlipayClient(runner=lambda args: ["closed"]).check_wallet()
    with pytest.raises(Exception, match="tradeNo"):
        AlipayClient(config_dir=str(tmp_path), runner=lambda args: ['{"code":200}']).start_402(
            "https://provider", {"extra": {"payment_needed_header": "challenge"}}
        )

    client = AlipayClient(executable="missing-alipay")
    monkeypatch.setattr("moltspay.alipay.shutil.which", lambda executable: None)
    with pytest.raises(Exception, match="not found"):
        client._run(["check-wallet"])
    assert client._extract_body(["plain text"]) == "plain text"
    assert client._extract_body(["[]"]) == []
    assert client._extract_body(['{"body":"no json here"}']) == "no json here"
    assert client._extract_body(['{"result":{"value":1}}']) == {"value": 1}


def test_alipay_blocking_wrapper_completes(tmp_path):
    trade_no = "4" * 32

    def runner(args):
        outputs = {
            "payment-intent": ["ok"],
            "check-wallet": ['{"code":200}'],
            "402-buyer-pay": [json.dumps({"tradeNo": trade_no})],
            "402-query-payment-status": ['{"success":true,"result":{"ok":true}}'],
            "402-buyer-fulfillment-ack": ["ok"],
        }
        return outputs[args[0]]

    result = AlipayClient(config_dir=str(tmp_path), runner=runner).pay_402(
        "https://provider/execute",
        {"extra": {"payment_needed_header": "challenge", "out_trade_no": "ORDER4"}},
        poll_interval=0.01,
    )
    assert result["body"] == {"ok": True}
