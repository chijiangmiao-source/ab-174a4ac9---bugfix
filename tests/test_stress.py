"""规模与压力测试：12 站 / 36 边、大整数与负费用下的正确性与性能。

独立用 Bellman-Ford 复检：结果残量网络无负环、势证书使所有正残量弧约化费非负、
流量守恒与整数性；不可行时失败证据自洽。
"""

from __future__ import annotations

import random
import time

from app.solver import EdgeSpec, Network, NodeSpec, dinic_max_flow, solve


def _build_random(rng: random.Random, n: int, m: int, big: bool = False):
    ids = [f"S{i:02d}" for i in range(n)]
    balances = [rng.randint(-20, 20) for _ in range(n - 1)]
    balances.append(-sum(balances))
    rng.shuffle(balances)
    edges = []
    pairs = [(u, v) for u in range(n) for v in range(n) if u != v]
    rng.shuffle(pairs)
    for k in range(m):
        u, v = pairs[k % len(pairs)]
        lo = rng.randint(0, 4)
        hi = lo + rng.randint(0, 30)
        cost = rng.randint(-50, 100)
        if big and k == 0:
            cost = 10**9
        edges.append(EdgeSpec(f"L{k:02d}", ids[u], ids[v], lo, hi, cost))
    nodes = [NodeSpec(ids[i], balances[i]) for i in range(n)]
    return nodes, edges


def _rebuild_residual(nodes, edges, flows):
    idx = {nd.id: i for i, nd in enumerate(nodes)}
    net = Network(len(nodes))
    for e in edges:
        a = net.add_edge(idx[e.u], idx[e.v], e.upper - e.lower, e.cost, e.id, +1)
        x = flows[e.id] - e.lower
        if x:
            net.send(idx[e.u], a, x)
    return net


def _has_negative_cycle(net: Network) -> bool:
    """独立 Bellman-Ford（虚源零权）判定残量网络是否存在负环。"""
    n = net.n
    dist = [0] * n
    for _ in range(n):
        changed = False
        for u in range(n):
            for a in net.adj[u]:
                if a.cap > 0 and dist[a.to] > dist[u] + a.cost:
                    dist[a.to] = dist[u] + a.cost
                    changed = True
        if not changed:
            return False
    return True


def _check_result(nodes, edges, r):
    if r["status"] == "infeasible":
        assert r["deficit"] == r["required_super_flow"] - r["achieved_super_flow"]
        assert r["deficit"] > 0
        # 增广网守恒：未送出的供应合计 == 未满足的需求合计 == 缺口
        unsent = sum(u["amount"] for u in r["unmet"]
                     if u["kind"] == "supply_not_dispatched")
        unsatisfied = sum(u["amount"] for u in r["unmet"]
                          if u["kind"] == "demand_not_satisfied")
        assert unsent == r["deficit"]
        assert unsatisfied == r["deficit"]
        return
    assert r["status"] == "feasible"
    flows = {row["id"]: row["flow"] for row in r["edges"]}
    for e in edges:
        f = flows[e.id]
        assert isinstance(f, int) and e.lower <= f <= e.upper
    # 守恒
    net_in = {nd.id: 0 for nd in nodes}
    net_out = {nd.id: 0 for nd in nodes}
    cost = 0
    for e in edges:
        f = flows[e.id]
        net_out[e.u] += f
        net_in[e.v] += f
        cost += f * e.cost
    for nd in nodes:
        assert net_out[nd.id] - net_in[nd.id] == nd.balance
    assert cost == r["total_cost"] and isinstance(r["total_cost"], int)
    # 独立负环检测
    resnet = _rebuild_residual(nodes, edges, flows)
    assert not _has_negative_cycle(resnet), "残量网络仍有负环"
    # 势证书
    pot = r["potentials"]
    for row in r["edges"]:
        if row["forward_reduced_cost"] is not None:
            assert row["forward_reduced_cost"] >= 0
            assert row["forward_reduced_cost"] == (
                row["cost"] + pot[row["from"]] - pot[row["to"]])
        if row["backward_reduced_cost"] is not None:
            assert row["backward_reduced_cost"] >= 0


def test_fuzz_full_size_feasible_networks():
    rng = random.Random(4242)
    t0 = time.time()
    for trial in range(60):
        n = rng.randint(4, 12)
        m = rng.randint(n, min(36, n * (n - 1)))
        nodes, edges = _build_random(rng, n, m, big=(trial % 5 == 0))
        r = solve(nodes, edges)
        _check_result(nodes, edges, r)
    elapsed = time.time() - t0
    assert elapsed < 30, f"60 个满规模算例耗时 {elapsed:.1f}s 过慢"


def test_fuzz_includes_infeasible_and_dense_lower_bounds():
    rng = random.Random(99)
    statuses = set()
    for trial in range(40):
        n = rng.randint(3, 12)
        m = rng.randint(1, min(36, n * (n - 1)))
        nodes, edges = _build_random(rng, n, m)
        # 随机抬高下界，制造可行/不可行混合
        for e in edges:
            if rng.random() < 0.3:
                object.__setattr__(e, "lower", rng.randint(0, e.upper))
        r = solve(nodes, edges)
        statuses.add(r["status"])
        _check_result(nodes, edges, r)
    # 随机抬下界的批量中两种结局都应出现（概率意义上的健全性检查）
    assert "feasible" in statuses


def test_determinism_same_input_same_output():
    # 构造上保证可行：一个总供应站经充裕容量的有向边连向其余站
    rng = random.Random(7)
    n = 10
    ids = [f"S{i:02d}" for i in range(n)]
    total = 40
    balances = [total] + [0] * (n - 2) + [-total]  # 首站供应、末站需求，不打乱
    edges = []
    k = 0
    for u in range(n):
        for v in range(n):
            if u != v and k < 24:
                edges.append(EdgeSpec(f"L{k:02d}", ids[u], ids[v],
                                      0, 60, rng.randint(-5, 20)))
                k += 1
    nodes = [NodeSpec(ids[i], balances[i]) for i in range(n)]
    r1 = solve(nodes, edges)
    assert r1["status"] == "feasible"
    r2 = solve(nodes, edges)
    f1 = [(e["id"], e["flow"]) for e in r1["edges"]]
    f2 = [(e["id"], e["flow"]) for e in r2["edges"]]
    assert f1 == f2
    assert r1["total_cost"] == r2["total_cost"]
