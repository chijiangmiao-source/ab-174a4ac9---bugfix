"""API 冒烟与审计幂等测试（通过 ASGI TestClient，真实 FastAPI 路由）。"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, audit_store  # noqa: E402


@pytest.fixture()
def client():
    audit_store._records.clear()
    with TestClient(app) as c:
        yield c


BASIC = {
    "nodes": [{"id": "S", "balance": 6}, {"id": "A", "balance": 0},
              {"id": "B", "balance": 0}, {"id": "T", "balance": -6}],
    "edges": [
        {"id": "e1", "from": "S", "to": "A", "lower": 0, "upper": 4, "cost": 2},
        {"id": "e2", "from": "S", "to": "B", "lower": 0, "upper": 6, "cost": 5},
        {"id": "e3", "from": "A", "to": "B", "lower": 0, "upper": 3, "cost": 1},
        {"id": "e4", "from": "A", "to": "T", "lower": 0, "upper": 5, "cost": 7},
        {"id": "e5", "from": "B", "to": "T", "lower": 0, "upper": 6, "cost": 2},
    ],
}


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "最小费用流" in r.text
    assert "/api/solve" in r.text


def test_solve_feasible_flow_and_cost(client):
    r = client.post("/api/solve", json=BASIC)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "feasible"
    assert data["total_cost"] == 36
    flows = {e["id"]: e["flow"] for e in data["edges"]}
    assert flows == {"e1": 3, "e2": 3, "e3": 3, "e4": 0, "e5": 6}
    # 每条正残量边约化费用非负
    for e in data["edges"]:
        if e["forward_residual"] > 0:
            assert e["forward_reduced_cost"] >= 0
        if e["backward_residual"] > 0:
            assert e["backward_reduced_cost"] >= 0
    # 站点守恒
    for n in data["nodes"]:
        assert n["ok"] is True


def test_solve_infeasible_evidence(client):
    payload = {
        "nodes": [{"id": "S", "balance": 10}, {"id": "A", "balance": 0},
                  {"id": "T", "balance": -10}],
        "edges": [
            {"id": "e1", "from": "S", "to": "A", "lower": 0, "upper": 4, "cost": 1},
            {"id": "e2", "from": "A", "to": "T", "lower": 0, "upper": 5, "cost": 1},
        ]}
    r = client.post("/api/solve", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "infeasible"
    assert data["deficit"] == 6
    assert isinstance(data["residual_reachable_nodes"], list)
    assert any(u["node"] == "T" and u["amount"] == 6
               and u["kind"] == "demand_not_satisfied" for u in data["unmet"])
    assert any(c["edge"] == "e1" and c["saturated"] for c in data["saturated_cut_edges"])


def test_imbalance_rejected_with_location(client):
    payload = {
        "nodes": [{"id": "S", "balance": 5}, {"id": "T", "balance": -3}],
        "edges": []}
    r = client.post("/api/solve", json=payload)
    assert r.status_code == 422
    codes = [e["code"] for e in r.json()["errors"]]
    assert "imbalance" in codes


def test_float_rejected(client):
    payload = {"nodes": [{"id": "S", "balance": 1.0}], "edges": []}
    r = client.post("/api/solve", json=payload)
    assert r.status_code == 422


def test_illegal_bounds_rejected(client):
    payload = {
        "nodes": [{"id": "S", "balance": 0}],
        "edges": [{"id": "e1", "from": "S", "to": "S",
                   "lower": 5, "upper": 2, "cost": 0}]}
    r = client.post("/api/solve", json=payload)
    assert r.status_code == 422
    codes = {e["code"] for e in r.json()["errors"]}
    assert "bad_bounds" in codes and "self_loop" in codes


def test_audit_same_payload_replays_original(client):
    payload = dict(BASIC, audit_key="run-001")
    r1 = client.post("/api/solve", json=payload)
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["status"] == "stored"
    created1 = d1["created_at"]

    r2 = client.post("/api/solve", json=payload)
    d2 = r2.json()
    assert d2["status"] == "idempotent_replay"
    assert d2["created_at"] == created1
    assert d2["result"]["total_cost"] == d1["result"]["total_cost"]

    # 记录可取回
    r3 = client.get("/api/audit/run-001")
    assert r3.status_code == 200
    assert r3.json()["status"] == "replayed"


def test_audit_changed_payload_same_key_rejected(client):
    payload = dict(BASIC, audit_key="run-002")
    r1 = client.post("/api/solve", json=payload)
    assert r1.json()["status"] == "stored"

    changed = dict(BASIC, audit_key="run-002")
    changed["edges"][0]["cost"] = 99
    r2 = client.post("/api/solve", json=changed)
    assert r2.status_code == 409
    assert r2.json()["status"] == "audit_conflict"

    # 原记录未被覆盖
    r3 = client.get("/api/audit/run-002")
    assert r3.json()["result"]["edges"][0]["cost"] == 2


def test_audit_key_format(client):
    payload = dict(BASIC, audit_key="bad key!!")
    r = client.post("/api/solve", json=payload)
    assert r.status_code == 422


def test_unknown_audit_record_404(client):
    assert client.get("/api/audit/nope").status_code == 404


def test_large_integer_costs_exact(client):
    payload = {
        "nodes": [{"id": "S", "balance": 1000000},
                  {"id": "T", "balance": -1000000}],
        "edges": [{"id": "e1", "from": "S", "to": "T",
                   "lower": 0, "upper": 1000000, "cost": 1000000000}]}
    r = client.post("/api/solve", json=payload)
    data = r.json()
    assert data["status"] == "feasible"
    assert data["total_cost"] == 10**15  # 精确整数，无浮点误差
    assert isinstance(data["total_cost"], int)
