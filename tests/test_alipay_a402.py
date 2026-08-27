"""Focused A402 protocol and persistence tests."""

import asyncio
import json
import sqlite3

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from moltspay.alipay import (
    AlipayBuyerClient,
    decode_a402_json,
    encode_a402_json,
    parse_alipay_cli_output,
)
from moltspay.client import MoltsPay
from moltspay.exceptions import (
    AlipayRequestContextInvalid,
    AlipayRequestContextMissing,
    AlipayWalletNotReady,
)
from moltspay.server.alipay_store import AlipayOrderStore
from moltspay.server.facilitators.alipay import AlipayFacilitator, normalize_cny_amount
from moltspay.server.server import MoltsPayServer
from moltspay.server.types import RegisteredSkill, ServiceConfig
from moltspay.x402 import _service_from_dict


def _keys():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return (
        private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode(),
        private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
    )


def _alipay_execution_handler(monkeypatch, *, target_alipay, target_input=None):
    calls = []
    server = object.__new__(MoltsPayServer)
    server.host = "127.0.0.1"
    server.port = 8402
    server.alipay_store = AlipayOrderStore(":memory:")

    class FakeAlipay:
        config = {}

        def __init__(self):
            self.order = None
            self.verify_calls = 0

        def parse_payment_proof(self, _payment_proof):
            return {"trade_no": "trade-cheap"}

        def verify_payment(self, proof):
            self.verify_calls += 1
            return {
                "code": "10000",
                "active": True,
                "amount": "0.01",
                "currency": "CNY",
                "trade_no": proof["trade_no"],
                "out_trade_no": self.order["out_trade_no"],
                "resource_id": self.order["resource_id"],
            }

        def create_payment_needed(self, **kwargs):
            return {
                "header": "payment-needed",
                "pay_before": "2099-01-01T00:00:00+00:00",
                **kwargs,
            }

        def confirm_fulfillment(self, _trade_no):
            return {"success": True}

    server.alipay = FakeAlipay()

    def cheap_handler(_params):
        calls.append("cheap")
        return {"service": "cheap"}

    def target_handler(_params):
        calls.append("target")
        return {"service": "target"}

    cheap_config = ServiceConfig(
        id="cheap", name="Cheap", price=0.01, function="cheap_handler",
        alipay={
            "service_id": "API_CHEAP",
            "price_cny": "0.01",
            "resource_id": "/execute?service=cheap",
        },
    )
    target_config = ServiceConfig(
        id="target", name="Target", price=100, function="target_handler",
        input=target_input or {}, alipay=target_alipay,
    )
    server.skills = {
        "cheap": RegisteredSkill(id="cheap", config=cheap_config, handler=cheap_handler),
        "target": RegisteredSkill(id="target", config=target_config, handler=target_handler),
    }
    captured = {}

    class FakeHTTPServer:
        def __init__(self, _address, handler):
            captured["handler"] = handler

        def serve_forever(self):
            return None

    monkeypatch.setattr("moltspay.server.server.HTTPServer", FakeHTTPServer)
    server.listen()

    handler = object.__new__(captured["handler"])
    handler.headers = {}
    responses = []
    handler._send_json = lambda status, data, headers=None: responses.append((status, data, headers))
    handler._send_alipay_402(server.skills["cheap"], "req-cheap")
    assert responses[-1][0] == 402
    server.alipay.order = server.alipay_store.get_by_request(
        "req-cheap", "service", "/execute?service=cheap",
    )
    assert server.alipay.order["skill_id"] == "cheap"
    responses.clear()
    return server, handler, calls, responses


