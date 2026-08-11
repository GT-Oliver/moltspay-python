"""Strict, side-effect-aware MCP adapter over MoltsPay public APIs."""

from __future__ import annotations

import base64
import io
import os
import uuid
from typing import Annotated, Any, Callable, Dict, Literal, Optional

import httpx
from pydantic import Field

from ..client import MoltsPay
from ..exceptions import MoltsPayError, PaymentError


NonEmpty = Annotated[str, Field(min_length=1, max_length=2048)]
HttpUrl = Annotated[str, Field(pattern=r"^https?://[^\s]+$", max_length=2048)]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
PageLimit = Annotated[int, Field(ge=1, le=100)]
PageOffset = Annotated[int, Field(ge=0)]
PositiveSeconds = Annotated[float, Field(gt=0)]
NonNegativeAmount = Annotated[float, Field(ge=0)]


def _camel(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump()
    if isinstance(value, dict):
        return {_snake_to_camel(str(key)): _camel(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_camel(item) for item in value]
    return value


def _snake_to_camel(value: str) -> str:
    parts = value.split("_")
    return parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])


def _wechat_session(session: Any) -> Dict[str, Any]:
    return _camel({
        "payment_session_id": session.payment_session_id,
        "status": session.status,
        "code_url": session.code_url,
        "out_trade_no": session.out_trade_no,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
        "last_http_status": session.last_http_status,
        "last_error": session.last_error,
        "result_body": session.result_body,
    })


def _alipay_session(session: Any) -> Dict[str, Any]:
    return _camel({
        "payment_session_id": session.payment_session_id,
        "status": session.status,
        "trade_no": session.trade_no,
        "out_trade_no": session.out_trade_no,
        "payment_url": session.payment_url,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
        "result": session.result,
        "last_error": session.last_error,
    })


