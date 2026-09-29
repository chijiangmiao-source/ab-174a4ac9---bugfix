"""低温光谱仪校准液路 —— 带整数下界的最小费用流 API。"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError as PydanticValidationError

from .audit import AuditStore, payload_fingerprint
from .models import SolveRequest, ValidationError, validate_request
from .solver import EdgeSpec, NodeSpec, solve

BASE_DIR = Path(__file__).resolve().parent

# 求解独立使用专用线程池：同标识并发请求中，等待首个结果的请求会在默认
# 线程池的 Condition 上阻塞；若求解也走默认池，等待者可能耗尽线程使首个
# 求解无法执行（饥饿/死锁）。专用池保证首个求解始终能推进。空闲线程由
# ThreadPoolExecutor 的 atexit 机制回收。
SOLVER_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="solver")

app = FastAPI(
    title="校准液路最小费用流服务",
    version="1.0.0",
    description="精确整数：消去下界 → 可行流 → 残量网络负费用改进消除 → 站点势证书",
)
audit_store = AuditStore()
logger = logging.getLogger("app.solve")

# 仅当显式开启 CRYOTEST_FAULT_INJECTION 时生效的测试缝：
# 生产 web 容器从不设置该变量，因此正常请求路径不会受任何影响。
FAULT_HEADER = "X-Cryotest-Fault"


def _run_solver(request: Request, nodes, edges):
    """在线程中执行求解。

    仅在显式设置 ``CRYOTEST_FAULT_INJECTION=1`` 的测试实例中生效的测试缝：
    生产 web 容器从不设置该变量，正常请求路径不受任何影响。
      - ``X-Cryotest-Delay-Ms``：求解前强制等待（毫秒），用于制造并发重叠；
      - ``X-Cryotest-Fault``：求解直接抛出运行异常，用于失败后重试核对。
    """
    if os.environ.get("CRYOTEST_FAULT_INJECTION") == "1":
        delay_ms = request.headers.get("X-Cryotest-Delay-Ms")
        if delay_ms:
            time.sleep(min(max(int(delay_ms), 0), 30_000) / 1000.0)
        if request.headers.get(FAULT_HEADER):
            raise RuntimeError("注入的求解器运行异常（故障注入测试）")
    return solve(nodes, edges)


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

    key = req.audit_key
    fp = payload_fingerprint(raw) if key else None

    if key:
        assert fp is not None
        # reserve 会阻塞等待同载荷的首个请求落盘，必须放到线程中，
        # 否则会卡住事件循环导致首个请求无法恢复执行 commit（死锁）。
        existing, conflict = await asyncio.to_thread(
            audit_store.reserve, key, fp)
        if conflict:
            return JSONResponse(
                status_code=409,
                content={"status": "audit_conflict",
                         "audit_key": key,
                         "msg": "该审计标识已绑定其他载荷；同标识重传必须保持载荷一致"})
        if existing is not None:
            # 顺序重传，或并发同载荷请求等到首个请求落盘后汇合于此：
            # 一律返回同一份完整原始结果与稳定创建时间。
            return JSONResponse(content={
                "status": "idempotent_replay",
                "audit_key": key,
                "fingerprint": fp,
                "created_at": existing["created_at"],
                "result": existing["result"],
            })

    nodes = [NodeSpec(id=n.id, balance=n.balance) for n in req.nodes]
    edges = [EdgeSpec(id=e.id, u=e.source, v=e.target,
                      lower=e.lower, upper=e.upper, cost=e.cost)
             for e in req.edges]

    async def produce() -> tuple[dict[str, Any], Optional[str]]:
        """在 shield 保护下求解并落盘：调用方被取消（如客户端断开）也不影响
        首个请求走到 commit / fail，避免 pending 半成品永久占位。

        求解走专用线程池，避免被默认池上阻塞等待的并发请求饿死。
        """
        loop = asyncio.get_running_loop()
        try:
            res = await loop.run_in_executor(
                SOLVER_EXECUTOR,
                partial(_run_solver, request, nodes, edges))
        except Exception:
            if key:
                audit_store.fail(key, fp)  # type: ignore[arg-type]
            raise
        if key:
            rec = audit_store.commit(key, fp, raw, res)  # type: ignore[arg-type]
            return res, rec["created_at"]
        return res, None

    try:
        result, created_at = await asyncio.shield(produce())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("求解失败 audit_key=%s", key)
        return JSONResponse(
            status_code=500,
            content={"status": "solver_error",
                     "msg": "求解过程发生异常，未保存审计记录；可使用同一审计标识重试"})

    if key:
        return JSONResponse(content={"status": "stored",
                                     "audit_key": key,
                                     "fingerprint": fp,
                                     "created_at": created_at,
                                     "result": result})

    return JSONResponse(content=result)


app.mount("/static",
          StaticFiles(directory=str(BASE_DIR / "static")),
          name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