def test_alipay_proof_cannot_be_redeemed_for_another_service(monkeypatch):
    server, handler, calls, responses = _alipay_execution_handler(
        monkeypatch,
        target_alipay={
            "service_id": "API_TARGET",
            "price_cny": "100.00",
            "resource_id": "/execute?service=target",
        },
    )

    handler._handle_execute(
        {"service": "target", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1][0] == 403
    assert responses[-1][1]["code"] == "alipay_service_mismatch"
    assert calls == []
    assert server.alipay_store.get(server.alipay.order["out_trade_no"])["status"] == "offered"

    handler._handle_execute(
        {"service": "cheap", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1][0] == 200
    assert responses[-1][1]["result"] == {"service": "cheap"}
    assert calls == ["cheap"]

    handler._handle_execute(
        {"service": "target", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1][0] == 403
    assert responses[-1][1]["code"] == "alipay_service_mismatch"
    assert calls == ["cheap"]


def test_alipay_skill_binding_does_not_rely_on_unique_alipay_fields(monkeypatch):
    server, handler, calls, responses = _alipay_execution_handler(
        monkeypatch,
        target_alipay={
            "service_id": "API_CHEAP",
            "price_cny": "0.01",
            "resource_id": "/execute?service=cheap",
        },
    )

    handler._handle_execute(
        {"service": "target", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1][0] == 403
    assert responses[-1][1]["code"] == "alipay_service_mismatch"
    assert calls == []
    assert server.alipay_store.get(server.alipay.order["out_trade_no"])["status"] == "offered"


def test_alipay_proof_cannot_select_service_without_alipay(monkeypatch):
    server, handler, calls, responses = _alipay_execution_handler(
        monkeypatch,
        target_alipay=None,
    )

    handler._handle_execute(
        {"service": "target", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1][0] == 400
    assert responses[-1][1]["code"] == "alipay_not_configured"
    assert server.alipay.verify_calls == 0
    assert calls == []
    assert server.alipay_store.get(server.alipay.order["out_trade_no"])["status"] == "offered"


def test_alipay_proof_does_not_bypass_required_service_params(monkeypatch):
    server, handler, calls, responses = _alipay_execution_handler(
        monkeypatch,
        target_alipay={
            "service_id": "API_TARGET",
            "price_cny": "100.00",
            "resource_id": "/execute?service=target",
        },
        target_input={"prompt": {"type": "string", "required": True}},
    )

    handler._handle_execute(
        {"service": "target", "params": {}}, None, "proof-cheap",
    )

    assert responses[-1] == (400, {"error": "Missing required param: prompt"}, None)
    assert server.alipay.verify_calls == 0
    assert calls == []
    assert server.alipay_store.get(server.alipay.order["out_trade_no"])["status"] == "offered"


def test_cny_and_payment_needed_are_canonical():
    assert normalize_cny_amount("1") == "1.00"
    for invalid in ("0", "1.234", "1e2", "NaN", "Infinity"):
        try:
            normalize_cny_amount(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(invalid)
    private, public = _keys()
    facilitator = AlipayFacilitator({
        "app_id": "app", "seller_id": "seller", "seller_name": "Provider",
        "private_key_pem": private, "alipay_public_key_pem": public,
        "allow_insecure_gateway": True,
    })
    bill = facilitator.create_payment_needed(
        out_trade_no="MPA1", amount="0.10", goods_name="AI service",
        resource_id="/execute?service=svc", service_id="svc",
    )
    decoded = decode_a402_json(bill["header"])
    assert decoded["protocol"]["amount"] == "0.10"
    assert decoded["protocol"]["service_id"] == "svc"
    assert decoded["protocol"]["seller_sign_type"] == "RSA2"


def test_order_store_claim_and_replay_are_idempotent():
    store = AlipayOrderStore(":memory:")
    order = store.create_order(
        request_id="req-1", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="",
        skill_id="svc", service_id="API_SVC",
    )
    claim = {"resource_id": order["resource_id"], "skill_id": "svc", "service_id": "API_SVC"}
    assert store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", **claim)["state"] == "claimed"
    store.complete(order["out_trade_no"], {"result": "ok"})
    replay = store.claim_execution(order["out_trade_no"], trade_no="trade-1", digest="hash-1", **claim)
    assert replay["state"] == "completed"
    assert replay["order"]["result"] == {"result": "ok"}
    rotated_proof = store.claim_execution(
        order["out_trade_no"], trade_no="trade-1", digest="hash-2",
        **claim,
    )
    assert rotated_proof["state"] == "completed"
    wrong_trade = store.claim_execution(
        order["out_trade_no"], trade_no="trade-2", digest="hash-2",
        **claim,
    )
    assert wrong_trade["state"] == "replay"
    assert wrong_trade["reason"] == "trade_mismatch"


def test_order_store_claim_enforces_skill_binding_before_consuming_order():
    store = AlipayOrderStore(":memory:")
    order = store.create_order(
        request_id="req-bound", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="",
        skill_id="svc", service_id="API_SVC",
    )

    rejected = store.claim_execution(
        order["out_trade_no"], trade_no="trade-bound", digest="hash-bound",
        resource_id=order["resource_id"], skill_id="other",
        service_id="API_SVC",
    )

    assert rejected["state"] == "skill_mismatch"
    assert store.get(order["out_trade_no"])["status"] == "offered"

    claimed = store.claim_execution(
        order["out_trade_no"], trade_no="trade-bound", digest="hash-bound",
        resource_id=order["resource_id"], skill_id="svc",
        service_id="API_SVC",
    )
    assert claimed["state"] == "claimed"


def test_order_store_migrates_legacy_schema_and_fails_closed(tmp_path):
    db_path = tmp_path / "legacy-alipay.sqlite"
    db = sqlite3.connect(db_path)
    db.executescript("""
        CREATE TABLE alipay_orders (
          out_trade_no TEXT PRIMARY KEY,
          request_id TEXT NOT NULL,
          kind TEXT NOT NULL CHECK(kind = 'service'),
          service_id TEXT,
          amount_fen INTEGER NOT NULL,
          currency TEXT NOT NULL DEFAULT 'CNY',
          resource_id TEXT NOT NULL,
          goods_name TEXT NOT NULL,
          pay_before TEXT NOT NULL,
          trade_no TEXT UNIQUE,
          proof_hash TEXT UNIQUE,
          status TEXT NOT NULL,
          result_json TEXT,
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          UNIQUE(request_id, kind, resource_id)
        );
        CREATE TABLE alipay_fulfillment_outbox (
          trade_no TEXT PRIMARY KEY,
          out_trade_no TEXT NOT NULL REFERENCES alipay_orders(out_trade_no),
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL,
          last_error_code TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        INSERT INTO alipay_orders (
          out_trade_no, request_id, kind, service_id, amount_fen, currency,
          resource_id, goods_name, pay_before, status, created_at, updated_at
        ) VALUES (
          'MPALEGACY', 'req-legacy', 'service', 'API_LEGACY', 1, 'CNY',
          '/execute?service=legacy', 'Legacy', '', 'offered', 'now', 'now'
        );
    """)
    db.close()

    store = AlipayOrderStore(str(db_path))

    assert store.get("MPALEGACY")["skill_id"] is None
    rejected = store.claim_execution(
        "MPALEGACY", trade_no="trade-legacy", digest="hash-legacy",
        resource_id="/execute?service=legacy", skill_id="legacy",
        service_id="API_LEGACY",
    )
    assert rejected["state"] == "skill_mismatch"
    assert store.get("MPALEGACY")["status"] == "offered"


def test_order_store_public_status_exposes_completion_without_proof_material():
    store = AlipayOrderStore(":memory:")
    order = store.create_order(
        request_id="req-public", kind="service", amount_fen=1,
        resource_id="/execute?service=ping", goods_name="Ping", pay_before="",
        skill_id="ping", service_id="API_PING",
    )
    store.claim_execution(
        order["out_trade_no"], trade_no="trade-public", digest="secret-proof-hash",
        resource_id=order["resource_id"], skill_id="ping", service_id="API_PING",
    )
    store.complete(order["out_trade_no"], {"ok": True})
    store.mark_outbox("trade-public", "confirmed")

    public = store.public_status(order["out_trade_no"])

    assert public == {
        "out_trade_no": order["out_trade_no"],
        "trade_no": "trade-public",
        "service": "ping",
        "service_id": "API_PING",
        "amount": "0.01",
        "currency": "CNY",
        "order_status": "completed",
        "payment_status": "paid",
        "fulfillment_status": "confirmed",
        "result": {"ok": True},
        "error_code": None,
        "created_at": public["created_at"],
        "updated_at": public["updated_at"],
        "completed_at": public["completed_at"],
    }
    assert "proof_hash" not in public
    assert "request_id" not in public


def test_order_store_rejects_service_change_for_same_idempotency_key():
    store = AlipayOrderStore(":memory:")
    store.create_order(
        request_id="req-1", kind="service", amount_fen=10,
        resource_id="/execute?service=svc", goods_name="AI", pay_before="",
        skill_id="svc", service_id="API_ONE",
    )
    try:
        store.create_order(
            request_id="req-1", kind="service", amount_fen=10,
            resource_id="/execute?service=svc", goods_name="AI", pay_before="",
            skill_id="svc", service_id="API_TWO",
        )
    except ValueError as exc:
        assert "idempotency key conflicts" in str(exc)
    else:
        raise AssertionError("service_id changes must not reuse an existing order")


def test_a402_order_store_rejects_balance_topup_orders():
    store = AlipayOrderStore(":memory:")

    with pytest.raises(ValueError, match="invalid Alipay order"):
        store.create_order(
            request_id="req-topup",
            kind="balance_topup",
            amount_fen=1000,
            resource_id="/balance/topup/alipay",
            goods_name="Balance top-up",
            pay_before="",
            skill_id="balance-topup",
            service_id="API_TOPUP",
        )


def test_facilitator_does_not_require_uncontracted_verified_service_id():
    private, public = _keys()
    facilitator = AlipayFacilitator({
        "app_id": "app", "seller_id": "seller", "seller_name": "Provider",
        "private_key_pem": private, "alipay_public_key_pem": public,
        "allow_insecure_gateway": True,
    })
    facilitator.verify_payment = lambda proof: {
        "code": "10000", "active": True, "amount": "0.10",
        "trade_no": proof["trade_no"], "out_trade_no": "MPA1",
        "service_id": "API_OTHER", "resource_id": "/execute?service=svc",
    }
    result = asyncio.run(facilitator.verify(
        {"payload": {"trade_no": "trade-1"}},
        {
            "amount": "0.10",
            "extra": {
                "out_trade_no": "MPA1", "service_id": "API_SVC",
                "resource_id": "/execute?service=svc",
            },
        },
    ))
    assert result.valid


def test_buyer_session_persists_and_resumes_without_proof(tmp_path):
    calls = []
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        calls.append(list(args))
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [
                "请使用支付宝完成支付",
                "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/"
                f"payment_{out_shake_no}.png",
            ]
        if args[0] == "402-query-payment-status":
            return [
                '{"status":"SUCCESS","tradeNo":"trade-1",'
                '"outTradeNo":"MPA1","resourceResponse":{"status":200,"body":{"ok":true}}}'
            ]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    session = client.start_402(
        "https://provider.test/execute", encode_a402_json({"protocol": {"out_trade_no": "MPA1"}}),
        intent_summary="Buy svc", request_id="req-1", request_body=json.dumps({"service": "svc"}),
        business_session_id="d52e3b71-d00e-4a51-bc16-169cba465bc9",
    )
    assert session.status == "pending"
    assert session.out_shake_no == out_shake_no
    assert session.out_trade_no == "MPA1"
    assert session.media_paths == [
        "/tmp/openclaw/alipay-bot-cli/qrcode/"
        f"payment_{out_shake_no}.png"
    ]
    completed = client.resume(session.payment_session_id)
    assert completed.status == "completed"
    query = next(call for call in calls if call[0] == "402-query-payment-status")
    assert query[query.index("--out-shake-no") + 1] == out_shake_no
    persisted = (tmp_path / "alipay-sessions" / f"{session.payment_session_id}.json").read_text()
    assert "Payment-Proof" not in persisted
    assert all("payment_proof" not in " ".join(call) for call in calls)
    assert all(
        "mpay_alipay_" not in call[call.index("--session-id") + 1]
        for call in calls if "--session-id" in call
    )


@pytest.mark.parametrize(
    ("provider_status", "normalized"),
    [
        ("INIT", "pending"),
        ("OPEN_LINK_CREATED", "pending"),
        ("TRADE_CREATED", "pending"),
        ("PAYING", "processing"),
        ("BIND", "processing"),
        ("PAID", "paid"),
        ("SUCCESS", "paid"),
        ("VALIDATED", "paid"),
        ("FAILED", "rejected"),
        ("EXPIRED", "expired"),
        ("CLOSED", "rejected"),
    ],
)
def test_cli_output_maps_official_a402_statuses_explicitly(provider_status, normalized):
    parsed = parse_alipay_cli_output([json.dumps({"status": provider_status})])

    assert parsed["provider_status"] == provider_status
    assert parsed["normalized_status"] == normalized


def test_cli_output_does_not_fuzzily_map_unofficial_status_words():
    parsed = parse_alipay_cli_output([
        '{"status":"payment_successful_someday","message":"not failed"}'
    ])

    assert parsed["normalized_status"] == "unknown"


def test_paid_is_not_completed_until_resource_delivery_succeeds(tmp_path):
    out_shake_no = "12345678908282123456789012345678"
    queries = iter([
        '{"status":"PAID","tradeNo":"trade-1","outTradeNo":"MPA1"}',
        '{"status":"SUCCESS","tradeNo":"trade-1","outTradeNo":"MPA1",'
        '"resourceResponse":{"statusCode":200,"body":{"result":"delivered"}},'
        '"fulfillmentStatus":"ACKED"}',
    ])
    query_count = 0

    def runner(args):
        nonlocal query_count
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            query_count += 1
            return [next(queries)]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA1"}}),
        request_id="req-paid", business_session_id="business-paid",
    )

    paid = client.resume(started.payment_session_id)
    assert paid.status == "paid"
    assert paid.provider_status == "PAID"
    completed = client.resume(started.payment_session_id)
    assert completed.status == "completed"
    assert completed.result == {"result": "delivered"}
    assert completed.resource_status_code == 200
    assert completed.fulfillment_status == "ACKED"

    replay = client.resume(started.payment_session_id)
    assert replay.status == "completed"
    assert query_count == 2


def test_paid_resource_failure_stays_recoverable_as_fulfilling(tmp_path):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            return [
                '{"status":"SUCCESS","tradeNo":"trade-1",'
                '"resourceResponse":{"status":503,"body":{"error":"unavailable"}}}'
            ]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA1"}}),
        request_id="req-fulfill", business_session_id="business-fulfill",
    )
    observed = client.resume(started.payment_session_id)

    assert observed.status == "fulfilling"
    assert observed.resource_status_code == 503
    assert observed.last_error_code == "alipay_fulfillment_incomplete"


def test_clean_cli_result_clears_stale_replay_diagnostics(tmp_path):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            return ['{"status":"VALIDATED","tradeNo":"trade-clean","outTradeNo":"MPA-CLEAN"}']
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-CLEAN"}}),
        request_id="req-clean", business_session_id="business-clean",
    )
    client._update(
        started, status="paid", provider_code="alipay_replay_detected",
        provider_message="stale replay",
    )

    observed = client.resume(started.payment_session_id)

    assert observed.status == "paid"
    assert observed.provider_status == "VALIDATED"
    assert observed.provider_code is None
    assert observed.provider_message is None


def test_provider_order_reconciliation_overrides_stale_local_replay(tmp_path):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-RECONCILE"}}),
        request_id="req-reconcile", business_session_id="business-reconcile",
    )
    stale = client._update(
        started, status="paid", provider_status="VALIDATED",
        provider_code="alipay_replay_detected", provider_message="stale replay",
        last_error_code="alipay_fulfillment_state_unknown", last_error="unknown",
    )

    reconciled = client.reconcile_order(stale.out_trade_no, {
        "outTradeNo": stale.out_trade_no,
        "tradeNo": "trade-reconciled",
        "orderStatus": "completed",
        "paymentStatus": "paid",
        "fulfillmentStatus": "confirmed",
        "result": {"ok": True},
    })

    assert reconciled.status == "completed"
    assert reconciled.result == {"ok": True}
    assert reconciled.fulfillment_status == "confirmed"
    assert reconciled.provider_code is None
    assert reconciled.provider_message is None
    assert reconciled.last_error_code is None
    assert reconciled.last_error is None
    assert reconciled.sync_source == "provider_order_api"
    assert reconciled.last_synced_at


def test_sdk_order_status_queries_provider_and_reconciles_local_session(tmp_path, monkeypatch):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        return ['{"ok":true}']

    buyer = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = buyer.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-SDK"}}),
        request_id="req-sdk", business_session_id="business-sdk",
    )
    sdk = object.__new__(MoltsPay)
    sdk._timeout = 5
    sdk._alipay_client = buyer
    calls = []

    class Response:
        status_code = 200
        is_success = True

        @staticmethod
        def json():
            return {
                "out_trade_no": "MPA-SDK", "trade_no": "trade-sdk",
                "order_status": "completed", "payment_status": "paid",
                "fulfillment_status": "confirmed", "result": {"ok": True},
            }

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("moltspay.client.httpx.get", fake_get)

    order = sdk.get_alipay_order_status("https://provider.test", "MPA-SDK")

    assert calls[0][0] == "https://provider.test/payments/alipay/MPA-SDK"
    assert order["authoritative"] is True
    assert order["reconciled"] is True
    assert order["payment_session_id"] == started.payment_session_id
    assert buyer.get_session(started.payment_session_id).status == "completed"


def test_local_session_status_and_list_are_read_only_and_filter_effective_expiry(tmp_path):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-READONLY"}}),
        request_id="req-readonly", business_session_id="business-readonly", timeout=-1,
    )
    path = tmp_path / "alipay-sessions" / f"{started.payment_session_id}.json"
    before = path.read_bytes()

    observed = client.get_session(started.payment_session_id)
    listed = client.list_sessions(status="expired")

    assert observed.status == "expired"
    assert [item.payment_session_id for item in listed] == [started.payment_session_id]
    assert path.read_bytes() == before


