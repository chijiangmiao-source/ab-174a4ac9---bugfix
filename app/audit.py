"""稳定审计标识的幂等记录存储（进程内）。

- 同载荷重传（同一 audit_key 且规范化载荷哈希一致）→ 返回原记录。
- 改载荷复用同一 audit_key → 拒绝（冲突）。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """对不含 audit_key 的载荷做规范化 SHA-256（确定性序列化，无浮点介入）。"""
    body = {k: v for k, v in payload.items() if k != "audit_key"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class AuditConflict(Exception):
    def __init__(self, key: str):
        self.key = key
        super().__init__(f"审计标识 {key} 已绑定另一载荷")


class AuditStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rec = self._records.get(key)
            return dict(rec) if rec else None

    def lookup_or_reserve(self, key: str, fingerprint: str) -> tuple[
            Optional[dict[str, Any]], bool]:
        """返回 (既有记录, 是否冲突)。既有记录命中同载荷时直接复用。"""
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = {
                    "audit_key": key,
                    "fingerprint": fingerprint,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "request": {},
                    "result": {},
                    "pending": True,
                }
                return None, False
            if rec["fingerprint"] != fingerprint:
                return None, True
            return dict(rec), False

    def save(self, key: str, fingerprint: str, request: dict[str, Any],
             result: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            existing = self._records.get(key)
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise AuditConflict(key)
                if not existing.get("pending"):
                    return dict(existing)
                existing["request"] = {k: v for k, v in request.items()
                                       if k != "audit_key"}
                existing["result"] = result
                existing.pop("pending", None)
                return dict(existing)
            raise AuditConflict(key)

    def all_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._records.keys())
