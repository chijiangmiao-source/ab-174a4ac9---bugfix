"""并发幂等、异载荷竞争与求解失败恢复：真实 HTTP（后台 uvicorn + 多线程）。

覆盖的关键不变量：
- 同 audit_key 同载荷的并发提交只形成一份最终记录：一个 stored，其余
  idempotent_replay，created_at / fingerprint / 完整结果完全一致；
- 同标识异载荷并发竞争：恰好一个 200，其余立即 409 audit_conflict，
  原记录不被覆盖；
- 求解抛异常：返回 500、不留 pending 占位；同标识重试可重新求解并落盘；
- 同载荷并发等待者在首个请求失败时被唤醒并接管求解，而非永久挂起。

故障 / 延迟测试缝仅在 CRYOTEST_FAULT_INJECTION=1 的本测试实例中启用，
生产 web 容器不设置该变量。
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import random
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import uvicorn

from app.main import app, audit_store

H_DELAY = {"X-Cryotest-Delay-Ms": "400"}
H_FAULT = {"X-Cryotest-Fault": "boom"}


# ---------------------------------------------------------------------------
# 测试数据：12 站点 / 36 管路、含负费用边且保证可行的较大液路
# ---------------------------------------------------------------------------


def large_feasible_payload() -> dict:
    rng = random.Random(20260929)
    n = 12
    ids = [f"S{i:02d}" for i in range(n)]
    total = 60
    balances = [total] + [0] * (n - 2) + [-total]  # 首站净供应、末站净需求
    edges = []

    def add(eid, u, v, lo, hi, cost):
        edges.append({"id": eid, "from": ids[u], "to": ids[v],
                      "lower": lo, "upper": hi, "cost": cost})

    add("D00", 0, n - 1, 0, total, 100)  # 昂贵但充裕的直连边：保底可行
    k = 1
    for v in range(1, n - 1):
        add(f"O{k:02d}", 0, v, 0, 40, rng.randint(-8, 12))
        k += 1
    for u in range(1, n - 1):
        add(f"I{k:02d}", u, n - 1, 0, 40, rng.randint(-8, 12))
        k += 1
    while k < 36:
        u, v = rng.sample(range(1, n - 1), 2)
        add(f"M{k:02d}", u, v, 0, 30, rng.randint(-20, 20))  # 含负费改进
        k += 1
    nodes = [{"id": ids[i], "balance": balances[i]} for i in range(n)]
    return {"nodes": nodes, "edges": edges}


INFEASIBLE_PAYLOAD = {
    "nodes": [{"id": "S", "balance": 10}, {"id": "A", "balance": 0},
              {"id": "T", "balance": -10}],
    "edges": [
        {"id": "e1", "from": "S", "to": "A", "lower": 0, "upper": 4, "cost": 1},
        {"id": "e2", "from": "A", "to": "T", "lower": 0, "upper": 5, "cost": 1}],
}


# ---------------------------------------------------------------------------
# 后台真实 HTTP 服务（同进程内的 uvicorn，启用故障注入缝）
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def server(monkeypatch):
    audit_store._records.clear()
    monkeypatch.setenv("CRYOTEST_FAULT_INJECTION", "1")
    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port,
                         log_level="error", lifespan="off")
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.02)
    if not srv.started:
        raise RuntimeError("测试用 uvicorn 未能启动")
    base = f"http://127.0.0.1:{port}"
    yield base
    srv.should_exit = True
    thread.join(timeout=5)


def post(base, payload, headers=None, timeout=30):
    data = json.dumps(payload).encode()
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(base + "/api/solve", data=data,
                                 headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def get(base, path, timeout=10):
    req = urllib.request.Request(base + path, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def post_concurrent(base, calls):
    """calls: list of (payload, headers)；用屏障保证同时发出。"""
    barrier = threading.Barrier(len(calls))
    results: dict[int, tuple] = {}

    def worker(i, payload, headers):
        barrier.wait()
        results[i] = post(base, payload, headers=headers)

    with cf.ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futs = [pool.submit(worker, i, p, h) for i, (p, h) in enumerate(calls)]
        for f in futs:
            f.result(timeout=30)
    return [results[i] for i in range(len(calls))]


# ---------------------------------------------------------------------------
# 结果完整性 / 可复算性核对
# ---------------------------------------------------------------------------


def assert_complete_feasible(result):
    assert result["status"] == "feasible"
    assert isinstance(result["total_cost"], int)
    assert len(result["edges"]) == 36
    assert len(result["potentials"]) == 12
    by_id = {e["id"]: e for e in result["edges"]}
    bal = {n["id"]: n["balance"] for n in result["nodes"]}
    inflow = {nid: 0 for nid in bal}
    outflow = {nid: 0 for nid in bal}
    cost = 0
    for row in result["edges"]:
        assert isinstance(row["flow"], int)
        assert row["lower"] <= row["flow"] <= row["upper"]
        cost += row["flow"] * row["cost"]
        assert row["cost_contribution"] == row["flow"] * row["cost"]
        outflow[row["from"]] += row["flow"]
        inflow[row["to"]] += row["flow"]
        if row["forward_residual"] > 0:
            assert row["forward_reduced_cost"] >= 0
        else:
            assert row["forward_reduced_cost"] is None
        if row["backward_residual"] > 0:
            assert row["backward_reduced_cost"] >= 0
        else:
            assert row["backward_reduced_cost"] is None
    assert cost == result["total_cost"]
    for nid, b in bal.items():
        assert outflow[nid] - inflow[nid] == b
    assert all(n["ok"] for n in result["nodes"])
    return by_id


# ---------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------


def test_concurrent_same_payload_single_record_full_results(server):
    base = server
    payload = dict(large_feasible_payload(), audit_key="cc-same-large")
    # 两个请求都带求解延迟：赢得预留的一方必然仍在求解时，另一方已到达并等待。
    results = post_concurrent(base, [(payload, H_DELAY), (payload, H_DELAY)])
    bodies = [b for _, b in results]
    assert all(code == 200 for code, _ in results), results

    statuses = sorted(b["status"] for b in bodies)
    assert statuses == ["idempotent_replay", "stored"], statuses
    stored = next(b for b in bodies if b["status"] == "stored")
    replay = next(b for b in bodies if b["status"] == "idempotent_replay")

    # 稳定创建时间与指纹：两份完全一致（同一份最终记录）
    assert stored["created_at"] == replay["created_at"]
    assert stored["fingerprint"] == replay["fingerprint"]
    assert stored["audit_key"] == replay["audit_key"] == "cc-same-large"

    # 两份响应都带完整、可复算结果，而不是 pending 半成品
    assert_complete_feasible(stored["result"])
    assert_complete_feasible(replay["result"])
    assert stored["result"] == replay["result"]

    # GET 取回同一份完整记录
    code, rec = get(base, "/api/audit/cc-same-large")
    assert code == 200 and rec["status"] == "replayed"
    assert rec["created_at"] == stored["created_at"]
    assert rec["result"] == stored["result"]


def test_four_concurrent_same_payload_all_get_full_result(server):
    base = server
    payload = dict(large_feasible_payload(), audit_key="cc-same-four")
    results = post_concurrent(base, [(payload, H_DELAY)] * 4)
    assert all(code == 200 for code, _ in results), results
    statuses = sorted(b["status"] for _, b in results)
    assert statuses == ["idempotent_replay"] * 3 + ["stored"], statuses
    created = {b["created_at"] for _, b in results}
    assert len(created) == 1
    ref = results[0][1]["result"]
    assert_complete_feasible(ref)
    for _, b in results[1:]:
        assert b["result"] == ref


def test_concurrent_different_payload_immediate_conflict(server):
    base = server
    payload_a = dict(large_feasible_payload(), audit_key="cc-conflict")
    payload_b = dict(large_feasible_payload(), audit_key="cc-conflict")
    payload_b["edges"][1]["cost"] = 99  # 异载荷
    calls = [(payload_a, H_DELAY), (payload_b, H_DELAY)]
    results = post_concurrent(base, calls)
    codes = sorted(code for code, _ in results)
    assert codes == [200, 409], results

    win_idx = next(i for i, (code, _) in enumerate(results) if code == 200)
    lose_idx = 1 - win_idx
    winner_payload = calls[win_idx][0]
    conflict = results[lose_idx][1]
    assert conflict["status"] == "audit_conflict"
    assert conflict["audit_key"] == "cc-conflict"
    assert "result" not in conflict  # 冲突响应不得携带任何结果

    winner = results[win_idx][1]
    assert winner["status"] == "stored"
    assert_complete_feasible(winner["result"])

    # 原记录属于赢得竞争的载荷，未被异载荷覆盖，创建时间稳定
    code, rec = get(base, "/api/audit/cc-conflict")
    assert code == 200
    assert rec["created_at"] == winner["created_at"]
    assert rec["result"] == winner["result"]

    # 之后再用失败的异载荷请求仍是冲突；赢家载荷顺序重放仍是原记录
    code, body = post(base, calls[lose_idx][0])
    assert code == 409 and body["status"] == "audit_conflict"
    code, body = post(base, winner_payload)
    assert code == 200 and body["status"] == "idempotent_replay"
    assert body["created_at"] == winner["created_at"]


def test_concurrent_infeasible_payload_shares_full_evidence(server):
    base = server
    payload = dict(INFEASIBLE_PAYLOAD, audit_key="cc-infeasible")
    results = post_concurrent(base, [(payload, H_DELAY), (payload, H_DELAY)])
    assert all(code == 200 for code, _ in results), results
    bodies = [b for _, b in results]
    assert {b["status"] for b in bodies} == {"stored", "idempotent_replay"}
    assert bodies[0]["created_at"] == bodies[1]["created_at"]
    for b in bodies:
        r = b["result"]
        assert r["status"] == "infeasible"
        assert r["deficit"] == 6
        assert r["residual_reachable_nodes"] == ["S"]
        assert any(c["edge"] == "e1" and c["saturated"]
                   for c in r["saturated_cut_edges"])
    assert bodies[0]["result"] == bodies[1]["result"]


def test_solver_failure_leaves_no_pending_and_retry_succeeds(server):
    base = server
    payload = dict(large_feasible_payload(), audit_key="cc-fail-retry")

    code, body = post(base, payload, headers=H_FAULT)
    assert code == 500 and body["status"] == "solver_error", body

    # 不留任何半成品：记录查询 404，内部存储也无 pending 占位
    code, rec = get(base, "/api/audit/cc-fail-retry")
    assert code == 404 and rec["status"] == "not_found"
    assert "cc-fail-retry" not in audit_store._records

    # 同标识立即重试：重新求解并完整落盘
    code, body = post(base, payload)
    assert code == 200 and body["status"] == "stored", body
    assert_complete_feasible(body["result"])
    created = body["created_at"]

    # 再次重传仍是同一份原始记录
    code, body = post(base, payload)
    assert code == 200 and body["status"] == "idempotent_replay"
    assert body["created_at"] == created
    assert_complete_feasible(body["result"])


def test_waiting_concurrent_request_takes_over_after_first_fails(server):
    base = server
    payload = dict(large_feasible_payload(), audit_key="cc-fail-wake")

    fault_result = {}
    wait_result = {}

    def faulting_first():
        # 延迟 400ms 制造 pending 窗口，再抛出运行异常
        fault_result[0] = post(base, payload,
                               headers={**H_DELAY, **H_FAULT})

    t_fault = threading.Thread(target=faulting_first)
    t_fault.start()

    # 等待首个请求完成预留（进入 pending）后，再发出同载荷并发请求
    deadline = time.time() + 5
    while time.time() < deadline:
        rec = audit_store._records.get("cc-fail-wake")
        if rec is not None and rec.get("pending"):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("未观察到 pending 预留")

    def waiting_second():
        wait_result[0] = post(base, payload)

    t_wait = threading.Thread(target=waiting_second)
    t_wait.start()
    t_fault.join(timeout=10)
    t_wait.join(timeout=10)

    code_f, body_f = fault_result[0]
    code_w, body_w = wait_result[0]
    assert code_f == 500 and body_f["status"] == "solver_error"
    # 等待者被 fail 唤醒后接管求解：不挂起、不回放半成品
    assert code_w == 200 and body_w["status"] == "stored"
    assert_complete_feasible(body_w["result"])

    code, rec = get(base, "/api/audit/cc-fail-wake")
    assert code == 200 and rec["result"] == body_w["result"]


def test_plain_solve_without_audit_key_unchanged(server):
    base = server
    code, body = post(base, large_feasible_payload())
    assert code == 200
    assert_complete_feasible(body)
    assert "audit_key" not in body and "created_at" not in body
