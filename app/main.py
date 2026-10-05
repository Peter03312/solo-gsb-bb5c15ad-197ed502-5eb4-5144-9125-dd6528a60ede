"""FastAPI 入口：供水管网隔离预演与复水计划 HTTP API。

接口一览：
  GET    /health                健康检查
  PUT    /api/network           整批录入/覆盖管网（原子事务；修订号 +1，未完计划失效）
  GET    /api/network           查询当前管网
  DELETE /api/network           清空管网（修订号 +1，未完计划失效）
  POST   /api/valves/change     阀门整批变更（校验通过才原子生效；修订号 +1，未完计划失效）
  POST   /api/drill             关阀隔离预演（不写入状态，返回分配/欠供/受限管段）
  GET    /api/status            当前真实阀门状态下的供水分派结果
  POST   /api/plans             提交复水操作清单：逐步预演，全部安全才保存不可变计划
  GET    /api/plans             列出计划
  GET    /api/plans/{plan_id}   查询计划（每步预演与执行结果、进度、修订状态）
  POST   /api/plans/{plan_id}/execute  只执行计划的下一步（乐观修订号，事务内复核）
"""
from __future__ import annotations

import json
import threading
import uuid
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
    Valve,
    build_network,
    find_forbidden_connections,
    rehearse_restoration,
    simulate,
    step_result,
)
from . import repository
from .repository import load_network, replace_all, serialize_network
from .schemas import (
    DrillRequest,
    NetworkLoad,
    PlanExecuteRequest,
    RestorationPlanCreate,
    ValveChangeBatch,
)


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
            "POST /api/plans", "GET /api/plans", "GET /api/plans/{plan_id}",
            "POST /api/plans/{plan_id}/execute",
        ],
    }


