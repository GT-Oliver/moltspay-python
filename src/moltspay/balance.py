"""Custodial balance rail: SQLite ledger and HTTP client."""

from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Union

import httpx

from .exceptions import InsufficientBalance, PaymentError
from .models import BuyerBalance, PaymentResult


BALANCE_NETWORK = "balance"
BALANCE_SCHEME = "balance"
BALANCE_AUTH_DOMAIN = "moltspay-balance-auth:v1"
BALANCE_AUTH_MAX_SKEW_SECONDS = 300
DEFAULT_SINGLE_LIMIT_SAT = 500
DEFAULT_DAILY_LIMIT_SAT = 1000


def to_sat(amount: Union[str, int, float, Decimal]) -> int:
    """Convert a decimal currency amount to integer cents without float drift."""
    try:
        value = Decimal(str(amount).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f'Invalid amount "{amount}"') from exc
    scaled = value * 100
    if value < 0 or scaled != scaled.to_integral_value():
        raise ValueError(f'Invalid amount "{amount}": expected a non-negative decimal with <= 2 places')
    return int(scaled)


def from_sat(value: int) -> str:
    return f"{Decimal(value) / Decimal(100):.2f}"


def build_deduct_message(buyer_id: str, request_id: str, service: str, timestamp: int) -> str:
    return "\n".join((BALANCE_AUTH_DOMAIN, "balance-deduct", buyer_id, request_id, service, str(timestamp)))


