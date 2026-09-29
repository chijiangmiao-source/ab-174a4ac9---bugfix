"""稳定审计标识的幂等记录存储（进程内）。

- 同载荷重传（同一 audit_key 且规范化载荷哈希一致）→ 返回原记录。
- 改载荷复用同一 audit_key → 拒绝（冲突）。
- 同标识同载荷的并发提交 → 只形成一份最终记录：首个请求持有预约并求解，
  其余请求阻塞等待其保存后复用同一创建时间与完整结果；
  若持有者求解失败/异常，预约被回滚，等待者之一接管重新求解，
  不会留下可被永久回放的半成品状态。
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


# acquire 的三种结局
ACQUIRE_OWNER = "owner"        # 调用者持有新预约，负责求解并 save / release
ACQUIRE_REPLAY = "replay"      # 已有完整记录，直接复用
ACQUIRE_CONFLICT = "conflict"  # 同标识已绑定其他载荷


class AuditStore:
    def __init__(self) -> None:
        # 条件变量同时充当互斥锁：等待者在持有者 save / release 时被唤醒重判
        self._cond = threading.Condition()
        self._records: dict[str, dict[str, Any]] = {}

    def get(self, key: str) -> Optional[dict[str, Any]]:
        with self._cond:
            rec = self._records.get(key)
            return dict(rec) if rec else None

    def acquire(self, key: str, fingerprint: str) -> tuple[str, Optional[dict]]:
        """阻塞至三选一：(结局, 记录)。

        - ``(ACQUIRE_OWNER, None)``：调用者成为预约持有者，必须求解后
          ``save``（或失败时 ``release``）；
        - ``(ACQUIRE_REPLAY, record)``：同载荷记录已完整保存，直接复用；
        - ``(ACQUIRE_CONFLICT, None)``：同标识已绑定其他载荷，立即拒绝。

        同载荷的在途预约（pending）会让调用者等待，直到持有者保存
        （随后命中 replay）或回滚（随后重新竞争，可能转为持有者）。
        """
        with self._cond:
            while True:
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
                    return ACQUIRE_OWNER, None
                if rec["fingerprint"] != fingerprint:
                    return ACQUIRE_CONFLICT, None
                if not rec.get("pending"):
                    return ACQUIRE_REPLAY, dict(rec)
                # 同载荷记录正在由持有者求解：等待其保存或回滚后重新判定
                self._cond.wait()

    def save(self, key: str, fingerprint: str, request: dict[str, Any],
             result: dict[str, Any]) -> dict[str, Any]:
        """持有者写入完整结果并唤醒所有等待者。"""
        with self._cond:
            existing = self._records.get(key)
            if existing is None or existing["fingerprint"] != fingerprint:
                raise AuditConflict(key)
            if not existing.get("pending"):
                return dict(existing)
            existing["request"] = {k: v for k, v in request.items()
                                   if k != "audit_key"}
            existing["result"] = result
            existing.pop("pending", None)
            self._cond.notify_all()
            return dict(existing)

    def release(self, key: str, fingerprint: str) -> None:
        """持有者求解失败/异常时回滚预约：删除半成品并唤醒等待者重新竞争。"""
        with self._cond:
            rec = self._records.get(key)
            if (rec is not None and rec.get("pending")
                    and rec["fingerprint"] == fingerprint):
                del self._records[key]
                self._cond.notify_all()

    def all_keys(self) -> list[str]:
        with self._cond:
            return sorted(self._records.keys())
