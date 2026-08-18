"""Buyer-side support for Alipay AI Pay (A402).

The buyer never handles an Alipay credential.  It only persists the minimum
amount of state needed to resume the official ``alipay-bot`` process.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .exceptions import (
    AlipayCliFailed,
    AlipayCliNotFound,
    AlipayPaymentRejected,
    AlipayPaymentStateUnknown,
    AlipayPaymentTimeout,
    AlipayProtocolError,
    AlipayRequestContextInvalid,
    AlipayRequestContextMissing,
    AlipayWalletNotReady,
)


ALIPAY_NETWORK = "alipay"
ALIPAY_SCHEME = "a402"
MAX_HEADER_BYTES = 16 * 1024
MAX_OUTPUT_BYTES = 256 * 1024
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TRADE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# 8282 is the documented query-number family; alipay-bot 0.4.0 emits the
# successor 8283 family. Both retain the same 32-digit recovery contract.
OUT_SHAKE_NO_RE = re.compile(r"^\d{10}828[23]\d{18}$")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def encode_a402_json(value: Dict[str, Any]) -> str:
    """Encode a JSON object as unpadded Base64URL."""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_HEADER_BYTES:
        raise AlipayProtocolError("Alipay header exceeds the maximum size")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_a402_json(value: str, *, name: str = "Alipay header") -> Dict[str, Any]:
    """Decode standard URL-safe Base64 with or without padding."""
    if not isinstance(value, str) or not value or len(value) > MAX_HEADER_BYTES * 2:
        raise AlipayProtocolError(f"{name} is missing or too large")
    if not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
        raise AlipayProtocolError(f"Malformed {name}")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) > MAX_HEADER_BYTES:
            raise AlipayProtocolError(f"{name} exceeds the maximum size")
        data = json.loads(raw.decode("utf-8"))
    except AlipayProtocolError:
        raise
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlipayProtocolError(f"Malformed {name}") from exc
    if not isinstance(data, dict):
        raise AlipayProtocolError(f"{name} must contain a JSON object")
    return data


def _b64url(data: bytes) -> str:
    """Compatibility helper retained for integrations using the old module."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode_b64url(value: str) -> Dict[str, Any]:
    return decode_a402_json(value)


def _safe_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_IDENTIFIER_RE.fullmatch(value):
        raise AlipayProtocolError(f"Invalid {label}")
    return value


class AlipayPaymentSession(BaseModel):
    """Recoverable local A402 session.

    Sensitive challenge/proof data is deliberately not part of this model.
    """

    model_config = ConfigDict(extra="ignore")

    payment_session_id: str
    business_session_id: str = ""
    amount: Optional[str] = None
    currency: Optional[str] = None
    status: Literal["created", "pending", "processing", "completed", "rejected", "expired", "unknown"] = "created"
    request_id: str = ""
    resource_url: str = ""
    method: str = "POST"
    request_body: Optional[str] = None
    data: Optional[str] = None
    request_headers: Dict[str, str] = Field(default_factory=dict)
    out_shake_no: Optional[str] = None
    trade_no: Optional[str] = None
    out_trade_no: Optional[str] = None
    payment_url: Optional[str] = None
    media_paths: List[str] = Field(default_factory=list)
    challenge_path: Optional[str] = None
    intent_summary: str = "Alipay payment"
    context: Dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""
    expires_at: str = ""
    last_error_code: Optional[str] = None
    last_error: Optional[str] = None
    result: Optional[Any] = None


