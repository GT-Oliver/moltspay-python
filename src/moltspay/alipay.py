"""Alipay AI Pay buyer client backed by the official ``alipay-bot`` CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Literal, Optional, Sequence

from pydantic import BaseModel

from .exceptions import (
    AlipayCliNotFound, AlipayPaymentRejected, AlipayPaymentTimeout,
    AlipayProtocolError,
)


ALIPAY_SCHEME = "alipay-aipay"
ALIPAY_NETWORK = "alipay"
TRADE_NO_RE = re.compile(r"(?:tradeNo|trade_no|trade-no)\s*[=:]\s*[\"']?(\d{32})(?!\d)", re.I)
TRADE_NO_BARE_RE = re.compile(r"\b(\d{32})\b")
URL_RE = re.compile(r"(?:alipays?://|https?://)[^\s\"'\]`)>」]+", re.I)
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class AlipayPaymentSession(BaseModel):
    payment_session_id: str
    status: Literal["pending", "completed", "rejected", "expired", "failed", "unknown"] = "pending"
    trade_no: str
    out_trade_no: str
    payment_url: Optional[str] = None
    resource_url: str
    method: str = "POST"
    data: Optional[str] = None
    created_at: str
    updated_at: str
    expires_at: str
    result: Optional[Any] = None
    last_error: Optional[str] = None


def _clean_url(value: str) -> str:
    return value.rstrip(")]} `>")


def parse_trade_no(lines: Sequence[str]) -> Optional[str]:
    text = "\n".join(lines)
    try:
        data = json.loads(text)
        for key in ("tradeNo", "trade_no", "out_trade_no"):
            value = str(data.get(key, ""))
            if re.fullmatch(r"\d{32}", value):
                return value
    except (json.JSONDecodeError, AttributeError):
        pass
    match = TRADE_NO_RE.search(text) or TRADE_NO_BARE_RE.search(text)
    return match.group(1) if match else None


def parse_payment_url(lines: Sequence[str]) -> Optional[str]:
    for line in lines:
        if "url" in line.lower() or "http" in line.lower():
            match = URL_RE.search(line)
            if match:
                return _clean_url(match.group(0))
    return None


def parse_status(lines: Sequence[str]) -> str:
    raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            if data.get("success") is True or data.get("code") == 200:
                return "paid"
            marker = (data["body"] if isinstance(data.get("body"), str) else f"{data.get('errorCode', '')} {data.get('message', '')} {data.get('reason', '')}").upper()
        else:
            marker = raw.upper()
    except json.JSONDecodeError:
        marker = raw.upper()
    if re.search(r"TRADE_SUCCESS|TRADE_FINISHED|\"STATUS\"\s*:\s*\"FULFILLED\"|RESOURCE\s+RESPONSE\s+STATUS\s+200", marker):
        return "paid"
    if re.search(r"CLOSED|CANCEL|FAIL|REJECT|REFUSE|TIMEOUT|EXPIRE", marker):
        return "rejected"
    if re.search(r"UNPAID|WAIT|PENDING|PROCESS|NOTPAY", marker):
        return "pending"
    return "unknown"


class AlipayClient:
    """Run the same Alipay 402 handshake used by the Node client."""

    def __init__(
        self,
        session_id: Optional[str] = None,
        config_dir: Optional[str] = None,
        framework: str = "openclaw",
        executable: str = "alipay-bot",
        runner: Optional[Callable[[Sequence[str]], list[str]]] = None,
    ):
        self.session_id = session_id or f"mpay_{uuid.uuid4()}"
        self.config_dir = Path(config_dir).expanduser() if config_dir else Path.home() / ".moltspay"
        self.session_dir = self.config_dir / "alipay-sessions"
        self.framework = framework
        self.executable = executable
        self.runner = runner or self._run

    def _run(self, args: Sequence[str]) -> list[str]:
        if not shutil.which(self.executable):
            raise AlipayCliNotFound(
                "alipay-bot was not found. Install it with: npx -y @alipay/agent-payment install-cli"
            )
        completed = subprocess.run(
            [self.executable, *args], capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        lines = [line for line in (completed.stdout + "\n" + completed.stderr).splitlines() if line.strip()]
        if completed.returncode != 0:
            raise AlipayProtocolError("alipay-bot failed: " + "\n".join(lines[-10:]))
        return lines

    def check_wallet(self) -> None:
        lines = self.runner(["check-wallet"])
        text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
            ready = isinstance(data, dict) and ("code" not in data or int(data["code"]) == 200)
        except (json.JSONDecodeError, TypeError, ValueError):
            ready = bool(re.search(r"READY|OPENED|BOUND|SUCCESS|已开通|已绑定", text, re.I))
        if not ready:
            raise AlipayProtocolError("Alipay wallet is not opened; run `moltspay alipay apply` and `bind`")

    def start_402(
        self,
        resource_url: str,
        requirement: Dict[str, Any],
        method: str = "POST",
        data: Optional[str] = None,
        intent_summary: Optional[str] = None,
        timeout: Optional[float] = None,
        on_payment_pending: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> AlipayPaymentSession:
        """Start an Alipay payment and persist enough state to resume it."""
        extra = requirement.get("extra") or {}
        payment_needed = str(extra.get("payment_needed_header", ""))
        if not payment_needed:
            raise AlipayProtocolError("Alipay requirement missing extra.payment_needed_header")
        summary = intent_summary or f"Pay {requirement.get('amount', '')} {requirement.get('asset', 'CNY')}"
        self.runner(["payment-intent", "--session-id", self.session_id, "--intent-summary", summary, "--framework", self.framework])
        self.check_wallet()
        challenge_dir = self.config_dir / "alipay"
        challenge_dir.mkdir(parents=True, exist_ok=True)
        request_id = str(extra.get("out_trade_no") or uuid.uuid4())
        challenge_id = request_id if SAFE_IDENTIFIER_RE.fullmatch(request_id) else uuid.uuid4().hex
        challenge = challenge_dir / f"402_{challenge_id}.txt"
        challenge.write_text(payment_needed, encoding="utf-8")
        args = ["402-buyer-pay", "-f", str(challenge), "-r", resource_url, "-s", self.session_id, "-i", summary, "-w", self.framework]
        if method:
            args.extend(["-m", method])
        if data:
            args.extend(["-d", data])
        lines = self.runner(args)
        trade_no = parse_trade_no(lines)
        if not trade_no:
            raise AlipayProtocolError("402-buyer-pay did not return a tradeNo")
        payment_url = parse_payment_url(lines)
        if on_payment_pending and payment_url:
            on_payment_pending({"payment_url": payment_url, "trade_no": trade_no})
        current = time.time()
        session = AlipayPaymentSession(
            payment_session_id=f"{self.session_id}_{uuid.uuid4().hex[:12]}",
            trade_no=trade_no,
            out_trade_no=request_id,
            payment_url=payment_url,
            resource_url=resource_url,
            method=method,
            data=data,
            created_at=_iso(current),
            updated_at=_iso(current),
            expires_at=_iso(current + float(timeout or requirement.get("maxTimeoutSeconds", 1800))),
        )
        self._save(session)
        return session

    def get_session(self, identifier: str) -> AlipayPaymentSession:
        """Read persisted state only; this never invokes alipay-bot."""
        return self._expire_if_needed(self._load(identifier))

    def resume(self, identifier: str) -> AlipayPaymentSession:
        """Resume the side-effectful Alipay query/fulfillment command once."""
        session = self.get_session(identifier)
        if session.status in ("completed", "rejected", "expired", "failed"):
            return session
        query = [
            "402-query-payment-status", "-t", session.trade_no,
            "-r", session.resource_url, "-m", session.method,
        ]
        if session.data:
            query.extend(["-d", session.data])
        try:
            lines = self.runner(query)
        except Exception as exc:
            return self._update(session, status="unknown", last_error=f"Alipay resume result is unknown: {exc}")
        status = parse_status(lines)
        if status == "pending":
            return self._update(session, status="pending", last_error=None)
        if status == "unknown":
            return self._update(session, status="unknown", last_error="Alipay returned an unknown payment state")
        if status == "rejected":
            return self._update(session, status="rejected", last_error=f"Alipay payment rejected: {session.trade_no}")
        body = self._extract_body(lines)
        try:
            self.runner(["402-buyer-fulfillment-ack", "-t", session.trade_no])
        except Exception:
            pass
        return self._update(session, status="completed", result=body, last_error=None)

    def pay_402(
        self,
        resource_url: str,
        requirement: Dict[str, Any],
        method: str = "POST",
        data: Optional[str] = None,
        intent_summary: Optional[str] = None,
        timeout: Optional[float] = None,
        poll_interval: float = 3.0,
        on_payment_pending: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> Dict[str, Any]:
        """Backward-compatible blocking wrapper over start/resume."""
        session = self.start_402(
            resource_url=resource_url, requirement=requirement, method=method, data=data,
            intent_summary=intent_summary, timeout=timeout, on_payment_pending=on_payment_pending,
        )
        deadline = time.monotonic() + float(timeout or requirement.get("maxTimeoutSeconds", 1800))
        if data:
            session.data = data
        while time.monotonic() < deadline:
            session = self.resume(session.payment_session_id)
            if session.status == "completed":
                return {
                    "body": session.result,
                    "payment": {
                        "trade_no": session.trade_no,
                        "out_trade_no": session.out_trade_no,
                        "payment_url": session.payment_url,
                        "session_id": session.payment_session_id,
                    },
                }
            if session.status == "rejected":
                raise AlipayPaymentRejected(session.last_error or f"Alipay payment rejected: {session.trade_no}")
            if session.status == "failed":
                raise AlipayProtocolError(session.last_error or "Alipay payment failed")
            time.sleep(max(0.05, poll_interval))
        self._update(session, status="expired", last_error=f"Alipay payment timed out: {session.trade_no}")
        raise AlipayPaymentTimeout(f"Alipay payment timed out: {session.trade_no}")

    def list_sessions(self) -> list[AlipayPaymentSession]:
        self.session_dir.mkdir(parents=True, exist_ok=True)
        sessions = []
        for path in self.session_dir.glob("*.json"):
            try:
                sessions.append(self._expire_if_needed(AlipayPaymentSession.model_validate_json(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return sorted(sessions, key=lambda item: item.created_at, reverse=True)

    @staticmethod
    def _validate_identifier(identifier: str) -> None:
        if not SAFE_IDENTIFIER_RE.fullmatch(identifier):
            raise AlipayProtocolError("Invalid Alipay payment identifier")

    def _load(self, identifier: str) -> AlipayPaymentSession:
        self._validate_identifier(identifier)
        direct = self.session_dir / f"{identifier}.json"
        if direct.exists():
            return AlipayPaymentSession.model_validate_json(direct.read_text(encoding="utf-8"))
        for session in self.list_sessions():
            if session.trade_no == identifier or session.out_trade_no == identifier:
                return session
        raise AlipayProtocolError(f"Alipay payment session not found: {identifier}")

    def _save(self, session: AlipayPaymentSession) -> None:
        self._validate_identifier(session.payment_session_id)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        (self.session_dir / f"{session.payment_session_id}.json").write_text(session.model_dump_json(indent=2), encoding="utf-8")

    def _update(self, session: AlipayPaymentSession, **updates: Any) -> AlipayPaymentSession:
        next_session = session.model_copy(update={**updates, "updated_at": _iso(time.time())})
        self._save(next_session)
        return next_session

    def _expire_if_needed(self, session: AlipayPaymentSession) -> AlipayPaymentSession:
        if session.status not in ("pending", "unknown"):
            return session
        expires = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00")).timestamp()
        if time.time() < expires:
            return session
        return self._update(session, status="expired", last_error=f"Alipay payment expired at {session.expires_at}")

    @staticmethod
    def _extract_body(lines: Sequence[str]) -> Any:
        text = "\n".join(lines).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        if not isinstance(data, dict):
            return data
        resource = data.get("resourceResponse")
        if resource is None and isinstance(data.get("body"), str):
            report = data["body"]
            match = re.search(r"\{(?:[^{}]|\{[^{}]*\})*\}", report, re.S)
            if not match:
                return report
            try:
                resource = json.loads(match.group(0))
            except json.JSONDecodeError:
                return report
        if resource is None:
            resource = data.get("result", data.get("data", data.get("body", data)))
        if isinstance(resource, dict):
            resource = resource.get("result", resource.get("data", resource.get("body", resource)))
        return resource
