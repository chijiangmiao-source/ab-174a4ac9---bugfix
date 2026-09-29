"""低温光谱仪校准液路 —— 带整数下界的最小费用流 API。"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from .audit import (ACQUIRE_CONFLICT, ACQUIRE_OWNER, ACQUIRE_REPLAY,
                    AuditStore, payload_fingerprint)
from .models import SolveRequest, ValidationError, validate_request
from .solver import EdgeSpec, NodeSpec, solve

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(
    title="校准液路最小费用流服务",
    version="1.0.0",
    description="精确整数：消去下界 → 可行流 → 残量网络负费用改进消除 → 站点势证书",
)
audit_store = AuditStore()

# 求解专用线程池：等待同标识在途记录的请求会阻塞在事件循环默认线程池里，
# 独立池保证持有预约的请求总能取得求解线程，不会被等待者饿死。
_solve_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mcf-solve")


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
    owns_reservation = False
    if req.audit_key:
        fp = payload_fingerprint(raw)
        # acquire 在同载荷在途时会阻塞等待，放到线程里以免卡住事件循环
        outcome, record = await asyncio.to_thread(
            audit_store.acquire, req.audit_key, fp)
        if outcome == ACQUIRE_CONFLICT:
            return JSONResponse(
                status_code=409,
                content={"status": "audit_conflict",
                         "audit_key": req.audit_key,
                         "msg": "该审计标识已绑定其他载荷；同标识重传必须保持载荷一致"})
        if outcome == ACQUIRE_REPLAY:
            assert record is not None
            replay = {"status": "idempotent_replay",
                      "audit_key": req.audit_key,
                      "fingerprint": fp,
                      "created_at": record["created_at"],
                      "result": record["result"]}
            return JSONResponse(content=replay)
        assert outcome == ACQUIRE_OWNER
        owns_reservation = True

    nodes = [NodeSpec(id=n.id, balance=n.balance) for n in req.nodes]
    edges = [EdgeSpec(id=e.id, u=e.source, v=e.target,
                      lower=e.lower, upper=e.upper, cost=e.cost)
             for e in req.edges]
    try:
        result: dict[str, Any] = await asyncio.get_running_loop() \
            .run_in_executor(_solve_pool, solve, nodes, edges)
        if req.audit_key:
            assert fp is not None
            record = audit_store.save(req.audit_key, fp, raw, result)
            return JSONResponse(content={"status": "stored",
                                         "audit_key": req.audit_key,
                                         "fingerprint": fp,
                                         "created_at": record["created_at"],
                                         "result": result})
        return JSONResponse(content=result)
    except BaseException:
        if owns_reservation:
            # 求解失败 / 保存失败 / 请求被取消：回滚预约，
            # 后续同标识请求重新求解，不会永久回放半成品
            assert fp is not None
            audit_store.release(req.audit_key, fp)
        raise


app.mount("/static",
          StaticFiles(directory=str(BASE_DIR / "static")),
          name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
