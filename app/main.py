"""FastAPI 入口：供水管网隔离预演 HTTP API。

接口一览（前缀 /api）：
  GET    /health                健康检查
  PUT    /api/network           整批录入/覆盖管网（原子事务）
  GET    /api/network           查询当前管网
  DELETE /api/network           清空管网
  POST   /api/valves/change     阀门整批变更（校验通过才原子生效，冲突拒绝并保持原状态）
  POST   /api/drill             关阀隔离预演（不写入状态，返回分配/欠供/受限管段）
  GET    /api/status            当前真实阀门状态下的供水分派结果
"""
from __future__ import annotations

import threading
from dataclasses import replace
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from . import database
from .database import get_conn
from .engine import (
    Network,
    build_network,
    find_forbidden_connections,
    simulate,
)
from .repository import load_network, replace_all, serialize_network
from .schemas import DrillRequest, NetworkLoad, ValveChangeBatch


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化数据库表（幂等）
    database.init_db()
    yield


app = FastAPI(
    title="供水管网隔离预演服务",
    version="1.0.0",
    description="物业抢修关阀隔离预演：水源上限、单向管段、阀门状态、"
    "用水点优先级分配与不可并联水源互锁校验。",
    lifespan=lifespan,
)

# 写操作串行化，保证“整批校验 + 原子生效”在并发下不被穿插
_write_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# 统一错误响应
# --------------------------------------------------------------------------- #
def error_response(
    status_code: int,
    code: str,
    message: str,
    details: list[dict] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    return JSONResponse(status_code=status_code, content=body)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError):
    """类型层校验失败（如负数、缺字段、非法枚举）：422 + 精确定位。"""
    details = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "body")
        details.append({
            "location": loc or "(body)",
            "code": err.get("type", "invalid"),
            "message": err.get("msg", "参数非法"),
        })
    return error_response(422, "validation_error", "请求参数校验失败", details)


def _require_network(conn) -> Network | JSONResponse:
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )
    return network  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# 基础接口
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok", "service": "water-isolation-drill"}


@app.get("/")
def root():
    return {
        "service": "供水管网隔离预演服务",
        "docs": "/docs",
        "endpoints": [
            "PUT /api/network", "GET /api/network", "DELETE /api/network",
            "POST /api/valves/change", "POST /api/drill", "GET /api/status",
        ],
    }


# --------------------------------------------------------------------------- #
# 管网录入
# --------------------------------------------------------------------------- #
@app.put("/api/network")
def load_net(payload: NetworkLoad, conn=Depends(get_conn)):
    """整批覆盖式录入。任一设施校验失败则整批回滚，库内原数据保持不变。"""
    data = payload.model_dump()
    with _write_lock:
        network, errs = build_network(data)
        if errs:
            return error_response(
                400, "invalid_network",
                "管网数据校验失败，整批录入已拒绝（未写入任何数据）",
                [e.to_dict() for e in errs],
            )
        try:
            with conn:  # 自动提交/回滚
                replace_all(conn, network)
        except Exception as exc:  # pragma: no cover - 兜底
            conn.rollback()
            return error_response(500, "persist_failed", f"写入失败：{exc}")
    return {
        "message": "管网已整批录入",
        "counts": {
            "sources": len(data["sources"]),
            "demands": len(data["demands"]),
            "junctions": len(data["junctions"]),
            "pipes": len(data["pipes"]),
            "valves": len(data["valves"]),
            "forbidden_pairs": len(data["forbidden_pairs"]),
        },
        "network": serialize_network(network),
    }


