"""带整数下界的最小费用流 —— 全程精确整数运算。

算法步骤（与业务描述一一对应）：

1. **消去下界**：令 ``f = lower + x``（0 <= x <= upper-lower），
   把下界预流量从节点净供需中扣除，得到调整后的节点供需 ``s'``。
2. **求可行流**：加超源 S / 超汇 T（``s'>0`` 接 S，``s'<0`` 接 T），
   用 Dinic 在整数残量网络上求最大流；S 侧全部饱和当且仅当可行。
   不可行时返回未满足量与残量可达集合（最小割失败证据）。
3. **消除负费用改进**：在原网络的 *完整残量网络*（含每条边的正反向残量弧）
   上反复消去 **最小均值负环**（Karp 精确分式判定 + 紧弧找环），
   直到不存在均值为负的环。最小均值消环具有多项式迭代界，
   且停止条件是"无负环"这一最优性判定本身，而非限定轮次。
4. **站点势证书**：在最终残量网络上以 Bellman-Ford（虚源到各点距离 0）
   求整数势 p，使每条正残量弧 a→b 满足 ``cost + p[a] - p[b] >= 0``，
   即每条正残量边约化费用非负，逐边可复算。

不使用任何浮点数：环均值以 ``分子/分母`` 交叉相乘比较。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# 输入模型
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeSpec:
    id: str
    balance: int  # 正=净供应，负=净需求


@dataclass(frozen=True)
class EdgeSpec:
    id: str
    u: str
    v: str
    lower: int
    upper: int
    cost: int


# ---------------------------------------------------------------------------
# 残量网络
# ---------------------------------------------------------------------------


class Arc:
    __slots__ = ("to", "rev", "cap", "cost", "eid", "direction")

    def __init__(self, to: int, rev: int, cap: int, cost: int,
                 eid: Optional[str], direction: int):
        self.to = to
        self.rev = rev          # 反向弧在 adj[to] 中的下标
        self.cap = cap
        self.cost = cost
        self.eid = eid          # 所属原始边标识（超源/超汇弧为 None）
        self.direction = direction  # +1 原始正向，-1 原始反向，0 超源弧


class Network:
    def __init__(self, n: int):
        self.n = n
        self.adj: list[list[Arc]] = [[] for _ in range(n)]

    def add_edge(self, u: int, v: int, cap: int, cost: int,
                 eid: Optional[str] = None, direction: int = 0) -> Arc:
        fwd = Arc(v, len(self.adj[v]), cap, cost, eid, direction)
        rev = Arc(u, len(self.adj[u]), 0, -cost, eid, -direction if eid else 0)
        self.adj[u].append(fwd)
        self.adj[v].append(rev)
        return fwd

    def send(self, tail: int, arc: Arc, delta: int) -> None:
        """沿 tail -> arc.to 推 delta 单位整数流。"""
        arc.cap -= delta
        self.adj[arc.to][arc.rev].cap += delta


# ---------------------------------------------------------------------------
# 分式比较（分母恒正，交叉相乘，无浮点）
# ---------------------------------------------------------------------------


def _frac_lt(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] * b[1] < b[0] * a[1]


def _frac_eq(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] * b[1] == b[0] * a[1]


# ---------------------------------------------------------------------------
# Dinic 最大流（整数）
# ---------------------------------------------------------------------------


def dinic_max_flow(net: Network, s: int, t: int) -> int:
    n = net.n
    total = 0
    INF = 10**60

    while True:
        level = [-1] * n
        level[s] = 0
        queue = [s]
        head = 0
        while head < len(queue):
            u = queue[head]
            head += 1
            for arc in net.adj[u]:
                if arc.cap > 0 and level[arc.to] < 0:
                    level[arc.to] = level[u] + 1
                    queue.append(arc.to)
        if level[t] < 0:
            return total

        it = [0] * n

        def dfs(u: int, pushed: int) -> int:
            if u == t:
                return pushed
            while it[u] < len(net.adj[u]):
                arc = net.adj[u][it[u]]
                if arc.cap > 0 and level[arc.to] == level[u] + 1:
                    tr = dfs(arc.to, min(pushed, arc.cap))
                    if tr:
                        net.send(u, arc, tr)
                        return tr
                it[u] += 1
            return 0

        while True:
            pushed = dfs(s, INF)
            if not pushed:
                break
            total += pushed


def residual_reachable(net: Network, source: int) -> set[int]:
    seen = {source}
    queue = [source]
    head = 0
    while head < len(queue):
        u = queue[head]
        head += 1
        for arc in net.adj[u]:
            if arc.cap > 0 and arc.to not in seen:
                seen.add(arc.to)
                queue.append(arc.to)
    return seen


# ---------------------------------------------------------------------------
# Tarjan 强连通分量（残量图上）
# ---------------------------------------------------------------------------


def tarjan_scc(net: Network) -> list[list[int]]:
    n = net.n
    idx = [-1] * n
    low = [0] * n
    on_stack = [False] * n
    stack: list[int] = []
    components: list[list[int]] = []
    counter = 0

    def strong(v: int) -> None:
        nonlocal counter
        idx[v] = low[v] = counter
        counter += 1
        stack.append(v)
        on_stack[v] = True
        for arc in net.adj[v]:
            if arc.cap <= 0:
                continue
            w = arc.to
            if idx[w] < 0:
                strong(w)
                low[v] = min(low[v], low[w])
            elif on_stack[w]:
                low[v] = min(low[v], idx[w])
        if low[v] == idx[v]:
            comp: list[int] = []
            while True:
                w = stack.pop()
                on_stack[w] = False
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for v in range(n):
        if idx[v] < 0:
            strong(v)
    return components


# ---------------------------------------------------------------------------
# Karp 最小环均值（在单个强连通分量内，精确整数）
# ---------------------------------------------------------------------------


def _karp_mean(net: Network, comp: list[int]
               ) -> Optional[tuple[int, int]]:
    """返回分量内最小环均值 (num, den)，den>0。"""
    nc = len(comp)
    if nc < 2:
        return None
    in_comp = [False] * net.n
    for v in comp:
        in_comp[v] = True
    root = comp[0]

    # F[k][v]：从 root 出发恰好走 k 条弧到 v 的最小权
    F: list[list[Optional[int]]] = [[None] * net.n for _ in range(nc + 1)]
    F[0][root] = 0
    for k in range(1, nc + 1):
        cur = F[k]
        prev = F[k - 1]
        for a in comp:
            base = prev[a]
            if base is None:
                continue
            for arc in net.adj[a]:
                if arc.cap > 0 and in_comp[arc.to]:
                    val = base + arc.cost
                    b = arc.to
                    if cur[b] is None or val < cur[b]:
                        cur[b] = val

    # Karp 定理：μ* = min_v max_{0<=k<n} (F_n(v)-F_k(v))/(n-k)
    best: Optional[tuple[int, int]] = None
    for v in comp:
        fn = F[nc][v]
        if fn is None:
            continue
        v_best: Optional[tuple[int, int]] = None
        for k in range(nc):
            fk = F[k][v]
            if fk is None:
                continue
            cand = (fn - fk, nc - k)  # den = nc-k > 0
            if v_best is None or _frac_lt(v_best, cand):
                v_best = cand  # 该 v 取最大比值
        if v_best is not None and (best is None or _frac_lt(v_best, best)):
            best = v_best  # 跨 v 取最小
    return best


def _find_tight_cycle(net: Network, num: int, den: int
                      ) -> Optional[list[tuple[int, Arc]]]:
    """在权值 w' = w*den - num 的紧弧子图中找一个环（均值恰为 num/den）。"""
    n = net.n
    dist = [0] * n  # 虚源到各点 0 权，等价于全 0 初始化后松弛
    for _ in range(n - 1):
        changed = False
        for u in range(n):
            du = dist[u]
            for arc in net.adj[u]:
                if arc.cap <= 0:
                    continue
                w = arc.cost * den - num
                if dist[arc.to] > du + w:
                    dist[arc.to] = du + w
                    changed = True
        if not changed:
            break

    # 紧弧：w' + dist[u] - dist[v] == 0，在其中 DFS 找环
    WHITE, GRAY, BLACK = 0, 1, 2
    color = [WHITE] * n
    parent: list[Optional[tuple[int, Arc]]] = [None] * n
    cycle: list[tuple[int, Arc]] = []

    def dfs(u: int) -> bool:
        color[u] = GRAY
        for arc in net.adj[u]:
            if arc.cap <= 0:
                continue
            v = arc.to
            w = arc.cost * den - num
            if w + dist[u] - dist[v] != 0:
                continue
            if color[v] == WHITE:
                parent[v] = (u, arc)
                if dfs(v):
                    return True
            elif color[v] == GRAY:
                # 回溯出环：v ... u 再加 u->v
                path: list[tuple[int, Arc]] = [(u, arc)]
                x = u
                while x != v:
                    p, par = parent[x]  # type: ignore[misc]
                    path.append((p, par))
                    x = p
                path.reverse()
                cycle.extend(path)
                return True
        color[u] = BLACK
        return False

    for v in range(n):
        if color[v] == WHITE and dfs(v):
            return cycle
    return None


