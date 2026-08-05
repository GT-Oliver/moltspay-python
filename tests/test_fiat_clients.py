"""Recoverable fiat client tests without live gateways."""

import json

import httpx
import pytest

from moltspay.alipay import parse_payment_url, parse_status, parse_trade_no
from moltspay.wechat import WechatClient


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)

    def request(self, *args, **kwargs):
        return self.responses.pop(0)


def response(status: int, body):
    return httpx.Response(status, json=body, request=httpx.Request("POST", "https://provider/execute"))


def test_wechat_session_persists_and_completes(tmp_path):
    fake = FakeHttp([response(402, {"error": "pending"}), response(200, {"result": {"ok": True}})])
    client = WechatClient(config_dir=str(tmp_path), http_client=fake)
    session = client.start_402(
        "https://provider/execute",
        {"scheme": "wechatpay-native", "network": "wechat", "extra": {"code_url": "weixin://pay", "out_trade_no": "WX1"}},
        data=json.dumps({"service": "demo", "params": {}}),
    )
    assert client.status(session.payment_session_id).status == "pending"
    completed = client.status("WX1")
    assert completed.status == "completed"
    assert json.loads(completed.result_body)["result"]["ok"] is True
    assert WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([])).list_sessions()[0].out_trade_no == "WX1"


def test_wechat_requires_native_order_fields(tmp_path):
    client = WechatClient(config_dir=str(tmp_path), http_client=FakeHttp([]))
    with pytest.raises(Exception, match="code_url"):
        client.start_402("https://provider/execute", {"extra": {}})


def test_alipay_cli_output_parsers():
    assert parse_trade_no(['{"tradeNo":"20260805001"}']) == "20260805001"
    assert parse_payment_url(["paymentUrl=https://example.com/pay/1"]) == "https://example.com/pay/1"
    assert parse_status(['{"success":false,"errorCode":"TRADE_STATUS_UNPAID"}']) == "pending"
    assert parse_status(['{"success":true}']) == "paid"
    assert parse_status(["TRADE_CLOSED"]) == "rejected"
