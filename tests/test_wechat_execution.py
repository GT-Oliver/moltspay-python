"""Regression tests for WeChat service-order binding and single execution."""

import base64
import io
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from moltspay.server.facilitators.base import SettleResult
from moltspay.server.facilitators.wechat import WechatFacilitator
from moltspay.server.server import MoltsPayServer
from moltspay.server.types import ChainConfig, ProviderConfig, RegisteredSkill, ServiceConfig
from moltspay.server.wechat_store import WechatOrderStore


class FakeWechat(WechatFacilitator):
    def create_payment_requirements(self, price_cny, description, out_trade_no=None, expires_seconds=300, attach=None):
        trade_no = out_trade_no or "WX-order-1"
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_seconds)).isoformat().replace("+00:00", "Z")
        return {
            "scheme": "wechatpay-native", "network": "wechat", "asset": "CNY",
            "amount": price_cny, "payTo": self.config["mchid"],
            "maxTimeoutSeconds": expires_seconds,
            "extra": {"code_url": "weixin://pay/qr", "out_trade_no": trade_no, "expires_at": expires_at},
        }

    def query_order(self, trade_no):
        return {
            "appid": self.config["appid"], "mchid": self.config["mchid"],
            "out_trade_no": trade_no, "trade_state": "SUCCESS",
            "amount": {"payer_total": 100, "currency": "CNY"},
            "transaction_id": "WX-transaction",
        }


class FakeRegistry:
    def __init__(self, wechat):
        self.wechat = wechat

    def get(self, name):
        return self.wechat if name == "wechat" else None

    def get_bnb_spender_address(self):
        return None

    def get_solana_fee_payer(self):
        return None

    def get_tempo_facilitator(self):
        return None

    async def verify(self, payment, requirements):
        return await self.wechat.verify(payment, requirements)

    async def settle(self, payment, requirements):
        return SettleResult(success=True, transaction="WX-transaction", status="fulfilled")


def _server(tmp_path, alpha_handler, beta_handler=None):
    config = {
        "appid": "wx-app", "mchid": "wx-merchant", "serial_no": "serial",
        "private_key_pem": "unused", "notify_url": "https://provider.test/wechat/notify",
    }
    wechat = FakeWechat(config)
    alpha = ServiceConfig(
        id="alpha", name="Alpha", price=1, function="alpha", wechat={"price_cny": "1.00", "description": "Alpha"},
    )
    beta = ServiceConfig(
        id="beta", name="Beta", price=1, function="beta", wechat={"price_cny": "1.00", "description": "Beta"},
    )
    server = object.__new__(MoltsPayServer)
    server.registry = FakeRegistry(wechat)
    server.wechat_store = WechatOrderStore(str(tmp_path / "wechat.sqlite"))
    server.provider = ProviderConfig(name="Provider", wallet="0xprovider", chains=["wechat"], wechat=config)
    server.chains = [ChainConfig(chain="wechat", network="wechat")]
    server.supported_networks = ["wechat"]
    server.skills = {
        "alpha": RegisteredSkill(id="alpha", config=alpha, handler=alpha_handler),
        "beta": RegisteredSkill(id="beta", config=beta, handler=beta_handler or alpha_handler),
    }
    server.manifests = []
    server.host = "127.0.0.1"
    server.port = 0
    return server


