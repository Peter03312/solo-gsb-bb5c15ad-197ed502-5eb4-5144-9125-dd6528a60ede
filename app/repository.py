"""数据库与内存模型之间的转换及事务写入。"""
from __future__ import annotations

import sqlite3
from typing import Optional

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
