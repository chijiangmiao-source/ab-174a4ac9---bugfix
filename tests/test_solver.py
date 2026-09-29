"""求解器测试：固定算例 + 随机网络独立核验 + 证书校验。"""

from __future__ import annotations

import itertools
import random

import pytest

from app.solver import EdgeSpec, NodeSpec, solve
from app.models import SolveRequest, ValidationError, validate_request


# ---------------------------------------------------------------------------
# 辅助：独立地核验一个可行结果
# ---------------------------------------------------------------------------


def verify_feasible(r: dict, nodes: list[NodeSpec], edges: list[EdgeSpec]):
    assert r["status"] == "feasible"
    by_id = {e.id: e for e in edges}
    bal = {n.id: n.balance for n in nodes}
    inflow = {n.id: 0 for n in nodes}
    outflow = {n.id: 0 for n in nodes}
    expected_cost = 0

    pot = r["potentials"]
    for row in r["edges"]:
        e = by_id[row["id"]]
        f = row["flow"]
        # 精确整数
        assert isinstance(f, int)
        # 上下界
        assert e.lower <= f <= e.upper, f"{e.id} 流量越界"
        expected_cost += f * e.cost
        outflow[e.u] += f
        inflow[e.v] += f
        # 逐边复算值一致
        assert row["cost_contribution"] == f * e.cost
        assert row["forward_residual"] == e.upper - f
        assert row["backward_residual"] == f - e.lower
        # 约化费用：正残量方向必须非负（最优性证书）
        if row["forward_residual"] > 0:
            rc = e.cost + pot[e.u] - pot[e.v]
            assert rc == row["forward_reduced_cost"]
            assert rc >= 0
        else:
            assert row["forward_reduced_cost"] is None
        if row["backward_residual"] > 0:
            rc = -e.cost + pot[e.v] - pot[e.u]
            assert rc == row["backward_reduced_cost"]
            assert rc >= 0
        else:
            assert row["backward_reduced_cost"] is None

    assert r["total_cost"] == expected_cost
    for nid, b in bal.items():
        assert outflow[nid] - inflow[nid] == b, f"站点 {nid} 净供需不满足"
    assert r["optimality"]["all_reduced_costs_non_negative"] is True
    assert isinstance(r["total_cost"], int)


def solve_raw(spec_nodes, spec_edges):
    nodes = [NodeSpec(i, b) for i, b in spec_nodes]
    edges = [EdgeSpec(eid, u, v, lo, hi, c)
             for eid, u, v, lo, hi, c in spec_edges]
    return solve(nodes, edges), nodes, edges


# ---------------------------------------------------------------------------
# 固定算例
# ---------------------------------------------------------------------------


def test_simple_mincost():
    r, nodes, edges = solve_raw(
        [("S", 6), ("A", 0), ("B", 0), ("T", -6)],
        [("e1", "S", "A", 0, 4, 2),
         ("e2", "S", "B", 0, 6, 5),
         ("e3", "A", "B", 0, 3, 1),
         ("e4", "A", "T", 0, 5, 7),
         ("e5", "B", "T", 0, 6, 2)])
    verify_feasible(r, nodes, edges)
    flows = {x["id"]: x["flow"] for x in r["edges"]}
    # 路径 S-A-B-T 成本 5 最便宜（e3 容量 3 走满）；余 3 单位走 S-B-T 成本 7
    assert flows == {"e1": 3, "e2": 3, "e3": 3, "e4": 0, "e5": 6}
    assert r["total_cost"] == 3 * 2 + 3 * 5 + 3 * 1 + 0 + 6 * 2
    assert r["total_cost"] == 36


def test_lower_bound_forced_flow():
    # 下界强制昂贵管路 e2 必须承载 3 单位
    r, nodes, edges = solve_raw(
        [("S", 5), ("T", -5)],
        [("e1", "S", "T", 0, 5, 1),
         ("e2", "S", "T", 3, 5, 10)])
    verify_feasible(r, nodes, edges)
    flows = {x["id"]: x["flow"] for x in r["edges"]}
    assert flows["e1"] == 2 and flows["e2"] == 3
    assert r["total_cost"] == 32