@app.get("/api/network")
def get_network(conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return {"initialized": False, "network": None}
    return {"initialized": True, "network": serialize_network(network)}


@app.delete("/api/network")
def clear_network(conn=Depends(get_conn)):
    with _write_lock:
        with conn:
            database.clear_all(conn)
    return {"message": "管网数据已清空"}


# --------------------------------------------------------------------------- #
# 阀门整批变更
# --------------------------------------------------------------------------- #
@app.post("/api/valves/change")
def change_valves(batch: ValveChangeBatch, conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )

    # 1) 条目级定位校验
    details: list[dict] = []
    wanted: dict[str, bool] = {}
    for i, op in enumerate(batch.changes):
        loc = f"changes[{i}]"
        if op.action is None and op.is_open is None:
            details.append({
                "location": f"{loc}.action",
                "code": "missing_field",
                "message": "必须提供 action(open/close) 或 is_open 之一",
            })
            continue
        if op.action is not None and op.is_open is not None:
            details.append({
                "location": f"{loc}.action",
                "code": "conflicting_fields",
                "message": "action 与 is_open 不能同时提供",
            })
            continue
        target_state = op.is_open if op.is_open is not None else (op.action == "open")
        if op.valve_id not in network.valves:
            details.append({
                "location": f"{loc}.valve_id",
                "code": "unknown_valve",
                "message": f"非法/未知阀门编号：{op.valve_id}",
            })
            continue
        if op.valve_id in wanted:
            details.append({
                "location": f"{loc}.valve_id",
                "code": "duplicate_valve",
                "message": f"阀门 {op.valve_id} 在同一批次中出现多次",
            })
            continue
        wanted[op.valve_id] = target_state
    if details:
        return error_response(
            400, "invalid_valve_change", "阀门变更批次校验失败，未做任何变更", details
        )

    # 2) 暂存新状态并整批校验互锁水源连通性（暂存不写库）
    tentative_valves = {
        vid: replace(v, is_open=wanted.get(vid, v.is_open))
        for vid, v in network.valves.items()
    }
    tentative = Network(
        network.nodes, network.pipes, tentative_valves,
        network.forbidden_pairs, network.valves_by_pipe,
    )
    violations = find_forbidden_connections(tentative)
    if violations:
        details = [
            {
                "location": "forbidden_pairs",
                "code": "forbidden_connected",
                "message": f"水源 {a} 与 {b} 不可并联，但变更后将经开启管段连通："
                           + " -> ".join(path),
                "source_a": a,
                "source_b": b,
                "path": path,
            }
            for a, b, path in violations
        ]
        return error_response(
            409,
            "forbidden_sources_connected",
            "阀门变更被拒绝：将使不可并联水源经开启管段连通，原阀门状态保持不变",
            details,
        )

    # 3) 校验通过：事务内原子生效
    affected: list[dict] = []
    with _write_lock:
        with conn:
            for vid, target in wanted.items():
                old = network.valves[vid].is_open
                conn.execute(
                    "UPDATE valves SET is_open = ? WHERE id = ?",
                    (1 if target else 0, vid),
                )
                affected.append({
                    "valve_id": vid,
                    "pipe_id": network.valves[vid].pipe_id,
                    "previous_is_open": old,
                    "current_is_open": target,
                    "changed": old != target,
                })
    return {
        "message": "阀门变更已整批生效",
        "applied": affected,
        "closed_valves": sorted(
            vid for vid, v in tentative_valves.items() if not v.is_open
        ),
    }


# --------------------------------------------------------------------------- #
# 关阀隔离预演（只读，不写入状态）
# --------------------------------------------------------------------------- #
@app.post("/api/drill")
def drill(payload: DrillRequest, conn=Depends(get_conn)):
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )

    close_list = payload.close_valves
    details: list[dict] = []
    seen: set[str] = set()
    for i, vid in enumerate(close_list):
        if not isinstance(vid, str) or not vid:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "invalid_valve",
                "message": "阀门编号必须是非空字符串",
            })
        elif vid not in network.valves:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "unknown_valve",
                "message": f"非法/未知阀门编号：{vid}",
            })
        elif vid in seen:
            details.append({
                "location": f"close_valves[{i}]",
                "code": "duplicate_valve",
                "message": f"阀门 {vid} 重复出现",
            })
        seen.add(vid)
    if details:
        return error_response(400, "invalid_drill", "预演参数校验失败", details)

    result = simulate(network, force_closed=set(close_list))
    result["message"] = "预演完成：本次结果为临时计算，未写入任何阀门状态"
    return result


@app.get("/api/status")
def status(conn=Depends(get_conn)):
    """当前库内真实阀门状态下的供水分派结果。"""
    network = load_network(conn)
    if network is None:
        return error_response(
            409, "network_not_initialized",
            "尚未录入管网数据，请先调用 PUT /api/network",
        )
    return simulate(network, force_closed=set())
