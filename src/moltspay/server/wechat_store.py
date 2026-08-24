"""Durable order state and single-use delivery claims for WeChat service orders."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .order_protection import OrderCapacityError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WechatOrderStore:
    """SQLite-backed WeChat service orders.

    A WeChat Native order is a payment credential.  The row binds that
    credential to one service resource and owns the single delivery claim.
    """

    def __init__(self, db_path: str = "data/wechat-x402.sqlite", *,
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
        if self.db_path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS wechat_orders (
              out_trade_no TEXT PRIMARY KEY,
              request_id TEXT NOT NULL,
              caller_id TEXT NOT NULL DEFAULT '',
              kind TEXT NOT NULL CHECK(kind = 'service'),
              skill_id TEXT NOT NULL,
              service_id TEXT NOT NULL,
              resource_id TEXT NOT NULL,
              amount_fen INTEGER NOT NULL,
              currency TEXT NOT NULL,
              appid TEXT NOT NULL,
              mchid TEXT NOT NULL,
              code_url TEXT NOT NULL,
              pay_before TEXT NOT NULL,
              status TEXT NOT NULL,
              result_json TEXT,
              error_code TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              completed_at TEXT,
              creation_claimed_at TEXT,
              UNIQUE(request_id, kind, resource_id)
            );
            CREATE INDEX IF NOT EXISTS idx_wechat_orders_request
              ON wechat_orders(request_id, kind, resource_id);
            """
        )
        columns = {
            str(row["name"])
            for row in self.db.execute("PRAGMA table_info(wechat_orders)").fetchall()
        }
        if "caller_id" not in columns:
            self.db.execute("ALTER TABLE wechat_orders ADD COLUMN caller_id TEXT NOT NULL DEFAULT ''")
        if "creation_claimed_at" not in columns:
            self.db.execute("ALTER TABLE wechat_orders ADD COLUMN creation_claimed_at TEXT")

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
        return self._row(
            self.db.execute(
                "SELECT * FROM wechat_orders WHERE out_trade_no=?", (out_trade_no,)
            ).fetchone()
        )

    def get_by_request(self, request_id: str, resource_id: str) -> Optional[Dict[str, Any]]:
        return self._row(
            self.db.execute(
                "SELECT * FROM wechat_orders WHERE request_id=? AND kind='service' AND resource_id=?",
                (request_id, resource_id),
            ).fetchone()
        )

    def cleanup_expired_unpaid(self) -> int:
        """Remove only unpaid Native-order rows after their pay window closes."""
        with self._transaction():
            result = self.db.execute(
                """DELETE FROM wechat_orders
                   WHERE status IN ('offered', 'expired') AND pay_before <= ?""",
                (utc_now(),),
            )
            return int(result.rowcount)

    def outstanding_count(self, caller_id: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) FROM wechat_orders WHERE status='offered'"
        args: tuple[str, ...] = ()
        if caller_id is not None:
            query += " AND caller_id=?"
            args = (caller_id,)
        return int(self.db.execute(query, args).fetchone()[0])

    def reserve_order(
        self, *, request_id: str, caller_id: Optional[str], skill_id: str,
        service_id: str, resource_id: str, amount_fen: int, currency: str,
        appid: str, mchid: str, pay_before: str,
    ) -> Dict[str, Any]:
        """Reserve one durable order before contacting the WeChat API."""
        values = (request_id, skill_id, service_id, resource_id, currency, appid, mchid, pay_before)
        if not all(isinstance(value, str) and value for value in values) or amount_fen <= 0:
            raise ValueError("invalid WeChat service order")
        now = utc_now()
        with self._transaction():
            self.db.execute(
                """DELETE FROM wechat_orders
                   WHERE status IN ('offered', 'expired') AND pay_before <= ?""", (now,)
            )
            existing = self._row(self.db.execute(
                "SELECT * FROM wechat_orders WHERE request_id=? AND kind='service' AND resource_id=?",
                (request_id, resource_id),
            ).fetchone())
            if existing:
                immutable = {
                    "skill_id": skill_id, "service_id": service_id,
                    "amount_fen": amount_fen, "currency": currency,
                    "appid": appid, "mchid": mchid,
                }
                if any(existing.get(key) != value for key, value in immutable.items()):
                    raise ValueError("WeChat idempotency key conflicts with the existing order")
                return existing
            if self.outstanding_count() >= self.max_outstanding_orders:
                raise OrderCapacityError("wechat_outstanding_limit", "WeChat unpaid order limit reached", 5)
            effective_caller = str(caller_id or "")
            if effective_caller and self.outstanding_count(effective_caller) >= self.max_outstanding_per_caller:
                raise OrderCapacityError("wechat_caller_outstanding_limit", "WeChat caller unpaid order limit reached", 5)
            out_trade_no = "WX" + uuid.uuid4().hex[:30]
            try:
                self.db.execute(
                    """INSERT INTO wechat_orders
                    (out_trade_no,request_id,caller_id,kind,skill_id,service_id,resource_id,
                     amount_fen,currency,appid,mchid,code_url,pay_before,status,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (out_trade_no, request_id, effective_caller, "service", skill_id,
                     service_id, resource_id, amount_fen, currency, appid, mchid,
                     "", pay_before, "offered", now, now),
                )
            except sqlite3.IntegrityError:
                existing = self._row(self.db.execute(
                    "SELECT * FROM wechat_orders WHERE request_id=? AND kind='service' AND resource_id=?",
                    (request_id, resource_id),
                ).fetchone())
                if existing:
                    return existing
                raise
        return self.get(out_trade_no) or {}

    def claim_creation(self, out_trade_no: str) -> Dict[str, Any]:
        """Elect one process to perform the external Native-order request."""
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        with self._transaction():
            row = self.db.execute("SELECT * FROM wechat_orders WHERE out_trade_no=?", (out_trade_no,)).fetchone()
            if row is None:
                return {"state": "not_found"}
            current = self._row(row)
            if current.get("code_url"):
                return {"state": "ready", "order": current}
            claimed_at = current.get("creation_claimed_at")
            if claimed_at:
                try:
                    age = (now - datetime.fromisoformat(claimed_at.replace("Z", "+00:00"))).total_seconds()
                except ValueError:
                    age = 0
                if age < 60:
                    return {"state": "in_progress", "order": current}
            self.db.execute(
                "UPDATE wechat_orders SET creation_claimed_at=?,updated_at=? WHERE out_trade_no=?",
                (now_text, now_text, out_trade_no),
            )
            return {"state": "claimed", "order": self.get(out_trade_no)}

    def finalize_order(self, out_trade_no: str, code_url: str, pay_before: str) -> Dict[str, Any]:
        if not isinstance(code_url, str) or not code_url or not isinstance(pay_before, str) or not pay_before:
            raise ValueError("invalid WeChat Native order fields")
        with self._transaction():
            self.db.execute(
                """UPDATE wechat_orders SET code_url=COALESCE(NULLIF(code_url,''),?),
                   pay_before=?,creation_claimed_at=NULL,updated_at=? WHERE out_trade_no=?""",
                (code_url, pay_before, utc_now(), out_trade_no),
            )
        return self.get(out_trade_no) or {}

    def create_order(
        self,
        *,
        request_id: str,
        out_trade_no: str,
        skill_id: str,
        service_id: str,
        resource_id: str,
        amount_fen: int,
        currency: str,
        appid: str,
        mchid: str,
        code_url: str,
        pay_before: str,
        caller_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        values = (skill_id, service_id, resource_id, amount_fen, currency, appid, mchid, code_url, pay_before)
        if not request_id or not out_trade_no or not all(isinstance(value, str) and value for value in values[:3] + values[5:]):
            raise ValueError("invalid WeChat service order")
        if amount_fen <= 0 or not pay_before:
            raise ValueError("invalid WeChat service order")
        now = utc_now()
        with self._transaction():
            self.db.execute(
                """DELETE FROM wechat_orders
                   WHERE status IN ('offered', 'expired') AND pay_before <= ?""", (now,)
            )
            existing = self._row(self.db.execute(
                "SELECT * FROM wechat_orders WHERE request_id=? AND kind='service' AND resource_id=?",
                (request_id, resource_id),
            ).fetchone())
            if existing:
                immutable = {
                    "skill_id": skill_id, "service_id": service_id,
                    "amount_fen": amount_fen, "currency": currency,
                    "appid": appid, "mchid": mchid,
                }
                if any(existing.get(key) != value for key, value in immutable.items()):
                    raise ValueError("WeChat idempotency key conflicts with the existing order")
                return existing
            if self.outstanding_count() >= self.max_outstanding_orders:
                raise OrderCapacityError("wechat_outstanding_limit", "WeChat unpaid order limit reached", 5)
            effective_caller = str(caller_id or "")
            if effective_caller and self.outstanding_count(effective_caller) >= self.max_outstanding_per_caller:
                raise OrderCapacityError("wechat_caller_outstanding_limit", "WeChat caller unpaid order limit reached", 5)
            try:
                self.db.execute(
                    """INSERT INTO wechat_orders
                    (out_trade_no,request_id,caller_id,kind,skill_id,service_id,resource_id,
                     amount_fen,currency,appid,mchid,code_url,pay_before,status,
                     created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        out_trade_no, request_id, effective_caller, "service", skill_id, service_id,
                        resource_id, amount_fen, currency, appid, mchid, code_url,
                        pay_before, "offered", now, now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = self.get_by_request(request_id, resource_id)
                if existing:
                    return existing
                raise
        return self.get(out_trade_no) or {}

    def claim_execution(
        self,
        out_trade_no: str,
        *,
        skill_id: str,
        service_id: str,
        resource_id: str,
        amount_fen: int,
        currency: str,
        appid: str,
        mchid: str,
    ) -> Dict[str, Any]:
        """Atomically bind the order and acquire its one handler execution slot."""
        now = utc_now()
        with self._transaction():
            row = self.db.execute(
                "SELECT * FROM wechat_orders WHERE out_trade_no=?", (out_trade_no,)
            ).fetchone()
            if row is None:
                return {"state": "not_found"}
            for field, expected, state in (
                ("skill_id", skill_id, "skill_mismatch"),
                ("service_id", service_id, "service_mismatch"),
                ("resource_id", resource_id, "resource_mismatch"),
                ("amount_fen", amount_fen, "amount_mismatch"),
                ("currency", currency, "currency_mismatch"),
                ("appid", appid, "merchant_mismatch"),
                ("mchid", mchid, "merchant_mismatch"),
            ):
                if row[field] != expected:
                    return {"state": state, "order": dict(row)}

            if row["status"] == "completed":
                return {"state": "completed", "order": self._row(row)}
            if row["status"] == "executing":
                return {"state": "executing", "order": self._row(row)}
            if row["status"] != "offered":
                return {"state": "rejected", "order": self._row(row)}
            try:
                expiry = datetime.fromisoformat(row["pay_before"].replace("Z", "+00:00"))
                if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                    self.db.execute(
                        "UPDATE wechat_orders SET status='expired',updated_at=? WHERE out_trade_no=?",
                        (now, out_trade_no),
                    )
                    return {"state": "expired", "order": self.get(out_trade_no)}
            except (AttributeError, ValueError):
                return {"state": "rejected", "order": self._row(row)}
            self.db.execute(
                "UPDATE wechat_orders SET status='executing',updated_at=? WHERE out_trade_no=? AND status='offered'",
                (now, out_trade_no),
            )
            return {"state": "claimed", "order": self.get(out_trade_no)}

    def complete(self, out_trade_no: str, result: Any) -> Dict[str, Any]:
        now = utc_now()
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        with self._transaction():
            row = self.db.execute(
                "SELECT status FROM wechat_orders WHERE out_trade_no=?", (out_trade_no,)
            ).fetchone()
            if not row:
                raise KeyError("WeChat order not found")
            if row["status"] == "executing":
                self.db.execute(
                    "UPDATE wechat_orders SET status='completed',result_json=?,updated_at=?,completed_at=? WHERE out_trade_no=?",
                    (encoded, now, now, out_trade_no),
                )
        return self.get(out_trade_no) or {}

    def fail_delivery(self, out_trade_no: str, error_code: str) -> Dict[str, Any]:
        with self._transaction():
            self.db.execute(
                "UPDATE wechat_orders SET status='delivery_failed',error_code=?,updated_at=? WHERE out_trade_no=? AND status='executing'",
                (error_code, utc_now(), out_trade_no),
            )
        return self.get(out_trade_no) or {}

    def close(self) -> None:
        self.db.close()


__all__ = ["WechatOrderStore"]