def test_negative_cost_cycle_gets_canceled():
    r, nodes, edges = solve_raw(
        [("S", 5), ("A", 0), ("B", 0), ("T", -5)],
        [("e1", "S", "A", 0, 5, 0),
         ("e2", "A", "B", 1, 10, 4),
         ("e3", "B", "A", 0, 5, -5),
         ("e4", "B", "T", 0, 5, 0)])
    verify_feasible(r, nodes, edges)
    flows = {x["id"]: x["flow"] for x in r["edges"]}
    # 环 A->B->A 费用 -1，环流 5 次顶到上限：e2=5+5=10，e3=5
    assert flows["e3"] == 5
    assert flows["e2"] == 10
    assert flows["e1"] == 5 and flows["e4"] == 5
    assert r["canceled_negative_cycles"] >= 1
    assert r["total_cost"] == 10 * 4 + 5 * -5  # 15


def test_lower_bound_only_solution():
    r, nodes, edges = solve_raw(
        [("S", 2), ("A", 0), ("T", -2)],
        [("e1", "S", "A", 2, 2, 3),
         ("e2", "A", "T", 2, 2, 4)])
    verify_feasible(r, nodes, edges)
    assert r["total_cost"] == 14


def test_infeasible_capacity_cut():
    r, _, _ = solve_raw(
        [("S", 10), ("A", 0), ("T", -10)],
        [("e1", "S", "A", 0, 4, 1),
         ("e2", "A", "T", 0, 5, 1)])
    assert r["status"] == "infeasible"
    # 真正瓶颈是 S->A 容量 4，最多送达 4，缺口 6
    assert r["deficit"] == 6
    unmet_nodes = {u["node"]: u for u in r["unmet"]}
    assert unmet_nodes["T"]["kind"] == "demand_not_satisfied"
    assert unmet_nodes["T"]["amount"] == 6
    assert unmet_nodes["S"]["kind"] == "supply_not_dispatched"
    assert unmet_nodes["S"]["amount"] == 6
    # 残量可达证据：超源仅能到 S（e1 已顶满），A/T 在割的另一侧
    assert "S" in r["residual_reachable_nodes"]
    assert "A" not in r["residual_reachable_nodes"]
    assert "T" not in r["residual_reachable_nodes"]
    # 割边 e1（S->A 容量 4）被顶满
    cut_ids = {c["edge"] for c in r["saturated_cut_edges"]}
    assert "e1" in cut_ids
    assert all(c["saturated"] for c in r["saturated_cut_edges"])


def test_infeasible_lower_bound_violates_balance():
    # 下界要求流出 5，但 S 只有 3 供应
    r, _, _ = solve_raw(
        [("S", 3), ("T", -3)],
        [("e1", "S", "T", 5, 8, 1)])
    assert r["status"] == "infeasible"
    assert any(u["node"] == "S" for u in r["unmet"])


def test_zero_balance_isolated_nodes_feasible():
    r, nodes, edges = solve_raw([("X", 0), ("Y", 0)], [])
    verify_feasible(r, nodes, edges)
    assert r["total_cost"] == 0
    assert r["potentials"] == {"X": 0, "Y": 0}


def test_parallel_edges_same_endpoints():
    r, nodes, edges = solve_raw(
        [("S", 4), ("T", -4)],
        [("a", "S", "T", 0, 3, 5),
         ("b", "S", "T", 0, 3, 2)])
    verify_feasible(r, nodes, edges)
    flows = {x["id"]: x["flow"] for x in r["edges"]}
    assert flows["b"] == 3 and flows["a"] == 1


def test_negative_node_costs_and_costs():
    r, nodes, edges = solve_raw(
        [("S", 3), ("A", 0), ("T", -3)],
        [("e1", "S", "A", 0, 3, -2),
         ("e2", "A", "T", 0, 3, -7)])
    verify_feasible(r, nodes, edges)
    assert r["total_cost"] == -27


# ---------------------------------------------------------------------------
# 随机算例：穷举流值网格做独立最优性核验（小规模）
# ---------------------------------------------------------------------------


def brute_force_optimal(spec_nodes, spec_edges):
    """在小网络上枚举每条边的可行整数流量，独立求最小费用。"""
    nodes = [NodeSpec(i, b) for i, b in spec_nodes]
    edges = [EdgeSpec(eid, u, v, lo, hi, c)
             for eid, u, v, lo, hi, c in spec_edges]
    node_ids = [n.id for n in nodes]
    ranges = [range(e.lower, e.upper + 1) for e in edges]
    best = None
    for vals in itertools.product(*ranges):
        net = {nid: 0 for nid in node_ids}
        cost = 0
        for e, f in zip(edges, vals):
            net[e.u] -= f
            net[e.v] += f
            cost += f * e.cost
        if all(net[n.id] == -n.balance for n in nodes):
            if best is None or cost < best:
                best = cost
    return best