def test_resume_queries_provider_after_local_session_timeout(tmp_path):
    out_shake_no = "12345678908282123456789012345678"
    query_count = 0

    def runner(args):
        nonlocal query_count
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            query_count += 1
            return [
                '{"status":"SUCCESS","tradeNo":"trade-late","outTradeNo":"MPA-LATE",'
                '"resourceResponse":{"status":200,"body":{"result":"late-delivery"}}}'
            ]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-LATE"}}),
        request_id="req-late", business_session_id="business-late", timeout=-1,
    )
    locally_expired = client.get_session(started.payment_session_id)

    assert locally_expired.status == "expired"
    assert locally_expired.provider_status is None
    assert locally_expired.challenge_path
    assert (tmp_path / "alipay-challenges" / f"{started.payment_session_id}.needed").exists()

    recovered = client.resume(started.payment_session_id)

    assert recovered.status == "completed"
    assert recovered.trade_no == "trade-late"
    assert recovered.result == {"result": "late-delivery"}
    assert query_count == 1


def test_provider_confirmed_expired_session_is_terminal(tmp_path):
    out_shake_no = "12345678908282123456789012345678"
    query_count = 0

    def runner(args):
        nonlocal query_count
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            query_count += 1
            return ['{"status":"EXPIRED","outTradeNo":"MPA-EXPIRED"}']
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {"out_trade_no": "MPA-EXPIRED"}}),
        request_id="req-expired", business_session_id="business-expired",
    )

    expired = client.resume(started.payment_session_id)
    replay = client.resume(started.payment_session_id)

    assert expired.status == "expired"
    assert expired.provider_status == "EXPIRED"
    assert replay.status == "expired"
    assert query_count == 1