class MoltsPayMCP:
    """Thin adapter: validation, confirmation, serialization, and errors only."""

    def __init__(self, client: Optional[MoltsPay] = None):
        self.client = client or MoltsPay()
        self.require_confirm = os.getenv("MOLTSPAY_MCP_REQUIRE_CONFIRM", "").lower() in {"1", "true", "yes"}

    def _ok(self, data: Any, request_id: Optional[str]) -> Dict[str, Any]:
        return {"ok": True, "data": _camel(data), "requestId": request_id or str(uuid.uuid4()), "retried": 0}

    def _fail(self, exc: Exception, request_id: Optional[str]) -> Dict[str, Any]:
        if isinstance(exc, httpx.TimeoutException):
            code, retryable = "timeout", True
        elif isinstance(exc, MoltsPayError):
            code = str(getattr(exc, "code", "payment_error")).lower()
            retryable = "timeout" in code
        elif isinstance(exc, (ValueError, TypeError)):
            code, retryable = "invalid_request", False
        else:
            code, retryable = "internal_error", False
        return {
            "ok": False,
            "error": {"code": code, "message": str(exc), "retryable": retryable, "details": {}},
            "requestId": request_id or str(uuid.uuid4()),
            "retried": 0,
        }

    def _run(self, call: Callable[[], Any], request_id: Optional[str]) -> Dict[str, Any]:
        try:
            return self._ok(call(), request_id)
        except Exception as exc:
            return self._fail(exc, request_id)

    def _confirm(self, confirmed: bool) -> None:
        if self.require_confirm and not confirmed:
            raise PaymentError("Explicit confirmation is required: pass confirmed=true")

    def status(self, serverUrl: Optional[HttpUrl] = None, buyerId: Optional[str] = None, requestId: Optional[str] = None):
        def call():
            config = self.client.get_config()
            buyer = buyerId or config.get("buyerId")
            result = {
                "address": self.client.address,
                "defaultChain": config.get("chain"),
                "balances": self.client.get_all_balances(),
                "limits": config.get("limits", {}),
                "buyerId": buyer,
                "fiatBalance": None,
                "warnings": [],
            }
            if serverUrl:
                try:
                    result["fiatBalance"] = self.client.get_buyer_balance(serverUrl, buyer)
                except Exception as exc:
                    result["warnings"].append({"code": "fiat_balance_unavailable", "message": str(exc)})
            return result
        return self._run(call, requestId)

    def balance_query(self, serverUrl: HttpUrl, buyerId: Optional[str] = None, requestId: Optional[str] = None):
        return self._run(lambda: self.client.get_buyer_balance(serverUrl, buyerId), requestId)

    def balance_transactions(self, serverUrl: HttpUrl, buyerId: Optional[str] = None, limit: PageLimit = 20, offset: PageOffset = 0, requestId: Optional[str] = None):
        return self._run(lambda: {
            "transactions": self.client.list_balance_transactions(serverUrl, buyerId, limit, offset),
            "limit": limit, "offset": offset,
        }, requestId)

    def balance_set_buyer(self, buyerId: NonEmpty, requestId: Optional[str] = None):
        return self._run(lambda: {"buyerId": buyerId, "config": self.client.update_config(buyer_id=buyerId)}, requestId)

    def balance_topup_order(self, serverUrl: HttpUrl, pack: Optional[str] = None, buyerId: Optional[str] = None, confirmed: bool = False, dryRun: bool = False, requestId: Optional[str] = None):
        def call():
            if dryRun:
                return {"intent": "create_balance_topup_order", "serverUrl": serverUrl, "pack": pack, "buyerId": buyerId}
            self._confirm(confirmed)
            return self.client.create_balance_topup_order(serverUrl, pack, buyerId)
        return self._run(call, requestId)

    def balance_topup_confirm(self, outTradeNo: Identifier, serverUrl: Optional[HttpUrl] = None, confirmed: bool = False, requestId: Optional[str] = None):
        def call():
            self._confirm(confirmed)
            return self.client.confirm_balance_topup(outTradeNo, serverUrl)
        return self._run(call, requestId)

    def balance_topup_status(self, outTradeNo: Identifier, requestId: Optional[str] = None):
        def call():
            session = self.client.get_balance_topup_session(outTradeNo)
            if session is None:
                raise PaymentError(f"Balance top-up session not found: {outTradeNo}")
            return session
        return self._run(call, requestId)

    def balance_topup_list(self, status: Optional[Literal["pending", "credited", "expired"]] = None, limit: PageLimit = 100, requestId: Optional[str] = None):
        def call():
            sessions = self.client.list_balance_topup_sessions()
            return {"sessions": [item for item in sessions if status is None or item.status == status][:limit], "limit": limit}
        return self._run(call, requestId)

    @staticmethod
    def _qr(code_url: str) -> Dict[str, str]:
        import qrcode
        image = qrcode.make(code_url)
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return {"mimeType": "image/png", "data": base64.b64encode(stream.getvalue()).decode("ascii"), "alt": "Scan with WeChat to pay."}

    def wechat_start(self, serverUrl: HttpUrl, service: NonEmpty, params: Optional[Dict[str, Any]] = None, confirmed: bool = False, dryRun: bool = False, requestId: Optional[str] = None):
        def call():
            if dryRun:
                return {"intent": "start_wechat_payment", "serverUrl": serverUrl, "service": service, "params": params or {}}
            self._confirm(confirmed)
            session = self.client.start_wechat_payment(serverUrl, service, params or {})
            result = _wechat_session(session)
            result["qrCode"] = self._qr(session.code_url)
            return result
        return self._run(call, requestId)

    def wechat_status(self, identifier: Identifier, requestId: Optional[str] = None):
        return self._run(lambda: _wechat_session(self.client.get_wechat_payment_status(identifier)), requestId)

    def wechat_fulfill(self, identifier: Identifier, confirmed: bool = False, requestId: Optional[str] = None):
        def call():
            self._confirm(confirmed)
            return _wechat_session(self.client.fulfill_wechat_payment(identifier))
        return self._run(call, requestId)

    def wechat_cancel(self, identifier: Identifier, requestId: Optional[str] = None):
        return self._run(lambda: _wechat_session(self.client.cancel_wechat_payment(identifier)), requestId)

    def wechat_list(self, status: Optional[str] = None, limit: PageLimit = 100, includeExpired: bool = True, requestId: Optional[str] = None):
        def call():
            sessions = self.client.list_wechat_payment_sessions()
            sessions = [item for item in sessions if (status is None or item.status == status) and (includeExpired or item.status != "expired")]
            return {"sessions": [_wechat_session(item) for item in sessions[:limit]], "limit": limit}
        return self._run(call, requestId)

    def alipay_check_wallet(self, requestId: Optional[str] = None):
        return self._run(lambda: (self.client.check_alipay_wallet() or {"ready": True, "walletStatus": "opened_bound"}), requestId)

    def alipay_start(self, serverUrl: HttpUrl, service: NonEmpty, params: Optional[Dict[str, Any]] = None, framework: str = "openclaw", timeoutSeconds: PositiveSeconds = 1800, confirmed: bool = False, dryRun: bool = False, requestId: Optional[str] = None):
        def call():
            if dryRun:
                return {"intent": "start_alipay_payment", "serverUrl": serverUrl, "service": service, "params": params or {}}
            self._confirm(confirmed)
            return _alipay_session(self.client.start_alipay_payment(serverUrl, service, params or {}, framework, timeoutSeconds))
        return self._run(call, requestId)

    def alipay_status(self, identifier: Identifier, requestId: Optional[str] = None):
        return self._run(lambda: _alipay_session(self.client.get_alipay_payment_status(identifier)), requestId)

    def alipay_fulfill(self, identifier: Identifier, confirmed: bool = False, requestId: Optional[str] = None):
        def call():
            self._confirm(confirmed)
            return _alipay_session(self.client.fulfill_alipay_payment(identifier))
        return self._run(call, requestId)

    def alipay_list(self, limit: PageLimit = 100, requestId: Optional[str] = None):
        return self._run(lambda: {"sessions": [_alipay_session(item) for item in self.client.list_alipay_payment_sessions()[:limit]], "limit": limit}, requestId)

    def pay(self, url: HttpUrl, service: NonEmpty, params: Dict[str, Any], chain: Optional[str] = None, token: str = "USDC", rail: Optional[Literal["balance", "wechat", "alipay"]] = None, confirmed: bool = False, dryRun: bool = False, requestId: Optional[str] = None):
        def call():
            if dryRun:
                return {"intent": "pay", "url": url, "service": service, "params": params, "chain": chain, "token": token, "rail": rail}
            self._confirm(confirmed)
            if rail in {"wechat", "alipay"}:
                raise PaymentError(f"Interactive rail '{rail}' requires its start/status/fulfill tools")
            options = {"auto_topup": False} if rail == "balance" else None
            return self.client.pay(url, service, token=token, chain=chain, rail=rail, payment_params=params, rail_options=options)
        return self._run(call, requestId)

    def config(self, maxPerTx: Optional[NonNegativeAmount] = None, maxPerDay: Optional[NonNegativeAmount] = None, requestId: Optional[str] = None):
        return self._run(lambda: self.client.update_config(max_per_tx=maxPerTx, max_per_day=maxPerDay) if maxPerTx is not None or maxPerDay is not None else self.client.get_config(), requestId)


