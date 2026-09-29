#!/usr/bin/env python3
"""verify 容器入口：在 Compose 环境内核对全部要求后自行退出。

检查项（全部通过退出码 0，任一失败非 0）：
  1. 镜像内应用可导入、静态页已打包（镜像构建核对）；
  2. 求解器与 API 测试（pytest，真实 FastAPI 路由，含并发幂等/失败恢复用例）；
  3. web 服务健康检查 /healthz；
  4. 本题 API 冒烟：可行流最优值与证书、不可行证据、输入错误定位、
     审计标识同载荷顺序重传 / 改载荷冲突；
  5. 并发验收（真实 HTTP）：
     a. 同标识同载荷的较大液路（12 站 / 36 边、含负费用改进）并发复核
        —— 仅形成一份最终记录，所有响应创建时间一致、结果完整且可复算；
     b. 同标识异载荷并发竞争 —— 一个 200、另一个立即 409，原记录不受影响；
     c. 求解异常后重试 —— 500 且不留 pending 半成品，同标识重试成功落盘。

c 项需要故障注入缝，故在 verify 容器内以 CRYOTEST_FAULT_INJECTION=1 另起一个
仅监听 127.0.0.1 的临时 uvicorn；生产 web 服务从不设置该变量。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import uvicorn

BASE = os.environ.get("WEB_BASE_URL", "http://web:8080")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"

failures: list[str] = []


def step(no: int, title: str):
    print(f"\n\033[36m[{no}/5]\033[0m {title}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(name)


def http(method: str, base: str, path: str, payload=None, headers=None,
         timeout=15):
    data = None
    hdrs = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base + path, data=data, headers=hdrs,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def post_concurrent(base: str, calls):
    """calls: [(payload, headers), ...]；屏障保证同时发出。"""
    barrier = threading.Barrier(len(calls))
    results: dict[int, tuple] = {}

    def worker(i, payload, headers):
        barrier.wait()
        results[i] = http("POST", base, "/api/solve", payload,
                          headers=headers, timeout=30)

    threads = [threading.Thread(target=worker, args=(i, p, h))
               for i, (p, h) in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    return [results[i] for i in range(len(calls))]


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
step(2, "求解器与 API 测试（pytest，含并发幂等用例）")
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
    status, body = http("GET", BASE, "/healthz")
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
status, body = http("POST", BASE, "/api/solve", FEASIBLE)
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
status, body = http("POST", BASE, "/api/solve", INFEASIBLE)
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
status, body = http("POST", BASE, "/api/solve", BAD)
codes = {e.get("code") for e in body.get("errors", [])}
locs = [tuple(e.get("loc", [])) for e in body.get("errors", [])]
ok = status == 422 and {"imbalance", "bad_bounds"} <= codes \
    and ("edges", 0, "upper") in locs
check("总供需不平 / 边界非法：422 且错误定位到字段", ok,
      f"HTTP {status} codes={sorted(codes)}")

FLOATED = {"nodes": [{"id": "S", "balance": 5}, {"id": "T", "balance": -5}],
           "edges": [{"id": "e1", "from": "S", "to": "T",
                      "lower": 0, "upper": 1, "cost": 1.5}]}
status, body = http("POST", BASE, "/api/solve", FLOATED)
ok = status == 422
check("浮点数量被精确整数校验拒绝（禁止浮点）", ok, f"HTTP {status}")

stamp = f"{os.getpid()}-{int(time.time())}"
keyed = dict(FEASIBLE, audit_key=f"verify-smoke-{stamp}")
s1, b1 = http("POST", BASE, "/api/solve", keyed)
s2, b2 = http("POST", BASE, "/api/solve", keyed)
ok = (s1 == 200 and b1.get("status") == "stored"
      and s2 == 200 and b2.get("status") == "idempotent_replay"
      and b2.get("created_at") == b1.get("created_at"))
check("审计：同载荷顺序重传返回原记录（创建时间稳定）", ok,
      f"{b1.get('status')} -> {b2.get('status')}")

changed = dict(FEASIBLE, audit_key=keyed["audit_key"])
changed["edges"][0]["cost"] = 99
s3, b3 = http("POST", BASE, "/api/solve", changed)
ok = s3 == 409 and b3.get("status") == "audit_conflict"
check("审计：改载荷复用标识被拒绝（409）", ok, f"HTTP {s3}")

# ---- 5. 并发验收 ----------------------------------------------------------
step(5, "并发验收：同载荷合并 / 异载荷竞争 / 异常后重试（真实 HTTP）")


def large_feasible_payload() -> dict:
    """12 站 / 36 管路、含负费用改进且保证可行的较大液路（确定性构造）。"""
    import random
    rng = random.Random(20260929)
    n = 12
    ids = [f"S{i:02d}" for i in range(n)]
    total = 60
    balances = [total] + [0] * (n - 2) + [-total]
    edges = []

    def add(eid, u, v, lo, hi, cost):
        edges.append({"id": eid, "from": ids[u], "to": ids[v],
                      "lower": lo, "upper": hi, "cost": cost})

    add("D00", 0, n - 1, 0, total, 100)  # 充裕直连边保底可行
    k = 1
    for v in range(1, n - 1):
        add(f"O{k:02d}", 0, v, 0, 40, rng.randint(-8, 12))
        k += 1
    for u in range(1, n - 1):
        add(f"I{k:02d}", u, n - 1, 0, 40, rng.randint(-8, 12))
        k += 1
    while k < 36:
        u, v = rng.sample(range(1, n - 1), 2)
        add(f"M{k:02d}", u, v, 0, 30, rng.randint(-20, 20))
        k += 1
    return {"nodes": [{"id": ids[i], "balance": balances[i]}
                      for i in range(n)],
            "edges": edges}


def assert_complete(result) -> bool:
    """完整、可复算的可行结果：状态 / 逐管流量 / 总成本 / 势证据齐备且自洽。"""
    if not isinstance(result, dict) or result.get("status") != "feasible":
        return False
    edge_rows = result.get("edges")
    nodes = result.get("nodes")
    pot = result.get("potentials")
    if not isinstance(result.get("total_cost"), int) \
            or not isinstance(edge_rows, list) or len(edge_rows) != 36 \
            or not isinstance(pot, dict) or len(pot) != 12:
        return False
    bal = {n["id"]: n["balance"] for n in nodes}
    inflow = {nid: 0 for nid in bal}
    outflow = {nid: 0 for nid in bal}
    cost = 0
    for e in edge_rows:
        f = e["flow"]
        if not isinstance(f, int) or not (e["lower"] <= f <= e["upper"]):
            return False
        cost += f * e["cost"]
        outflow[e["from"]] += f
        inflow[e["to"]] += f
        if e["forward_residual"] > 0:
            if e["forward_reduced_cost"] != (
                    e["cost"] + pot[e["from"]] - pot[e["to"]]) \
                    or e["forward_reduced_cost"] < 0:
                return False
        elif e["forward_reduced_cost"] is not None:
            return False
        if e["backward_residual"] > 0 and e["backward_reduced_cost"] < 0:
            return False
    if cost != result["total_cost"]:
        return False
    return all(outflow[nid] - inflow[nid] == b for nid, b in bal.items())


# 5a. 同标识同载荷并发复核：只形成一份最终记录
big = large_feasible_payload()
same_key = f"verify-concurrent-same-{stamp}"
same_payload = dict(big, audit_key=same_key)
rs = post_concurrent(BASE, [(same_payload, None), (same_payload, None)])
all200 = all(code == 200 for code, _ in rs)
sts = sorted(b.get("status") for _, b in rs)
created = {b.get("created_at") for _, b in rs}
ok = (all200 and sts == ["idempotent_replay", "stored"] and len(created) == 1)
check("并发同载荷：一个 stored、一个重放，创建时间一致", ok,
      f"HTTP {[c for c, _ in rs]} status={sts}")
ref = next(b["result"] for _, b in rs if b.get("status") == "stored")
ok = all(assert_complete(b.get("result")) for _, b in rs) \
    and all(b.get("result") == ref for _, b in rs)
check("并发同载荷：两份响应均含一致、完整、可复算结果", ok,
      f"total_cost={ref.get('total_cost') if isinstance(ref, dict) else None}")
g_status, g_body = http("GET", BASE, f"/api/audit/{same_key}")
ok = (g_status == 200 and g_body.get("status") == "replayed"
      and g_body.get("created_at") == next(iter(created))
      and g_body.get("result") == ref)
check("并发同载荷：记录查询取回同一份完整记录", ok, f"HTTP {g_status}")

# 5b. 同标识异载荷并发竞争：一个 200、一个立即 409，原记录不受影响
conf_key = f"verify-concurrent-conflict-{stamp}"
# 两次独立构造，避免浅拷贝共享 edges 导致“异载荷”实际上指纹相同
payload_a = dict(large_feasible_payload(), audit_key=conf_key)
payload_b = dict(large_feasible_payload(), audit_key=conf_key)
payload_b["edges"][1]["cost"] = 99
rc = post_concurrent(BASE, [(payload_a, None), (payload_b, None)])
ccodes = sorted(code for code, _ in rc)
win = next(b for code, b in rc if code == 200)
lose_body = next(b for code, b in rc if code == 409)
ok = (ccodes == [200, 409] and lose_body.get("status") == "audit_conflict"
      and win.get("status") == "stored" and assert_complete(win.get("result")))
check("并发异载荷：一个 200、另一个立即 409 audit_conflict", ok,
      f"HTTP {ccodes}")
g_status, g_body = http("GET", BASE, f"/api/audit/{conf_key}")
ok = (g_status == 200 and g_body.get("result") == win.get("result")
      and g_body.get("created_at") == win.get("created_at"))
check("并发异载荷：原记录完整且未被异载荷覆盖", ok, f"HTTP {g_status}")

# 5c. 求解异常后的重试：在本进程内另起一个开启故障注入缝的临时 HTTP 服务
#     （仅监听 127.0.0.1 临时端口；生产 web 是另一容器，不受此环境变量影响）


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


fault_port = _free_port()
fault_base = f"http://127.0.0.1:{fault_port}"
os.environ["CRYOTEST_FAULT_INJECTION"] = "1"
fault_cfg = uvicorn.Config("app.main:app", host="127.0.0.1",
                           port=fault_port, log_level="error", lifespan="off")
fault_srv = uvicorn.Server(fault_cfg)
fault_thread = threading.Thread(target=fault_srv.run, daemon=True)
fault_thread.start()
try:
    healthy = False
    for _ in range(100):
        if fault_srv.started:
            healthy = True
            break
        time.sleep(0.05)

    fail_key = f"verify-fault-retry-{stamp}"
    fail_payload = dict(big, audit_key=fail_key)
    if healthy:
        # 该请求会刻意制造异常；静默应用日志，避免注入的堆栈污染验收输出。
        import logging
        solve_logger = logging.getLogger("app.solve")
        prev_level = solve_logger.level
        solve_logger.setLevel(logging.CRITICAL)
        try:
            f1, fb1 = http("POST", fault_base, "/api/solve", fail_payload,
                           headers={"X-Cryotest-Fault": "boom"})
        finally:
            solve_logger.setLevel(prev_level)
        f2, fb2 = http("GET", fault_base, f"/api/audit/{fail_key}")
        ok = (f1 == 500 and fb1.get("status") == "solver_error"
              and f2 == 404 and fb2.get("status") == "not_found")
        check("求解异常：返回 500 且无 pending 半成品（记录 404）", ok,
              f"HTTP {f1}/{f2}")

        f3, fb3 = http("POST", fault_base, "/api/solve", fail_payload)
        f4, fb4 = http("POST", fault_base, "/api/solve", fail_payload)
        ok = (f3 == 200 and fb3.get("status") == "stored"
              and assert_complete(fb3.get("result"))
              and f4 == 200 and fb4.get("status") == "idempotent_replay"
              and fb4.get("created_at") == fb3.get("created_at")
              and fb4.get("result") == fb3.get("result"))
        check("求解异常后：同标识重试成功落盘，重传结果一致", ok,
              f"HTTP {f3}/{f4}")
    else:
        check("故障注入临时服务启动", False, "未在时限内就绪")
finally:
    os.environ.pop("CRYOTEST_FAULT_INJECTION", None)
    fault_srv.should_exit = True
    fault_thread.join(timeout=5)

# ---- 汇总 -----------------------------------------------------------------
print("\n" + "=" * 60)
if failures:
    print(f"{FAIL} 核对未通过：{len(failures)} 项 -> {failures}")
    sys.exit(1)
print(f"{PASS} 全部核对通过：求解器测试、镜像构建、API 冒烟、并发幂等验收")
sys.exit(0)