def test_session_persists_bill_metadata_and_sanitized_diagnostics(tmp_path):
    out_shake_no = "12345678908282123456789012345678"
    secret = "secret-wallet-token"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            return [json.dumps({
                "status": "PAYING", "code": "10000",
                "message": f"authorization=Bearer-{secret}",
            })]
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {
            "amount": "0.10", "currency": "CNY", "out_trade_no": "MPA1",
            "pay_before": "2026-08-18T12:00:00+00:00", "service_id": "API_SVC",
            "resource_id": "/execute?service=svc",
        }}),
        request_id="req-safe", business_session_id="business-safe",
        headers={
            "Content-Type": "application/json", "Authorization": "Bearer top-secret",
            "Payment-Proof": "proof-secret", "Cookie": "session=secret",
        },
    )
    observed = client.resume(started.payment_session_id)
    persisted = (
        tmp_path / "alipay-sessions" / f"{started.payment_session_id}.json"
    ).read_text(encoding="utf-8")

    assert observed.status == "processing"
    assert observed.out_trade_no == "MPA1"
    assert observed.pay_before == "2026-08-18T12:00:00+00:00"
    assert observed.service_id == "API_SVC"
    assert observed.resource_id == "/execute?service=svc"
    assert observed.provider_status == "PAYING"
    assert observed.provider_code == "10000"
    assert secret not in persisted
    assert "top-secret" not in persisted
    assert "proof-secret" not in persisted
    assert "session=secret" not in persisted
    assert "[REDACTED]" in persisted


