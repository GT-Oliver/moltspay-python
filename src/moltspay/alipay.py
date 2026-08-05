"""Alipay AI Pay buyer client backed by the official ``alipay-bot`` CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

from .exceptions import (
    AlipayCliNotFound, AlipayPaymentRejected, AlipayPaymentTimeout,
    AlipayProtocolError,
)


ALIPAY_SCHEME = "alipay-aipay"
ALIPAY_NETWORK = "alipay"
TRADE_NO_RE = re.compile(r"(?:tradeNo|trade_no|trade-no)\s*[=:]\s*[\"']?(\d{32})(?!\d)", re.I)
TRADE_NO_BARE_RE = re.compile(r"\b(\d{32})\b")
URL_RE = re.compile(r"(?:alipays?://|https?://)[^\s\"'\]`)>」]+", re.I)


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
        challenge = challenge_dir / f"402_{request_id}.txt"
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
        deadline = time.monotonic() + (timeout or float(requirement.get("maxTimeoutSeconds", 1800)))
        query = ["402-query-payment-status", "-t", trade_no, "-r", resource_url, "-m", method]
        if data:
            query.extend(["-d", data])
        final_lines: list[str] = []
        while time.monotonic() < deadline:
            final_lines = self.runner(query)
            status = parse_status(final_lines)
            if status == "paid":
                break
            if status == "rejected":
                raise AlipayPaymentRejected(f"Alipay payment rejected: {trade_no}")
            time.sleep(max(0.05, poll_interval))
        else:
            raise AlipayPaymentTimeout(f"Alipay payment timed out: {trade_no}")
        body = self._extract_body(final_lines)
        try:
            self.runner(["402-buyer-fulfillment-ack", "-t", trade_no])
        except Exception:
            pass
        return {
            "body": body,
            "payment": {"trade_no": trade_no, "out_trade_no": request_id, "payment_url": payment_url},
        }

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
