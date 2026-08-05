"""Tamper-evident JSON-lines audit log."""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


class AuditLog:
    def __init__(self, path: str):
        self.path = Path(path).expanduser()

    def _last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 64
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return json.loads(lines[-1])["hash"] if lines else "0" * 64

    def append(self, action: str, request_id: Optional[str] = None, **metadata: Any) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        entry = {
            "timestamp": int(now.timestamp() * 1000), "datetime": now.isoformat(),
            "action": action, "request_id": request_id or str(uuid.uuid4()),
            **metadata, "prev_hash": self._last_hash(),
        }
        canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
        entry["hash"] = hashlib.sha256(canonical.encode()).hexdigest()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return entry

    def verify(self) -> bool:
        previous = "0" * 64
        if not self.path.exists():
            return True
        for line in self.path.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            digest = entry.pop("hash")
            if entry.get("prev_hash") != previous:
                return False
            canonical = json.dumps(entry, sort_keys=True, separators=(",", ":"), default=str)
            if hashlib.sha256(canonical.encode()).hexdigest() != digest:
                return False
            previous = digest
        return True
