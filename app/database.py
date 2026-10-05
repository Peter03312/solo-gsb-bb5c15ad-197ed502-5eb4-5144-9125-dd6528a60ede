"""SQLite 持久化层。

每张表对应一类设施：
- nodes       水源 / 用水点 / 连接点（节点）
- pipes       单向管段（容量、起终点）
- valves      阀门（位于某管段上，状态 open / closed）
- pairs       不可并联的水源对
- meta        全局键值（当前管网基准修订号 revision）
- plans       复水计划（不可变；pending/completed/invalidated）
- plan_steps  计划步骤（创建时的逐步预演 + 执行后的实际结果）
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "water.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('source', 'demand', 'junction')),
    capacity     REAL,   -- 水源：供水上限
    demand       REAL,   -- 用水点：需求量
    priority     INTEGER -- 用水点：优先级（数值越大越优先）
);

CREATE TABLE IF NOT EXISTS pipes (
    id        TEXT PRIMARY KEY,
    from_node TEXT NOT NULL,
    to_node   TEXT NOT NULL,
    capacity  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS valves (
    id       TEXT PRIMARY KEY,
    pipe_id  TEXT NOT NULL,
    is_open  INTEGER NOT NULL CHECK (is_open IN (0, 1))
);

CREATE TABLE IF NOT EXISTS pairs (
    source_a  TEXT NOT NULL,
    source_b  TEXT NOT NULL,
    PRIMARY KEY (source_a, source_b)
);

-- 全局键值：revision = 当前管网/阀位基准修订号（录网、清空、阀门变更时 +1）
CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  INTEGER NOT NULL
);

-- 复水计划：创建后步骤不可变。status: pending 执行中 / completed 全部完成 /
-- invalidated 因录网、清空或阀门变更而失效（revision 必然已落后）
CREATE TABLE IF NOT EXISTS plans (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'invalidated')),
    base_revision   INTEGER NOT NULL,
    progress        INTEGER NOT NULL DEFAULT 0,  -- 已完成步数
    total_steps     INTEGER NOT NULL,
    initial_valves  TEXT NOT NULL,               -- 创建时全量阀位快照 JSON
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    invalidated_at  TEXT,
    invalidate_reason TEXT
);

CREATE TABLE IF NOT EXISTS plan_steps (
    plan_id        TEXT NOT NULL,
    step_no        INTEGER NOT NULL,             -- 从 1 开始
    valve_id       TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('open', 'close')),
    preview_json   TEXT NOT NULL,                -- 创建时逐步预演结果（不可变）
    result_json    TEXT,                         -- 执行后实际结果（幂等重放用）
    executed_at    TEXT,
    PRIMARY KEY (plan_id, step_no)
);

CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON plan_steps(plan_id);
"""


def db_path() -> str:
    return os.environ.get("WATER_DB_PATH", DEFAULT_DB_PATH)


def connect(path: str | None = None) -> sqlite3.Connection:
    """打开一个带外键约束的连接。

    check_same_thread=False：FastAPI 的依赖生成在事件循环线程建连、
    端点函数在线程池中使用；连接本身每请求独占，且写操作在应用层加锁，安全。
    """
    path = path or db_path()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接（普通生成器，配合 Depends 的 yield 语义）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    """初始化数据库表结构（幂等）。容器启动时执行一次。"""
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def clear_all(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pairs")
    conn.execute("DELETE FROM valves")
    conn.execute("DELETE FROM pipes")
    conn.execute("DELETE FROM nodes")


def is_empty(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