def minimum_negative_cycle(net: Network
                           ) -> Optional[list[tuple[int, Arc]]]:
    """找一条最小均值负环（以 (尾节点, 弧) 序列返回）；无负环返回 None。"""
    best_mean: Optional[tuple[int, int]] = None
    for comp in tarjan_scc(net):
        mean = _karp_mean(net, comp)
        if mean is not None and (best_mean is None or _frac_lt(mean, best_mean)):
            best_mean = mean
    if best_mean is None or best_mean[0] >= 0:
        return None
    cycle = _find_tight_cycle(net, best_mean[0], best_mean[1])
    if cycle is None:  # 理论上不可达：保底防御
        raise RuntimeError("最小均值负环存在但紧弧重构失败")
    return cycle


# ---------------------------------------------------------------------------
# Bellman-Ford 站点势
# ---------------------------------------------------------------------------


def reduced_costs_nonnegative(net: Network, n: int) -> list[int]:
    """返回使所有正残量弧约化费用非负的整数势；残量网络存在负环则报错。"""
    pot = [0] * n
    for iteration in range(n):
        changed = False
        for u in range(n):
            du = pot[u]
            for arc in net.adj[u]:
                if arc.cap > 0 and pot[arc.to] > du + arc.cost:
                    pot[arc.to] = du + arc.cost
                    changed = True
        if not changed:
            break
        if iteration == n - 1:
            raise RuntimeError("残量网络仍存在负费用环，求解器不变量被破坏")
    for u in range(n):
        for arc in net.adj[u]:
            if arc.cap > 0 and arc.cost + pot[u] - pot[arc.to] < 0:
                raise RuntimeError("站点势约化费用校验失败")
    return pot


