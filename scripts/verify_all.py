#!/usr/bin/env python3
"""verify 容器入口：在 Compose 环境内核对全部要求后自行退出。

检查项（全部通过退出码 0，任一失败非 0）：
  1. 镜像内应用可导入、静态页已打包（镜像构建核对）；
  2. 求解器与 API 测试（pytest，真实 FastAPI 路由；含并发审计提交、
     求解异常注入后的释放与重试等故障注入用例）；
  3. web 服务健康检查 /healthz；
  4. 本题 API 冒烟：可行流最优值与证书、不可行证据、输入错误定位、
     审计标识同载荷重传 / 改载荷冲突；
  5. 并发审计提交（真实 HTTP）：同标识同载荷并发复核较大液路模型，
     两份响应均含一致的完整结果与稳定创建时间；同标识异载荷竞争、
     顺序幂等重传与记录查询。
"""

from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("WEB_BASE_URL", "http://web:8080")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
STEPS = 5

failures: list[str] = []


def step(no: int, title: str):
    print(f"\n\033[36m[{no}/{STEPS}]\033[0m {title}", flush=True)


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


def result_complete(result: dict, edges: int, nodes: int) -> bool:
    """完整、可复算：可行性状态、逐管路流量、总成本、势与约化费用证据。"""
    if not isinstance(result, dict) or result.get("status") != "feasible":
        return False
    if len(result.get("edges", [])) != edges:
        return False
    if len(result.get("nodes", [])) != nodes:
        return False
    if not isinstance(result.get("total_cost"), int):
        return False
    if len(result.get("potentials", {})) != nodes:
        return False
    for row in result["edges"]:
        if not isinstance(row.get("flow"), int):
            return False
        if not (row["lower"] <= row["flow"] <= row["upper"]):
            return False
        if row["forward_residual"] > 0 and not row["forward_reduced_cost"] >= 0:
            return False
        if row["backward_residual"] > 0 and not row["backward_reduced_cost"] >= 0:
            return False
    return result.get("optimality", {}).get("all_reduced_costs_non_negative") is True


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

# ---- 2. 求解器 + API 测试（含并发与故障注入用例） -------------------------
step(2, "求解器与 API 测试（pytest，含并发审计与求解异常重试用例）")
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

keyed = dict(FEASIBLE, audit_key=f"verify-smoke-{os.getpid()}-{int(time.time())}")
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

# ---- 5. 并发审计提交（真实 HTTP） -----------------------------------------
step(5, "并发审计提交与同标识竞争（真实 HTTP）")

