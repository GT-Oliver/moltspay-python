"""Strict, side-effect-aware MCP adapter over MoltsPay public APIs."""

from __future__ import annotations

import base64
import io
import json
import os
import uuid
from pathlib import Path
from typing import Annotated, Any, Callable, Dict, Literal, Optional, Union

import httpx
from pydantic import BaseModel, Field

from ..client import MoltsPay
from ..exceptions import MoltsPayError, PaymentError, InteractiveRailRequiresLifecycle


ServiceId = Annotated[
    str,
    Field(min_length=1, max_length=2048, description="Provider service ID; 1-2048 characters."),
]
HttpUrl = Annotated[
    str,
    Field(
        pattern=r"^https?://[^\s]+$",
        max_length=2048,
        description="Provider base URL using http or https; maximum 2048 characters.",
    ),
]
BuyerId = Annotated[
    str,
    Field(min_length=1, max_length=2048, description="Provider buyer identifier; 1-2048 characters."),
]
Identifier = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        description=(
            "A local payment session ID or provider order/trade number, as accepted by the tool; "
            "1-128 characters using letters, digits, dot, underscore, or hyphen."
        ),
    ),
]
BusinessSessionId = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        description=(
            "Real current framework conversation/session ID passed to alipay-bot; usually a UUID. "
            "Never pass a local mpay_alipay_* payment session ID."
        ),
    ),
]
OutTradeNo = Annotated[
    str,
    Field(
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        description="Provider top-up order number; 1-128 letters, digits, dots, underscores, or hyphens.",
    ),
]
TopupPack = Annotated[
    str,
    Field(
        min_length=1,
        max_length=64,
        pattern=r"^(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)$",
        description=(
            "Provider-offered top-up amount as a positive decimal string, for example '10.00'; "
            "1-64 characters. The provider determines the available pack values."
        ),
    ),
]
RequestId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="Caller correlation ID; 1-256 characters. Omit to generate a UUID.",
    ),
]
PageLimit = Annotated[int, Field(ge=1, le=100, description="Maximum records to return; 1-100 inclusive.")]
PageOffset = Annotated[int, Field(ge=0, description="Zero-based record offset; minimum 0.")]
PositiveSeconds = Annotated[
    float,
    Field(gt=0, description="Timeout in seconds; must be greater than 0; no SDK maximum."),
]
NonNegativeAmount = Annotated[
    float,
    Field(ge=0, description="Local spending limit in token units; minimum 0; no SDK maximum."),
]
PaymentChain = Annotated[
    Literal[
        "base", "polygon", "base_sepolia", "bnb", "bnb_testnet", "tempo_moderato",
        "solana", "solana_devnet",
    ],
    Field(description="Payment chain. Omit to use the client's configured default chain."),
]
PaymentToken = Annotated[
    Literal["USDC", "USDT"],
    Field(description="Payment token; USDC or USDT. Defaults to USDC."),
]
PaymentRail = Union[
    Annotated[Literal["balance"], Field(description="Use the provider balance rail.")],
    Annotated[Literal["alipay"], Field(description="Use the explicit Alipay A402 lifecycle.")],
]
AlipayStatus = Annotated[
    Literal[
        "created", "pending", "processing", "paid", "fulfilling",
        "completed", "rejected", "expired", "unknown",
    ],
    Field(description="Optional Alipay session status filter."),
]
TopupStatus = Annotated[
    Literal["pending", "credited", "expired"],
    Field(description="Optional top-up status filter: pending, credited, or expired."),
]
TopupRail = Annotated[
    Literal["wechat"],
    Field(description="Use the WeChat Native top-up flow."),
]
WechatStatus = Annotated[
    Literal["pending", "paid", "completed", "expired", "cancelled", "failed", "unknown"],
    Field(
        description=(
            "Optional WeChat status filter: pending, paid, completed, expired, cancelled, failed, or unknown."
        )
    ),
]
IncludeExpired = Annotated[
    bool,
    Field(description="Include expired sessions when true; defaults to true."),
]
Confirmed = Annotated[
    bool,
    Field(
        description=(
            "Explicitly approve the side effect. Required as true only when "
            "MOLTSPAY_MCP_REQUIRE_CONFIRM=1; otherwise false is accepted."
        )
    ),
]
DryRun = Annotated[
    bool,
    Field(description="When true, return the intended action without external requests, payment, or local mutation."),
]
ServiceParams = Annotated[
    Dict[str, Any],
    Field(description="Provider service parameters. Use an empty object when the service takes no parameters."),
]
IntentSummary = Annotated[
    str,
    Field(min_length=1, max_length=1024, description="Human-readable, non-sensitive reason for the payment."),
]


