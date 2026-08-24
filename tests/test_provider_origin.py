"""Provider URL policy regression tests; all DNS and HTTP are mocked."""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from moltspay.provider_origin import ProviderOrigin, ProviderURLPolicyError
from moltspay.x402 import AsyncX402Client, X402Client


PUBLIC_IP = "93.184.216.34"


def resolver(host, port):
    return [PUBLIC_IP]


def response(status, *, payload=None, headers=None):
    result = Mock()
    result.status_code = status
    result.headers = headers or {}
    result.json = Mock(return_value=payload or {})
    result.text = json.dumps(payload or {})
    result.is_success = 200 <= status < 300
    return result


def test_http_and_https_origins_allow_public_dns():
    origin = ProviderOrigin.from_url("https://provider.test/a", resolver=resolver)
    assert origin.base_url == "https://provider.test/a"
    http_origin = ProviderOrigin.from_url("http://provider.test/a", resolver=resolver)
    assert http_origin.base_url == "http://provider.test/a"

    with pytest.raises(ProviderURLPolicyError):
        ProviderOrigin.from_url("https://127.0.0.1/a", resolver=resolver)
    with pytest.raises(ProviderURLPolicyError):
        ProviderOrigin.from_url("https://localhost/a", resolver=resolver)
    with pytest.raises(ProviderURLPolicyError):
        ProviderOrigin.from_url(
            "https://rebind.test/a",
            resolver=lambda host, port: ["127.0.0.1"],
        )
    http_loopback = ProviderOrigin.from_url(
        "http://127.0.0.1/a", resolver=lambda host, port: ["127.0.0.1"],
    )
    assert http_loopback.base_url == "http://127.0.0.1/a"


def test_http_loopback_does_not_require_an_exception():
    origin = ProviderOrigin.from_url(
        "http://127.0.0.1:8765/test",
        resolver=lambda host, port: ["127.0.0.1"],
    )
    assert origin.base_url == "http://127.0.0.1:8765/test"


def test_provider_transport_reaches_a_real_local_http_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"local-http-ok")

        def log_message(self, _format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = X402Client(timeout=2)
    try:
        url = f"http://127.0.0.1:{server.server_port}/health"
        result = client.request("GET", url)
        assert result.status_code == 200
        assert result.text == "local-http-ok"
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_mcp_public_schema_accepts_http_and_https():
    from pydantic import TypeAdapter, ValidationError
    from moltspay.mcp.server import HttpUrl

    assert TypeAdapter(HttpUrl).validate_python("https://provider.test/a")
    assert TypeAdapter(HttpUrl).validate_python("http://provider.test/a")
    with pytest.raises(ValidationError):
        TypeAdapter(HttpUrl).validate_python("ftp://provider.test/a")


def test_http_discovery_and_paid_retry_reuse_same_origin():
    services = response(200, payload={"services": [{"id": "svc", "price": 1, "currency": "USDC"}]})
    challenge = response(402, headers={"X-Payment-Required": json.dumps({"amount": "1", "currency": "USDC", "payTo": "0x1"})})
    completed = response(200, payload={"ok": True})
    with patch.object(httpx.Client, "get", return_value=services), patch.object(
        httpx.Client, "post", side_effect=[challenge, completed]
    ) as post:
        client = X402Client(resolver=resolver)
        origin = client.provider_origin("http://provider.test/a")
        assert [svc.id for svc in client.discover_services("http://provider.test/a", origin=origin)] == ["svc"]
        result = client.pay_and_call(
            "http://provider.test/a", "svc", {"prompt": "safe"},
            lambda spender, amount: {"spender": spender, "amount": amount},
            origin=origin,
        )
        assert result["ok"] is True
        assert post.call_count == 2
        assert all(call.args[0].startswith("http://provider.test/a") for call in post.call_args_list)


def test_sync_discovery_and_paid_retry_reuse_same_origin_and_payment_material_stays_put():
    services = response(200, payload={"services": [{"id": "svc", "price": 1, "currency": "USDC"}]})
    challenge = response(402, headers={"X-Payment-Required": json.dumps({"amount": "1", "currency": "USDC", "payTo": "0x1"})})
    completed = response(200, payload={"ok": True})
    with patch.object(httpx.Client, "get", return_value=services) as get, patch.object(
        httpx.Client, "post", side_effect=[challenge, completed]
    ) as post:
        client = X402Client(resolver=resolver)
        origin = client.provider_origin("https://provider.test/a")
        assert [svc.id for svc in client.discover_services("https://provider.test/a", origin=origin)] == ["svc"]
        result = client.pay_and_call(
            "https://provider.test/a", "svc", {"prompt": "safe"},
            lambda spender, amount: {"spender": spender, "amount": amount},
            origin=origin,
        )
        assert result["ok"] is True
        assert get.call_count == 1
        assert post.call_count == 2
        assert post.call_args_list[1].kwargs["headers"]["X-PAYMENT"]
        assert all(call.args[0].startswith("https://provider.test/a") for call in post.call_args_list)


@pytest.mark.asyncio
async def test_async_discovery_and_paid_retry_use_https_same_origin():
    services = response(200, payload={"services": [{"id": "svc", "price": 1}]})
    challenge = response(402, headers={"X-Payment-Required": json.dumps({"amount": "1", "currency": "USDC", "payTo": "0x1"})})
    completed = response(200, payload={"ok": True})
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=services)), patch.object(
        httpx.AsyncClient, "post", new=AsyncMock(side_effect=[challenge, completed])
    ):
        client = AsyncX402Client(resolver=resolver)
        origin = client.provider_origin("https://provider.test/a")
        assert (await client.discover_services("https://provider.test/a", origin=origin))[0].id == "svc"
        result = await client.pay_and_call(
            "https://provider.test/a", "svc", {},
            lambda spender, amount: {"spender": spender, "amount": amount},
            origin=origin,
        )
        assert result["ok"] is True


def test_dns_rebinding_to_private_address_is_rejected_before_http():
    addresses = iter(([PUBLIC_IP], ["10.0.0.7"]))

    def rebinding_resolver(host, port):
        return next(addresses)

    client = X402Client(resolver=rebinding_resolver)
    with patch.object(httpx.Client, "post") as post:
        origin = client.provider_origin("https://rebind.test/a")
        with pytest.raises(ProviderURLPolicyError):
            client.call_service("https://rebind.test/a", "svc", {}, origin=origin)
        post.assert_not_called()


@pytest.mark.parametrize(
    "location",
    ["http://provider.test/execute", "https://attacker.test/execute"],
)
def test_downgrade_and_cross_origin_redirects_are_not_followed(location):
    client = X402Client(resolver=resolver)
    origin = client.provider_origin("https://provider.test/a")
    redirect = response(302, headers={"location": location})
    with patch.object(httpx.Client, "post", return_value=redirect) as post:
        with pytest.raises(ProviderURLPolicyError):
            client.call_service("https://provider.test/a", "svc", {}, origin=origin)
        post.assert_called_once()


def test_http_same_origin_redirect_is_not_followed():
    client = X402Client(resolver=resolver)
    origin = client.provider_origin("http://provider.test/a")
    redirect = response(302, headers={"location": "http://provider.test/execute"})
    with patch.object(httpx.Client, "post", return_value=redirect) as post:
        with pytest.raises(ProviderURLPolicyError, match="redirects are not followed"):
            client.call_service("http://provider.test/a", "svc", {}, origin=origin)
        post.assert_called_once()