class BalanceLedger:
    """Atomic, idempotent ledger matching the Node balance-rail semantics."""

    def __init__(
        self,
        db_path: Union[str, Path] = ":memory:",
        currency: str = "USD",
        default_single_limit_sat: int = DEFAULT_SINGLE_LIMIT_SAT,
        default_daily_limit_sat: int = DEFAULT_DAILY_LIMIT_SAT,
    ):
        self.db_path = str(db_path)
        self.currency = currency
        self.default_single_limit_sat = default_single_limit_sat
        self.default_daily_limit_sat = default_daily_limit_sat
        self.db = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        try:
            if self.db_path != ":memory:":
                self.db.execute("PRAGMA journal_mode = WAL")
            self._init_schema()
        except Exception:
            self.db.close()
            raise

    def _init_schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS buyers (
          buyer_id TEXT PRIMARY KEY, display_name TEXT,
          balance_sat INTEGER NOT NULL DEFAULT 0,
          total_topup_sat INTEGER NOT NULL DEFAULT 0,
          total_spent_sat INTEGER NOT NULL DEFAULT 0,
          daily_limit_sat INTEGER NOT NULL, single_limit_sat INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'active', signer_address TEXT,
          wechat_openid TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS ledger_transactions (
          id TEXT PRIMARY KEY, buyer_id TEXT NOT NULL REFERENCES buyers(buyer_id),
          type TEXT NOT NULL, amount_sat INTEGER NOT NULL, service TEXT,
          description TEXT, request_id TEXT, external_ref TEXT,
          refunds_tx_id TEXT, status TEXT NOT NULL DEFAULT 'completed',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_request_id
          ON ledger_transactions(request_id) WHERE request_id IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_external_ref
          ON ledger_transactions(external_ref) WHERE external_ref IS NOT NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_ledger_refunds_tx
          ON ledger_transactions(refunds_tx_id) WHERE refunds_tx_id IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_ledger_buyer_time
          ON ledger_transactions(buyer_id, created_at);
        CREATE TABLE IF NOT EXISTS ledger_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        existing = self.db.execute("SELECT value FROM ledger_meta WHERE key='currency'").fetchone()
        if existing and existing["value"] != self.currency:
            raise ValueError(f"Balance ledger currency mismatch: db={existing['value']} config={self.currency}")
        if not existing:
            self.db.execute("INSERT INTO ledger_meta(key, value) VALUES('currency', ?)", (self.currency,))

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    @staticmethod
    def _dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        return dict(row) if row is not None else None

    def get_buyer(self, buyer_id: str) -> Optional[Dict[str, Any]]:
        return self._dict(self.db.execute("SELECT * FROM buyers WHERE buyer_id=?", (buyer_id,)).fetchone())

    def get_or_create_buyer(self, buyer_id: str, display_name: Optional[str] = None) -> Dict[str, Any]:
        buyer = self.get_buyer(buyer_id)
        if buyer:
            return buyer
        self.db.execute(
            "INSERT INTO buyers(buyer_id, display_name, daily_limit_sat, single_limit_sat) VALUES(?,?,?,?)",
            (buyer_id, display_name, self.default_daily_limit_sat, self.default_single_limit_sat),
        )
        return self.get_buyer(buyer_id) or {}

    def bind_signer(self, buyer_id: str, address: str) -> Dict[str, Any]:
        normalized = address.lower()
        buyer = self.get_or_create_buyer(buyer_id)
        existing = buyer.get("signer_address")
        if existing and existing != normalized:
            return {"bound": False, "conflict": True, "existing": existing}
        if not existing:
            self.db.execute("UPDATE buyers SET signer_address=?, updated_at=datetime('now') WHERE buyer_id=?", (normalized, buyer_id))
        return {"bound": True, "conflict": False, "existing": existing}

    def bind_openid(self, buyer_id: str, openid: str) -> Dict[str, Any]:
        buyer = self.get_or_create_buyer(buyer_id)
        existing = buyer.get("wechat_openid")
        if existing and existing != openid:
            return {"bound": False, "conflict": True, "existing": existing}
        if not existing:
            self.db.execute("UPDATE buyers SET wechat_openid=?, updated_at=datetime('now') WHERE buyer_id=?", (openid, buyer_id))
        return {"bound": True, "conflict": False, "existing": existing}

    def spent_today_sat(self, buyer_id: str) -> int:
        row = self.db.execute("""
          SELECT COALESCE(SUM(CASE type WHEN 'deduct' THEN amount_sat ELSE -amount_sat END), 0) spent
          FROM ledger_transactions WHERE buyer_id=? AND type IN ('deduct','refund')
          AND date(created_at)=date('now')
        """, (buyer_id,)).fetchone()
        return max(0, int(row["spent"] if row else 0))

    def check_deduct(self, buyer_id: str, amount_sat: int) -> Dict[str, Any]:
        buyer = self.get_buyer(buyer_id)
        if not buyer:
            return {"success": False, "error": "buyer_not_found"}
        if buyer["status"] != "active":
            return {"success": False, "error": "buyer_not_active"}
        if amount_sat > buyer["single_limit_sat"]:
            return {"success": False, "error": "exceeds_single_limit", "limit_sat": buyer["single_limit_sat"]}
        if self.spent_today_sat(buyer_id) + amount_sat > buyer["daily_limit_sat"]:
            return {"success": False, "error": "exceeds_daily_limit", "limit_sat": buyer["daily_limit_sat"]}
        if buyer["balance_sat"] < amount_sat:
            return {"success": False, "error": "insufficient_balance", "balance_sat": buyer["balance_sat"]}
        return {"success": True, "balance_sat": buyer["balance_sat"]}

    def deduct(self, buyer_id: str, amount_sat: int, request_id: Optional[str] = None, service: Optional[str] = None, description: Optional[str] = None) -> Dict[str, Any]:
        if amount_sat <= 0:
            raise ValueError("deduct amount_sat must be positive")
        if request_id:
            prior = self.db.execute("SELECT * FROM ledger_transactions WHERE request_id=?", (request_id,)).fetchone()
            if prior:
                buyer = self.get_buyer(prior["buyer_id"]) or {}
                return {"success": True, "tx_id": prior["id"], "replayed": True, "balance_sat": buyer.get("balance_sat")}
        with self._transaction():
            check = self.check_deduct(buyer_id, amount_sat)
            if not check["success"]:
                return check
            changed = self.db.execute("""
              UPDATE buyers SET balance_sat=balance_sat-?, total_spent_sat=total_spent_sat+?,
              updated_at=datetime('now') WHERE buyer_id=? AND balance_sat>=? AND status='active'
            """, (amount_sat, amount_sat, buyer_id, amount_sat)).rowcount
            if changed != 1:
                return {"success": False, "error": "insufficient_balance"}
            tx_id = f"btx_{uuid.uuid4()}"
            self.db.execute("""
              INSERT INTO ledger_transactions(id,buyer_id,type,amount_sat,service,description,request_id)
              VALUES(?,?,'deduct',?,?,?,?)
            """, (tx_id, buyer_id, amount_sat, service, description, request_id))
        buyer = self.get_buyer(buyer_id) or {}
        return {"success": True, "tx_id": tx_id, "balance_sat": buyer.get("balance_sat")}

    def topup(self, buyer_id: str, amount_sat: int, external_ref: str, description: Optional[str] = None) -> Dict[str, Any]:
        if amount_sat <= 0:
            raise ValueError("topup amount_sat must be positive")
        prior = self.db.execute("SELECT * FROM ledger_transactions WHERE external_ref=?", (external_ref,)).fetchone()
        if prior:
            buyer = self.get_buyer(prior["buyer_id"]) or {}
            return {"tx_id": prior["id"], "balance_sat": buyer.get("balance_sat"), "replayed": True}
        self.get_or_create_buyer(buyer_id)
        tx_id = f"btx_{uuid.uuid4()}"
        with self._transaction():
            self.db.execute("""
              UPDATE buyers SET balance_sat=balance_sat+?, total_topup_sat=total_topup_sat+?,
              updated_at=datetime('now') WHERE buyer_id=?
            """, (amount_sat, amount_sat, buyer_id))
            self.db.execute("""
              INSERT INTO ledger_transactions(id,buyer_id,type,amount_sat,description,external_ref)
              VALUES(?,?,'topup',?,?,?)
            """, (tx_id, buyer_id, amount_sat, description, external_ref))
        buyer = self.get_buyer(buyer_id) or {}
        return {"tx_id": tx_id, "balance_sat": buyer.get("balance_sat"), "replayed": False}

    def refund(self, deduct_tx_id: str, reason: Optional[str] = None) -> Dict[str, Any]:
        deduct = self.db.execute("SELECT * FROM ledger_transactions WHERE id=?", (deduct_tx_id,)).fetchone()
        if not deduct:
            return {"success": False, "error": "tx_not_found"}
        if deduct["type"] != "deduct":
            return {"success": False, "error": "not_a_deduct"}
        prior = self.db.execute("SELECT * FROM ledger_transactions WHERE refunds_tx_id=?", (deduct_tx_id,)).fetchone()
        if prior:
            buyer = self.get_buyer(deduct["buyer_id"]) or {}
            return {"success": True, "tx_id": prior["id"], "balance_sat": buyer.get("balance_sat"), "replayed": True}
        tx_id = f"btx_{uuid.uuid4()}"
        with self._transaction():
            self.db.execute("""
              UPDATE buyers SET balance_sat=balance_sat+?, total_spent_sat=total_spent_sat-?,
              updated_at=datetime('now') WHERE buyer_id=?
            """, (deduct["amount_sat"], deduct["amount_sat"], deduct["buyer_id"]))
            self.db.execute("UPDATE ledger_transactions SET status='refunded' WHERE id=?", (deduct_tx_id,))
            self.db.execute("""
              INSERT INTO ledger_transactions(id,buyer_id,type,amount_sat,description,refunds_tx_id)
              VALUES(?,?,'refund',?,?,?)
            """, (tx_id, deduct["buyer_id"], deduct["amount_sat"], reason, deduct_tx_id))
        buyer = self.get_buyer(deduct["buyer_id"]) or {}
        return {"success": True, "tx_id": tx_id, "balance_sat": buyer.get("balance_sat"), "replayed": False}

    def list_transactions(self, buyer_id: str, limit: int = 20, offset: int = 0) -> list[Dict[str, Any]]:
        rows = self.db.execute("""
          SELECT * FROM ledger_transactions WHERE buyer_id=?
          ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?
        """, (buyer_id, max(1, min(limit, 100)), max(0, offset))).fetchall()
        return [dict(row) for row in rows]

    def integrity_ok(self) -> bool:
        return self.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "BalanceLedger":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


class BalanceClient:
    """Buyer-side HTTP client for the custodial balance rail."""

    def __init__(self, buyer_id: Optional[str] = None, timeout: Optional[float] = None, http_client: Optional[httpx.Client] = None, account: Any = None):
        self.buyer_id = buyer_id
        self.account = account
        self._owns_client = http_client is None
        self.http = http_client or httpx.Client(timeout=timeout)

    def _buyer(self, buyer_id: Optional[str]) -> str:
        value = buyer_id or self.buyer_id
        if not value:
            raise PaymentError("Balance rail requires buyer_id; call set_buyer_id() first")
        return value

    def get_balance(self, server_url: str, buyer_id: Optional[str] = None) -> BuyerBalance:
        buyer = self._buyer(buyer_id)
        response = self.http.get(f"{server_url.rstrip('/')}/balance", params={"buyer_id": buyer})
        if response.status_code == 404:
            response = self.http.get(f"{server_url.rstrip('/')}/balance/query", params={"buyer_id": buyer})
        response.raise_for_status()
        data = response.json()
        return BuyerBalance(
            buyer_id=buyer,
            currency=data.get("currency", "USD"),
            balance=str(data.get("balance", "0.00")),
            spent_today=str(data.get("spent_today", data.get("today_spent", "0.00"))),
            single_limit=data.get("single_limit"), daily_limit=data.get("daily_limit"),
            status=data.get("status", "active"),
            topup_packs=[str(item) for item in data.get("topupPacks", data.get("topup_packs", []))],
            custom_topup_max=data.get("customTopupMax", data.get("custom_topup_max")),
        )

    def list_transactions(self, server_url: str, buyer_id: Optional[str] = None, limit: int = 20, offset: int = 0) -> list[Dict[str, Any]]:
        response = self.http.get(
            f"{server_url.rstrip('/')}/balance/transactions",
            params={"buyer_id": self._buyer(buyer_id), "limit": limit, "offset": offset},
        )
        response.raise_for_status()
        return response.json().get("transactions", [])

    def create_topup_order(
        self, server_url: str, pack: Optional[str] = None, context: Optional[Dict[str, Any]] = None,
        buyer_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a recoverable WeChat top-up order (Node parity)."""
        buyer = self._buyer(buyer_id)
        body = {"buyer_id": buyer}
        if pack is not None:
            body["pack"] = pack
        if self.account is not None:
            body["signer_address"] = self.account.address.lower()
        response = self.http.post(f"{server_url.rstrip('/')}/balance/topup/order", json=body)
        data = response.json()
        if not response.is_success:
            raise PaymentError(data.get("error", f"Top-up order failed with HTTP {response.status_code}"))
        return data

    def confirm_topup(self, server_url: str, out_trade_no: str) -> Dict[str, Any]:
        response = self.http.post(
            f"{server_url.rstrip('/')}/balance/topup/confirm",
            json={"out_trade_no": out_trade_no},
        )
        data = response.json()
        if not response.is_success:
            return {"credited": False, "reason": data.get("error", f"Confirm failed with HTTP {response.status_code}")}
        return data

    def topup_balance(
        self, server_url: str, amount: str, rail: str, buyer_id: Optional[str] = None,
        tx_hash: Optional[str] = None, chain: Optional[str] = None,
        out_trade_no: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Report an externally settled payment for ledger credit (Node parity)."""
        if str(rail).lower() == "alipay":
            raise PaymentError("Alipay balance top-ups are not supported")
        body: Dict[str, Any] = {"buyer_id": self._buyer(buyer_id), "amount": str(amount), "rail": rail}
        if tx_hash:
            body["tx_hash"] = tx_hash
        if chain:
            body["chain"] = chain
        if out_trade_no:
            body["out_trade_no"] = out_trade_no
        body["external_ref"] = tx_hash or out_trade_no or f"{rail}:{amount}"
        response = self.http.post(f"{server_url.rstrip('/')}/balance/topup", json=body)
        data = response.json()
        if not response.is_success:
            raise PaymentError(data.get("error", f"Balance top-up failed with HTTP {response.status_code}"))
        return data

    def pay(
        self, server_url: str, service_id: str, params: Dict[str, Any], amount: float = 0.0,
        buyer_id: Optional[str] = None, request_id: Optional[str] = None,
    ) -> PaymentResult:
        buyer = self._buyer(buyer_id)
        body = {"service": service_id, "params": params, "rail": "balance"}
        url = f"{server_url.rstrip('/')}/execute"
        initial = self.http.post(url, json=body, headers={"Accept-Payment-Rail": "balance"})
        if initial.status_code != 402:
            if initial.is_success:
                return PaymentResult(success=True, amount=amount, token="BALANCE", service_id=service_id, result=initial.json().get("result", initial.json()))
            raise PaymentError(f"Service error: {initial.status_code} {initial.text}")
        request_id = request_id or str(uuid.uuid4())
        payment_payload: Dict[str, Any] = {"buyer_id": buyer, "request_id": request_id}
        if self.account is not None:
            from eth_account.messages import encode_defunct
            timestamp = int(time.time())
            message = build_deduct_message(buyer, request_id, service_id, timestamp)
            signature = self.account.sign_message(encode_defunct(text=message)).signature.hex()
            payment_payload["auth"] = {"timestamp": timestamp, "signature": signature}
        payload = {
            "x402Version": 2, "scheme": BALANCE_SCHEME, "network": BALANCE_NETWORK,
            "accepted": {"scheme": BALANCE_SCHEME, "network": BALANCE_NETWORK},
            "payload": payment_payload,
        }
        payment = base64.b64encode(json.dumps(payload).encode()).decode()
        paid = self.http.post(url, json=body, headers={"X-Payment": payment})
        if not paid.is_success:
            try:
                error_data = paid.json()
            except (ValueError, json.JSONDecodeError):
                error_data = {}
            error_message = str(error_data.get("error") or paid.text)
            error_code = str(error_data.get("code") or "").lower()
            if error_code == "insufficient_balance" or "insufficient_balance" in error_message.lower():
                details = error_data.get("details") if isinstance(error_data.get("details"), dict) else {}
                raise InsufficientBalance(
                    required=details.get("required"),
                    balance=details.get("balance"),
                    currency=details.get("currency", "CNY"),
                    topup_packs=details.get("topupPacks") or details.get("topup_packs"),
                    message=error_message,
                    details=details,
                )
            raise PaymentError(f"Balance payment failed: {paid.status_code} {error_message}")
        data = paid.json()
        return PaymentResult(
            success=True, amount=amount, token="BALANCE", service_id=service_id,
            result=data.get("result", data), tx_hash=data.get("transaction") or data.get("txHash"),
            facilitator="balance", network="balance", payment={"request_id": request_id, "buyer_id": buyer},
        )

    def close(self) -> None:
        if self._owns_client:
            self.http.close()