def _json_objects(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Extract JSON objects from compact or pretty-printed CLI output.

    The official CLI writes indented JSON and may surround it with diagnostic
    lines.  Decode from each opening brace so formatting and log prefixes do
    not hide the final structured result.
    """
    raw = "\n".join(str(line) for line in lines)
    if len(raw.encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
        return []
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    offset = 0
    while offset < len(raw):
        start = raw.find("{", offset)
        if start < 0:
            break
        try:
            item, end = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if isinstance(item, dict):
            objects.append(item)
        offset = end
    return objects


def _flatten_cli(lines: Sequence[str]) -> str:
    return "\n".join(str(line) for line in lines if str(line).strip())[-4000:]


def _value(objects: Sequence[Dict[str, Any]], *keys: str) -> Optional[str]:
    for obj in objects:
        for key in keys:
            value = obj.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, (int, float)):
                return str(value)
        for nested_key in ("data", "payment", "result", "details"):
            nested = obj.get(nested_key)
            if isinstance(nested, dict):
                found = _value([nested], *keys)
                if found:
                    return found
    return None


def parse_alipay_cli_output(lines: Sequence[str]) -> Dict[str, Any]:
    """Normalize official JSON output while retaining strict text fallbacks."""
    objects = _json_objects(lines)
    result: Dict[str, Any] = {}
    for key, aliases in {
        "out_shake_no": ("outShakeNo", "out_shake_no"),
        "trade_no": ("tradeNo", "trade_no", "transactionId"),
        "out_trade_no": ("outTradeNo", "out_trade_no"),
        "payment_url": ("paymentUrl", "payment_url", "url"),
        "status": ("status", "state", "paymentStatus"),
    }.items():
        found = _value(objects, *aliases)
        if found:
            result[key] = found
    raw = _flatten_cli(lines)
    media_paths = [
        match.group(1).strip()
        for match in re.finditer(r"(?m)^MEDIA:\s*(\S.*?)\s*$", raw)
        if match.group(1).strip()
    ]
    if media_paths:
        result["media_paths"] = media_paths
    if not result.get("out_shake_no"):
        # The official CLI's interactive output uses customer-facing Chinese
        # labels rather than JSON in some channels. Only accept an unambiguous
        # 32-digit value directly following an approved recovery label; never
        # infer a query number from a URL or unrelated digits.
        candidates = {
            match.group(1)
            for match in re.finditer(
                r"(?:订单号|查询单号)\s*[:：=]\s*(\d{32})(?!\d)", raw
            )
            if OUT_SHAKE_NO_RE.fullmatch(match.group(1))
        }
        if len(candidates) == 1:
            result["out_shake_no"] = candidates.pop()
    if not result.get("out_shake_no") and media_paths:
        # alipay-bot 0.4.0 can emit only a MEDIA line for polling-pay QR
        # responses. Its official payment PNG basename is the outShakeNo.
        # Limit this compatibility path to the current command output, the
        # exact official temporary directory, and one unambiguous valid ID.
        candidates = set()
        for media_path in media_paths:
            match = re.fullmatch(
                r"/(?:private/)?tmp/openclaw/alipay-bot-cli/qrcode/"
                r"payment_(\d{32})\.png",
                media_path,
            )
            if match and OUT_SHAKE_NO_RE.fullmatch(match.group(1)):
                candidates.add(match.group(1))
        if len(candidates) == 1:
            result["out_shake_no"] = candidates.pop()
    if not result.get("trade_no"):
        match = re.search(r"(?:交易号|trade[_ -]?no)\s*[:：=]\s*([A-Za-z0-9._-]+)", raw, re.I)
        if match and TRADE_NO_RE.fullmatch(match.group(1)):
            result["trade_no"] = match.group(1)
    status = str(result.get("status", "")).upper()
    upper = raw.upper()
    if any(marker in status for marker in ("SUCCESS", "PAID", "COMPLETED", "FULFILLED")) or any(
        marker in upper for marker in ("TRADE_SUCCESS", "RESOURCE RESPONSE STATUS 200")
    ):
        result["normalized_status"] = "completed"
    elif any(marker in status for marker in ("REJECT", "CANCEL", "CLOSED", "FAIL", "EXPIRE")) or any(
        marker in upper for marker in ("REJECT", "CANCEL", "CLOSED", "FAIL", "EXPIRE")
    ):
        result["normalized_status"] = "rejected"
    elif any(marker in status for marker in ("PENDING", "WAIT", "PROCESS", "UNPAID")) or any(
        marker in upper for marker in ("PENDING", "WAIT", "UNPAID", "PROCESS")
    ):
        result["normalized_status"] = "pending"
    else:
        result["normalized_status"] = "unknown"
    result["raw"] = raw
    return result


def parse_trade_no(lines: Sequence[str]) -> Optional[str]:
    return parse_alipay_cli_output(lines).get("trade_no")


def parse_payment_url(lines: Sequence[str]) -> Optional[str]:
    parsed = parse_alipay_cli_output(lines).get("payment_url")
    if parsed:
        return parsed.rstrip(")]}> `")
    match = re.search(r"(?:支付方式|payment\s*(?:url|link)|url)\s*[:：=]\s*((?:https?://|alipays?://)[^\s\"']+)", _flatten_cli(lines), re.I)
    return match.group(1).rstrip(")]}> `") if match else None


def parse_status(lines: Sequence[str]) -> str:
    return {"completed": "paid", "rejected": "rejected", "pending": "pending", "unknown": "unknown"}.get(
        parse_alipay_cli_output(lines).get("normalized_status", "unknown"), "unknown"
    )


class AlipayBuyerClient:
    """Adapter for the official ``alipay-bot`` executable."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        executable: str = "alipay-bot",
        framework: str = "moltspay",
        runner: Optional[Callable[[Sequence[str]], Sequence[str]]] = None,
    ):
        self.config_dir = Path(config_dir).expanduser() if config_dir else Path.home() / ".moltspay"
        self.session_dir = self.config_dir / "alipay-sessions"
        self.executable = executable
        self.framework = os.environ.get("AIPAY_FRAMEWORK") or framework
        self.runner = runner or self._run

    def _run(self, args: Sequence[str]) -> Sequence[str]:
        executable = shutil.which(self.executable)
        if not executable:
            raise AlipayCliNotFound("alipay-bot was not found; install the official Alipay agent-payment CLI")
        completed = subprocess.run(
            [executable, *list(args)], shell=False, capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False,
        )
        output = (completed.stdout + "\n" + completed.stderr).splitlines()
        if len("\n".join(output).encode("utf-8", "replace")) > MAX_OUTPUT_BYTES:
            raise AlipayCliFailed("alipay-bot output is too large")
        if completed.returncode != 0:
            raise AlipayCliFailed("alipay-bot failed", details={"exitCode": completed.returncode})
        return output

    def _save(self, session: AlipayPaymentSession) -> None:
        _safe_identifier(session.payment_session_id, "payment session ID")
        self.session_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.session_dir, 0o700)
        except OSError:
            pass
        target = self.session_dir / f"{session.payment_session_id}.json"
        fd, temporary = tempfile.mkstemp(prefix=f".{session.payment_session_id}.", suffix=".tmp", dir=str(self.session_dir))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(session.model_dump_json(indent=2))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _load(self, identifier: str) -> AlipayPaymentSession:
        _safe_identifier(identifier, "payment identifier")
        path = self.session_dir / f"{identifier}.json"
        if path.exists() and not path.is_symlink():
            return AlipayPaymentSession.model_validate_json(path.read_text(encoding="utf-8"))
        for item in self.list_sessions():
            if identifier in {item.trade_no, item.out_trade_no, item.out_shake_no}:
                return item
        raise AlipayProtocolError("Alipay payment session was not found")

    def _update(self, session: AlipayPaymentSession, **changes: Any) -> AlipayPaymentSession:
        updated = session.model_copy(update={**changes, "updated_at": _iso(time.time())})
        self._save(updated)
        if updated.status in {"completed", "rejected", "expired"} and updated.challenge_path:
            try:
                challenge = Path(updated.challenge_path)
                if challenge.is_file() and not challenge.is_symlink():
                    challenge.unlink()
            except OSError:
                pass
        return updated

    def list_sessions(self, *, status: Optional[str] = None, limit: int = 100) -> List[AlipayPaymentSession]:
        if not self.session_dir.exists():
            return []
        items: List[AlipayPaymentSession] = []
        for path in self.session_dir.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                item = AlipayPaymentSession.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if status and item.status != status:
                continue
            items.append(self._expire(item))
        return sorted(items, key=lambda item: item.created_at, reverse=True)[: max(1, min(limit, 100))]

    def _expire(self, session: AlipayPaymentSession) -> AlipayPaymentSession:
        if session.status in {"completed", "rejected", "expired"}:
            return session
        try:
            expired = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00")).timestamp() <= time.time()
        except ValueError:
            expired = True
        return self._update(session, status="expired", last_error_code="alipay_payment_timeout", last_error="Alipay payment session expired") if expired else session

    def get_session(self, identifier: str) -> AlipayPaymentSession:
        return self._expire(self._load(identifier))

    def check_wallet(self) -> Dict[str, Any]:
        try:
            lines = list(self.runner(["check-wallet"]))
        except AlipayCliNotFound:
            raise
        parsed = parse_alipay_cli_output(lines)
        raw = parsed.get("raw", "").upper()
        objects = _json_objects(lines)
        result = objects[-1] if objects else {}
        code = result.get("code")
        message = str(result.get("message") or "")
        status = str(result.get("status") or "")

        # The official CLI uses code=200 for both a bound wallet and the
        # applied-but-unbound state.  Treat only an explicit bound contract as
        # ready; a successful status code alone is never sufficient.
        ready = (
            (code in (200, "200") and message == "已开启支付宝支付功能")
            or (
                result.get("ready") is True
                and result.get("bound") is True
            )
            or (result.get("opened") is True and result.get("bound") is True)
            or "已开启支付宝支付功能" in raw
        )
        if ready:
            return {"ready": True, "opened": True, "bound": True, "status": "bound"}
        if code in (200, "200") and status == "applied_unbound":
            raise AlipayWalletNotReady(
                "Alipay AI wallet application is waiting for authorization",
                details={"status": "applied_unbound", "reason": "waiting_for_authorization"},
            )
        if code in (500, "500") and message == "未开通":
            raise AlipayWalletNotReady(
                "Alipay AI wallet is not opened",
                details={"status": "not_opened"},
            )
        raise AlipayWalletNotReady(
            "Alipay AI wallet readiness could not be confirmed",
            details={"status": status or "unknown"},
        )

    @staticmethod
    def _business_session_id(value: Optional[str]) -> str:
        session_id = str(value or os.environ.get("AIPAY_SESSION_ID") or "").strip()
        if not session_id:
            raise AlipayRequestContextMissing(
                "A real runtime business session ID is required for Alipay payments"
            )
        if not SAFE_IDENTIFIER_RE.fullmatch(session_id) or session_id.startswith("mpay_alipay_"):
            raise AlipayRequestContextInvalid(
                "The Alipay business session ID is invalid or refers to a local payment session"
            )
        return session_id

    def start_402(
        self,
        resource_url: str,
        payment_needed: Any = None,
        *,
        requirement: Optional[Dict[str, Any]] = None,
        data: Optional[str] = None,
        request_id: Optional[str] = None,
        method: str = "POST",
        request_body: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        business_session_id: Optional[str] = None,
        intent_summary: str = "Alipay payment",
        timeout: float = 1800,
        context: Optional[Dict[str, Any]] = None,
    ) -> AlipayPaymentSession:
        if payment_needed is None and requirement is not None:
            payment_needed = (requirement.get("extra") or {}).get("payment_needed_header")
        if data is not None and request_body is None:
            request_body = data
        if not isinstance(payment_needed, str) or not payment_needed or len(payment_needed) > MAX_HEADER_BYTES * 2:
            raise AlipayProtocolError("Payment-Needed is missing or too large")
        requirement_data = decode_a402_json(payment_needed, name="Payment-Needed")
        protocol = requirement_data.get("protocol") if isinstance(requirement_data.get("protocol"), dict) else {}
        amount = str(protocol.get("amount")) if protocol.get("amount") is not None else None
        currency = str(protocol.get("currency")) if protocol.get("currency") is not None else None
        business_session_id = self._business_session_id(business_session_id)
        request_id = request_id or f"req_{uuid.uuid4().hex}"
        _safe_identifier(request_id, "request ID")
        now = time.time()
        session = AlipayPaymentSession(
            payment_session_id=f"mpay_alipay_{uuid.uuid4().hex}",
            business_session_id=business_session_id, amount=amount, currency=currency,
            status="pending",
            request_id=request_id, resource_url=resource_url, method=method.upper(),
            request_body=request_body, data=request_body, request_headers=headers or {}, intent_summary=intent_summary,
            context=context or {}, created_at=_iso(now), updated_at=_iso(now), expires_at=_iso(now + timeout),
        )
        self._save(session)
        challenge_dir = self.config_dir / "alipay-challenges"
        challenge_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(challenge_dir, 0o700)
        challenge_path = challenge_dir / f"{session.payment_session_id}.needed"
        fd = os.open(str(challenge_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(payment_needed)
        except Exception:
            try:
                challenge_path.unlink()
            except OSError:
                pass
            raise
        session = self._update(session, challenge_path=str(challenge_path))
        try:
            self.runner([
                "payment-intent", "--session-id", business_session_id,
                "--intent-summary", intent_summary, "--framework", self.framework,
            ])
            self.check_wallet()
            args = [
                "402-buyer-pay", "--file", str(challenge_path), "--resource-url", resource_url,
                "--resource-type", "http", "--session-id", business_session_id,
                "--intent-summary", intent_summary, "--framework", self.framework,
            ]
            if method:
                args += ["--method", method.upper()]
            if request_body is not None:
                args += ["--data", request_body]
            for key, value in (headers or {}).items():
                if key.lower() not in {"payment-proof", "authorization"}:
                    args += ["--header", f"{key}:{value}"]
            parsed = parse_alipay_cli_output(list(self.runner(args)))
        except AlipayWalletNotReady:
            raise
        except (AlipayCliNotFound, AlipayCliFailed):
            raise
        except Exception as exc:
            return self._update(session, status="unknown", last_error_code="alipay_payment_state_unknown", last_error="Alipay payment result is unknown")
        changes: Dict[str, Any] = {}
        for key in ("out_shake_no", "trade_no", "out_trade_no", "payment_url", "media_paths"):
            if parsed.get(key):
                changes[key] = parsed[key]
        changes["status"] = "completed" if parsed.get("normalized_status") == "completed" else "pending"
        return self._update(session, **changes)

    def resume(self, identifier: str) -> AlipayPaymentSession:
        session = self.get_session(identifier)
        if session.status in {"completed", "rejected", "expired"}:
            return session
        query_args = ["402-query-payment-status"]
        if session.out_shake_no:
            query_args += ["--out-shake-no", session.out_shake_no]
        elif session.trade_no:
            query_args += ["--trade-no", session.trade_no]
        else:
            return self._update(session, status="unknown", last_error_code="alipay_payment_state_unknown", last_error="Alipay session has no recoverable payment number")
        query_args += ["--resource-url", session.resource_url, "--resource-type", "http"]
        if session.method:
            query_args += ["--method", session.method]
        if session.request_body is not None:
            query_args += ["--data", session.request_body]
        for key, value in session.request_headers.items():
            if key.lower() not in {"payment-proof", "authorization"}:
                query_args += ["--header", f"{key}:{value}"]
        try:
            parsed = parse_alipay_cli_output(list(self.runner(query_args)))
        except (AlipayCliNotFound, AlipayCliFailed):
            return self._update(session, status="unknown", last_error_code="alipay_payment_state_unknown", last_error="Alipay payment result is unknown")
        status = parsed.get("normalized_status")
        if status == "pending":
            return self._update(session, status="pending", last_error_code=None, last_error=None)
        if status == "rejected":
            return self._update(session, status="rejected", last_error_code="alipay_payment_rejected", last_error="Alipay payment was rejected")
        if status != "completed":
            return self._update(session, status="unknown", last_error_code="alipay_payment_state_unknown", last_error="Alipay returned an unknown payment state")
        result: Any = None
        objects = _json_objects([parsed.get("raw", "")])
        if objects:
            result = objects[-1].get("result", objects[-1].get("data", objects[-1]))
        return self._update(session, status="completed", result=result, last_error_code=None, last_error=None)

    def resume_alipay_payment(self, identifier: str) -> AlipayPaymentSession:
        """Deprecated compatibility alias."""
        return self.resume(identifier)

    def status(self, identifier: str) -> AlipayPaymentSession:
        return self.get_session(identifier)

    def fulfill(self, identifier: str) -> AlipayPaymentSession:
        return self.resume(identifier)

    def pay_402(self, resource_url: str, requirement: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
        session = self.start_402(resource_url, requirement=requirement, **kwargs)
        deadline = time.monotonic() + float(kwargs.get("timeout") or requirement.get("maxTimeoutSeconds", 1800))
        while time.monotonic() < deadline:
            session = self.resume(session.payment_session_id)
            if session.status == "completed":
                return {"body": session.result, "payment": {"trade_no": session.trade_no, "out_trade_no": session.out_trade_no, "session_id": session.payment_session_id}}
            if session.status == "rejected":
                raise AlipayPaymentRejected(session.last_error or "Alipay payment was rejected")
            time.sleep(max(0.05, float(kwargs.get("poll_interval", 3.0))))
        raise AlipayPaymentTimeout("Alipay payment timed out")

    @staticmethod
    def _extract_body(lines: Sequence[str]) -> Any:
        text = _flatten_cli(lines)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.S)
            if not match:
                return text
            try:
                value = json.loads(match.group(0))
            except json.JSONDecodeError:
                return text
        if not isinstance(value, dict):
            return value
        result = value.get("resourceResponse", value.get("result", value.get("data", value.get("body", value))))
        if isinstance(result, dict) and set(result) >= {"result"}:
            result = result["result"]
        return result

    def close(self) -> None:
        """The subprocess adapter owns no persistent network connection."""
        return None


AlipayClient = AlipayBuyerClient

__all__ = [
    "ALIPAY_NETWORK", "ALIPAY_SCHEME", "AlipayBuyerClient", "AlipayClient",
    "AlipayPaymentSession", "decode_a402_json", "encode_a402_json",
    "parse_alipay_cli_output", "parse_trade_no", "parse_payment_url", "parse_status",
]