def test_legacy_unknown_session_recovers_bill_metadata_from_needed_file(tmp_path):
    out_shake_no = "12345678908282123456789012345678"

    def runner(args):
        if args[0] == "check-wallet":
            return ['{"ready":true,"opened":true,"bound":true}']
        if args[0] == "402-buyer-pay":
            return [f"查询单号：{out_shake_no}"]
        if args[0] == "402-query-payment-status":
            return ['{"status":"TRADE_CREATED"}']
        return ['{"ok":true}']

    client = AlipayBuyerClient(config_dir=str(tmp_path), runner=runner)
    started = client.start_402(
        "https://provider.test/execute",
        encode_a402_json({"protocol": {
            "out_trade_no": "MPA-LEGACY", "pay_before": "2026-08-18T12:00:00+00:00",
            "service_id": "API_LEGACY", "resource_id": "/execute?service=legacy",
        }}),
        request_id="req-legacy", business_session_id="business-legacy",
    )
    session_path = tmp_path / "alipay-sessions" / f"{started.payment_session_id}.json"
    legacy = json.loads(session_path.read_text(encoding="utf-8"))
    for key in ("out_trade_no", "pay_before", "service_id", "resource_id"):
        legacy.pop(key, None)
    legacy["status"] = "unknown"
    session_path.write_text(json.dumps(legacy), encoding="utf-8")

    recovered = client.resume(started.payment_session_id)

    assert recovered.status == "pending"
    assert recovered.out_trade_no == "MPA-LEGACY"
    assert recovered.pay_before == "2026-08-18T12:00:00+00:00"
    assert recovered.service_id == "API_LEGACY"
    assert recovered.resource_id == "/execute?service=legacy"