@pytest.mark.parametrize("seed", range(12))
def test_random_small_networks_match_bruteforce(seed):
    rng = random.Random(seed)
    n = rng.randint(2, 4)
    node_ids = [f"n{i}" for i in range(n)]
    balances = [rng.randint(-3, 3) for _ in range(n)]
    balances[-1] = -sum(balances[:-1])  # 强制总供需为 0
    # 随机打乱谁是源汇无所谓
    m = rng.randint(1, min(4, n * (n - 1)))
    edges = []
    used = set()
    for k in range(m):
        attempts = 0
        while attempts < 50:
            u, v = rng.sample(range(n), 2)
            attempts += 1
            if (u, v) not in used:
                used.add((u, v))
                break
        else:
            continue  # 该节点规模下有向对已用尽
        lo = rng.randint(0, 2)
        hi = lo + rng.randint(0, 3)
        cost = rng.randint(-5, 8)
        edges.append(EdgeSpec(f"e{k}", node_ids[u], node_ids[v], lo, hi, cost))
    nodes = [NodeSpec(node_ids[i], balances[i]) for i in range(n)]

    result = solve(nodes, edges)
    expected = brute_force_optimal(
        [(node_ids[i], balances[i]) for i in range(n)],
        [(e.id, e.u, e.v, e.lower, e.upper, e.cost) for e in edges])

    if expected is None:
        assert result["status"] == "infeasible"
        # 不可行证据自洽：缺口 = 超源需求 - 已送达
        assert result["deficit"] == (
            result["required_super_flow"] - result["achieved_super_flow"])
        assert result["deficit"] > 0
    else:
        verify_feasible(result, nodes, edges)
        assert result["total_cost"] == expected


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


def _validate(payload):
    validate_request(SolveRequest.model_validate(payload))


def test_validation_rejects_float_and_bool():
    with pytest.raises(Exception):
        SolveRequest.model_validate(
            {"nodes": [{"id": "S", "balance": 1.5}], "edges": []})
    with pytest.raises(Exception):
        SolveRequest.model_validate(
            {"nodes": [{"id": "S", "balance": True}], "edges": []})


def test_validation_locates_errors():
    with pytest.raises(ValidationError) as exc:
        _validate({
            "nodes": [{"id": "S", "balance": 5}, {"id": "T", "balance": -4}],
            "edges": [{"id": "e1", "from": "S", "to": "T",
                       "lower": 3, "upper": 2, "cost": 1}]})
    codes = {e["code"] for e in exc.value.errors}
    assert "imbalance" in codes
    assert "bad_bounds" in codes
    locs = [tuple(e["loc"]) for e in exc.value.errors]
    assert ("edges", 0, "upper") in locs


def test_validation_unknown_node_duplicate_and_selfloop():
    with pytest.raises(ValidationError) as exc:
        _validate({
            "nodes": [{"id": "S", "balance": 0}],
            "edges": [
                {"id": "a", "from": "S", "to": "X", "lower": 0, "upper": 1, "cost": 0},
                {"id": "b", "from": "S", "to": "S", "lower": 0, "upper": 1, "cost": 0}]})
    codes = {e["code"] for e in exc.value.errors}
    assert "unknown_node" in codes and "self_loop" in codes


def test_validation_duplicate_ids_and_limits():
    payload = {"nodes": [{"id": "S", "balance": 0}, {"id": "S", "balance": 0}],
               "edges": []}
    with pytest.raises(ValidationError) as exc:
        _validate(payload)
    assert any(e["code"] == "duplicate" for e in exc.value.errors)


def test_validation_too_many_nodes_edges():
    payload = {
        "nodes": [{"id": f"n{i}", "balance": 0} for i in range(13)],
        "edges": [{"id": f"e{i}", "from": "n0", "to": "n1",
                   "lower": 0, "upper": 1, "cost": 0} for i in range(37)]}
    with pytest.raises(ValidationError) as exc:
        _validate(payload)
    codes = {e["code"] for e in exc.value.errors}
    assert "too_many" in codes
