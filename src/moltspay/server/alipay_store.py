"""Durable SQLite state for A402 orders and fulfillment acknowledgements."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import timedelta
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import parse_qs, urlparse

from .order_protection import OrderCapacityError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def proof_hash(payment_proof: str) -> str:
    return hashlib.sha256(payment_proof.encode("utf-8")).hexdigest()


class AlipayOrderStore:
    """SQLite-backed order store with database-level replay protection."""

    def __init__(self, db_path: str = "data/alipay-a402.sqlite", *,
                 max_outstanding_orders: int = 1000,
                 max_outstanding_per_caller: int = 16):
        self.db_path = str(db_path)
        self.max_outstanding_orders = max(1, int(max_outstanding_orders))
        self.max_outstanding_per_caller = max(1, int(max_outstanding_per_caller))
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.db_path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self.db.execute("PRAGMA foreign_keys=ON")
        if self.db_path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS alipay_orders (
          out_trade_no TEXT PRIMARY KEY,
          request_id TEXT NOT NULL,
          caller_id TEXT NOT NULL DEFAULT '',
          kind TEXT NOT NULL CHECK(kind = 'service'),
          skill_id TEXT NOT NULL,
          service_id TEXT,
          amount_fen INTEGER NOT NULL,
          currency TEXT NOT NULL DEFAULT 'CNY',
          resource_id TEXT NOT NULL,
          goods_name TEXT NOT NULL,
          pay_before TEXT NOT NULL,
          trade_no TEXT UNIQUE,
          proof_hash TEXT UNIQUE,
          challenge_header TEXT,
          challenge_claimed_at TEXT,
          status TEXT NOT NULL,
          result_json TEXT,
          error_code TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT,
          UNIQUE(request_id, kind, resource_id)
        );
        CREATE TABLE IF NOT EXISTS alipay_fulfillment_outbox (
          trade_no TEXT PRIMARY KEY,
          out_trade_no TEXT NOT NULL REFERENCES alipay_orders(out_trade_no),
          status TEXT NOT NULL,
          attempt_count INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT NOT NULL,
          last_error_code TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_alipay_orders_request ON alipay_orders(request_id, kind, resource_id);
        """)
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(alipay_orders)").fetchall()
        }
        if "skill_id" not in columns:
            self.db.execute("ALTER TABLE alipay_orders ADD COLUMN skill_id TEXT")
        if "caller_id" not in columns:
            self.db.execute("ALTER TABLE alipay_orders ADD COLUMN caller_id TEXT NOT NULL DEFAULT ''")
        if "challenge_header" not in columns:
            self.db.execute("ALTER TABLE alipay_orders ADD COLUMN challenge_header TEXT")
        if "challenge_claimed_at" not in columns:
            self.db.execute("ALTER TABLE alipay_orders ADD COLUMN challenge_claimed_at TEXT")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        value = dict(row)
        if value.get("result_json"):
            try:
                value["result"] = json.loads(value["result_json"])
            except json.JSONDecodeError:
                value["result"] = None
        value.pop("result_json", None)
        return value

    def get(self, out_trade_no: str) -> Optional[Dict[str, Any]]:
        return self._row(self.db.execute("SELECT * FROM alipay_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone())

    def get_by_request(self, request_id: str, kind: str, resource_id: str) -> Optional[Dict[str, Any]]:
        return self._row(self.db.execute(
            "SELECT * FROM alipay_orders WHERE request_id=? AND kind=? AND resource_id=?",
            (request_id, kind, resource_id),
        ).fetchone())

    def get_by_trade(self, trade_no: str) -> Optional[Dict[str, Any]]:
        return self._row(self.db.execute("SELECT * FROM alipay_orders WHERE trade_no=?", (trade_no,)).fetchone())

    def get_by_proof_hash(self, digest: str) -> Optional[Dict[str, Any]]:
        return self._row(self.db.execute("SELECT * FROM alipay_orders WHERE proof_hash=?", (digest,)).fetchone())

    def public_status(self, out_trade_no: str) -> Optional[Dict[str, Any]]:
        """Return a proof-free, credential-free provider view of one order."""
        order = self.get(out_trade_no)
        if order is None:
            return None
        order_status = str(order.get("status") or "unknown")
        payment_status = {
            "offered": "pending",
            "verified": "paid",
            "executing": "paid",
            "completed": "paid",
            "delivery_failed": "paid",
            "expired": "expired",
        }.get(order_status, "unknown")
        fulfillment_status = {
            "offered": "not_started",
            "verified": "not_started",
            "executing": "executing",
            "delivery_failed": "failed",
            "expired": "not_started",
        }.get(order_status, "unknown")
        if order_status == "completed":
            outbox = None
            if order.get("trade_no"):
                outbox = self.db.execute(
                    "SELECT status FROM alipay_fulfillment_outbox WHERE trade_no=?",
                    (order["trade_no"],),
                ).fetchone()
            outbox_status = str(outbox["status"]) if outbox else "confirmed"
            fulfillment_status = {
                "confirmed": "confirmed",
                "pending": "confirmation_pending",
                "retry_wait": "confirmation_pending",
                "sending": "confirmation_pending",
                "exhausted": "confirmation_failed",
            }.get(outbox_status, "unknown")
        resource_id = str(order.get("resource_id") or "")
        service_values = parse_qs(urlparse(resource_id).query).get("service") or []
        return {
            "out_trade_no": order["out_trade_no"],
            "trade_no": order.get("trade_no"),
            "service": service_values[0] if service_values else None,
            "service_id": order.get("service_id"),
            "amount": f"{int(order['amount_fen']) / 100:.2f}",
            "currency": order.get("currency") or "CNY",
            "order_status": order_status,
            "payment_status": payment_status,
            "fulfillment_status": fulfillment_status,
            "result": order.get("result"),
            "error_code": order.get("error_code"),
            "created_at": order.get("created_at"),
            "updated_at": order.get("updated_at"),
            "completed_at": order.get("completed_at"),
        }

    def cleanup_expired_unpaid(self) -> int:
        """Remove only unpaid, unclaimed rows whose challenge is no longer valid."""
        now = utc_now()
        with self._transaction():
            result = self.db.execute(
                """DELETE FROM alipay_orders
                   WHERE status IN ('offered', 'expired')
                     AND trade_no IS NULL
                     AND (pay_before = '' OR pay_before <= ?)""",
                (now,),
            )
            return int(result.rowcount)

    def outstanding_count(self, caller_id: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) FROM alipay_orders WHERE status='offered'"
        args: tuple[str, ...] = ()
        if caller_id is not None:
            query += " AND caller_id=?"
            args = (caller_id,)
        return int(self.db.execute(query, args).fetchone()[0])

    def create_order(self, *, request_id: str, kind: str, amount_fen: int, resource_id: str, goods_name: str,
                     pay_before: str, skill_id: str, service_id: str,
                     out_trade_no: Optional[str] = None,
                     currency: str = "CNY", caller_id: Optional[str] = None) -> Dict[str, Any]:
        if (
            kind != "service"
            or amount_fen <= 0
            or not isinstance(skill_id, str)
            or not skill_id.strip()
            or not isinstance(service_id, str)
            or not service_id.strip()
        ):
            raise ValueError("invalid Alipay order")
        skill_id = skill_id.strip()
        service_id = service_id.strip()
        trade = out_trade_no or "MPA" + uuid.uuid4().hex[:26].upper()
        now = utc_now()
        with self._transaction():
            self.db.execute(
                """DELETE FROM alipay_orders
                   WHERE status IN ('offered', 'expired') AND trade_no IS NULL
                     AND pay_before <> '' AND pay_before <= ?""", (now,)
            )
            existing = self.db.execute(
                "SELECT * FROM alipay_orders WHERE request_id=? AND kind=? AND resource_id=?",
                (request_id, kind, resource_id),
            ).fetchone()
            if existing:
                existing = self._row(existing)
                immutable = {
                    "skill_id": skill_id, "service_id": service_id,
                    "amount_fen": amount_fen, "currency": currency,
                }
                if any(existing.get(key) != value for key, value in immutable.items()):
                    raise ValueError("Alipay idempotency key conflicts with the existing order")
                return existing
            if self.outstanding_count() >= self.max_outstanding_orders:
                raise OrderCapacityError("alipay_outstanding_limit", "Alipay unpaid order limit reached", 5)
            effective_caller = str(caller_id or "")
            if effective_caller and self.outstanding_count(effective_caller) >= self.max_outstanding_per_caller:
                raise OrderCapacityError("alipay_caller_outstanding_limit", "Alipay caller unpaid order limit reached", 5)
            try:
                self.db.execute(
                    """INSERT INTO alipay_orders
                    (out_trade_no,request_id,caller_id,kind,skill_id,service_id,amount_fen,currency,resource_id,goods_name,pay_before,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (trade, request_id, effective_caller, kind, skill_id, service_id, amount_fen, currency, resource_id, goods_name, pay_before, "offered", now, now),
                )
            except sqlite3.IntegrityError:
                existing = self._row(self.db.execute(
                    "SELECT * FROM alipay_orders WHERE request_id=? AND kind=? AND resource_id=?",
                    (request_id, kind, resource_id),
                ).fetchone())
                if existing:
                    return existing
                raise
        return self.get(trade) or {}

    def claim_challenge(self, out_trade_no: str) -> Dict[str, Any]:
        """Atomically elect one process to produce a missing signed challenge."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._transaction():
            row = self.db.execute("SELECT * FROM alipay_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()
            if row is None:
                return {"state": "not_found"}
            current = self._row(row)
            if current.get("challenge_header"):
                return {"state": "ready", "order": current}
            claimed_at = current.get("challenge_claimed_at")
            if claimed_at:
                try:
                    age = (now - datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))).total_seconds()
                except ValueError:
                    age = 0
                if age < 60:
                    return {"state": "in_progress", "order": current}
            self.db.execute(
                "UPDATE alipay_orders SET challenge_claimed_at=?,updated_at=? WHERE out_trade_no=?",
                (now_text, now_text, out_trade_no),
            )
            return {"state": "claimed", "order": self.get(out_trade_no)}

    def set_challenge(self, out_trade_no: str, header: str, pay_before: str) -> Dict[str, Any]:
        if not isinstance(header, str) or not header or not isinstance(pay_before, str) or not pay_before:
            raise ValueError("invalid Alipay challenge")
        with self._transaction():
            self.db.execute(
                """UPDATE alipay_orders
                   SET challenge_header=COALESCE(challenge_header,?), pay_before=?,
                       challenge_claimed_at=NULL, updated_at=?
                   WHERE out_trade_no=?""",
                (header, pay_before, utc_now(), out_trade_no),
            )
        return self.get(out_trade_no) or {}

    def claim_execution(
        self,
        out_trade_no: str,
        *,
        trade_no: str,
        digest: str,
        resource_id: str,
        skill_id: str,
        service_id: str,
        expected_resource_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Bind proof/trade and atomically acquire the one execution slot."""
        now = utc_now()
        authorized_resource_id = expected_resource_id or resource_id
        with self._transaction():
            row = self.db.execute("SELECT * FROM alipay_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()
            if row is None:
                return {"state": "not_found"}
            if row["skill_id"] != skill_id:
                return {"state": "skill_mismatch", "order": dict(row)}
            if row["service_id"] != service_id:
                return {"state": "service_mismatch", "order": dict(row)}
            if row["resource_id"] != authorized_resource_id or resource_id != authorized_resource_id:
                return {"state": "resource_mismatch", "order": dict(row)}
            if row["trade_no"] and row["trade_no"] != trade_no:
                return {"state": "replay", "reason": "trade_mismatch", "order": dict(row)}
            other = self.db.execute("SELECT * FROM alipay_orders WHERE trade_no=? AND out_trade_no<>?", (trade_no, out_trade_no)).fetchone()
            if other is not None:
                return {"state": "replay", "reason": "trade_used_by_other_order", "order": dict(other)}
            # Alipay may issue a fresh proof for the same trade when the buyer
            # resumes. Once delivery is complete, the verified trade identity
            # is the idempotency boundary; a rotated proof must return the
            # cached result instead of being rejected as a replay.
            if row["status"] == "completed":
                return {"state": "completed", "order": self._row(row)}
            if row["proof_hash"] and row["proof_hash"] != digest:
                return {"state": "replay", "reason": "proof_changed_before_completion", "order": dict(row)}
            if row["pay_before"]:
                try:
                    expiry = datetime.fromisoformat(row["pay_before"].replace("Z", "+00:00"))
                    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                        self.db.execute("UPDATE alipay_orders SET status='expired',updated_at=? WHERE out_trade_no=?", (now, out_trade_no))
                        return {"state": "expired", "order": self.get(out_trade_no)}
                except ValueError:
                    return {"state": "rejected", "order": self._row(row)}
            if row["status"] == "executing":
                return {"state": "executing", "order": self._row(row)}
            if row["status"] not in {"offered", "verified", "unknown"}:
                return {"state": "rejected", "order": self._row(row)}
            self.db.execute(
                "UPDATE alipay_orders SET trade_no=?,proof_hash=?,status='executing',updated_at=? WHERE out_trade_no=?",
                (trade_no, digest, now, out_trade_no),
            )
            return {"state": "claimed", "order": self.get(out_trade_no)}

    def mark_verified(self, out_trade_no: str, trade_no: str, digest: str) -> None:
        with self._transaction():
            self.db.execute("UPDATE alipay_orders SET trade_no=?,proof_hash=?,status='verified',updated_at=? WHERE out_trade_no=? AND status='offered'", (trade_no, digest, utc_now(), out_trade_no))

    def complete(self, out_trade_no: str, result: Any) -> Dict[str, Any]:
        now = utc_now()
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self._transaction():
            row = self.db.execute("SELECT trade_no,status FROM alipay_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()
            if not row:
                raise KeyError("Alipay order not found")
            if row["status"] != "completed":
                self.db.execute("UPDATE alipay_orders SET status='completed',result_json=?,updated_at=?,completed_at=? WHERE out_trade_no=?", (encoded, now, now, out_trade_no))
            trade_no = row["trade_no"]
            if trade_no:
                self.db.execute("INSERT OR IGNORE INTO alipay_fulfillment_outbox(trade_no,out_trade_no,status,next_attempt_at,created_at,updated_at) VALUES(?,?, 'pending', ?, ?, ?)", (trade_no, out_trade_no, now, now, now))
        return self.get(out_trade_no) or {}

    def fail_delivery(self, out_trade_no: str, error_code: str, result: Any = None) -> Dict[str, Any]:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")) if result is not None else None
        with self._transaction():
            self.db.execute("UPDATE alipay_orders SET status='delivery_failed',error_code=?,result_json=?,updated_at=? WHERE out_trade_no=?", (error_code, encoded, utc_now(), out_trade_no))
        return self.get(out_trade_no) or {}

    def list_outbox(self, limit: int = 100) -> list[Dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM alipay_fulfillment_outbox WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?", (utc_now(), max(1, min(limit, 1000)))).fetchall()
        return [dict(row) for row in rows]

    def claim_outbox(self, limit: int = 100) -> list[Dict[str, Any]]:
        """Atomically lease due outbox rows for one worker."""
        claimed: list[Dict[str, Any]] = []
        with self._transaction():
            rows = self.db.execute("SELECT * FROM alipay_fulfillment_outbox WHERE status IN ('pending','retry_wait') AND next_attempt_at<=? ORDER BY next_attempt_at LIMIT ?", (utc_now(), max(1, min(limit, 1000)))).fetchall()
            for row in rows:
                changed = self.db.execute("UPDATE alipay_fulfillment_outbox SET status='sending',updated_at=? WHERE trade_no=? AND status IN ('pending','retry_wait')", (utc_now(), row["trade_no"])).rowcount
                if changed:
                    claimed.append(dict(row))
        return claimed

    def run_fulfillment_once(self, confirm: Any, *, retry_limit: int = 12) -> int:
        processed = 0
        for row in self.claim_outbox():
            processed += 1
            try:
                confirm(row["trade_no"])
            except Exception:
                attempts = int(row.get("attempt_count", 0)) + 1
                if attempts >= retry_limit:
                    self.mark_outbox(row["trade_no"], "exhausted", error_code="alipay_fulfillment_exhausted", error="fulfillment confirmation failed")
                else:
                    delay = min(3600, 2 ** min(attempts, 10))
                    next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds")
                    self.mark_outbox(row["trade_no"], "retry_wait", error_code="alipay_fulfillment_pending", error="fulfillment confirmation failed", next_attempt_at=next_at)
            else:
                self.mark_outbox(row["trade_no"], "confirmed")
        return processed

    def mark_outbox(self, trade_no: str, status: str, *, error_code: Optional[str] = None, error: Optional[str] = None, next_attempt_at: Optional[str] = None) -> None:
        with self._transaction():
            self.db.execute("UPDATE alipay_fulfillment_outbox SET status=?,attempt_count=attempt_count+1,last_error_code=?,last_error=?,next_attempt_at=COALESCE(?,next_attempt_at),updated_at=? WHERE trade_no=?", (status, error_code, error, next_attempt_at or utc_now(), utc_now(), trade_no))

    def close(self) -> None:
        self.db.close()


__all__ = ["AlipayOrderStore", "proof_hash"]