# 12 站点 / 36 管路、含负费用改进的较大液路模型
# （确定性结果：消去 5 个负费用环，最优总成本 954）
LARGE = {'nodes': [{'id': 'V00', 'balance': -25}, {'id': 'V01', 'balance': 17},
                   {'id': 'V02', 'balance': 1}, {'id': 'V03', 'balance': -11},
                   {'id': 'V04', 'balance': 5}, {'id': 'V05', 'balance': 3},
                   {'id': 'V06', 'balance': 5}, {'id': 'V07', 'balance': -7},
                   {'id': 'V08', 'balance': -2}, {'id': 'V09', 'balance': 20},
                   {'id': 'V10', 'balance': -7}, {'id': 'V11', 'balance': 1}],
         'edges': [
             {'id': 'P00', 'from': 'V02', 'to': 'V11', 'lower': 1, 'upper': 15, 'cost': 15},
             {'id': 'P01', 'from': 'V04', 'to': 'V09', 'lower': 1, 'upper': 15, 'cost': -17},
             {'id': 'P02', 'from': 'V07', 'to': 'V08', 'lower': 2, 'upper': 25, 'cost': 23},
             {'id': 'P03', 'from': 'V05', 'to': 'V04', 'lower': 1, 'upper': 16, 'cost': -14},
             {'id': 'P04', 'from': 'V03', 'to': 'V02', 'lower': 3, 'upper': 5, 'cost': 22},
             {'id': 'P05', 'from': 'V07', 'to': 'V02', 'lower': 2, 'upper': 22, 'cost': -3},
             {'id': 'P06', 'from': 'V06', 'to': 'V10', 'lower': 0, 'upper': 4, 'cost': 56},
             {'id': 'P07', 'from': 'V09', 'to': 'V11', 'lower': 2, 'upper': 27, 'cost': 23},
             {'id': 'P08', 'from': 'V06', 'to': 'V05', 'lower': 1, 'upper': 20, 'cost': 42},
             {'id': 'P09', 'from': 'V03', 'to': 'V09', 'lower': 3, 'upper': 11, 'cost': 35},
             {'id': 'P10', 'from': 'V08', 'to': 'V07', 'lower': 3, 'upper': 21, 'cost': -23},
             {'id': 'P11', 'from': 'V02', 'to': 'V10', 'lower': 3, 'upper': 22, 'cost': -30},
             {'id': 'P12', 'from': 'V04', 'to': 'V05', 'lower': 0, 'upper': 25, 'cost': 44},
             {'id': 'P13', 'from': 'V09', 'to': 'V04', 'lower': 2, 'upper': 5, 'cost': 19},
             {'id': 'P14', 'from': 'V08', 'to': 'V00', 'lower': 0, 'upper': 17, 'cost': 53},
             {'id': 'P15', 'from': 'V11', 'to': 'V00', 'lower': 0, 'upper': 18, 'cost': 2},
             {'id': 'P16', 'from': 'V04', 'to': 'V06', 'lower': 1, 'upper': 6, 'cost': 51},
             {'id': 'P17', 'from': 'V02', 'to': 'V05', 'lower': 2, 'upper': 16, 'cost': -4},
             {'id': 'P18', 'from': 'V08', 'to': 'V10', 'lower': 3, 'upper': 7, 'cost': -12},
             {'id': 'P19', 'from': 'V11', 'to': 'V03', 'lower': 1, 'upper': 17, 'cost': -14},
             {'id': 'P20', 'from': 'V08', 'to': 'V06', 'lower': 0, 'upper': 22, 'cost': -40},
             {'id': 'P21', 'from': 'V09', 'to': 'V10', 'lower': 1, 'upper': 13, 'cost': 10},
             {'id': 'P22', 'from': 'V07', 'to': 'V00', 'lower': 1, 'upper': 4, 'cost': -6},
             {'id': 'P23', 'from': 'V08', 'to': 'V01', 'lower': 0, 'upper': 14, 'cost': 40},
             {'id': 'P24', 'from': 'V04', 'to': 'V07', 'lower': 0, 'upper': 6, 'cost': 45},
             {'id': 'P25', 'from': 'V02', 'to': 'V06', 'lower': 1, 'upper': 9, 'cost': 24},
             {'id': 'P26', 'from': 'V02', 'to': 'V01', 'lower': 2, 'upper': 20, 'cost': -37},
             {'id': 'P27', 'from': 'V01', 'to': 'V04', 'lower': 1, 'upper': 18, 'cost': 34},
             {'id': 'P28', 'from': 'V01', 'to': 'V02', 'lower': 3, 'upper': 16, 'cost': -23},
             {'id': 'P29', 'from': 'V07', 'to': 'V06', 'lower': 3, 'upper': 28, 'cost': 51},
             {'id': 'P30', 'from': 'V01', 'to': 'V03', 'lower': 1, 'upper': 6, 'cost': -16},
             {'id': 'P31', 'from': 'V02', 'to': 'V03', 'lower': 0, 'upper': 4, 'cost': 7},
             {'id': 'P32', 'from': 'V01', 'to': 'V07', 'lower': 2, 'upper': 24, 'cost': 42},
             {'id': 'P33', 'from': 'V11', 'to': 'V06', 'lower': 0, 'upper': 6, 'cost': -36},
             {'id': 'P34', 'from': 'V09', 'to': 'V00', 'lower': 1, 'upper': 22, 'cost': 48},
             {'id': 'P35', 'from': 'V06', 'to': 'V02', 'lower': 0, 'upper': 25, 'cost': 57}]}

