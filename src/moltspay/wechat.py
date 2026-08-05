"""Recoverable WeChat Pay Native buyer sessions."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional

import httpx
from pydantic import AliasChoices, BaseModel, Field

from .exceptions import PaymentError


WECHAT_SCHEME = "wechatpay-native"
WECHAT_NETWORK = "wechat"


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class WechatPaymentSession(BaseModel):
    # Node.js persists the same session using camelCase names. Accept both
    # formats so sessions can be resumed across SDK implementations.
    payment_session_id: str = Field(validation_alias=AliasChoices("payment_session_id", "paymentSessionId"))
    status: Literal["pending", "paid", "completed", "expired", "cancelled", "failed"] = "pending"
    resource_url: str = Field(validation_alias=AliasChoices("resource_url", "resourceUrl"))
    method: str = "POST"
    data: Optional[str] = None
    requirement: Dict[str, Any] = Field(default_factory=dict)
    code_url: str = Field(validation_alias=AliasChoices("code_url", "codeUrl"))
    out_trade_no: str = Field(validation_alias=AliasChoices("out_trade_no", "outTradeNo"))
    created_at: str = Field(validation_alias=AliasChoices("created_at", "createdAt"))
    updated_at: str = Field(validation_alias=AliasChoices("updated_at", "updatedAt"))
    expires_at: str = Field(validation_alias=AliasChoices("expires_at", "expiresAt"))
    context: Dict[str, Any] = Field(default_factory=dict)
    last_http_status: Optional[int] = Field(
        default=None, validation_alias=AliasChoices("last_http_status", "lastHttpStatus")
    )
    last_error: Optional[str] = Field(default=None, validation_alias=AliasChoices("last_error", "lastError"))
    result_body: Optional[str] = Field(default=None, validation_alias=AliasChoices("result_body", "resultBody"))


class WechatClient:
    def __init__(
        self,
        config_dir: Optional[str] = None,
        timeout: Optional[float] = None,
        http_client: Optional[httpx.Client] = None,
        now: Callable[[], float] = time.time,
    ):
        self.config_dir = Path(config_dir).expanduser() if config_dir else Path.home() / ".moltspay"
        self.session_dir = self.config_dir / "wechat-sessions"
        self.http = http_client or httpx.Client(timeout=timeout)
        self._owns_client = http_client is None
        self.now = now

    def start_402(
        self,
        resource_url: str,
        requirement: Dict[str, Any],
        method: str = "POST",
        data: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        payment_session_id: Optional[str] = None,
        timeout: Optional[float] = None,
        on_payment_pending: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> WechatPaymentSession:
        extra = requirement.get("extra") or {}
        code_url = str(extra.get("code_url", ""))
        out_trade_no = str(extra.get("out_trade_no", ""))
        if not code_url or not out_trade_no:
            raise PaymentError("WeChat requirement is missing extra.code_url or extra.out_trade_no")
        current = self.now()
        lifetime = timeout or float(requirement.get("maxTimeoutSeconds", 300))
        session = WechatPaymentSession(
            payment_session_id=payment_session_id or f"mpay_sess_{uuid.uuid4()}",
            resource_url=resource_url,
            method=method.upper(), data=data, requirement=requirement,
            code_url=code_url, out_trade_no=out_trade_no,
            created_at=_iso(current), updated_at=_iso(current), expires_at=_iso(current + lifetime),
            context=context or {},
        )
        self._save(session)
        if on_payment_pending:
            on_payment_pending({"code_url": code_url, "out_trade_no": out_trade_no})
        return session

    def _payment_header(self, session: WechatPaymentSession) -> str:
        payload = {
            "x402Version": 2, "scheme": WECHAT_SCHEME, "network": WECHAT_NETWORK,
            "accepted": {"scheme": WECHAT_SCHEME, "network": WECHAT_NETWORK, "extra": {"out_trade_no": session.out_trade_no}},
            "payload": {"out_trade_no": session.out_trade_no},
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def status(self, identifier: str) -> WechatPaymentSession:
        session = self._load(identifier)
        if session.status in ("cancelled", "completed"):
            return session
        expires = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00")).timestamp()
        if self.now() >= expires:
            return self._update(session, status="expired", last_error=f"WeChat payment expired at {session.expires_at}")
        response = self.http.request(
            session.method, session.resource_url,
            content=session.data if session.method == "POST" else None,
            headers={"Content-Type": "application/json", "X-Payment": self._payment_header(session)},
        )
        if response.status_code == 200:
            return self._update(session, status="completed", last_http_status=200, last_error=None, result_body=response.text)
        if response.status_code == 402:
            return self._update(session, status="pending", last_http_status=402, last_error=response.text[:500] or "wechat payment pending")
        return self._update(
            session, status="failed", last_http_status=response.status_code,
            last_error=f"WeChat /execute returned HTTP {response.status_code}: {response.text[:500]}",
        )

    def poll_session(self, identifier: str, poll_interval: float = 3.0, timeout: float = 300.0) -> WechatPaymentSession:
        deadline = self.now() + timeout
        session = self._load(identifier)
        while self.now() < deadline:
            session = self.status(identifier)
            if session.status != "pending":
                return session
            time.sleep(max(0.05, poll_interval))
        return self._update(session, status="expired", last_error=f"WeChat payment timed out after {timeout:.0f}s")

    def pay_402(self, **kwargs: Any) -> Dict[str, Any]:
        poll_interval = float(kwargs.pop("poll_interval", 3.0))
        timeout = float(kwargs.get("timeout") or 300.0)
        session = self.start_402(**kwargs)
        completed = self.poll_session(session.payment_session_id, poll_interval=poll_interval, timeout=timeout)
        if completed.status != "completed":
            raise PaymentError(completed.last_error or f"WeChat payment ended with {completed.status}")
        try:
            body = json.loads(completed.result_body or "{}")
        except json.JSONDecodeError:
            body = completed.result_body
        return {"status": completed.last_http_status or 200, "body": body, "session": completed}

    def fulfill(self, identifier: str) -> WechatPaymentSession:
        session = self._load(identifier)
        return session if session.status == "completed" else self.status(identifier)

    def cancel(self, identifier: str) -> WechatPaymentSession:
        return self._update(self._load(identifier), status="cancelled", last_error="cancelled locally")

    def list_sessions(self) -> list[WechatPaymentSession]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        sessions = [WechatPaymentSession.model_validate_json(path.read_text(encoding="utf-8")) for path in self.session_dir.glob("*.json")]
        return sorted(sessions, key=lambda item: item.created_at, reverse=True)

    def _load(self, identifier: str) -> WechatPaymentSession:
        direct = self.session_dir / f"{identifier}.json"
        if direct.exists():
            return WechatPaymentSession.model_validate_json(direct.read_text(encoding="utf-8"))
        for session in self.list_sessions():
            if session.out_trade_no == identifier:
                return session
        raise PaymentError(f"WeChat payment session not found: {identifier}")

    def _save(self, session: WechatPaymentSession) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / f"{session.payment_session_id}.json").write_text(session.model_dump_json(indent=2), encoding="utf-8")

    def _update(self, session: WechatPaymentSession, **updates: Any) -> WechatPaymentSession:
        next_session = session.model_copy(update={**updates, "updated_at": _iso(self.now())})
        self._save(next_session)
        return next_session

    def close(self) -> None:
        if self._owns_client:
            self.http.close()