# ---------------------------------------------------------------------------
# 主求解流程
# ---------------------------------------------------------------------------


def solve(nodes: list[NodeSpec], edges: list[EdgeSpec]) -> dict:
    idx = {node.id: i for i, node in enumerate(nodes)}
    n = len(nodes)
    S = n
    T = n + 1

    # ---- 第 1 步：消去下界，计算调整后供需 s' -----------------------------
    sprime = [node.balance for node in nodes]
    for e in edges:
        u, v = idx[e.u], idx[e.v]
        sprime[u] -= e.lower   # 下界预流从 u 送出
        sprime[v] += e.lower   # 进入 v

    feas = Network(n + 2)
    orig_arcs: list[tuple[int, Arc, EdgeSpec]] = []
    for e in edges:
        arc = feas.add_edge(idx[e.u], idx[e.v], e.upper - e.lower, e.cost,
                            e.id, +1)
        orig_arcs.append((idx[e.u], arc, e))

    super_arcs: list[tuple[int, Arc]] = []  # (node, S->node 或 node->T 弧)
    required = 0
    for i, sp in enumerate(sprime):
        if sp > 0:
            super_arcs.append((i, feas.add_edge(S, i, sp, 0)))
            required += sp
        elif sp < 0:
            super_arcs.append((i, feas.add_edge(i, T, -sp, 0)))

    # ---- 第 2 步：Dinic 可行流判定 -----------------------------------------
    pushed = dinic_max_flow(feas, S, T)

    if pushed != required:
        return _infeasible_result(nodes, edges, sprime, feas, S, T,
                                  super_arcs, orig_arcs, required, pushed)

    # 读取 x_e，恢复真实流量 f = lower + x
    flows: dict[str, int] = {}
    for u, arc, e in orig_arcs:
        x = (e.upper - e.lower) - arc.cap
        flows[e.id] = e.lower + x

    # ---- 第 3 步：在原网络的完整残量网络上消除负费用改进 -------------------
    opt = Network(n)
    opt_arcs: list[tuple[int, Arc, EdgeSpec]] = []
    for e in edges:
        u, v = idx[e.u], idx[e.v]
        arc = opt.add_edge(u, v, e.upper - e.lower, e.cost, e.id, +1)
        seed = flows[e.id] - e.lower
        if seed > 0:
            opt.send(u, arc, seed)
        opt_arcs.append((u, arc, e))

    cancel_count = 0
    while True:
        cycle = minimum_negative_cycle(opt)
        if cycle is None:
            break
        delta = min(arc.cap for _, arc in cycle)
        if delta <= 0:
            raise RuntimeError("负环上存在零容量弧")
        for tail, arc in cycle:
            opt.send(tail, arc, delta)
        cancel_count += 1

    # ---- 第 4 步：站点势与逐边复算 ----------------------------------------
    pot = reduced_costs_nonnegative(opt, n)

    final_flows: dict[str, int] = {}
    for u, arc, e in opt_arcs:
        x = (e.upper - e.lower) - arc.cap
        final_flows[e.id] = e.lower + x

    return _feasible_result(nodes, edges, opt, final_flows, pot,
                            cancel_count, sprime)


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------