def _handler_class(server, monkeypatch):
    captured = {}

    class FakeHTTPServer:
        def __init__(self, _address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            return None

    monkeypatch.setattr("moltspay.server.server.HTTPServer", FakeHTTPServer)
    server.listen()
    return captured["handler"]


def _new_handler(handler_class):
    handler = object.__new__(handler_class)
    handler.headers = {}
    return handler


def _get_payment_requirement(handler_class, server, service, monkeypatch):
    handler = _new_handler(handler_class)
    response_headers = {}
    handler.wfile = io.BytesIO()
    handler.send_response = lambda _status: None
    handler.send_header = lambda name, value: response_headers.__setitem__(name, value)
    handler.end_headers = lambda: None
    handler._handle_execute({"service": service, "params": {}}, None, None, f"request-{service}")
    assert response_headers.get("X-Payment-Required")
    return json.loads(base64.b64decode(response_headers["X-Payment-Required"]).decode())["accepts"][0]


def _execute(handler_class, service, payment):
    handler = _new_handler(handler_class)
    responses = []
    handler._send_json = lambda status, data, headers=None: responses.append((status, data, headers))
    header = base64.b64encode(json.dumps(payment).encode()).decode()
    handler._handle_execute(
        {"service": service, "params": {}}, header, None, None,
    )
    return responses[-1]


def _payment(requirement, resource_service="alpha"):
    return {
        "x402Version": 2,
        "accepted": requirement,
        "scheme": "wechatpay-native",
        "network": "wechat",
        "payload": {"out_trade_no": requirement["extra"]["out_trade_no"]},
        "resource": {"url": f"/execute?service={resource_service}"},
    }


def test_wechat_service_order_succeeds_once_and_replays_cached_result(tmp_path, monkeypatch):
    calls = []
    server = _server(tmp_path, lambda _params: calls.append("alpha") or {"value": "ok"})
    handler_class = _handler_class(server, monkeypatch)
    requirement = _get_payment_requirement(handler_class, server, "alpha", monkeypatch)
    payment = _payment(requirement)

    first_status, first_body, _ = _execute(handler_class, "alpha", payment)
    second_status, second_body, _ = _execute(handler_class, "alpha", payment)

    assert first_status == 200
    assert first_body["result"] == {"value": "ok"}
    assert second_status == 200
    assert second_body == {
        "success": True, "result": {"value": "ok"}, "replayed": True,
        "payment": {"status": "settled", "network": "wechat"},
    }
    assert calls == ["alpha"]
    order = server.wechat_store.get(requirement["extra"]["out_trade_no"])
    assert order["skill_id"] == "alpha"
    assert order["service_id"] == "alpha"
    assert order["resource_id"] == "/execute?service=alpha"
    assert order["amount_fen"] == 100
    assert order["currency"] == "CNY"
    assert order["appid"] == "wx-app"
    assert order["mchid"] == "wx-merchant"
    assert order["pay_before"]
    assert order["status"] == "completed"


def test_wechat_order_cannot_be_used_by_another_service(tmp_path, monkeypatch):
    calls = []
    server = _server(
        tmp_path,
        lambda _params: calls.append("alpha"),
        lambda _params: calls.append("beta"),
    )
    handler_class = _handler_class(server, monkeypatch)
    requirement = _get_payment_requirement(handler_class, server, "alpha", monkeypatch)
    response = _execute(handler_class, "beta", _payment(requirement, resource_service="beta"))

    assert response[0] == 403
    assert response[1]["code"] == "wechat_order_mismatch"
    assert response[1]["reason"] == "skill_mismatch"
    assert calls == []


def test_wechat_order_claim_is_atomic_under_concurrent_execute(tmp_path, monkeypatch):
    calls = 0
    calls_lock = threading.Lock()
    entered = threading.Event()
    release = threading.Event()

    def handler(_params):
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(2)
        return {"value": "once"}

    server = _server(tmp_path, handler)
    handler_class = _handler_class(server, monkeypatch)
    requirement = _get_payment_requirement(handler_class, server, "alpha", monkeypatch)
    payment = _payment(requirement)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(_execute, handler_class, "alpha", payment)
        assert entered.wait(2)
        second = executor.submit(_execute, handler_class, "alpha", payment)
        second_response = second.result(timeout=2)
        release.set()
        first_response = first.result(timeout=2)

    assert first_response[0] == 200
    assert second_response[0] == 409
    assert second_response[1]["code"] == "wechat_execution_in_progress"
    assert calls == 1
