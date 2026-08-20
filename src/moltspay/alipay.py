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
A402_PROVIDER_STATUS = {
    "INIT": "pending",
    "OPEN_LINK_CREATED": "pending",
    "TRADE_CREATED": "pending",
    "PAYING": "processing",
    "BIND": "processing",
    "PAID": "paid",
    "SUCCESS": "paid",
    "VALIDATED": "paid",
    "FAILED": "rejected",
    "EXPIRED": "expired",
    "CLOSED": "rejected",
}
SENSITIVE_REPLAY_HEADERS = {
    "authorization", "proxy-authorization", "payment-proof", "cookie", "set-cookie",
}


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


def _bounded_protocol_text(value: Any, label: str, limit: int = 1024) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(char) < 32 for char in value):
        raise AlipayProtocolError(f"Invalid {label}")
    return value


def _bill_metadata(protocol: Dict[str, Any]) -> Dict[str, str]:
    metadata: Dict[str, str] = {}
    out_trade_no = protocol.get("out_trade_no") or protocol.get("outTradeNo")
    if out_trade_no is not None:
        if not isinstance(out_trade_no, str) or not TRADE_NO_RE.fullmatch(out_trade_no):
            raise AlipayProtocolError("Invalid Alipay merchant order number")
        metadata["out_trade_no"] = out_trade_no
    for field, limit in (("pay_before", 128), ("service_id", 256), ("resource_id", 1024)):
        value = _bounded_protocol_text(protocol.get(field), f"Alipay {field}", limit)
        if value is not None:
            metadata[field] = value
    return metadata


def _safe_replay_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    safe: Dict[str, str] = {}
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.strip().lower() in SENSITIVE_REPLAY_HEADERS:
            continue
        safe[key] = value
    return safe


