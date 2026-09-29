"""并发审计提交：同标识同载荷只形成一份完整记录；失败预约回滚后可重试。

通过 ASGI TestClient（真实 FastAPI 路由）+ 多线程制造真实并发；
用 monkeypatch 注入求解延时 / 异常，使竞态窗口确定性可复现。
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.main import app, audit_store  # noqa: E402

# 12 站点 / 36 管路、含负费用改进的较大液路模型（求解器确定性结果：
# 消去 5 个负费用环，最优总成本 954）
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

EXPECTED_TOTAL_COST = 954
EXPECTED_CANCELED_CYCLES = 5


@pytest.fixture()
def client():
    audit_store._records.clear()
    with TestClient(app) as c:
        yield c
    audit_store._records.clear()


def _assert_complete_feasible_result(result: dict,
                                     expected_total_cost: int | None = EXPECTED_TOTAL_COST,
                                     expected_cycles: int | None = EXPECTED_CANCELED_CYCLES
                                     ) -> None:
    """完整、可复算的原始结果：可行性状态、逐管路流量、总成本、势证据。"""
    assert result["status"] == "feasible"
    assert len(result["edges"]) == 36
    assert len(result["nodes"]) == 12
    if expected_total_cost is not None:
        assert result["total_cost"] == expected_total_cost
    if expected_cycles is not None:
        assert result["canceled_negative_cycles"] == expected_cycles
    assert len(result["potentials"]) == 12
    for row in result["edges"]:
        assert isinstance(row["flow"], int)
        assert row["lower"] <= row["flow"] <= row["upper"]
        if row["forward_residual"] > 0:
            assert row["forward_reduced_cost"] >= 0
        if row["backward_residual"] > 0:
            assert row["backward_reduced_cost"] >= 0
    assert result["optimality"]["all_reduced_costs_non_negative"] is True


def _post_concurrently(client, payloads: list[dict]) -> list:
    """用线程池同时发出请求，按提交顺序返回 (status_code, json|None) 列表。"""
    barrier = threading.Barrier(len(payloads))
    responses: list = [None] * len(payloads)

    def post(i: int, payload: dict) -> None:
        barrier.wait()
        r = client.post("/api/solve", json=payload)
        try:
            body = r.json()
        except ValueError:
            body = None  # 例如 500 纯文本响应
        responses[i] = (r.status_code, body)

    threads = [threading.Thread(target=post, args=(i, p))
               for i, p in enumerate(payloads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "并发请求未在限定时间内完成"
    return responses


def test_concurrent_same_payload_single_complete_record(client, monkeypatch):
    """同标识同载荷并发提交：只形成一份记录，两份响应均为完整结果。"""
    real_solve = main.solve

    def slow_solve(nodes, edges):
        time.sleep(0.4)  # 放大竞态窗口，确保两请求真正重叠
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", slow_solve)
    payload = dict(LARGE, audit_key="conc-same-001")

    responses = _post_concurrently(client, [payload, copy.deepcopy(payload)])

    assert [s for s, _ in responses] == [200, 200]
    bodies = [b for _, b in responses]
    # 恰好一个 stored、一个 idempotent_replay，且创建时间一致
    assert sorted(b["status"] for b in bodies) == ["idempotent_replay", "stored"]
    assert bodies[0]["created_at"] == bodies[1]["created_at"]
    assert bodies[0]["fingerprint"] == bodies[1]["fingerprint"]
    for body in bodies:
        _assert_complete_feasible_result(body["result"])
    # 两份响应的结果逐字节一致（同一份记录）
    assert bodies[0]["result"] == bodies[1]["result"]

    # 只形成一份最终记录，查询结果与响应一致
    assert audit_store.all_keys() == ["conc-same-001"]
    rec = client.get("/api/audit/conc-same-001").json()
    assert rec["created_at"] == bodies[0]["created_at"]
    _assert_complete_feasible_result(rec["result"])


def test_concurrent_many_same_payload_single_record(client, monkeypatch):
    """8 路并发同标识同载荷：全部拿到同一创建时间的完整结果。"""
    real_solve = main.solve

    def slow_solve(nodes, edges):
        time.sleep(0.3)
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", slow_solve)
    payload = dict(LARGE, audit_key="conc-same-008")

    responses = _post_concurrently(
        client, [copy.deepcopy(payload) for _ in range(8)])

    assert all(s == 200 for s, _ in responses)
    bodies = [b for _, b in responses]
    assert sum(1 for b in bodies if b["status"] == "stored") == 1
    assert sum(1 for b in bodies if b["status"] == "idempotent_replay") == 7
    created = {b["created_at"] for b in bodies}
    assert len(created) == 1
    for body in bodies:
        _assert_complete_feasible_result(body["result"])
    assert audit_store.all_keys() == ["conc-same-008"]


def test_conflicting_payload_rejected_while_solve_in_flight(client, monkeypatch):
    """首个请求求解期间，异载荷同标识请求必须立即 409，不得等待。"""
    real_solve = main.solve
    entered = threading.Event()
    finish = threading.Event()

    def blocking_solve(nodes, edges):
        entered.set()
        assert finish.wait(timeout=10)
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", blocking_solve)
    payload = dict(LARGE, audit_key="conc-conflict-001")
    changed = copy.deepcopy(payload)
    changed["edges"][0]["cost"] = 999  # 同标识、异载荷

    owner_response: list = []

    def owner_post():
        r = client.post("/api/solve", json=payload)
        owner_response.append((r.status_code, r.json()))

    t = threading.Thread(target=owner_post)
    t.start()
    try:
        assert entered.wait(timeout=5), "首个请求未进入求解"
        t0 = time.monotonic()
        r = client.post("/api/solve", json=changed)
        elapsed = time.monotonic() - t0
        assert r.status_code == 409
        assert r.json()["status"] == "audit_conflict"
        assert elapsed < 5, "异载荷冲突应当立即拒绝，而非等待在途请求"
    finally:
        finish.set()
        t.join(timeout=15)

    status, body = owner_response[0]
    assert status == 200 and body["status"] == "stored"
    _assert_complete_feasible_result(body["result"])
    # 原记录未被异载荷请求污染
    rec = client.get("/api/audit/conc-conflict-001").json()
    assert rec["result"]["edges"][0]["cost"] == 15


def test_concurrent_competing_payloads_exactly_one_record(client, monkeypatch):
    """同标识异载荷并发竞争：恰好一个 stored，另一个 409，只留一份记录。"""
    real_solve = main.solve

    def slow_solve(nodes, edges):
        time.sleep(0.3)
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", slow_solve)
    payload_a = dict(LARGE, audit_key="conc-race-001")
    payload_b = copy.deepcopy(payload_a)
    payload_b["edges"][0]["cost"] = 999

    responses = _post_concurrently(client, [payload_a, payload_b])

    statuses = sorted(b["status"] for _, b in responses)
    codes = sorted(s for s, _ in responses)
    assert codes == [200, 409]
    assert statuses == ["audit_conflict", "stored"]
    # 哪份载荷胜出不确定，但胜者的记录必须完整且与留档一致
    stored = next(b for s, b in responses if s == 200)
    _assert_complete_feasible_result(stored["result"],
                                     expected_total_cost=None,
                                     expected_cycles=None)
    assert audit_store.all_keys() == ["conc-race-001"]
    rec = client.get("/api/audit/conc-race-001").json()
    assert rec["created_at"] == stored["created_at"]
    assert rec["result"] == stored["result"]


def test_solver_failure_releases_reservation_and_retry_succeeds(monkeypatch):
    """求解异常：不留半成品预约，同标识同载荷重试可重新求解并完整保存。"""
    audit_store._records.clear()
    client = TestClient(app, raise_server_exceptions=False)
    real_solve = main.solve
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky_solve(nodes, edges):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise RuntimeError("注入的求解异常")
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", flaky_solve)
    payload = dict(LARGE, audit_key="conc-fail-001")
    try:
        r1 = client.post("/api/solve", json=payload)
        assert r1.status_code == 500
        # 半成品不得残留：同标识查询不应拿到空结果
        assert audit_store.get("conc-fail-001") is None
        assert client.get("/api/audit/conc-fail-001").status_code == 404

        # 重试：重新求解并完整保存
        r2 = client.post("/api/solve", json=payload)
        assert r2.status_code == 200
        body = r2.json()
        assert body["status"] == "stored"
        _assert_complete_feasible_result(body["result"])

        # 此后顺序重传仍为幂等回放，创建时间稳定
        r3 = client.post("/api/solve", json=payload)
        assert r3.json()["status"] == "idempotent_replay"
        assert r3.json()["created_at"] == body["created_at"]
        _assert_complete_feasible_result(r3.json()["result"])
    finally:
        client.close()
        audit_store._records.clear()


def test_failed_attempt_leaves_no_phantom_conflict(monkeypatch):
    """失败的尝试不绑定标识：之后同标识异载荷按全新请求处理，而非 409。"""
    audit_store._records.clear()
    client = TestClient(app, raise_server_exceptions=False)
    real_solve = main.solve

    def always_fail(nodes, edges):
        raise RuntimeError("注入的求解异常")

    monkeypatch.setattr(main, "solve", always_fail)
    payload = dict(LARGE, audit_key="conc-fail-003")
    try:
        r1 = client.post("/api/solve", json=payload)
        assert r1.status_code == 500
        assert audit_store.get("conc-fail-003") is None

        # 同标识异载荷：没有已完成记录可冲突，应正常求解保存
        monkeypatch.setattr(main, "solve", real_solve)
        changed = copy.deepcopy(payload)
        changed["edges"][0]["cost"] = 999
        r2 = client.post("/api/solve", json=changed)
        assert r2.status_code == 200
        assert r2.json()["status"] == "stored"
        _assert_complete_feasible_result(r2.json()["result"],
                                         expected_total_cost=None,
                                         expected_cycles=None)
    finally:
        client.close()
        audit_store._records.clear()


def test_concurrent_waiter_takes_over_after_owner_failure(monkeypatch):
    """持有者求解异常时，等待中的同载荷请求接管并产出完整记录。"""
    audit_store._records.clear()
    client = TestClient(app, raise_server_exceptions=False)
    real_solve = main.solve
    calls = {"n": 0}
    lock = threading.Lock()

    def fail_first_solve(nodes, edges):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            time.sleep(0.3)  # 让第二个请求进入等待后再失败
            raise RuntimeError("注入的求解异常")
        return real_solve(nodes, edges)

    monkeypatch.setattr(main, "solve", fail_first_solve)
    payload = dict(LARGE, audit_key="conc-fail-002")
    try:
        responses = _post_concurrently(
            client, [payload, copy.deepcopy(payload)])

        codes = sorted(s for s, _ in responses)
        assert codes == [200, 500]
        stored = next(b for s, b in responses if s == 200)
        assert stored["status"] == "stored"
        _assert_complete_feasible_result(stored["result"])

        # 最终只有一份完整记录，后续重传幂等回放
        assert audit_store.all_keys() == ["conc-fail-002"]
        r = client.post("/api/solve", json=payload)
        assert r.json()["status"] == "idempotent_replay"
        assert r.json()["created_at"] == stored["created_at"]
        _assert_complete_feasible_result(r.json()["result"])
    finally:
        client.close()
        audit_store._records.clear()
