"""数据库与内存模型之间的转换及事务写入。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from .engine import Network, Node, Pipe, Valve


def load_network(conn: sqlite3.Connection) -> Optional[Network]:
    node_rows = conn.execute("SELECT * FROM nodes").fetchall()
    if not node_rows:
        return None
    nodes: dict[str, Node] = {}
    for r in node_rows:
        nodes[r["id"]] = Node(
            id=r["id"],
            type=r["type"],
            capacity=r["capacity"],
            demand=r["demand"],
            priority=r["priority"],
        )
    pipes = {
        r["id"]: Pipe(r["id"], r["from_node"], r["to_node"], r["capacity"])
        for r in conn.execute("SELECT * FROM pipes").fetchall()
    }
    valves = {
        r["id"]: Valve(r["id"], r["pipe_id"], bool(r["is_open"]))
        for r in conn.execute("SELECT * FROM valves").fetchall()
    }
    valves_by_pipe: dict[str, list[str]] = {}
    for vid, v in valves.items():
        valves_by_pipe.setdefault(v.pipe_id, []).append(vid)
    for vids in valves_by_pipe.values():
        vids.sort()
    pairs = [
        (r["source_a"], r["source_b"])
        for r in conn.execute("SELECT source_a, source_b FROM pairs").fetchall()
    ]
    return Network(nodes, pipes, valves, pairs, valves_by_pipe)


def replace_all(conn: sqlite3.Connection, network: Network) -> None:
    """在调用方事务内覆盖式写入全部设施。"""
    cur = conn.cursor()
    cur.execute("DELETE FROM pairs")
    cur.execute("DELETE FROM valves")
    cur.execute("DELETE FROM pipes")
    cur.execute("DELETE FROM nodes")
    cur.executemany(
        "INSERT INTO nodes (id, type, capacity, demand, priority) VALUES (?,?,?,?,?)",
        [
            (n.id, n.type, n.capacity, n.demand, n.priority)
            for n in network.nodes.values()
        ],
    )
    cur.executemany(
        "INSERT INTO pipes (id, from_node, to_node, capacity) VALUES (?,?,?,?)",
        [(p.id, p.from_node, p.to_node, p.capacity) for p in network.pipes.values()],
    )
    cur.executemany(
        "INSERT INTO valves (id, pipe_id, is_open) VALUES (?,?,?)",
        [(v.id, v.pipe_id, 1 if v.is_open else 0) for v in network.valves.values()],
    )
    cur.executemany(
        "INSERT INTO pairs (source_a, source_b) VALUES (?,?)",
        network.forbidden_pairs,
    )


def serialize_network(network: Network) -> dict:
    return {
        "sources": [
            {"id": n.id, "capacity": n.capacity}
            for n in network.nodes.values()
            if n.type == "source"
        ],
        "demands": [
            {"id": n.id, "demand": n.demand, "priority": n.priority}
            for n in network.nodes.values()
            if n.type == "demand"
        ],
        "junctions": [
            {"id": n.id}
            for n in network.nodes.values()
            if n.type == "junction"
        ],
        "pipes": [
            {"id": p.id, "from_node": p.from_node, "to_node": p.to_node,
             "capacity": p.capacity}
            for p in network.pipes.values()
        ],
        "valves": [
            {"id": v.id, "pipe_id": v.pipe_id, "is_open": v.is_open}
            for v in network.valves.values()
        ],
        "forbidden_pairs": [
            {"source_a": a, "source_b": b} for a, b in network.forbidden_pairs
        ],
    }


# --------------------------------------------------------------------------- #
# 基准修订号：录网 / 清空 / 阀门变更时在同一事务内 +1，并使未完计划失效
# --------------------------------------------------------------------------- #
def get_revision(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    return int(row["value"]) if row else 0


def bump_revision_and_invalidate(
    conn: sqlite3.Connection, reason: str
) -> int:
    """修订号 +1，并把所有未完（pending）计划置为失效。须在调用方事务内执行。"""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO meta(key, value) VALUES('revision', 1) "
        "ON CONFLICT(key) DO UPDATE SET value = value + 1"
    )
    now = _now()
    cur.execute(
        "UPDATE plans SET status = 'invalidated', invalidated_at = ?, "
        "invalidate_reason = ? WHERE status = 'pending'",
        (now, reason),
    )
    return get_revision(conn)


# --------------------------------------------------------------------------- #
# 复水计划持久化（计划保存后步骤不可变；执行结果仅由执行事务写入一次）
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_plan(
    conn: sqlite3.Connection,
    plan_id: str,
    base_revision: int,
    initial_valves: list[dict],
    steps: list[dict],
) -> None:
    """在调用方事务内写入不可变计划与其逐步预演结果。"""
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO plans(id, status, base_revision, progress, total_steps, "
        "initial_valves, created_at) VALUES (?,?,?,?,?,?,?)",
        (
            plan_id, "pending", base_revision, 0, len(steps),
            json.dumps(initial_valves, ensure_ascii=False), _now(),
        ),
    )
    cur.executemany(
        "INSERT INTO plan_steps(plan_id, step_no, valve_id, action, preview_json) "
        "VALUES (?,?,?,?,?)",
        [
            (
                plan_id,
                s["step_no"],
                s["valve_id"],
                s["action"],
                json.dumps(s, ensure_ascii=False),
            )
            for s in steps
        ],
    )


def get_plan_row(conn: sqlite3.Connection, plan_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM plans WHERE id = ?", (plan_id,)
    ).fetchone()


def get_plan_steps(conn: sqlite3.Connection, plan_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM plan_steps WHERE plan_id = ? ORDER BY step_no", (plan_id,)
    ).fetchall()


def list_plan_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM plans ORDER BY created_at DESC, id DESC"
    ).fetchall()


def serialize_plan_summary(row: sqlite3.Row) -> dict:
    return {
        "plan_id": row["id"],
        "status": row["status"],
        "base_revision": row["base_revision"],
        "current_revision": None,  # 由调用方按当前修订号填充
        "progress": row["progress"],
        "total_steps": row["total_steps"],
        "created_at": row["created_at"],
        "completed_at": row["completed_at"],
        "invalidated_at": row["invalidated_at"],
        "invalidate_reason": row["invalidate_reason"],
    }


def serialize_plan(
    conn: sqlite3.Connection, row: sqlite3.Row, current_revision: int
) -> dict:
    """组装单个计划的完整视图：含每步预演（preview）与执行实际结果（result）。"""
    out = serialize_plan_summary(row)
    out["current_revision"] = current_revision
    out["revision_stale"] = current_revision != row["base_revision"]
    out["initial_valves"] = json.loads(row["initial_valves"])
    step_rows = get_plan_steps(conn, row["id"])
    steps: list[dict[str, Any]] = []
    for r in step_rows:
        preview = json.loads(r["preview_json"])
        item = {
            "step_no": r["step_no"],
            "valve_id": r["valve_id"],
            "action": r["action"],
            "state": "executed" if r["result_json"] is not None else "pending",
            "preview": preview,
            "result": json.loads(r["result_json"]) if r["result_json"] else None,
            "executed_at": r["executed_at"],
        }
        steps.append(item)
    out["steps"] = steps
    next_no = row["progress"] + 1
    out["next_step_no"] = next_no if next_no <= row["total_steps"] else None
    return out


def mark_step_executed(
    conn: sqlite3.Connection,
    plan_id: str,
    step_no: int,
    result: dict,
    completed: bool,
) -> None:
    """在调用方事务内写入某步执行结果并推进进度；末步完成时关闭计划。"""
    cur = conn.cursor()
    cur.execute(
        "UPDATE plan_steps SET result_json = ?, executed_at = ? "
        "WHERE plan_id = ? AND step_no = ?",
        (json.dumps(result, ensure_ascii=False), _now(), plan_id, step_no),
    )
    if completed:
        cur.execute(
            "UPDATE plans SET progress = total_steps, status = 'completed', "
            "completed_at = ? WHERE id = ?",
            (_now(), plan_id),
        )
    else:
        cur.execute(
            "UPDATE plans SET progress = ? WHERE id = ?",
            (step_no, plan_id),
        )