def _sanitize_diagnostic(value: Any, limit: int = 512) -> Optional[str]:
    if not isinstance(value, (str, int, float)):
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return None
    text = re.sub(
        r"(?i)\b(authorization|payment-proof|access[_-]?token|refresh[_-]?token|"
        r"wallet[_-]?(?:token|credential|secret))\s*[:=]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    return text[:limit]


class AlipayPaymentSession(BaseModel):
    """Recoverable local A402 session.

    Sensitive challenge/proof data is deliberately not part of this model.
    """

    model_config = ConfigDict(extra="ignore")

    payment_session_id: str
    business_session_id: str = ""
    amount: Optional[str] = None
    currency: Optional[str] = None
    status: Literal[
        "created", "pending", "processing", "paid", "fulfilling",
        "completed", "rejected", "expired", "unknown",
    ] = "created"
    request_id: str = ""
    resource_url: str = ""
    method: str = "POST"
    request_body: Optional[str] = None
    data: Optional[str] = None
    request_headers: Dict[str, str] = Field(default_factory=dict)
    out_shake_no: Optional[str] = None
    trade_no: Optional[str] = None
    out_trade_no: Optional[str] = None
    pay_before: Optional[str] = None
    service_id: Optional[str] = None
    resource_id: Optional[str] = None
    provider_status: Optional[str] = None
    provider_code: Optional[str] = None
    provider_message: Optional[str] = None
    resource_status_code: Optional[int] = None
    fulfillment_status: Optional[str] = None
    order_status: Optional[str] = None
    sync_source: Optional[str] = None
    last_synced_at: Optional[str] = None
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
    if result.get("out_shake_no") and not OUT_SHAKE_NO_RE.fullmatch(str(result["out_shake_no"])):
        result.pop("out_shake_no", None)
    for key in ("trade_no", "out_trade_no"):
        if result.get(key) and not TRADE_NO_RE.fullmatch(str(result[key])):
            result.pop(key, None)
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
    provider_status = str(result.get("status", "")).strip().upper()
    if provider_status:
        result["provider_status"] = provider_status
    result["normalized_status"] = A402_PROVIDER_STATUS.get(provider_status, "unknown")
    provider_code = _value(objects, "errorCode", "error_code", "subCode", "sub_code", "code")
    provider_message = _value(objects, "errorMessage", "error_message", "message", "msg", "error")
    if provider_code:
        result["provider_code"] = _sanitize_diagnostic(provider_code, 128)
    if provider_message:
        result["provider_message"] = _sanitize_diagnostic(provider_message)
    result["raw"] = raw
    return result


def _resource_outcome(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Extract explicit resource/receipt evidence without retaining CLI output."""
    objects = _json_objects([str(parsed.get("raw") or "")])
    response: Any = None
    fulfillment_status: Optional[str] = None
    candidates: List[Dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            candidates.append(value)
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)

    for item in objects:
        collect(item)
    for obj in reversed(candidates):
        for key in ("fulfillmentStatus", "fulfillment_status", "ackStatus", "ack_status"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                fulfillment_status = value.strip().upper()
                break
        for key in ("resourceResponse", "resource_response"):
            if key in obj:
                response = obj[key]
                break
        if response is not None:
            break

    raw = str(parsed.get("raw") or "")
    upper = raw.upper()
    explicit_ack_failure = bool(re.search(r"FULFILLMENT\s+ACK\s+FAIL", upper)) or "履约回执失败" in raw
    if fulfillment_status in {"FAILED", "FAIL", "ERROR", "REJECTED"}:
        explicit_ack_failure = True

    attempted = response is not None
    status_code: Optional[int] = None
    body: Any = None
    success_flag = False
    if isinstance(response, dict):
        for key in ("statusCode", "status_code", "httpStatus", "http_status", "status"):
            value = response.get(key)
            try:
                status_code = int(value)
            except (TypeError, ValueError):
                continue
            break
        for key in ("body", "result", "data", "content"):
            if key in response:
                body = response[key]
                break
        success_flag = response.get("success") is True or response.get("ok") is True
    elif response is not None:
        body = response

    marker = re.search(r"RESOURCE\s+RESPONSE\s+STATUS\s*[:=]?\s*(\d{3})", upper)
    if marker:
        attempted = True
        status_code = int(marker.group(1))
    has_body = body not in (None, "", b"", {}, [])
    resource_succeeded = has_body and (
        (status_code is not None and 200 <= status_code < 300) or success_flag
    )
    fulfillment_succeeded = resource_succeeded and not explicit_ack_failure
    if fulfillment_status in {"SUCCESS", "SUCCEEDED", "COMPLETED", "ACKED"}:
        fulfillment_succeeded = resource_succeeded
    return {
        "attempted": attempted,
        "resource_succeeded": resource_succeeded,
        "fulfillment_succeeded": fulfillment_succeeded,
        "result": body,
        "resource_status_code": status_code,
        "fulfillment_status": fulfillment_status,
    }


def parse_trade_no(lines: Sequence[str]) -> Optional[str]:
    return parse_alipay_cli_output(lines).get("trade_no")


def parse_payment_url(lines: Sequence[str]) -> Optional[str]:
    parsed = parse_alipay_cli_output(lines).get("payment_url")
    if parsed:
        return parsed.rstrip(")]}> `")
    match = re.search(r"(?:支付方式|payment\s*(?:url|link)|url)\s*[:：=]\s*((?:https?://|alipays?://)[^\s\"']+)", _flatten_cli(lines), re.I)
    return match.group(1).rstrip(")]}> `") if match else None


def parse_status(lines: Sequence[str]) -> str:
    return {
        "paid": "paid", "rejected": "rejected", "expired": "rejected",
        "pending": "pending", "processing": "pending", "unknown": "unknown",
    }.get(
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
        provider_expired = updated.status == "expired" and updated.provider_status == "EXPIRED"
        if updated.status in {"completed", "rejected"} or provider_expired:
            if not updated.challenge_path:
                return updated
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
            item = self._effective_session(item)
            if status and item.status != status:
                continue
            items.append(item)
        return sorted(items, key=lambda item: item.created_at, reverse=True)[: max(1, min(limit, 100))]

    def _effective_session(self, session: AlipayPaymentSession) -> AlipayPaymentSession:
        """Derive local expiry for display without mutating persisted state."""
        # Once payment is confirmed, the checkout deadline must not erase the
        # ability to recover an idempotent resource delivery.
        if session.status in {"paid", "fulfilling", "completed", "rejected", "expired"}:
            return session
        try:
            expired = datetime.fromisoformat(session.expires_at.replace("Z", "+00:00")).timestamp() <= time.time()
        except ValueError:
            expired = True
        return session.model_copy(update={
            "status": "expired",
            "last_error_code": "alipay_payment_timeout",
            "last_error": "Alipay payment session expired",
        }) if expired else session

    def get_session(self, identifier: str) -> AlipayPaymentSession:
        return self._effective_session(self._load(identifier))

    def reconcile_order(
        self, identifier: str, order: Dict[str, Any], *, source: str = "provider_order_api",
    ) -> AlipayPaymentSession:
        """Persist a safe provider-order observation into its local session."""
        session = self._load(identifier)
        out_trade_no = order.get("outTradeNo") or order.get("out_trade_no")
        if not isinstance(out_trade_no, str) or out_trade_no != session.out_trade_no:
            raise AlipayProtocolError("Provider returned a conflicting Alipay merchant order number")
        trade_no = order.get("tradeNo") or order.get("trade_no")
        if trade_no and session.trade_no and trade_no != session.trade_no:
            raise AlipayProtocolError("Provider returned a conflicting Alipay trade number")

        order_status = str(order.get("orderStatus") or order.get("order_status") or "unknown").lower()
        payment_status = str(order.get("paymentStatus") or order.get("payment_status") or "unknown").lower()
        fulfillment_status = order.get("fulfillmentStatus") or order.get("fulfillment_status")
        result_present = "result" in order
        changes: Dict[str, Any] = {
            "order_status": order_status,
            "sync_source": source,
            "last_synced_at": _iso(time.time()),
            "provider_code": None,
            "provider_message": None,
        }
        if trade_no and not session.trade_no:
            changes["trade_no"] = trade_no
        if isinstance(fulfillment_status, str):
            changes["fulfillment_status"] = fulfillment_status

        if order_status == "completed":
            changes.update(
                status="completed",
                last_error_code=None,
                last_error=None,
            )
            if result_present:
                changes["result"] = order.get("result")
        elif order_status == "delivery_failed":
            changes.update(
                status="fulfilling",
                last_error_code=order.get("errorCode") or order.get("error_code")
                or "service_execution_failed_after_payment",
                last_error="Provider confirmed that service delivery failed after payment",
            )
            if result_present:
                changes["result"] = order.get("result")
        elif order_status == "executing":
            changes.update(
                status="fulfilling",
                last_error_code=None,
                last_error=None,
            )
        elif order_status == "verified" or payment_status == "paid":
            changes.update(status="paid", last_error_code=None, last_error=None)
        elif order_status == "expired":
            changes.update(
                status="expired",
                last_error_code="alipay_payment_timeout",
                last_error="Provider order expired before payment was confirmed",
            )
        elif order_status == "offered" or payment_status == "pending":
            changes.update(status="pending", last_error_code=None, last_error=None)
        else:
            changes.update(
                status="unknown",
                last_error_code="alipay_payment_state_unknown",
                last_error="Provider order state is unknown",
            )
        return self._update(session, **changes)

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

    def _recover_bill_metadata(self, session: AlipayPaymentSession) -> AlipayPaymentSession:
        """Backfill safe bill fields for sessions created before they were persisted."""
        if all((session.out_trade_no, session.pay_before, session.service_id, session.resource_id)):
            return session
        challenge = self.config_dir / "alipay-challenges" / f"{session.payment_session_id}.needed"
        try:
            if not challenge.is_file() or challenge.is_symlink() or challenge.stat().st_size > MAX_HEADER_BYTES * 2:
                return session
            needed = decode_a402_json(challenge.read_text(encoding="utf-8"), name="Payment-Needed")
            protocol = needed.get("protocol") if isinstance(needed.get("protocol"), dict) else {}
            recovered = _bill_metadata(protocol)
        except (OSError, UnicodeError, AlipayProtocolError):
            return session
        changes = {
            key: value for key, value in recovered.items()
            if getattr(session, key, None) in (None, "")
        }
        return self._update(session, **changes) if changes else session

    def _mark_query_unknown(
        self, session: AlipayPaymentSession, payment_message: str,
    ) -> AlipayPaymentSession:
        if session.status in {"paid", "fulfilling"}:
            return self._update(
                session, status=session.status,
                last_error_code="alipay_fulfillment_state_unknown",
                last_error="Alipay payment is confirmed but fulfillment state is unknown",
            )
        return self._update(
            session, status="unknown", last_error_code="alipay_payment_state_unknown",
            last_error=payment_message,
        )

    def _apply_cli_result(
        self, session: AlipayPaymentSession, parsed: Dict[str, Any], *, initiation: bool = False,
    ) -> AlipayPaymentSession:
        changes: Dict[str, Any] = {}
        for key in ("out_shake_no", "trade_no", "out_trade_no"):
            value = parsed.get(key)
            current = getattr(session, key)
            if value and current and value != current:
                return self._update(
                    session, status="unknown", provider_status=parsed.get("provider_status"),
                    last_error_code="alipay_order_mismatch",
                    last_error=f"Alipay returned a conflicting {key}",
                )
            if value and not current:
                changes[key] = value
        for key in ("payment_url", "media_paths", "provider_status"):
            if parsed.get(key) is not None:
                changes[key] = parsed[key]
        # Diagnostics describe only the latest CLI response. A clean response
        # must clear a replay/error left by an earlier query.
        changes["provider_code"] = parsed.get("provider_code")
        changes["provider_message"] = parsed.get("provider_message")

        outcome = _resource_outcome(parsed)
        if outcome["resource_status_code"] is not None:
            changes["resource_status_code"] = outcome["resource_status_code"]
        if outcome["fulfillment_status"] is not None:
            changes["fulfillment_status"] = outcome["fulfillment_status"]

        status = parsed.get("normalized_status", "unknown")
        if outcome["fulfillment_succeeded"]:
            # A non-empty 2xx resource response from the official query command
            # is stronger completion evidence than a missing wrapper status.
            changes.update(
                status="completed", result=outcome["result"],
                last_error_code=None, last_error=None,
            )
        elif status in {"pending", "processing"}:
            if session.status in {"paid", "fulfilling"}:
                changes.update(
                    status=session.status, last_error_code="alipay_fulfillment_state_unknown",
                    last_error="Alipay payment is confirmed but fulfillment did not advance",
                )
            else:
                next_status = "processing" if session.status == "processing" else status
                changes.update(status=next_status, last_error_code=None, last_error=None)
        elif status == "rejected":
            if session.status in {"paid", "fulfilling"}:
                changes.update(
                    status=session.status, last_error_code="alipay_fulfillment_state_unknown",
                    last_error=parsed.get("provider_message")
                    or "Alipay returned a terminal state after payment was confirmed",
                )
            else:
                changes.update(
                    status="rejected", last_error_code="alipay_payment_rejected",
                    last_error=parsed.get("provider_message") or "Alipay payment was rejected",
                )
        elif status == "expired":
            if session.status in {"paid", "fulfilling"}:
                changes.update(
                    status=session.status, last_error_code="alipay_fulfillment_state_unknown",
                    last_error=parsed.get("provider_message")
                    or "Alipay returned an expired state after payment was confirmed",
                )
            else:
                changes.update(
                    status="expired", last_error_code="alipay_payment_timeout",
                    last_error=parsed.get("provider_message") or "Alipay payment expired",
                )
        elif status == "paid":
            if outcome["attempted"]:
                changes.update(
                    status="fulfilling", last_error_code="alipay_fulfillment_incomplete",
                    last_error=parsed.get("provider_message")
                    or "Alipay payment is confirmed but resource fulfillment is incomplete",
                )
            else:
                changes.update(
                    status="fulfilling" if session.status == "fulfilling" else "paid",
                    last_error_code=None, last_error=None,
                )
        elif initiation and any(parsed.get(key) for key in ("out_shake_no", "trade_no", "payment_url", "media_paths")):
            # Some alipay-bot versions emit only the customer-facing QR/link on
            # initiation. That is a valid pending state, not an unknown state.
            changes.update(status="pending", last_error_code=None, last_error=None)
        else:
            if session.status in {"paid", "fulfilling"}:
                changes.update(
                    status=session.status, last_error_code="alipay_fulfillment_state_unknown",
                    last_error=parsed.get("provider_message")
                    or "Alipay payment is confirmed but fulfillment state is unknown",
                )
            else:
                changes.update(
                    status="unknown", last_error_code="alipay_payment_state_unknown",
                    last_error=parsed.get("provider_message") or "Alipay returned an unknown payment state",
                )
        return self._update(session, **changes)

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
        bill_metadata = _bill_metadata(protocol)
        business_session_id = self._business_session_id(business_session_id)
        request_id = request_id or f"req_{uuid.uuid4().hex}"
        _safe_identifier(request_id, "request ID")
        now = time.time()
        session = AlipayPaymentSession(
            payment_session_id=f"mpay_alipay_{uuid.uuid4().hex}",
            business_session_id=business_session_id, amount=amount, currency=currency,
            status="pending",
            request_id=request_id, resource_url=resource_url, method=method.upper(),
            request_body=request_body, data=request_body, request_headers=_safe_replay_headers(headers), intent_summary=intent_summary,
            context=context or {}, created_at=_iso(now), updated_at=_iso(now), expires_at=_iso(now + timeout),
            **bill_metadata,
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
        return self._apply_cli_result(session, parsed, initiation=True)

    def resume(self, identifier: str) -> AlipayPaymentSession:
        # Do not call get_session() here: its local display timeout is not an
        # authoritative Alipay payment state. A user may pay near the deadline
        # and still need the official query command to recover fulfillment.
        session = self._load(identifier)
        if session.status in {"completed", "rejected"}:
            return session
        if session.status == "expired" and session.provider_status == "EXPIRED":
            return session
        session = self._recover_bill_metadata(session)
        query_args = ["402-query-payment-status"]
        if session.out_shake_no:
            query_args += ["--out-shake-no", session.out_shake_no]
        elif session.trade_no:
            query_args += ["--trade-no", session.trade_no]
        else:
            return self._mark_query_unknown(session, "Alipay session has no recoverable payment number")
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
            return self._mark_query_unknown(session, "Alipay payment result is unknown")
        return self._apply_cli_result(session, parsed)

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
