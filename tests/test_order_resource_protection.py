"""Focused regression coverage for durable fiat-order resource protection."""

from datetime import datetime, timedelta, timezone

import pytest

from moltspay.server.alipay_store import AlipayOrderStore
from moltspay.server.order_protection import OrderCapacityError, OrderCreationLimiter, OrderProtectionError
from moltspay.server.server import MoltsPayServer
from moltspay.server.wechat_store import WechatOrderStore


def _future(seconds=300):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _past():
    return (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()


def test_no_key_identity_is_stable_per_caller_and_resource():
    first = MoltsPayServer._stable_order_request_id(None, "198.51.100.7", "wechat", "/execute?service=one")
    second = MoltsPayServer._stable_order_request_id(None, "198.51.100.7", "wechat", "/execute?service=one")
    other_caller = MoltsPayServer._stable_order_request_id(None, "198.51.100.8", "wechat", "/execute?service=one")
    other_resource = MoltsPayServer._stable_order_request_id(None, "198.51.100.7", "wechat", "/execute?service=two")
    assert first == second
    assert first != other_caller
    assert first != other_resource


def test_alipay_duplicate_idempotency_reuses_signed_challenge_and_capacity_is_durable(tmp_path):
    store = AlipayOrderStore(str(tmp_path / "alipay.sqlite"), max_outstanding_orders=1)
    first = store.create_order(
        request_id="same-key", caller_id="caller", kind="service", amount_fen=100,
        resource_id="/execute?service=one", goods_name="One", pay_before=_future(),
        skill_id="one", service_id="API_ONE",
    )
    store.set_challenge(first["out_trade_no"], "signed-challenge", first["pay_before"])
    second = store.create_order(
        request_id="same-key", caller_id="caller", kind="service", amount_fen=100,
        resource_id="/execute?service=one", goods_name="One", pay_before=_future(),
        skill_id="one", service_id="API_ONE",
    )
    assert second["out_trade_no"] == first["out_trade_no"]
    assert second["challenge_header"] == "signed-challenge"
    with pytest.raises(OrderCapacityError):
        store.create_order(
            request_id="different-key", caller_id="caller", kind="service", amount_fen=100,
            resource_id="/execute?service=two", goods_name="Two", pay_before=_future(),
            skill_id="two", service_id="API_TWO",
        )


def test_alipay_expired_unpaid_is_removed_but_completed_is_preserved():
    store = AlipayOrderStore(":memory:")
    completed = store.create_order(
        request_id="paid", kind="service", amount_fen=100, resource_id="/execute?service=two",
        goods_name="Two", pay_before=_future(), skill_id="two", service_id="API_TWO",
    )
    store.db.execute(
        "UPDATE alipay_orders SET status='completed',result_json=? WHERE out_trade_no=?",
        ('{"ok":true}', completed["out_trade_no"]),
    )
    expired = store.create_order(
        request_id="expired", kind="service", amount_fen=100, resource_id="/execute?service=one",
        goods_name="One", pay_before=_past(), skill_id="one", service_id="API_ONE",
    )
    assert store.cleanup_expired_unpaid() == 1
    assert store.get(expired["out_trade_no"]) is None
    assert store.get(completed["out_trade_no"])["status"] == "completed"


def test_wechat_duplicate_reservation_reuses_native_order_without_second_provider_call(tmp_path):
    store = WechatOrderStore(str(tmp_path / "wechat.sqlite"))
    kwargs = dict(
        request_id="same-key", caller_id="caller", skill_id="one", service_id="one",
        resource_id="/execute?service=one", amount_fen=100, currency="CNY",
        appid="app", mchid="merchant", pay_before=_future(),
    )
    first = store.reserve_order(**kwargs)
    store.finalize_order(first["out_trade_no"], "weixin://pay/one", first["pay_before"])
    second = store.reserve_order(**kwargs)
    assert second["out_trade_no"] == first["out_trade_no"]
    assert second["code_url"] == "weixin://pay/one"


def test_wechat_outstanding_limit_and_expiry_cleanup_do_not_delete_paid_order():
    store = WechatOrderStore(":memory:", max_outstanding_orders=1)
    expired = store.reserve_order(
        request_id="expired", caller_id="caller", skill_id="one", service_id="one",
        resource_id="/execute?service=one", amount_fen=100, currency="CNY",
        appid="app", mchid="merchant", pay_before=_past(),
    )
    store.cleanup_expired_unpaid()
    assert store.get(expired["out_trade_no"]) is None
    paid = store.reserve_order(
        request_id="paid", caller_id="caller", skill_id="one", service_id="one",
        resource_id="/execute?service=one", amount_fen=100, currency="CNY",
        appid="app", mchid="merchant", pay_before=_future(),
    )
    store.finalize_order(paid["out_trade_no"], "weixin://pay/paid", paid["pay_before"])
    store.db.execute("UPDATE wechat_orders SET status='completed' WHERE out_trade_no=?", (paid["out_trade_no"],))
    assert store.cleanup_expired_unpaid() == 0
    assert store.get(paid["out_trade_no"])["status"] == "completed"


def test_burst_is_bounded_by_global_and_caller_rate_and_concurrency_limits():
    limiter = OrderCreationLimiter({
        "global_rate_per_second": 1, "global_burst": 2,
        "caller_rate_per_second": 1, "caller_burst": 1,
        "max_concurrent_order_creations": 1,
    })
    with limiter.slot("caller"):
        with pytest.raises(OrderProtectionError):
            with limiter.slot("caller"):
                pass
    with pytest.raises(OrderProtectionError):
        with limiter.slot("caller"):
            pass