def _feasible_result(nodes: list[NodeSpec], edges: list[EdgeSpec],
                     net: Network, flows: dict[str, int], pot: list[int],
                     cancel_count: int, sprime: list[int]) -> dict:
    idx = {node.id: i for i, node in enumerate(nodes)}
    inflow = [0] * len(nodes)
    outflow = [0] * len(nodes)
    edge_rows = []
    total_cost = 0

    for e in edges:
        f = flows[e.id]
        total_cost += f * e.cost
        u, v = idx[e.u], idx[e.v]
        outflow[u] += f
        inflow[v] += f
        fwd_residual = e.upper - f
        bwd_residual = f - e.lower
        # 正残量弧的约化费用；容量为 0 的方向给 null（不存在该弧）
        fwd_rc = (e.cost + pot[u] - pot[v]) if fwd_residual > 0 else None
        bwd_rc = (-e.cost + pot[v] - pot[u]) if bwd_residual > 0 else None
        edge_rows.append({
            "id": e.id,
            "from": e.u,
            "to": e.v,
            "lower": e.lower,
            "upper": e.upper,
            "cost": e.cost,
            "flow": f,
            "cost_contribution": f * e.cost,
            "forward_residual": fwd_residual,
            "backward_residual": bwd_residual,
            "forward_reduced_cost": fwd_rc,
            "backward_reduced_cost": bwd_rc,
        })

    node_rows = []
    for i, node in enumerate(nodes):
        node_rows.append({
            "id": node.id,
            "balance": node.balance,
            "inflow": inflow[i],
            "outflow": outflow[i],
            "net_out": outflow[i] - inflow[i],
            "ok": outflow[i] - inflow[i] == node.balance,
        })

    all_rc_ok = all(
        (row["forward_reduced_cost"] is None or row["forward_reduced_cost"] >= 0)
        and (row["backward_reduced_cost"] is None or row["backward_reduced_cost"] >= 0)
        for row in edge_rows
    )

    return {
        "status": "feasible",
        "total_cost": total_cost,
        "total_flow": sum(flows.values()),
        "canceled_negative_cycles": cancel_count,
        "potentials": {node.id: pot[i] for i, node in enumerate(nodes)},
        "edges": edge_rows,
        "nodes": node_rows,
        "adjusted_balances": {node.id: sprime[i]
                              for i, node in enumerate(nodes)},
        "optimality": {
            "negative_residual_cycle": False,
            "all_reduced_costs_non_negative": all_rc_ok,
        },
        "algorithm": [
            "lower_bound_elimination",
            "dinic_integral_max_flow",
            "minimum_mean_negative_cycle_canceling",
            "bellman_ford_node_potentials",
        ],
    }


def _infeasible_result(nodes: list[NodeSpec], edges: list[EdgeSpec],
                       sprime: list[int], feas: Network, S: int, T: int,
                       super_arcs: list[tuple[int, Arc]],
                       orig_arcs: list[tuple[int, Arc, EdgeSpec]],
                       required: int, pushed: int) -> dict:
    idx = {node.id: i for i, node in enumerate(nodes)}

    unmet: list[dict] = []
    for i, arc in super_arcs:
        if arc.cap > 0:  # 超源/超汇弧未饱和
            if sprime[i] > 0:
                unmet.append({"node": nodes[i].id,
                              "kind": "supply_not_dispatched",
                              "amount": arc.cap})
            else:
                unmet.append({"node": nodes[i].id,
                              "kind": "demand_not_satisfied",
                              "amount": arc.cap})

    reachable = residual_reachable(feas, S)
    reachable_stations = [node.id for i, node in enumerate(nodes)
                          if i in reachable]

    # 最小割证据：从残量可达侧指向不可达侧的原始边必已顶到上界
    blocked = []
    for u, arc, e in orig_arcs:
        if u in reachable and arc.to not in reachable and arc.to != T:
            x = (e.upper - e.lower) - arc.cap
            blocked.append({
                "edge": e.id,
                "from": e.u,
                "to": e.v,
                "flow": e.lower + x,
                "upper": e.upper,
                "saturated": arc.cap == 0,
            })

    cut_net = sum(sprime[i] for i in reachable if i < len(nodes))

    return {
        "status": "infeasible",
        "required_super_flow": required,
        "achieved_super_flow": pushed,
        "deficit": required - pushed,
        "unmet": unmet,
        "residual_reachable_nodes": reachable_stations,
        "cut_adjusted_net_supply": cut_net,
        "saturated_cut_edges": blocked,
        "adjusted_balances": {node.id: sprime[i]
                              for i, node in enumerate(nodes)},
        "algorithm": [
            "lower_bound_elimination",
            "dinic_integral_max_flow",
            "residual_min_cut_witness",
        ],
    }