TOOL_DESCRIPTIONS = {
    "status": "Return wallet, on-chain balance, limits, and optional provider balance. Read-only.",
    "balance_query": "Query provider-side buyer balance. Read-only.",
    "balance_transactions": "List provider-side balance transactions. Read-only.",
    "balance_set_buyer": "Set the local default provider buyer identifier.",
    "balance_topup_order": "Create a recoverable balance top-up order; this creates an external payment order.",
    "balance_topup_confirm": "Confirm one existing top-up order idempotently; never creates another order.",
    "balance_topup_status": "Read one locally persisted balance top-up session.",
    "balance_topup_list": "List locally persisted balance top-up sessions.",
    "wechat_start": "Create a WeChat Native payment session and return its URL and PNG QR image.",
    "wechat_status": "Query WeChat order state without executing the paid service.",
    "wechat_fulfill": "Submit a paid WeChat credential and execute the provider service.",
    "wechat_cancel": "Cancel a local pending WeChat session; this does not refund a payment.",
    "wechat_list": "List locally persisted WeChat payment sessions.",
    "alipay_check_wallet": "Check the configured local Alipay wallet. Read-only.",
    "alipay_start": "Start an Alipay payment and return a recoverable session without polling.",
    "alipay_status": "Read a locally persisted Alipay session without invoking alipay-bot.",
    "alipay_fulfill": "Resume Alipay payment and provider fulfillment once; this may have side effects.",
    "alipay_list": "List locally persisted Alipay payment sessions.",
    "pay": "Execute a non-interactive on-chain or provider-balance payment; interactive rails use dedicated tools.",
    "config": "Read or update local spending limits; this does not make a payment.",
}


def create_mcp_server(client: Optional[MoltsPay] = None):
    try:
        from mcp.server.fastmcp import FastMCP, Image
    except ImportError as exc:
        raise RuntimeError("MCP support requires: pip install 'moltspay[mcp]'") from exc
    adapter = MoltsPayMCP(client)
    server = FastMCP("moltspay")
    for name, description in TOOL_DESCRIPTIONS.items():
        if name == "wechat_start":
            def wechat_start_tool(
                serverUrl: HttpUrl,
                service: NonEmpty,
                params: Optional[Dict[str, Any]] = None,
                confirmed: bool = False,
                dryRun: bool = False,
                requestId: Optional[str] = None,
            ):
                result = adapter.wechat_start(serverUrl, service, params, confirmed, dryRun, requestId)
                qr = result.get("data", {}).get("qrCode") if result.get("ok") else None
                if not qr:
                    return result
                return [result, Image(data=base64.b64decode(qr["data"]), format="png")]

            handler = wechat_start_tool
        else:
            handler = getattr(adapter, name)
        server.tool(name=f"moltspay_{name}", description=description)(handler)
    return server


def main() -> None:
    create_mcp_server().run()


__all__ = ["MoltsPayMCP", "create_mcp_server", "main"]