class ToolErrorPayload(BaseModel):
    """Structured MCP tool error returned inside a failure envelope."""

    code: str = Field(description="Stable machine-readable error code.")
    message: str = Field(description="Human-readable error message.")
    retryable: bool = Field(description="Whether retrying the same operation may succeed.")
    details: Dict[str, Any] = Field(description="Additional safe error context; empty when unavailable.")


class ToolSuccessEnvelope(BaseModel):
    """Successful MCP tool result."""

    ok: Literal[True] = Field(description="Always true for a successful result.")
    data: Any = Field(description="Tool-specific success payload described by the tool.")
    requestId: RequestId
    retried: Annotated[int, Field(ge=0, description="Number of automatic retries performed; currently 0.")]


class ToolFailureEnvelope(BaseModel):
    """Failed MCP tool result."""

    ok: Literal[False] = Field(description="Always false for a failed result.")
    error: ToolErrorPayload
    requestId: RequestId
    retried: Annotated[int, Field(ge=0, description="Number of automatic retries performed; currently 0.")]


class ToolEnvelope(BaseModel):
    """OpenClaw-compatible top-level object shared by every MCP tool."""

    ok: bool = Field(description="Whether the tool operation succeeded.")
    data: Optional[Any] = Field(default=None, description="Tool-specific success payload; null on failure.")
    error: Optional[ToolErrorPayload] = Field(default=None, description="Structured error; null on success.")
    requestId: RequestId
    retried: Annotated[int, Field(ge=0, description="Number of automatic retries performed; currently 0.")]


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
    """Serialize only safe recoverability metadata; never expose proof/challenge."""
    return _camel({
        "payment_session_id": session.payment_session_id,
        "business_session_id": session.business_session_id,
        "amount": session.amount,
        "currency": session.currency,
        "status": session.status,
        "request_id": session.request_id,
        "resource_url": session.resource_url,
        "method": session.method,
        "out_shake_no": session.out_shake_no,
        "trade_no": session.trade_no,
        "out_trade_no": session.out_trade_no,
        "pay_before": getattr(session, "pay_before", None),
        "service_id": getattr(session, "service_id", None),
        "resource_id": getattr(session, "resource_id", None),
        "provider_status": getattr(session, "provider_status", None),
        "provider_code": getattr(session, "provider_code", None),
        "provider_message": getattr(session, "provider_message", None),
        "resource_status_code": getattr(session, "resource_status_code", None),
        "fulfillment_status": getattr(session, "fulfillment_status", None),
        "media_paths": getattr(session, "media_paths", []),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "expires_at": session.expires_at,
        "last_error_code": session.last_error_code,
        "last_error": session.last_error,
        "result": session.result,
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
            retryable = code in {"alipay_verify_unavailable", "alipay_payment_state_unknown", "alipay_execution_in_progress", "alipay_payment_timeout"} or "timeout" in code
        elif isinstance(exc, (ValueError, TypeError)):
            code, retryable = "invalid_request", False
        else:
            code, retryable = "internal_error", False
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": str(exc),
                "retryable": retryable,
                "details": _camel(getattr(exc, "details", {}) or {}),
            },
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

    def status(self, serverUrl: Optional[HttpUrl] = None, buyerId: Optional[BuyerId] = None, requestId: Optional[RequestId] = None) -> ToolEnvelope:
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

    def balance_query(self, serverUrl: HttpUrl, buyerId: Optional[BuyerId] = None, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: self.client.get_buyer_balance(serverUrl, buyerId), requestId)

    def balance_transactions(self, serverUrl: HttpUrl, buyerId: Optional[BuyerId] = None, limit: PageLimit = 20, offset: PageOffset = 0, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: {
            "transactions": self.client.list_balance_transactions(serverUrl, buyerId, limit, offset),
            "limit": limit, "offset": offset,
        }, requestId)

    def balance_set_buyer(self, buyerId: BuyerId, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: {"buyerId": buyerId, "config": self.client.update_config(buyer_id=buyerId)}, requestId)

    def balance_topup_order(self, serverUrl: HttpUrl, pack: Optional[TopupPack] = None, buyerId: Optional[BuyerId] = None, rail: TopupRail = "wechat", confirmed: Confirmed = False, dryRun: DryRun = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            if dryRun:
                return {"intent": "create_balance_topup_order", "serverUrl": serverUrl, "pack": pack, "buyerId": buyerId, "rail": rail}
            self._confirm(confirmed)
            return self.client.create_balance_topup_order(serverUrl, pack, buyerId)
        return self._run(call, requestId)

    def balance_topup_confirm(self, outTradeNo: OutTradeNo, serverUrl: Optional[HttpUrl] = None, confirmed: Confirmed = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            self._confirm(confirmed)
            return self.client.confirm_balance_topup(outTradeNo, serverUrl)
        return self._run(call, requestId)

    def balance_topup_status(self, outTradeNo: OutTradeNo, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            session = self.client.get_balance_topup_session(outTradeNo)
            if session is None:
                raise PaymentError(f"Balance top-up session not found: {outTradeNo}")
            return session
        return self._run(call, requestId)

    def balance_topup_resume(self, outTradeNo: OutTradeNo, confirmed: Confirmed = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            self._confirm(confirmed)
            return self.client.confirm_balance_topup(outTradeNo)
        return self._run(call, requestId)

    def balance_topup_list(self, status: Optional[TopupStatus] = None, limit: PageLimit = 100, requestId: Optional[RequestId] = None) -> ToolEnvelope:
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

    def wechat_start(self, serverUrl: HttpUrl, service: ServiceId, params: Optional[ServiceParams] = None, confirmed: Confirmed = False, dryRun: DryRun = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            if dryRun:
                return {"intent": "start_wechat_payment", "serverUrl": serverUrl, "service": service, "params": params or {}}
            self._confirm(confirmed)
            session = self.client.start_wechat_payment(serverUrl, service, params or {})
            result = _wechat_session(session)
            result["qrCode"] = self._qr(session.code_url)
            return result
        return self._run(call, requestId)

    def wechat_status(self, identifier: Identifier, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: _wechat_session(self.client.get_wechat_payment_status(identifier)), requestId)

    def wechat_fulfill(self, identifier: Identifier, confirmed: Confirmed = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            self._confirm(confirmed)
            return _wechat_session(self.client.fulfill_wechat_payment(identifier))
        return self._run(call, requestId)

    def wechat_cancel(self, identifier: Identifier, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: _wechat_session(self.client.cancel_wechat_payment(identifier)), requestId)

    def wechat_list(self, status: Optional[WechatStatus] = None, limit: PageLimit = 100, includeExpired: IncludeExpired = True, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            sessions = self.client.list_wechat_payment_sessions()
            sessions = [item for item in sessions if (status is None or item.status == status) and (includeExpired or item.status != "expired")]
            return {"sessions": [_wechat_session(item) for item in sessions[:limit]], "limit": limit}
        return self._run(call, requestId)

    def alipay_check_wallet(self, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(self.client.check_alipay_wallet, requestId)

    def alipay_start(self, serverUrl: HttpUrl, service: ServiceId, sessionId: BusinessSessionId, params: Optional[ServiceParams] = None, intentSummary: Optional[IntentSummary] = None, timeoutSeconds: Optional[PositiveSeconds] = None, confirmed: Confirmed = False, dryRun: DryRun = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            if dryRun:
                return {"intent": "start_alipay_payment", "serverUrl": serverUrl, "service": service, "sessionId": sessionId, "params": params or {}, "intentSummary": intentSummary, "timeoutSeconds": timeoutSeconds}
            self._confirm(confirmed)
            return _alipay_session(self.client.start_alipay_payment(
                serverUrl, service, params or {}, business_session_id=sessionId,
                intent_summary=intentSummary, timeout=timeoutSeconds, request_id=requestId,
            ))
        return self._run(call, requestId)

    def alipay_status(self, identifier: Identifier, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: _alipay_session(self.client.get_alipay_payment_status(identifier)), requestId)

    def alipay_resume(self, identifier: Identifier, confirmed: Confirmed = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            self._confirm(confirmed)
            return _alipay_session(self.client.resume_alipay_payment(identifier))
        return self._run(call, requestId)

    def alipay_list(self, status: Optional[AlipayStatus] = None, limit: PageLimit = 100, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: {"sessions": [_alipay_session(item) for item in self.client.list_alipay_payment_sessions(status=status, limit=limit)], "limit": limit}, requestId)

    def pay(self, url: HttpUrl, service: ServiceId, params: ServiceParams, chain: Optional[PaymentChain] = None, token: PaymentToken = "USDC", rail: Optional[PaymentRail] = None, confirmed: Confirmed = False, dryRun: DryRun = False, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        def call():
            if dryRun:
                return {"intent": "pay", "url": url, "service": service, "params": params, "chain": chain, "token": token, "rail": rail}
            self._confirm(confirmed)
            if rail == "alipay":
                raise InteractiveRailRequiresLifecycle("Interactive Alipay payments require moltspay_alipay_start", details={"tool": "moltspay_alipay_start"})
            if rail not in {None, "balance"}:
                raise PaymentError(f"Interactive rail '{rail}' requires its start/status/fulfill tools")
            preferences = self.client.get_config().get("railPreference", []) if rail is None else []
            if preferences and preferences[0] in {"wechat", "alipay"}:
                raise PaymentError(
                    f"Configured interactive rail '{preferences[0]}' requires its start/status/fulfill tools"
                )
            options = {"auto_topup": False} if rail == "balance" else None
            if options is not None and requestId:
                options["request_id"] = requestId
            return self.client.pay(url, service, token=token, chain=chain, rail=rail, payment_params=params, rail_options=options)
        return self._run(call, requestId)

    def config(self, maxPerTx: Optional[NonNegativeAmount] = None, maxPerDay: Optional[NonNegativeAmount] = None, requestId: Optional[RequestId] = None) -> ToolEnvelope:
        return self._run(lambda: self.client.update_config(max_per_tx=maxPerTx, max_per_day=maxPerDay) if maxPerTx is not None or maxPerDay is not None else self.client.get_config(), requestId)


TOOL_DESCRIPTIONS = {
    "status": (
        "Read wallet address, balances on every configured chain, spending limits, and the optional provider "
        "balance. serverUrl is optional; when omitted, fiatBalance is null. buyerId is optional and otherwise "
        "uses the local default. Read-only and never sends a payment."
    ),
    "balance_query": (
        "Query one provider-side buyer balance. serverUrl is required. buyerId is optional and otherwise uses "
        "the local default buyer ID. Read-only and never sends a payment."
    ),
    "balance_transactions": (
        "List provider-side balance transactions. buyerId is optional and otherwise uses the local default; "
        "limit is 1-100 (default 20), and offset is at least 0 (default 0). Read-only."
    ),
    "balance_set_buyer": (
        "Persist buyerId as the local default provider buyer identifier for later balance operations. "
        "buyerId must contain 1-2048 characters. This changes local configuration but does not move funds."
    ),
    "balance_topup_order": (
        "Create and locally persist a recoverable external WeChat balance top-up order. pack is an optional "
        "provider-offered decimal amount; omission uses the provider default. buyerId is optional and otherwise "
        "uses the local default. dryRun=true returns only the intent. confirmed=true is required when the MCP "
        "confirmation gate is enabled. Returns codeUrl plus a PNG QR image for scanning. This does not itself "
        "confirm or credit the top-up."
    ),
    "balance_topup_resume": "Resume a locally persisted WeChat top-up; this may contact the provider and credit the account.",
    "balance_topup_confirm": (
        "Ask the provider to confirm an existing top-up order; a paid order may be credited to the provider "
        "balance and the local session may be updated. outTradeNo is required. serverUrl is optional when the "
        "order was created locally. confirmed=true is required when the MCP confirmation gate is enabled. "
        "This never creates a new order."
    ),
    "balance_topup_status": (
        "Read one locally persisted top-up session by outTradeNo without contacting the provider or creating an "
        "order. Reading an expired pending session may persist its local expired status."
    ),
    "balance_topup_list": (
        "List locally persisted top-up sessions. status may be pending, credited, expired, or omitted for all; "
        "limit is 1-100 (default 100). Does not contact the provider, although local expiry may be persisted."
    ),
    "wechat_start": (
        "Start a WeChat Native payment challenge for a provider service, persist a recoverable local session, "
        "and return codeUrl plus a PNG QR image. params is optional and defaults to an empty object. "
        "dryRun=true returns only the intent. confirmed=true is required when the MCP confirmation gate is "
        "enabled. This creates an external payment order but does not execute the paid service."
    ),
    "wechat_status": (
        "Query the external payment status for a locally persisted WeChat session and persist the observed "
        "state. identifier may be the local session ID or outTradeNo. This does not execute the paid service."
    ),
    "wechat_fulfill": (
        "For a WeChat session already observed as paid, submit its stored payment credential and execute the "
        "provider service. identifier may be the local session ID or outTradeNo. confirmed=true is required "
        "when the MCP confirmation gate is enabled."
    ),
    "wechat_cancel": (
        "Mark a locally persisted pending or unknown WeChat session as cancelled. identifier may be the local "
        "session ID or outTradeNo. This is local only and does not cancel or refund an external payment."
    ),
    "wechat_list": (
        "List locally persisted WeChat sessions. status may be pending, paid, completed, expired, cancelled, "
        "failed, unknown, or omitted for all. limit is 1-100 (default 100); includeExpired defaults to true."
    ),
    "alipay_check_wallet": "Read-only check of the official Alipay AI wallet readiness; never pays or changes local state.",
    "alipay_start": (
        "Start a recoverable Alipay A402 service payment. This creates an interactive payment session and may "
        "invoke the official alipay-bot. sessionId is required and must be the real current framework "
        "conversation/session ID (usually a UUID), never the returned local mpay_alipay_* paymentSessionId. "
        "dryRun=true performs no network, subprocess, or local write."
    ),
    "alipay_status": "Read one local Alipay session without contacting Alipay, invoking alipay-bot, or retrying the provider.",
    "alipay_resume": "Resume an Alipay session; this is side-effectful and may query payment, retry the resource, and confirm fulfillment.",
    "alipay_list": "List local Alipay sessions without returning payment proofs, wallet credentials, or challenge contents.",
    "pay": (
        "Pay for and execute a provider service using only an on-chain payment or provider balance. params is "
        "required and may be empty. chain may be base, polygon, base_sepolia, bnb, bnb_testnet, "
        "tempo_moderato, solana, or solana_devnet; omission uses the configured default. token is USDC or USDT "
        "(default USDC). rail may be balance or omitted for on-chain payment. The interactive WeChat rail is "
        "rejected and requires dedicated tools. dryRun=true returns only the intent; confirmed=true is "
        "required when the MCP confirmation gate is enabled. This operation may spend funds and execute service. "
        "If it returns insufficient_balance, show error.details.topupPacks plus a validated custom amount, create "
        "and confirm a balance top-up, then retry this tool; repeat while the balance is still insufficient."
    ),
    "config": (
        "Read local MoltsPay configuration when both limits are omitted. Otherwise persist maxPerTx and/or "
        "maxPerDay; each is at least 0 with no SDK maximum. Updating limits changes local spending policy but "
        "does not make a payment."
    ),
}


def create_mcp_server(client: Optional[MoltsPay] = None):
    try:
        from mcp.server.fastmcp import FastMCP
        from mcp.types import CallToolResult, ImageContent, TextContent
    except ImportError as exc:
        raise RuntimeError("MCP support requires: pip install 'moltspay[mcp]'") from exc
    adapter = MoltsPayMCP(client)
    # OpenClaw requires a top-level object output schema. ToolEnvelope is flat
    # rather than a root-level union so schema validation stays enabled.
    server = FastMCP("moltspay")

    def image_result(result: Dict[str, Any], code_url: Optional[str] = None) -> Any:
        qr = result.get("data", {}).get("qrCode") if result.get("ok") else None
        if not qr and result.get("ok") and code_url:
            qr = adapter._qr(code_url)
            result["data"]["qrCode"] = qr
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
        if qr:
            content.append(ImageContent(type="image", data=qr["data"], mimeType="image/png"))
        return CallToolResult(content=content, structuredContent=result)

    def alipay_image_result(result: Dict[str, Any]) -> Any:
        content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
        media_paths = result.get("data", {}).get("mediaPaths", []) if result.get("ok") else []
        if not isinstance(media_paths, list):
            media_paths = []
        for value in media_paths:
            try:
                path = Path(value)
                if path.suffix.lower() != ".png" or path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_size > 10 * 1024 * 1024:
                    continue
                payload = path.read_bytes()
                if len(payload) > 10 * 1024 * 1024 or not payload.startswith(b"\x89PNG\r\n\x1a\n"):
                    continue
                content.append(ImageContent(
                    type="image",
                    data=base64.b64encode(payload).decode("ascii"),
                    mimeType="image/png",
                ))
            except (OSError, TypeError, ValueError):
                continue
        return CallToolResult(content=content, structuredContent=result)

    for name, description in TOOL_DESCRIPTIONS.items():
        if name == "balance_topup_order":
            def balance_topup_order_tool(
                serverUrl: HttpUrl,
                pack: Optional[TopupPack] = None,
                buyerId: Optional[BuyerId] = None,
                rail: TopupRail = "wechat",
                confirmed: Confirmed = False,
                dryRun: DryRun = False,
                requestId: Optional[RequestId] = None,
            ):
                result = adapter.balance_topup_order(
                    serverUrl, pack, buyerId, rail, confirmed, dryRun, requestId
                )
                code_url = result.get("data", {}).get("codeUrl") if result.get("ok") else None
                return image_result(result, code_url)

            balance_topup_order_tool.__annotations__["return"] = Annotated[CallToolResult, ToolEnvelope]
            handler = balance_topup_order_tool
        elif name == "wechat_start":
            def wechat_start_tool(
                serverUrl: HttpUrl,
                service: ServiceId,
                params: Optional[ServiceParams] = None,
                confirmed: Confirmed = False,
                dryRun: DryRun = False,
                requestId: Optional[RequestId] = None,
            ):
                result = adapter.wechat_start(serverUrl, service, params, confirmed, dryRun, requestId)
                return image_result(result)

            wechat_start_tool.__annotations__["return"] = Annotated[CallToolResult, ToolEnvelope]
            handler = wechat_start_tool
        elif name == "alipay_start":
            def alipay_start_tool(
                serverUrl: HttpUrl,
                service: ServiceId,
                sessionId: BusinessSessionId,
                params: Optional[ServiceParams] = None,
                intentSummary: Optional[IntentSummary] = None,
                timeoutSeconds: Optional[PositiveSeconds] = None,
                confirmed: Confirmed = False,
                dryRun: DryRun = False,
                requestId: Optional[RequestId] = None,
            ):
                result = adapter.alipay_start(
                    serverUrl, service, sessionId, params, intentSummary,
                    timeoutSeconds, confirmed, dryRun, requestId,
                )
                return alipay_image_result(result)

            alipay_start_tool.__annotations__["return"] = Annotated[CallToolResult, ToolEnvelope]
            handler = alipay_start_tool
        else:
            handler = getattr(adapter, name)
        server.tool(name=f"moltspay_{name}", description=description)(handler)
    return server


def main() -> None:
    create_mcp_server().run()


__all__ = ["MoltsPayMCP", "create_mcp_server", "main"]
