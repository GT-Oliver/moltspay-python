"""MCP adapter for MoltsPay wallet, balance and fiat operations.

Business logic intentionally stays in :class:`MoltsPay`; this module only
validates MCP-shaped input, applies the confirmation policy and serializes
results.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from ..client import MoltsPay
from ..exceptions import (
    AlipayCliNotFound, AlipayProtocolError, AlipayPaymentRejected,
    AlipayPaymentTimeout, PaymentError, WalletError,
)


_AMOUNT = re.compile(r"^(?:0|[1-9]\d*)(?:\.\d{1,2})?$")
_READ_RETRY_STATUSES = {408, 429, 500, 502, 503, 504}


def _camel(value: Any) -> Any:
    if isinstance(value, dict):
        return {_camel_key(str(k)): _camel(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_camel(item) for item in value]
    return value


def _camel_key(key: str) -> str:
    parts = key.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    return _camel(value)


def _required(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _server(value: str) -> str:
    value = _required(value, "serverUrl")
    if not re.match(r"^https?://[^\s]+$", value):
        raise ValueError("serverUrl must be an http(s) URL")
    return value.rstrip("/")


def _pack(value: Any) -> str:
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value) or float(value) <= 0:
        raise ValueError("pack must be a positive decimal string with at most 2 fractional digits")
    return value


def _error_code(exc: Exception) -> str:
    if isinstance(exc, (AlipayCliNotFound,)):
        return "dependency_missing"
    if isinstance(exc, AlipayProtocolError):
        return "wallet_not_ready"
    if isinstance(exc, AlipayPaymentTimeout):
        return "payment_expired"
    if isinstance(exc, AlipayPaymentRejected):
        return "payment_rejected"
    message = str(exc)
    lower = message.lower()
    if "buyer_id" in lower or "buyer id" in lower:
        return "buyer_id_required"
    if "not found" in lower:
        return "not_found"
    if "insufficient" in lower:
        return "insufficient_balance"
    if "confirmation" in lower:
        return "confirmation_required"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_input"
    if isinstance(exc, WalletError):
        return "wallet_not_found"
    return "provider_unavailable" if isinstance(exc, (PaymentError, OSError)) else "invalid_input"


def create_mcp_server(dry_run: bool = False, config_dir: Optional[str] = None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("MCP support requires: pip install moltspay[mcp]") from exc

    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".moltspay"
    wallet_path = root / "wallet.json"
    if not wallet_path.exists():
        raise RuntimeError("MoltsPay wallet not found. Run `moltspay init` before starting the MCP server.")
    client = MoltsPay(wallet_path=str(wallet_path), config_dir=str(root))
    server = FastMCP("moltspay")

    def call(fn, *, request_id: Optional[str] = None) -> Dict[str, Any]:
        rid = request_id or f"mcp-{uuid.uuid4()}"
        try:
            return {"ok": True, "data": _dump(fn()), "requestId": rid, "retried": 0}
        except Exception as exc:
            return {"ok": False, "error": {"code": _error_code(exc), "message": str(exc),
                    "retryable": False, "details": {}}, "requestId": rid, "retried": 0}

    def require_confirm(confirmed: bool, action: str) -> None:
        if os.environ.get("MOLTSPAY_MCP_REQUIRE_CONFIRM") == "1" and not confirmed:
            raise ValueError(f"Confirmation required for {action}; call again with confirmed=true")

    @server.tool(description="Return wallet and optional provider fiat balance.")
    def moltspay_status(serverUrl: Optional[str] = None, buyerId: Optional[str] = None) -> Dict[str, Any]:
        def run():
            cfg = client.get_config()
            data = {"address": client.address, "defaultChain": cfg["chain"],
                    "balances": client.get_all_balances(), "limits": cfg["limits"],
                    "buyerId": buyerId or cfg.get("buyerId"), "fiatBalance": None, "warnings": []}
            if serverUrl:
                try:
                    data["fiatBalance"] = client.get_buyer_balance(_server(serverUrl), buyerId)
                except Exception as exc:
                    data["warnings"].append({"code": _error_code(exc), "message": str(exc)})
            return data
        return call(run)

    @server.tool(description="Query a provider buyer balance.")
    def moltspay_balance_query(serverUrl: str, buyerId: Optional[str] = None) -> Dict[str, Any]:
        return call(lambda: client.get_buyer_balance(_server(serverUrl), buyerId))

    @server.tool(description="List provider buyer balance transactions.")
    def moltspay_balance_transactions(serverUrl: str, buyerId: Optional[str] = None,
                                      limit: int = 20, offset: int = 0) -> Dict[str, Any]:
        def run():
            if not isinstance(limit, int) or not 1 <= limit <= 100 or not isinstance(offset, int) or offset < 0:
                raise ValueError("limit must be 1..100 and offset must be >= 0")
            return {"transactions": client.list_balance_transactions(_server(serverUrl), buyerId, limit, offset),
                    "limit": limit, "offset": offset}
        return call(run)

    @server.tool(description="Set the local default buyer ID.")
    def moltspay_balance_set_buyer(buyerId: str) -> Dict[str, Any]:
        return call(lambda: {"buyerId": _required(buyerId, "buyerId"),
                             "config": client.update_config(buyer_id=_required(buyerId, "buyerId"))})

    @server.tool(description="Create a recoverable balance top-up order.")
    def moltspay_balance_topup_order(serverUrl: str, pack: Optional[str] = None,
                                     buyerId: Optional[str] = None, confirmed: bool = False) -> Dict[str, Any]:
        def run():
            if dry_run:
                return {"dryRun": True, "intent": {"serverUrl": _server(serverUrl), "pack": pack, "buyerId": buyerId}}
            require_confirm(confirmed, "balance top-up")
            result = client.create_balance_topup_order(_server(serverUrl), _pack(pack) if pack is not None else None, buyerId)
            session = client.get_balance_topup_session(result["outTradeNo"])
            return {**result, "status": "pending", "expiresAt": session.expires_at if session else None}
        return call(run)

    @server.tool(description="Idempotently confirm a balance top-up order.")
    def moltspay_balance_topup_confirm(outTradeNo: str, serverUrl: Optional[str] = None,
                                       confirmed: bool = False) -> Dict[str, Any]:
        def run():
            if dry_run:
                return {"dryRun": True, "intent": {"outTradeNo": _required(outTradeNo, "outTradeNo"), "serverUrl": serverUrl}}
            return client.confirm_balance_topup(_required(outTradeNo, "outTradeNo"), _server(serverUrl) if serverUrl else None)
        return call(run)

    @server.tool(description="Read a locally persisted balance top-up session.")
    def moltspay_balance_topup_status(outTradeNo: str) -> Dict[str, Any]:
        def run():
            session = client.get_balance_topup_session(_required(outTradeNo, "outTradeNo"))
            if session is None:
                raise ValueError("top-up session not found")
            return session
        return call(run)

    @server.tool(description="List locally persisted balance top-up sessions.")
    def moltspay_balance_topup_list(status: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
        def run():
            if status and status not in {"pending", "credited", "expired"}:
                raise ValueError("status must be pending, credited or expired")
            if not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError("limit must be 1..100")
            sessions = [s for s in client.list_balance_topup_sessions() if not status or s.status == status]
            return {"sessions": sessions[:limit], "limit": limit, "warnings": []}
        return call(run)

    @server.tool(description="Start a recoverable WeChat payment session.")
    def moltspay_wechat_start(serverUrl: str, service: str, params: Optional[Dict[str, Any]] = None,
                              confirmed: bool = False) -> Dict[str, Any]:
        def run():
            if dry_run:
                return {"dryRun": True, "intent": {"serverUrl": _server(serverUrl), "service": _required(service, "service"), "params": params or {}}}
            require_confirm(confirmed, "WeChat payment")
            return client.start_wechat_payment(_server(serverUrl), _required(service, "service"), params or {})
        return call(run)

    def wechat_action(action, identifier: str, confirmed: bool = False) -> Dict[str, Any]:
        def run():
            value = _required(identifier, "identifier")
            # Only fulfill has a financial side effect. Status and cancel are
            # safe read/local-state operations even when confirmation is on.
            if action == client.fulfill_wechat_payment and not dry_run:
                require_confirm(confirmed, "WeChat payment fulfillment")
            if dry_run:
                return {"dryRun": True, "intent": {"identifier": value}}
            return action(value)
        return call(run)

    @server.tool(description="Query a WeChat payment session.")
    def moltspay_wechat_status(identifier: str) -> Dict[str, Any]:
        return wechat_action(client.get_wechat_payment_status, identifier)

    @server.tool(description="Fulfill a WeChat payment session.")
    def moltspay_wechat_fulfill(identifier: str, confirmed: bool = False) -> Dict[str, Any]:
        return wechat_action(client.fulfill_wechat_payment, identifier, confirmed)

    @server.tool(description="Cancel a local WeChat payment session.")
    def moltspay_wechat_cancel(identifier: str, confirmed: bool = False) -> Dict[str, Any]:
        return wechat_action(client.cancel_wechat_payment, identifier, confirmed)

    @server.tool(description="List persisted WeChat payment sessions.")
    def moltspay_wechat_list(status: Optional[str] = None, limit: int = 100,
                             includeExpired: bool = True) -> Dict[str, Any]:
        def run():
            valid = {"pending", "paid", "completed", "expired", "cancelled", "failed"}
            if status and status not in valid:
                raise ValueError("invalid WeChat session status")
            if not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError("limit must be 1..100")
            items = client.list_wechat_payment_sessions()
            if status:
                items = [item for item in items if item.status == status]
            if not includeExpired:
                items = [item for item in items if item.status != "expired"]
            return {"sessions": items[:limit], "limit": limit}
        return call(run)

    @server.tool(description="Check the local Alipay wallet dependency.")
    def moltspay_alipay_check_wallet(executable: str = "alipay-bot") -> Dict[str, Any]:
        return call(lambda: (client.check_alipay_wallet(_required(executable, "executable")) or
                             {"ready": True, "executable": executable, "walletStatus": "opened_bound"}))

    @server.tool(description="Pay for a service through Alipay.")
    def moltspay_alipay_pay(serverUrl: str, service: str, params: Optional[Dict[str, Any]] = None,
                            framework: str = "openclaw", timeoutSeconds: float = 1800,
                            pollIntervalSeconds: float = 3, confirmed: bool = False) -> Dict[str, Any]:
        def run():
            if timeoutSeconds <= 0 or pollIntervalSeconds <= 0:
                raise ValueError("timeoutSeconds and pollIntervalSeconds must be positive")
            if dry_run:
                return {"dryRun": True, "intent": {"serverUrl": _server(serverUrl), "service": _required(service, "service"), "params": params or {}, "rail": "alipay"}}
            require_confirm(confirmed, "Alipay payment")
            result = client.pay(_server(serverUrl), _required(service, "service"), rail="alipay",
                                payment_params=params or {}, rail_options={"framework": framework,
                                "timeout": timeoutSeconds, "poll_interval": pollIntervalSeconds})
            data = _dump(result)
            payment = data.get("payment") or {}
            data.update({"tradeNo": payment.get("tradeNo"), "outTradeNo": payment.get("outTradeNo"),
                         "paymentUrl": payment.get("paymentUrl")})
            return data
        return call(run)

    @server.tool(description="Pay for and execute a MoltsPay provider service.")
    def moltspay_pay(url: str, service: str, params: Dict[str, Any], chain: Optional[str] = None,
                     token: str = "USDC", rail: Optional[str] = None, confirmed: bool = False) -> Dict[str, Any]:
        def run():
            intent = {"url": url, "service": service, "params": params, "chain": chain, "token": token, "rail": rail}
            if dry_run:
                return {"dryRun": True, "message": "No payment executed", "intent": intent}
            if rail in {"balance", "wechat", "alipay"}:
                require_confirm(confirmed, f"{rail} payment")
            return client.pay(url, service, token=token, chain=chain, rail=rail, payment_params=params)
        return call(run)

    @server.tool(description="Read or update MoltsPay spending limits.")
    def moltspay_config(maxPerTx: Optional[float] = None, maxPerDay: Optional[float] = None) -> Dict[str, Any]:
        return call(lambda: client.update_config(max_per_tx=maxPerTx, max_per_day=maxPerDay)
                    if maxPerTx is not None or maxPerDay is not None else client.get_config())

    return server
