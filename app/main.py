"""低温光谱仪校准液路 —— 带整数下界的最小费用流 API。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from .audit import AuditConflict, AuditStore, payload_fingerprint
from .models import SolveRequest, ValidationError, validate_request
from .solver import EdgeSpec, NodeSpec, solve

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="校准液路最小费用流服务",
    version="1.0.0",
    description="精确整数：消去下界 → 可行流 → 残量网络负费用改进消除 → 站点势证书",
)
audit_store = AuditStore()


@app.exception_handler(PydanticValidationError)
async def _pydantic_handler(_: Request, exc: PydanticValidationError):
    errors = []
    for err in exc.errors():
        loc = [str(x) for x in err["loc"] if x != "body"]
        errors.append({"loc": loc, "code": err["type"], "msg": err["msg"]})
    return JSONResponse(status_code=422,
                        content={"status": "invalid_request", "errors": errors})


@app.exception_handler(ValidationError)
async def _semantic_handler(_: Request, exc: ValidationError):
    return JSONResponse(status_code=422,
                        content={"status": "invalid_request", "errors": exc.errors})


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "service": app.title}


@app.get("/api/audit/{key}")
async def get_audit_record(key: str) -> JSONResponse:
    rec = audit_store.get(key)
    if rec is None:
        return JSONResponse(status_code=404,
                            content={"status": "not_found", "audit_key": key})
    return JSONResponse(content={"status": "replayed", **rec})


@app.post("/api/solve")
async def api_solve(request: Request) -> JSONResponse:
    try:
        raw = await request.json()
    except Exception:
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_request",
                     "errors": [{"loc": [], "code": "bad_json",
                                 "msg": "请求体不是合法 JSON"}]})
    if not isinstance(raw, dict):
        return JSONResponse(
            status_code=422,
            content={"status": "invalid_request",
                     "errors": [{"loc": [], "code": "bad_type",
                                 "msg": "请求体必须是 JSON 对象"}]})

    # Pydantic 严格整数校验（拒绝 1.0 / true 冒充整数）
    req = SolveRequest.model_validate(raw)
    validate_request(req)

    fp = None
    if req.audit_key:
        fp = payload_fingerprint(raw)
        existing, conflict = audit_store.lookup_or_reserve(req.audit_key, fp)
        if conflict:
            return JSONResponse(
                status_code=409,
                content={"status": "audit_conflict",
                         "audit_key": req.audit_key,
                         "msg": "该审计标识已绑定其他载荷；同标识重传必须保持载荷一致"})
        if existing is not None:
            replay = {"status": "idempotent_replay",
                      "audit_key": req.audit_key,
                      "fingerprint": fp,
                      "created_at": existing["created_at"],
                      "result": existing["result"]}
            return JSONResponse(content=replay)

    nodes = [NodeSpec(id=n.id, balance=n.balance) for n in req.nodes]
    edges = [EdgeSpec(id=e.id, u=e.source, v=e.target,
                      lower=e.lower, upper=e.upper, cost=e.cost)
             for e in req.edges]
    result: dict[str, Any] = await asyncio.to_thread(solve, nodes, edges)

    if req.audit_key:
        assert fp is not None
        record = audit_store.save(req.audit_key, fp, raw, result)
        return JSONResponse(content={"status": "stored",
                                     "audit_key": req.audit_key,
                                     "fingerprint": fp,
                                     "created_at": record["created_at"],
                                     "result": result})

    return JSONResponse(content=result)


app.mount("/static",
          StaticFiles(directory=str(BASE_DIR / "static")),
          name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