@pytest.mark.parametrize("label", ["订单号", "查询单号"])
@pytest.mark.parametrize("family", ["8282", "8283"])
def test_cli_output_parses_labeled_recovery_number(label, family):
    out_shake_no = f"1234567890{family}123456789012345678"

    parsed = parse_alipay_cli_output([
        f"- {label}：{out_shake_no}",
        "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/payment_ignored.png",
    ])

    assert parsed["out_shake_no"] == out_shake_no


def test_cli_output_parses_recovery_number_from_official_current_media_path():
    out_shake_no = "12345678908283123456789012345678"
    parsed = parse_alipay_cli_output([
        "MEDIA: /tmp/openclaw/alipay-bot-cli/qrcode/"
        f"payment_{out_shake_no}.png",
    ])

    assert parsed["out_shake_no"] == out_shake_no


def test_cli_output_does_not_infer_recovery_number_from_arbitrary_media_path():
    parsed = parse_alipay_cli_output([
        "MEDIA: /tmp/untrusted/payment_12345678908282123456789012345678.png",
    ])

    assert "out_shake_no" not in parsed


def test_wallet_code_200_applied_unbound_is_not_ready():
    client = AlipayBuyerClient(
        runner=lambda _: ['{"code":200,"status":"applied_unbound","message":"已申请开通"}']
    )

    with pytest.raises(AlipayWalletNotReady) as error:
        client.check_wallet()

    assert error.value.details == {
        "status": "applied_unbound", "reason": "waiting_for_authorization",
    }


