import io
import json

from moltspay.server.facilitators.balance import BalanceFacilitator
from moltspay.server.facilitators.wechat import WechatFacilitator
from moltspay.server.server import MoltsPayServer
from moltspay.server.types import ChainConfig, ProviderConfig, ServiceConfig, ServicesManifest


class DiscoveryRegistry:
    def __init__(self, balance, wechat):
        self.facilitators = {"balance": balance, "wechat": wechat}

    def get(self, name):
        return self.facilitators.get(name)


def test_service_discovery_describes_configured_rails_and_supported_chains(tmp_path, monkeypatch):
    server = object.__new__(MoltsPayServer)
    server.chains = [
        ChainConfig(chain="base", network="eip155:8453", tokens=["USDC", "USDT"]),
        ChainConfig(chain="base_sepolia", network="eip155:84532", tokens=["USDT"]),
        ChainConfig(chain="balance", network="balance"),
        ChainConfig(chain="wechat", network="wechat"),
        ChainConfig(chain="alipay", network="alipay"),
    ]
    server.registry = DiscoveryRegistry(
        BalanceFacilitator(str(tmp_path / "balance.sqlite"), currency="CNY"),
        WechatFacilitator({
            "appid": "app", "mchid": "merchant", "serial_no": "serial",
            "notify_url": "https://provider.test/notify",
        }),
    )
    server.alipay = object()
    server.skills = {"ping": object()}
    service = ServiceConfig(
        id="ping",
        name="Ping",
        description="All payment methods",
        price=0.01,
        currency="USDC",
        acceptedCurrencies=["USDC"],
        function="ping",
        input={"message": {"type": "string", "required": True}},
        output={"result": "pong"},
        balance={"price": "0.01"},
        wechat={"price_cny": "0.1", "description": "Ping"},
        alipay={"price_cny": "0.2", "service_id": "API_PING", "pay_timeout_seconds": 600},
    )

    entry = server._service_discovery_entry(service)

    assert entry["chains"] == ["base"]
    assert entry["input"]["message"]["required"] is True
    assert entry["output"] == {"result": "pong"}
    assert entry["paymentRails"] == {
        "balance": {
            "available": True, "interactive": False, "protocol": "x402",
            "currency": "CNY", "amount": "0.01",
        },
        "wechat": {
            "available": True, "interactive": True, "protocol": "x402",
            "currency": "CNY", "amount": "0.10",
        },
        "alipay": {
            "available": True, "interactive": True, "protocol": "a402",
            "currency": "CNY", "amount": "0.20",
            "serviceId": "API_PING", "resourceId": "/execute?service=ping",
            "maxTimeoutSeconds": 600,
        },
    }

    provider = ProviderConfig(name="Provider", wallet="0x0000000000000000000000000000000000000001")
    server.provider = provider
    server.manifests = [ServicesManifest(provider=provider, services=[service])]
    server.host = "127.0.0.1"
    server.port = 8402
    captured = {}

    class FakeHTTPServer:
        def __init__(self, address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            return None

    monkeypatch.setattr("moltspay.server.server.HTTPServer", FakeHTTPServer)
    server.listen()

    def get_json(path):
        handler = object.__new__(captured["handler"])
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.send_response = lambda status: None
        handler.send_header = lambda name, value: None
        handler.end_headers = lambda: None
        handler.do_GET()
        return json.loads(handler.wfile.getvalue())

    services_payload = get_json("/services")
    well_known_payload = get_json("/.well-known/agent-services.json")
    assert services_payload["services"] == [entry]
    assert well_known_payload["services"] == [entry]


def test_service_discovery_omits_unconfigured_or_unavailable_rails(tmp_path):
    server = object.__new__(MoltsPayServer)
    server.chains = [ChainConfig(chain="polygon", network="eip155:137", tokens=["USDC"])]
    server.registry = DiscoveryRegistry(
        BalanceFacilitator(str(tmp_path / "balance.sqlite")),
        None,
    )
    server.alipay = None
    server.skills = {}
    service = ServiceConfig(
        id="ping", name="Ping", price=1, currency="USDT",
        acceptedCurrencies=["USDT"], function="ping",
        wechat={"price_cny": "1.00", "description": "Ping"},
        alipay={"price_cny": "1.00"},
    )

    entry = server._service_discovery_entry(service)

    assert entry["chains"] == []
    assert entry["paymentRails"] == {}
    assert entry["available"] is False