run_id = f"{os.getpid()}-{int(time.time())}"

# 5.1 同标识同载荷并发提交较大模型：只形成一份记录，
#     两份响应均含一致的完整结果与稳定创建时间
conc_key = f"verify-conc-{run_id}"
conc_payload = dict(LARGE, audit_key=conc_key)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    fut = [pool.submit(http, "POST", "/api/solve", copy.deepcopy(conc_payload))
           for _ in range(2)]
    pair = [f.result() for f in fut]
codes = sorted(s for s, _ in pair)
bodies = [b for _, b in pair]
statuses = sorted(b.get("status") for b in bodies)
ok = codes == [200, 200] and statuses == ["idempotent_replay", "stored"]
check("并发同标识同载荷：一个 stored、一个 idempotent_replay", ok,
      f"HTTP {codes} {statuses}")

ok = (bodies[0].get("created_at") is not None
      and bodies[0].get("created_at") == bodies[1].get("created_at"))
check("并发同标识同载荷：创建时间一致且稳定", ok,
      f"created_at={bodies[0].get('created_at')}")

ok = all(result_complete(b.get("result"), edges=36, nodes=12) for b in bodies)
check("并发同标识同载荷：两份响应均含完整可复算结果"
      "（可行性状态/逐管路流量/总成本/势证据）", ok)

ok = (bodies[0].get("result", {}).get("total_cost") == 954
      and bodies[0].get("result", {}).get("canceled_negative_cycles") == 5
      and bodies[0].get("result") == bodies[1].get("result"))
check("并发同标识同载荷：结果一致（总成本 954，消去 5 个负费用环）", ok)

# 5.2 顺序幂等重传与记录查询：同一创建时间与完整结果
s4, b4 = http("POST", "/api/solve", copy.deepcopy(conc_payload))
ok = (s4 == 200 and b4.get("status") == "idempotent_replay"
      and b4.get("created_at") == bodies[0].get("created_at")
      and result_complete(b4.get("result"), edges=36, nodes=12))
check("并发后顺序重传：幂等回放同一完整记录", ok,
      f"HTTP {s4} {b4.get('status')}")

s5, b5 = http("GET", f"/api/audit/{conc_key}")
ok = (s5 == 200 and b5.get("created_at") == bodies[0].get("created_at")
      and result_complete(b5.get("result"), edges=36, nodes=12))
check("记录查询：留档与并发响应一致", ok, f"HTTP {s5}")

# 5.3 同标识异载荷并发竞争：恰好一个 stored，另一个立即 409
race_key = f"verify-race-{run_id}"
race_a = dict(LARGE, audit_key=race_key)
race_b = copy.deepcopy(race_a)
race_b["edges"][0]["cost"] = 999
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    fa = pool.submit(http, "POST", "/api/solve", race_a)
    fb = pool.submit(http, "POST", "/api/solve", race_b)
    race = [fa.result(), fb.result()]
codes = sorted(s for s, _ in race)
statuses = sorted(b.get("status") for _, b in race)
ok = codes == [200, 409] and statuses == ["audit_conflict", "stored"]
check("竞争异载荷：恰好一个 stored、另一个 409 audit_conflict", ok,
      f"HTTP {codes} {statuses}")

stored_body = next(b for s, b in race if s == 200)
ok = result_complete(stored_body.get("result"), edges=36, nodes=12)
check("竞争异载荷：胜出记录完整可复算", ok)

s6, b6 = http("GET", f"/api/audit/{race_key}")
ok = (s6 == 200 and b6.get("created_at") == stored_body.get("created_at")
      and b6.get("result") == stored_body.get("result"))
check("竞争异载荷：留档未被败方覆盖", ok, f"HTTP {s6}")

# ---- 汇总 -----------------------------------------------------------------
print("\n" + "=" * 60)
if failures:
    print(f"{FAIL} 核对未通过：{len(failures)} 项 -> {failures}")
    sys.exit(1)
print(f"{PASS} 全部核对通过：求解器测试、镜像构建、API 冒烟、并发审计提交")
sys.exit(0)