def test_wallet_requires_explicit_bound_signal():
    ambiguous = AlipayBuyerClient(runner=lambda _: ['{"code":200,"success":true}'])
    with pytest.raises(AlipayWalletNotReady):
        ambiguous.check_wallet()

    bound = AlipayBuyerClient(
        runner=lambda _: ['{"code":200,"message":"已开启支付宝支付功能"}']
    )
    assert bound.check_wallet()["bound"] is True


def test_wallet_parses_pretty_printed_json_with_diagnostic_lines():
    bound = AlipayBuyerClient(runner=lambda _: [
        "checking wallet...",
        "{",
        '  "code": 200,',
        '  "message": "已开启支付宝支付功能"',
        "}",
        "done",
    ])
    assert bound.check_wallet()["status"] == "bound"

    not_opened = AlipayBuyerClient(runner=lambda _: [
        "{",
        '  "code": 500,',
        '  "message": "未开通",',
        '  "reason": "当前尚未开启支付宝支付能力"',
        "}",
    ])
    with pytest.raises(AlipayWalletNotReady) as error:
        not_opened.check_wallet()
    assert error.value.details == {"status": "not_opened"}


def test_business_session_uses_runtime_context_and_rejects_local_session(monkeypatch):
    monkeypatch.setenv("AIPAY_SESSION_ID", "d52e3b71-d00e-4a51-bc16-169cba465bc9")
    monkeypatch.setenv("AIPAY_FRAMEWORK", "openclaw")
    client = AlipayBuyerClient(framework="moltspay")

    assert client._business_session_id(None) == "d52e3b71-d00e-4a51-bc16-169cba465bc9"
    assert client.framework == "openclaw"
    with pytest.raises(AlipayRequestContextInvalid):
        client._business_session_id("mpay_alipay_deadbeef")


