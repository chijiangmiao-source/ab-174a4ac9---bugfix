"""请求模型与输入校验：所有数量必须是整数（拒绝浮点、布尔冒充整数）。"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

MAX_NODES = 12
MAX_EDGES = 36
LIMIT_MAGNITUDE = 10**9
ID_PATTERN = re.compile(r"^[A-Za-z0-9_一-鿿.\-]{1,24}$")
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class NodeIn(StrictModel):
    id: str = Field(..., min_length=1, max_length=24)
    balance: StrictInt  # 正=供应，负=需求


class EdgeIn(StrictModel):
    id: str = Field(..., min_length=1, max_length=24)
    source: str = Field(..., alias="from")
    target: str = Field(..., alias="to")
    lower: StrictInt
    upper: StrictInt
    cost: StrictInt

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SolveRequest(StrictModel):
    nodes: list[NodeIn]
    edges: list[EdgeIn] = Field(default_factory=list)
    audit_key: str | None = Field(default=None, max_length=64)

    @field_validator("audit_key")
    @classmethod
    def _audit_key(cls, v: str | None) -> str | None:
        if v is not None and not KEY_PATTERN.match(v):
            raise ValueError(
                "审计标识只允许字母、数字、下划线、连字符，长度 1-64")
        return v


class ValidationError(Exception):
    def __init__(self, errors: list[dict[str, Any]]):
        self.errors = errors
        super().__init__("请求校验失败")


def validate_request(req: SolveRequest) -> None:
    """语义级校验，错误全部定位到具体站点/管路与字段。"""
    errors: list[dict[str, Any]] = []

    nodes = req.nodes
    edges = req.edges

    if len(nodes) == 0:
        errors.append({"loc": ["nodes"], "code": "empty",
                       "msg": "至少需要一个站点"})
    if len(nodes) > MAX_NODES:
        errors.append({"loc": ["nodes"], "code": "too_many",
                       "msg": f"站点数不得超过 {MAX_NODES}"})
    if len(edges) > MAX_EDGES:
        errors.append({"loc": ["edges"], "code": "too_many",
                       "msg": f"管路数不得超过 {MAX_EDGES}"})

    seen_nodes: set[str] = set()
    for i, node in enumerate(nodes):
        if not ID_PATTERN.match(node.id):
            errors.append({"loc": ["nodes", i, "id"], "code": "bad_id",
                           "msg": "站点标识非法（允许字母数字下划线短横线，1-24 字符）"})
        elif node.id in seen_nodes:
            errors.append({"loc": ["nodes", i, "id"], "code": "duplicate",
                           "msg": f"站点标识重复：{node.id}"})
        else:
            seen_nodes.add(node.id)
        if abs(node.balance) > LIMIT_MAGNITUDE:
            errors.append({"loc": ["nodes", i, "balance"], "code": "out_of_range",
                           "msg": f"供需绝对值不得超过 {LIMIT_MAGNITUDE}"})

    seen_edges: set[str] = set()
    for i, e in enumerate(edges):
        if not ID_PATTERN.match(e.id):
            errors.append({"loc": ["edges", i, "id"], "code": "bad_id",
                           "msg": "管路标识非法（允许字母数字下划线短横线，1-24 字符）"})
        elif e.id in seen_edges:
            errors.append({"loc": ["edges", i, "id"], "code": "duplicate",
                           "msg": f"管路标识重复：{e.id}"})
        else:
            seen_edges.add(e.id)
        if e.source not in seen_nodes:
            errors.append({"loc": ["edges", i, "from"], "code": "unknown_node",
                           "msg": f"起点站点不存在：{e.source}"})
        if e.target not in seen_nodes:
            errors.append({"loc": ["edges", i, "to"], "code": "unknown_node",
                           "msg": f"终点站点不存在：{e.target}"})
        if e.source == e.target and e.source in seen_nodes:
            errors.append({"loc": ["edges", i], "code": "self_loop",
                           "msg": "管路起止站点不能相同"})
        if e.lower < 0:
            errors.append({"loc": ["edges", i, "lower"], "code": "negative",
                           "msg": "下界不得为负"})
        if e.lower > e.upper:
            errors.append({"loc": ["edges", i, "upper"], "code": "bad_bounds",
                           "msg": f"上界 {e.upper} 小于下界 {e.lower}"})
        if e.upper > LIMIT_MAGNITUDE:
            errors.append({"loc": ["edges", i, "upper"], "code": "out_of_range",
                           "msg": f"上界不得超过 {LIMIT_MAGNITUDE}"})
        if abs(e.cost) > LIMIT_MAGNITUDE:
            errors.append({"loc": ["edges", i, "cost"], "code": "out_of_range",
                           "msg": f"单位费用绝对值不得超过 {LIMIT_MAGNITUDE}"})

    total_balance = sum(n.balance for n in nodes)
    if total_balance != 0:
        errors.append({"loc": ["nodes"], "code": "imbalance",
                       "msg": f"总供需不平：供应合计与需求合计之差为 {total_balance}，必须为 0",
                       "imbalance": total_balance})

    if errors:
        raise ValidationError(errors)
