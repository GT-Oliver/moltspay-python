from datetime import datetime, timedelta, timezone

from moltspay.server.server import MoltsPayServer


def _server_with_cached_order(order):
    server = object.__new__(MoltsPayServer)
    server._balance_topup_orders = {"buyer|0.01|": order}
    return server


def test_active_balance_topup_order_can_be_reused():
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    order = {"out_trade_no": "WX-active", "expires_at": expires_at}
    server = _server_with_cached_order(order)

    assert server._get_cached_balance_topup_order("buyer|0.01|") is order


def test_expired_balance_topup_order_is_evicted():
    expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    server = _server_with_cached_order({"out_trade_no": "WX-expired", "expires_at": expires_at})

    assert server._get_cached_balance_topup_order("buyer|0.01|") is None
    assert server._balance_topup_orders == {}


def test_legacy_balance_topup_order_without_expiry_is_evicted():
    server = _server_with_cached_order({"out_trade_no": "WX-legacy"})

    assert server._get_cached_balance_topup_order("buyer|0.01|") is None
    assert server._balance_topup_orders == {}