def test_missing_business_session_fails_before_provider_request(tmp_path, monkeypatch):
    monkeypatch.delenv("AIPAY_SESSION_ID", raising=False)
    called = False

    def unexpected_post(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("provider request must not run")

    monkeypatch.setattr("moltspay.client.httpx.post", unexpected_post)
    client = MoltsPay(
        config_dir=str(tmp_path), wallet_path=str(tmp_path / "wallet.json"),
        solana_wallet_path=str(tmp_path / "wallet-solana.json"),
    )

    with pytest.raises(AlipayRequestContextMissing):
        client.start_alipay_payment("https://provider.test", "pong")

    assert called is False


def test_service_discovery_and_alipay_pay_use_cny_rail_quote(tmp_path):
    service = _service_from_dict({
        "id": "pong", "name": "Pong", "price": 1.0, "currency": "USDC",
        "paymentRails": {"alipay": {"amount": "1.00", "currency": "CNY"}},
    })
    assert service.payment_rails["alipay"] == {"amount": "1.00", "currency": "CNY"}

    client = MoltsPay(
        config_dir=str(tmp_path), wallet_path=str(tmp_path / "wallet.json"),
        solana_wallet_path=str(tmp_path / "wallet-solana.json"),
    )
    client.discover = lambda _: [service]
    observed = {}

    def pay_alipay(url, service_id, params, amount, currency, **options):
        observed.update(amount=amount, currency=currency, options=options)
        return "paid"

    client._pay_alipay = pay_alipay
    result = client.pay(
        "https://provider.test", "pong", rail="alipay",
        rail_options={"business_session_id": "d52e3b71-d00e-4a51-bc16-169cba465bc9"},
    )

    assert result == "paid"
    assert observed == {
        "amount": 1.0,
        "currency": "CNY",
        "options": {"business_session_id": "d52e3b71-d00e-4a51-bc16-169cba465bc9"},
    }
