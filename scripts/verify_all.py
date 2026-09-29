#!/usr/bin/env python3
"""verify 容器入口：在 Compose 环境内核对全部要求后自行退出。

检查项（全部通过退出码 0，任一失败非 0）：
  1. 镜像内应用可导入、静态页已打包（镜像构建核对）；
  2. 求解器与 API 测试（pytest，真实 FastAPI 路由）；
  3. web 服务健康检查 /healthz；
  4. 本题 API 冒烟：可行流最优值与证书、不可行证据、输入错误定位、
     审计标识同载荷重传 / 改载荷冲突。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WEB_BASE_URL", "http://web:8080")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures: list[str] = []


def step(no: int, title: str):
    print(f"\n\033[36m[{no}/4]\033[0m {title}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def http(method: str, path: str, payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ---- 1. 镜像构建核对：包与静态资源确实在镜像内 ---------------------------
step(1, "镜像构建核对（包可导入、页面已打包）")
try:
    import app.main  # noqa: F401
    from pathlib import Path
    page = Path(ROOT) / "app" / "static" / "index.html"
    check("应用包可导入", True)
    check("页面已打入镜像", page.is_file() and "最小费用流" in page.read_text(encoding="utf-8"))
except Exception as exc:  # noqa: BLE001
    check("镜像内应用导入", False, repr(exc))

# ---- 2. 求解器 + API 测试 -------------------------------------------------
step(2, "求解器与 API 测试（pytest）")
proc = subprocess.run(
    [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider"],
    cwd=ROOT, capture_output=True, text=True)
last_line = proc.stdout.strip().splitlines()[-1:] or [""]
check("pytest 全部通过", proc.returncode == 0, last_line[0])
if proc.returncode:
    print(proc.stdout[-3000:])
    print(proc.stderr[-1500:])

# ---- 3. 健康检查 ----------------------------------------------------------
step(3, f"健康检查 GET {BASE}/healthz")
try:
    status, body = http("GET", "/healthz")
    check("/healthz 200 ok", status == 200 and body.get("status") == "ok",
          f"HTTP {status}")
except Exception as exc:  # noqa: BLE001
    check("/healthz 可达", False, repr(exc))

# ---- 4. 本题 API 冒烟 -----------------------------------------------------
step(4, "本题 API 冒烟（真实 HTTP）")

FEASIBLE = {
    "nodes": [{"id": "S", "balance": 6}, {"id": "A", "balance": 0},
              {"id": "B", "balance": 0}, {"id": "T", "balance": -6}],
    "edges": [
        {"id": "e1", "from": "S", "to": "A", "lower": 0, "upper": 4, "cost": 2},
        {"id": "e2", "from": "S", "to": "B", "lower": 0, "upper": 6, "cost": 5},
        {"id": "e3", "from": "A", "to": "B", "lower": 0, "upper": 3, "cost": 1},
        {"id": "e4", "from": "A", "to": "T", "lower": 0, "upper": 5, "cost": 7},
        {"id": "e5", "from": "B", "to": "T", "lower": 0, "upper": 6, "cost": 2}],
}
status, body = http("POST", "/api/solve", FEASIBLE)
ok = (status == 200 and body.get("status") == "feasible"
      and body.get("total_cost") == 36)
flows = {e["id"]: e["flow"] for e in body.get("edges", [])}
ok = ok and flows == {"e1": 3, "e2": 3, "e3": 3, "e4": 0, "e5": 6}
ok = ok and all(
    (e["forward_reduced_cost"] is None or e["forward_reduced_cost"] >= 0)
    and (e["backward_reduced_cost"] is None or e["backward_reduced_cost"] >= 0)
    for e in body.get("edges", []))
check("可行流：最优成本 36 且正残量边约化费用非负", ok, f"HTTP {status}")

INFEASIBLE = {
    "nodes": [{"id": "S", "balance": 10}, {"id": "A", "balance": 0},
              {"id": "T", "balance": -10}],
    "edges": [
        {"id": "e1", "from": "S", "to": "A", "lower": 0, "upper": 4, "cost": 1},
        {"id": "e2", "from": "A", "to": "T", "lower": 0, "upper": 5, "cost": 1}],
}
status, body = http("POST", "/api/solve", INFEASIBLE)
ok = (status == 200 and body.get("status") == "infeasible"
      and body.get("deficit") == 6
      and any(u["node"] == "T" and u["kind"] == "demand_not_satisfied"
              and u["amount"] == 6 for u in body.get("unmet", []))
      and any(c["edge"] == "e1" and c["saturated"]
              for c in body.get("saturated_cut_edges", []))
      and "S" in body.get("residual_reachable_nodes", []))
check("不可行流：未满足量与残量可达站点证据", ok, f"HTTP {status}")

BAD = {"nodes": [{"id": "S", "balance": 5}, {"id": "T", "balance": -3}],
       "edges": [{"id": "e1", "from": "S", "to": "T",
                  "lower": 4, "upper": 1, "cost": 1}]}
status, body = http("POST", "/api/solve", BAD)
codes = {e.get("code") for e in body.get("errors", [])}
locs = [tuple(e.get("loc", [])) for e in body.get("errors", [])]
ok = status == 422 and {"imbalance", "bad_bounds"} <= codes \
    and ("edges", 0, "upper") in locs
check("总供需不平 / 边界非法：422 且错误定位到字段", ok,
      f"HTTP {status} codes={sorted(codes)}")

FLOATED = {"nodes": [{"id": "S", "balance": 5}, {"id": "T", "balance": -5}],
           "edges": [{"id": "e1", "from": "S", "to": "T",
                      "lower": 0, "upper": 1, "cost": 1.5}]}
status, body = http("POST", "/api/solve", FLOATED)
ok = status == 422
check("浮点数量被精确整数校验拒绝（禁止浮点）", ok, f"HTTP {status}")

keyed = dict(FEASIBLE, audit_key=f"verify-smoke-{os.getpid()}-{int(__import__('time').time())}")
s1, b1 = http("POST", "/api/solve", keyed)
s2, b2 = http("POST", "/api/solve", keyed)
ok = (s1 == 200 and b1.get("status") == "stored"
      and s2 == 200 and b2.get("status") == "idempotent_replay"
      and b2.get("created_at") == b1.get("created_at"))
check("审计：同载荷重传返回原记录", ok, f"{b1.get('status')} -> {b2.get('status')}")

changed = dict(FEASIBLE, audit_key=keyed["audit_key"])
changed["edges"][0]["cost"] = 99
s3, b3 = http("POST", "/api/solve", changed)
ok = s3 == 409 and b3.get("status") == "audit_conflict"
check("审计：改载荷复用标识被拒绝（409）", ok, f"HTTP {s3}")

# ---- 汇总 -----------------------------------------------------------------
print("\n" + "=" * 60)
if failures:
    print(f"{FAIL} 核对未通过：{len(failures)} 项 -> {failures}")
    sys.exit(1)
print(f"{PASS} 全部核对通过：求解器测试、镜像构建、API 冒烟")
sys.exit(0)