# --------------------------------------------------------------------------- #
# 管网录入
# --------------------------------------------------------------------------- #
@app.put("/api/network")
def load_net(payload: NetworkLoad, conn=Depends(get_conn)):
    """整批覆盖式录入。任一设施校验失败则整批回滚，库内原数据保持不变。

    写入成功后在同一事务内把基准修订号 +1，并使所有未完复水计划失效。
    """
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
                revision = repository.bump_revision_and_invalidate(
                    conn, "管网已被整批重新录入（PUT /api/network）"
                )
        except Exception as exc:  # pragma: no cover - 兜底
            conn.rollback()
            return error_response(500, "persist_failed", f"写入失败：{exc}")
    return {
        "message": "管网已整批录入",
        "revision": revision,
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
    revision = repository.get_revision(conn)
    if network is None:
        return {"initialized": False, "revision": revision, "network": None}
    return {
        "initialized": True,
        "revision": revision,
        "network": serialize_network(network),
    }


@app.delete("/api/network")
def clear_network(conn=Depends(get_conn)):
    with _write_lock:
        with conn:
            database.clear_all(conn)
            revision = repository.bump_revision_and_invalidate(
                conn, "管网已被清空（DELETE /api/network）"
            )
    return {"message": "管网数据已清空", "revision": revision}


# --------------------------------------------------------------------------- #
# 阀门整批变更
# --------------------------------------------------------------------------- #
@app.post("/api/valves/change")
def change_valves(batch: ValveChangeBatch, conn=Depends(get_conn)):
    with _write_lock:
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

        # 3) 校验通过：事务内原子生效，并把修订号 +1、未完复水计划置失效
        affected: list[dict] = []
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
            revision = repository.bump_revision_and_invalidate(
                conn, "阀门状态被整批变更（POST /api/valves/change）"
            )
    return {
        "message": "阀门变更已整批生效",
        "revision": revision,
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


# --------------------------------------------------------------------------- #
# 复水计划：提交逐步预演 → 保存不可变计划 → 按下一步逐步执行
# --------------------------------------------------------------------------- #
def _plan_conflict_response(conflict: dict, safe_previews: list[dict]) -> JSONResponse:
    """逐步预演首个互锁冲突：409，返回步骤位置、连通水源与路径；不保存计划。"""
    detail = dict(conflict)
    detail["safe_steps_before"] = conflict["step_no"] - 1
    detail["safe_previews"] = safe_previews
    return error_response(
        409,
        "plan_forbidden_sources_connected",
        "复水计划预演未通过：存在步骤将使不可并联水源经开启管段连通，计划未保存",
        [detail],
    )


@app.post("/api/plans")
def create_plan(payload: RestorationPlanCreate, conn=Depends(get_conn)):
    """提交有序开关步骤：服务以当前管网和阀位逐步预演（不改变实际状态）。

    步骤条目定位校验（非空、阀门存在、编号不重复）不通过 → 400；
    逐步预演中首个互锁冲突 → 409（步骤位置/连通水源/路径，不保存）；
    全部安全 → 保存不可变计划，返回每步阀位与供水/欠供结果及基准修订号。
    """
    with _write_lock:
        network = load_network(conn)
        if network is None:
            return error_response(
                409, "network_not_initialized",
                "尚未录入管网数据，请先调用 PUT /api/network",
            )

        # 条目级定位校验：非空由 schema(min_length=1) 保证；
        # 此处定位非法动作、未知阀门与清单内重复编号
        details: list[dict] = []
        normalized: list[tuple[str, str]] = []
        seen: set[str] = set()
        for i, step in enumerate(payload.steps):
            if step.action not in ("open", "close"):
                details.append({
                    "location": f"steps[{i}].action",
                    "code": "invalid_action",
                    "message": f"非法动作：{step.action!r}（仅允许 open / close）",
                })
                continue
            if step.valve_id not in network.valves:
                details.append({
                    "location": f"steps[{i}].valve_id",
                    "code": "unknown_valve",
                    "message": f"非法/未知阀门编号：{step.valve_id}",
                })
                continue
            if step.valve_id in seen:
                details.append({
                    "location": f"steps[{i}].valve_id",
                    "code": "duplicate_valve",
                    "message": f"阀门 {step.valve_id} 在操作清单中重复出现",
                })
                continue
            seen.add(step.valve_id)
            normalized.append((step.valve_id, step.action))
        if details:
            return error_response(
                400, "invalid_plan", "复水操作清单校验失败，未保存计划", details
            )

        # 逐步预演：从当前阀位起在内存叠加，不触碰库内状态
        previews, conflict = rehearse_restoration(network, normalized)
        if conflict is not None:
            return _plan_conflict_response(conflict, previews)

        # 全部安全：保存不可变计划（与读取同处写锁内，基准修订号即快照修订号）
        revision = repository.get_revision(conn)
        initial_valves = [
            {"valve_id": vid, "pipe_id": v.pipe_id, "is_open": v.is_open}
            for vid, v in sorted(network.valves.items())
        ]
        plan_id = "PL-" + uuid.uuid4().hex[:12]
        with conn:
            repository.save_plan(conn, plan_id, revision, initial_valves, previews)
            row = repository.get_plan_row(conn, plan_id)
            body = repository.serialize_plan(conn, row, revision)

    body["message"] = "复水计划已通过逐步预演并保存（不可变）"
    return body


@app.get("/api/plans")
def list_plans(conn=Depends(get_conn)):
    revision = repository.get_revision(conn)
    rows = repository.list_plan_rows(conn)
    plans = []
    for row in rows:
        summary = repository.serialize_plan_summary(row)
        summary["current_revision"] = revision
        summary["revision_stale"] = revision != row["base_revision"]
        plans.append(summary)
    return {"revision": revision, "plans": plans}


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: str, conn=Depends(get_conn)):
    row = repository.get_plan_row(conn, plan_id)
    if row is None:
        return error_response(
            404, "plan_not_found", f"复水计划不存在：{plan_id}"
        )
    revision = repository.get_revision(conn)
    return repository.serialize_plan(conn, row, revision)


@app.post("/api/plans/{plan_id}/execute")
def execute_plan(
    plan_id: str, payload: PlanExecuteRequest, conn=Depends(get_conn)
):
    """执行计划的下一步。

    只接受“计划的下一步”与调用方所见修订号；同一事务内复核修订与安全性、
    更新阀位与进度。重复提交已完成步骤 → 幂等返回原结果；
    修订号过期或跳步 → 拒绝且不改变任何状态。
    """
    with _write_lock:
        network = load_network(conn)
        if network is None:
            return error_response(
                409, "network_not_initialized",
                "尚未录入管网数据，请先调用 PUT /api/network",
            )
        row = repository.get_plan_row(conn, plan_id)
        if row is None:
            return error_response(
                404, "plan_not_found", f"复水计划不存在：{plan_id}"
            )

        current_revision = repository.get_revision(conn)

        # 1) 修订号复核：过期一律拒绝（无论执行第几步），不改变状态
        if payload.revision != current_revision:
            return error_response(
                409,
                "revision_conflict",
                f"基准修订号已过期：所见 {payload.revision}，当前 {current_revision}；"
                "管网/阀位在计划之外发生过变更，请重新查询计划后再执行",
                [{
                    "location": "revision",
                    "code": "revision_stale",
                    "seen_revision": payload.revision,
                    "current_revision": current_revision,
                }],
            )

        step_rows = repository.get_plan_steps(conn, plan_id)
        next_no = row["progress"] + 1

        # 2) 失效计划：不允许继续执行或重放（其基准修订号必然已落后）
        if row["status"] == "invalidated":
            return error_response(
                409,
                "plan_invalidated",
                f"计划已失效：{row['invalidate_reason']}；请按当前管网重新制定计划",
                [{"code": "plan_invalidated",
                  "invalidate_reason": row["invalidate_reason"]}],
            )

        # 3) 幂等重放：重复提交“已完成”的某一步 → 原样返回，不改变状态。
        #    对已完成计划同样适用（计划不可变，历史结果永久可重放）。
        if payload.step_no <= row["progress"]:
            match = next((r for r in step_rows if r["step_no"] == payload.step_no), None)
            if match is None or match["result_json"] is None:
                return error_response(400, "step_not_found",
                                      f"计划中不存在已完成的第 {payload.step_no} 步")
            result = json.loads(match["result_json"])
            result["message"] = "该步骤已完成，返回原执行结果（幂等，未再次改变状态）"
            result["idempotent_replay"] = True
            result["plan_status"] = row["status"]
            result["plan_id"] = plan_id
            result["revision"] = current_revision
            return result

        # 4) 计划已全部完成、却提交不存在的“下一步”
        if row["status"] == "completed":
            return error_response(
                409, "plan_completed",
                f"计划已全部执行完成（共 {row['total_steps']} 步）"
            )

        # 5) 跳步：pending 计划只接受下一步
        if payload.step_no != next_no:
            return error_response(
                409,
                "step_out_of_order",
                f"只允许执行计划的下一步：期望第 {next_no} 步，收到第 {payload.step_no} 步；"
                "本次拒绝，未改变任何状态",
                [{
                    "location": "step_no",
                    "code": "unexpected_step",
                    "expected_step_no": next_no,
                    "received_step_no": payload.step_no,
                }],
            )

        # 6) 防御性兜底：pending 计划的基准修订号理应等于当前修订号
        if current_revision != row["base_revision"]:
            return error_response(
                409, "plan_invalidated", "计划已失效：基准修订号与计划不一致"
            )

        step_row = next(r for r in step_rows if r["step_no"] == next_no)
        valve_id = step_row["valve_id"]
        action = step_row["action"]
        target_open = action == "open"
        valve = network.valves.get(valve_id)

        # 7) 事务内复核当前阀位与安全性，随后更新阀位和进度（全程持写锁，状态不会被穿插）
        with conn:
            if valve is None:
                conn.rollback()
                return error_response(
                    409, "plan_invalidated",
                    f"计划引用的阀门 {valve_id} 在当前管网中已不存在，计划不可继续",
                )
            if valve.is_open == target_open:
                changed = False
                new_valves = network.valves
                updated = network
            else:
                changed = True
                new_valves = dict(network.valves)
                new_valves[valve_id] = Valve(
                    valve_id, valve.pipe_id, target_open
                )
                updated = Network(
                    network.nodes, network.pipes, new_valves,
                    network.forbidden_pairs, network.valves_by_pipe,
                )

            violations = find_forbidden_connections(updated)
            if violations:
                conn.rollback()
                a, b, path = violations[0]
                return error_response(
                    409,
                    "plan_step_unsafe",
                    f"执行第 {next_no} 步将使不可并联水源 {a} 与 {b} 连通，"
                    "事务已回滚，未改变任何状态",
                    [{
                        "step_no": next_no,
                        "location": f"steps[{next_no - 1}]",
                        "valve_id": valve_id,
                        "action": action,
                        "code": "forbidden_connected",
                        "source_a": a,
                        "source_b": b,
                        "path": path,
                    }],
                )

            if changed:
                conn.execute(
                    "UPDATE valves SET is_open = ? WHERE id = ?",
                    (1 if target_open else 0, valve_id),
                )

            result = step_result(
                next_no, valve_id, action, changed, updated
            )
            completed = next_no >= row["total_steps"]
            repository.mark_step_executed(
                conn, plan_id, next_no, result, completed
            )
            new_row = repository.get_plan_row(conn, plan_id)

        result["message"] = (
            f"第 {next_no} 步已执行"
            + ("（阀门位态未变化）" if not changed else "")
            + ("；计划已全部完成" if completed else "")
        )
        result["idempotent_replay"] = False
        result["plan_status"] = new_row["status"]
        result["plan_id"] = plan_id
        result["revision"] = current_revision
        result["next_step_no"] = (
            next_no + 1 if not completed else None
        )
        return result
