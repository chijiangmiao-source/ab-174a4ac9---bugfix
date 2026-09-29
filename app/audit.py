"""稳定审计标识的幂等记录存储（进程内，并发安全）。

- 同载荷重传（同一 audit_key 且规范化载荷哈希一致）→ 返回同一份最终记录，
  包括首个请求尚在求解时并发到达的重传：等待首个请求落盘后返回其完整结果，
  绝不回放 pending 的半成品。
- 改载荷复用同一 audit_key → 立即拒绝（冲突），即使首个请求仍在求解。
- 求解或运行异常 → 释放预留，使后续同标识请求可以重新求解，不会永久卡死。
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
    """预留-完成（reserve/commit/fail）式的并发幂等存储。

    每个 key 在首个请求进入时被原子预留并同步求解；同载荷并发请求在同一
    Condition 上等待最终记录；异载荷请求无需等待，立即得到冲突。
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._records: dict[str, dict[str, Any]] = {}

    # -- 内部工具（调用方须自持 self._cond）----------------------------------

    def _snapshot_locked(self, rec: dict[str, Any]) -> dict[str, Any]:
        """复制一份对外只读的记录（含 request / result，避免外部改写内部状态）。"""
        return {
            "audit_key": rec["audit_key"],
            "fingerprint": rec["fingerprint"],
            "created_at": rec["created_at"],
            "request": dict(rec.get("request", {})),
            "result": dict(rec.get("result", {})),
        }

    # -- 读取 ----------------------------------------------------------------

    def get(self, key: str) -> Optional[dict[str, Any]]:
        """取回已完成记录；pending 或未知 key 返回 None（不泄露半成品）。"""
        with self._cond:
            rec = self._records.get(key)
            if rec is None or rec.get("pending"):
                return None
            return self._snapshot_locked(rec)

    # -- 预留 / 完成 / 失败 ---------------------------------------------------

    def reserve(self, key: str, fingerprint: str
                ) -> tuple[Optional[dict[str, Any]], bool]:
        """为 key 预留求解槽位。

        返回 ``(record, conflict)``：
        - ``(None, False)``：本调用赢得预留，调用方应当执行求解并随后
          :meth:`commit`（异常时 :meth:`fail`）；
        - ``(record, False)``：同载荷记录已完成（``record`` 为其快照），
          或同载荷请求正在求解——后一种情况本方法阻塞至其完成后再返回快照；
        - ``(None, True)``：该 key 已绑定其他载荷（冲突）。
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
                    return None, False
                if rec["fingerprint"] != fingerprint:
                    return None, True
                if not rec.get("pending"):
                    return self._snapshot_locked(rec), False
                # 同载荷的首个请求仍在求解：等待其 commit / fail，绝不回放半成品。
                self._cond.wait()

    def commit(self, key: str, fingerprint: str, request: dict[str, Any],
               result: dict[str, Any]) -> dict[str, Any]:
        """首个请求求解成功后落盘；唤醒所有等待者并返回最终记录快照。"""
        with self._cond:
            rec = self._records.get(key)
            if rec is None or rec["fingerprint"] != fingerprint:
                raise AuditConflict(key)
            if rec.get("pending"):
                rec["request"] = {k: v for k, v in request.items()
                                  if k != "audit_key"}
                rec["result"] = result
                rec.pop("pending", None)
            self._cond.notify_all()
            return self._snapshot_locked(rec)

    def fail(self, key: str, fingerprint: str) -> None:
        """首个请求求解失败 / 运行异常：释放预留，允许同标识请求重试。"""
        with self._cond:
            rec = self._records.get(key)
            if rec is not None and rec.get("pending") \
                    and rec["fingerprint"] == fingerprint:
                del self._records[key]
            # 无论是否删除都唤醒等待者：若预留已易主（理论上不会发生），
            # 等待者也应重新检查状态而不是永久挂起。
            self._cond.notify_all()

    def all_keys(self) -> list[str]:
        with self._cond:
            return sorted(self._records.keys())
