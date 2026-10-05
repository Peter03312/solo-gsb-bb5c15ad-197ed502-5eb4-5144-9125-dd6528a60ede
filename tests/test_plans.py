"""复水计划端到端测试：逐步预演、不可变计划、乐观锁执行与失效联动。

复水测试管网（抢修时各支线阀已关闭，须逐个安全开启；S1/S2 互锁）：
  S1 --P1(V1,关,cap10)--> D1(需求10, 优先级9)
  S2 --P2(cap8)--------> J1 --P3(V3,关,cap8)--> D2(需求5, 优先级1)
                              \\--P6(V6,关,cap8)--> D4(需求4, 优先级1)
  S1 --P5(V5,关,cap5)--> S2  （打开即互锁冲突）
  J1 --P7(V7,开,cap5)--> S2  （打开 V6 后再开 V7 会让 S1? 否——
                              只有 V5 才直接连接 S1/S2；V7 用于冗余/无操作场景）
"""
from __future__ import annotations

import os
import tempfile

import pytest

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["WATER_DB_PATH"] = _tmp.name

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.database import init_db  # noqa: E402


# 抢修后待复水管网：V1/V3/V5/V6 关闭（各支线被隔离），V7 开启
def _network():
    return {
        "sources": [
            {"id": "S1", "capacity": 10},
            {"id": "S2", "capacity": 8},
        ],
        "demands": [
            {"id": "D1", "demand": 10, "priority": 9},
            {"id": "D2", "demand": 5, "priority": 1},
            {"id": "D4", "demand": 4, "priority": 1},
        ],
        "junctions": [{"id": "J1"}],
        "pipes": [
            {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
            {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 8},
            {"id": "P3", "from_node": "J1", "to_node": "D2", "capacity": 8},
            {"id": "P6", "from_node": "J1", "to_node": "D4", "capacity": 8},
            {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5},
            {"id": "P7", "from_node": "J1", "to_node": "S2", "capacity": 5},
        ],
        "valves": [
            {"id": "V1", "pipe_id": "P1", "is_open": False},
            {"id": "V3", "pipe_id": "P3", "is_open": False},
            {"id": "V5", "pipe_id": "P5", "is_open": False},
            {"id": "V6", "pipe_id": "P6", "is_open": False},
            {"id": "V7", "pipe_id": "P7", "is_open": True},
        ],
        "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}],
    }


# 安全复水顺序（阀门编号互不重复；不碰 V5）：
# 先恢复 S2 子系统（V6、V3），再恢复 S1 子系统（V1）
SAFE_STEPS = [
    {"valve_id": "V6", "action": "open"},
    {"valve_id": "V3", "action": "open"},
    {"valve_id": "V1", "action": "open"},
]


@pytest.fixture(scope="module")
def client():
    init_db()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def loaded(client):
    client.delete("/api/network")
    r = client.put("/api/network", json=_network())
    assert r.status_code == 200, r.text
    return r.json()["revision"]


def _create(client, steps, expected=200):
    r = client.post("/api/plans", json={"steps": steps})
    assert r.status_code == expected, r.text
    return r


# --------------------------------------------------------------------------- #
# 提交：条目定位校验
# --------------------------------------------------------------------------- #
def test_empty_steps_rejected(client, loaded):
    r = client.post("/api/plans", json={"steps": []})
    assert r.status_code == 422


def test_unknown_valve_located(client, loaded):
    r = _create(client, [
        {"valve_id": "V1", "action": "open"},
        {"valve_id": "GHOST", "action": "open"},
    ], expected=400)
    body = r.json()["error"]
    assert body["code"] == "invalid_plan"
    assert body["details"][0]["location"] == "steps[1].valve_id"
    assert body["details"][0]["code"] == "unknown_valve"


def test_duplicate_valve_in_list_located(client, loaded):
    r = _create(client, [
        {"valve_id": "V1", "action": "open"},
        {"valve_id": "V1", "action": "close"},
    ], expected=400)
    d = r.json()["error"]["details"][0]
    assert d["location"] == "steps[1].valve_id"
    assert d["code"] == "duplicate_valve"


def test_illegal_action_rejected(client, loaded):
    r = _create(client, [{"valve_id": "V1", "action": "half-open"}], expected=400)
    d = r.json()["error"]["details"][0]
    assert d["location"] == "steps[0].action"
    assert d["code"] == "invalid_action"


def test_plan_requires_network(client):
    client.delete("/api/network")
    r = client.post("/api/plans", json={"steps": [{"valve_id": "V1", "action": "open"}]})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"


# --------------------------------------------------------------------------- #
# 逐步预演：首个冲突定位、不保存、不改实际状态
# --------------------------------------------------------------------------- #
def test_rehearse_stops_at_first_conflict_and_does_not_save(client, loaded):
    # 前两步安全，第三步开 V5 立即让 S1/S2 经 P5 无向连通
    r = _create(client, [
        {"valve_id": "V6", "action": "open"},
        {"valve_id": "V1", "action": "open"},
        {"valve_id": "V5", "action": "open"},
    ], expected=409)
    body = r.json()["error"]
    assert body["code"] == "plan_forbidden_sources_connected"
    d = body["details"][0]
    assert d["step_no"] == 3
    assert d["location"] == "steps[2]"
    assert d["valve_id"] == "V5"
    assert d["source_a"] == "S1"
    assert d["source_b"] == "S2"
    assert d["path"][0] == "S1" and d["path"][-1] == "S2"
    # 冲突前安全步骤的逐步预演可查
    assert d["safe_steps_before"] == 2
    preview2 = d["safe_previews"][1]
    assert preview2["step_no"] == 2
    alloc2 = {a["demand_id"]: a for a in preview2["allocations"]}
    assert alloc2["D1"]["supplied"] == pytest.approx(10)

    # 预演不写库：V6/V1 仍关闭
    net = client.get("/api/network").json()["network"]
    states = {v["id"]: v["is_open"] for v in net["valves"]}
    assert states["V6"] is False
    assert states["V1"] is False

    # 冲突计划不保存
    assert client.get("/api/plans").json()["plans"] == []


def test_conflict_on_first_step(client, loaded):
    r = _create(client, [{"valve_id": "V5", "action": "open"}], expected=409)
    d = r.json()["error"]["details"][0]
    assert d["step_no"] == 1
    assert d["safe_steps_before"] == 0
    assert d["safe_previews"] == []


def test_conflict_path_may_be_multihop(client, loaded):
    """路径不只看直接管段：开 V6 后 S2 已可达 J1，若某阀把 S1 间接连入也应报路径。"""
    # 先构造：V5 关闭，但加入 S1->J1 的常通管段会录入即冲突，故仍用 P5 直连；
    # 这里验证开 V5 时返回的路径是节点序列（含管段走向 S1->S2）。
    r = _create(client, [
        {"valve_id": "V6", "action": "open"},
        {"valve_id": "V5", "action": "open"},
    ], expected=409)
    path = r.json()["error"]["details"][0]["path"]
    assert path == ["S1", "S2"]


# --------------------------------------------------------------------------- #
# 全部安全：保存不可变计划，返回每步阀位与供水/欠供、基准修订号
# --------------------------------------------------------------------------- #
def test_create_safe_plan_returns_step_valves_and_allocations(client, loaded):
    r = _create(client, SAFE_STEPS)
    body = r.json()
    assert body["status"] == "pending"
    assert body["progress"] == 0
    assert body["base_revision"] == loaded
    assert body["total_steps"] == 3
    assert body["next_step_no"] == 1

    steps = body["steps"]
    v6 = [next(x for x in s["preview"]["valves"] if x["valve_id"] == "V6")["is_open"]
          for s in steps]
    v3 = [next(x for x in s["preview"]["valves"] if x["valve_id"] == "V3")["is_open"]
          for s in steps]
    v1 = [next(x for x in s["preview"]["valves"] if x["valve_id"] == "V1")["is_open"]
          for s in steps]
    assert v6 == [True, True, True]
    assert v3 == [False, True, True]
    assert v1 == [False, False, True]
    assert all(s["preview"]["changed"] is True for s in steps)

    # 第 1 步后：仅 D4 恢复（D1/D2 仍隔离）
    a1 = {a["demand_id"]: a for a in steps[0]["preview"]["allocations"]}
    assert a1["D4"]["supplied"] == pytest.approx(4)
    assert a1["D4"]["fulfilled"] is True
    assert a1["D2"]["shortfall_reason_code"] == "ISOLATED"
    assert a1["D1"]["shortfall_reason_code"] == "ISOLATED"

    # 第 3 步后：全部恢复。D1 由 S1 供 10；S2 子系统中 D2（编号小）先占 5，
    # D4 在水源/干管 P2 仅剩 3 额度下得 3（欠 1），总供 18，守恒
    a3 = {a["demand_id"]: a for a in steps[2]["preview"]["allocations"]}
    assert a3["D1"]["supplied"] == pytest.approx(10)
    assert a3["D2"]["supplied"] == pytest.approx(5)
    assert a3["D4"]["supplied"] == pytest.approx(3)
    assert a3["D4"]["shortfall"] == pytest.approx(1)
    assert steps[2]["preview"]["totals"]["supplied"] == pytest.approx(18)
    assert "source_utilization" in steps[2]["preview"]
    assert "pipe_utilization" in steps[2]["preview"]

    # 计划保存不改变实际阀位
    net = client.get("/api/network").json()
    assert net["revision"] == loaded
    states = {v["id"]: v["is_open"] for v in net["network"]["valves"]}
    assert states["V6"] is False and states["V3"] is False and states["V1"] is False


def test_noop_step_marked_changed_false(client, loaded):
    # V7 初始即开，冗余 open 合法，changed=False
    r = _create(client, [{"valve_id": "V7", "action": "open"}])
    assert r.json()["steps"][0]["preview"]["changed"] is False


# --------------------------------------------------------------------------- #
# 执行：只接受下一步 + 所见修订号；事务内复核、更新阀位与进度
# --------------------------------------------------------------------------- #
def test_execute_happy_path(client, loaded):
    plan = _create(client, SAFE_STEPS).json()
    pid = plan["plan_id"]

    r1 = client.post(f"/api/plans/{pid}/execute",
                     json={"step_no": 1, "revision": loaded})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["step_no"] == 1
    assert b1["plan_status"] == "pending"
    assert b1["next_step_no"] == 2
    assert b1["idempotent_replay"] is False
    assert next(v for v in b1["valves"] if v["valve_id"] == "V6")["is_open"] is True
    alloc = {a["demand_id"]: a for a in b1["allocations"]}
    assert alloc["D4"]["supplied"] == pytest.approx(4)

    # 阀位落库；计划执行不改修订号
    net = client.get("/api/network").json()
    assert next(v for v in net["network"]["valves"] if v["id"] == "V6")["is_open"] is True
    assert net["revision"] == loaded

    r2 = client.post(f"/api/plans/{pid}/execute",
                     json={"step_no": 2, "revision": loaded})
    assert r2.status_code == 200
    assert r2.json()["next_step_no"] == 3

    r3 = client.post(f"/api/plans/{pid}/execute",
                     json={"step_no": 3, "revision": loaded})
    assert r3.status_code == 200
    assert r3.json()["plan_status"] == "completed"
    assert r3.json()["next_step_no"] is None

    got = client.get(f"/api/plans/{pid}").json()
    assert got["status"] == "completed"
    assert got["progress"] == 3
    assert got["next_step_no"] is None
    assert all(s["state"] == "executed" for s in got["steps"])
    for s in got["steps"]:  # 执行结果与创建时逐步预演口径一致
        assert s["result"]["totals"] == s["preview"]["totals"]
        assert s["result"]["valves"] == s["preview"]["valves"]


def test_execute_unknown_plan_404(client, loaded):
    r = client.post("/api/plans/PL-nope/execute",
                    json={"step_no": 1, "revision": loaded})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "plan_not_found"


def test_get_unknown_plan_404(client, loaded):
    r = client.get("/api/plans/PL-nope")
    assert r.status_code == 404


def test_execute_skip_step_rejected_without_state_change(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 2, "revision": loaded})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "step_out_of_order"
    assert body["details"][0]["expected_step_no"] == 1
    assert body["details"][0]["received_step_no"] == 2

    got = client.get(f"/api/plans/{pid}").json()
    assert got["progress"] == 0
    net = client.get("/api/network").json()["network"]
    assert all(v["is_open"] is False for v in net["valves"] if v["id"] in ("V6", "V3", "V1"))


def test_execute_stale_revision_rejected(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 1, "revision": loaded + 99})
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "revision_conflict"
    assert body["details"][0]["code"] == "revision_stale"
    assert client.get(f"/api/plans/{pid}").json()["progress"] == 0


def test_replay_completed_step_is_idempotent(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    client.post(f"/api/plans/{pid}/execute",
                json={"step_no": 1, "revision": loaded})
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 1, "revision": loaded})
    assert r.status_code == 200
    b = r.json()
    assert b["idempotent_replay"] is True
    assert b["step_no"] == 1
    assert client.get(f"/api/plans/{pid}").json()["progress"] == 1


# --------------------------------------------------------------------------- #
# 录网 / 清空 / 阀门变更：未完计划失效，执行被过期修订号拒绝
# --------------------------------------------------------------------------- #
def test_external_valve_change_bumps_revision_and_invalidates(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    assert client.post(f"/api/plans/{pid}/execute",
                       json={"step_no": 1, "revision": loaded}).status_code == 200

    # 计划外阀门变更：修订号 +1，未完计划失效
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V7", "action": "close"}]})
    assert r.status_code == 200
    new_rev = r.json()["revision"]
    assert new_rev == loaded + 1

    got = client.get(f"/api/plans/{pid}").json()
    assert got["status"] == "invalidated"
    assert got["progress"] == 1  # 已完成步骤保留
    assert got["revision_stale"] is True
    assert got["steps"][0]["state"] == "executed"
    assert got["steps"][1]["state"] == "pending"
    assert got["steps"][0]["result"] is not None

    # 旧修订号执行下一步 → 409 revision_conflict，状态不变
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 2, "revision": loaded})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "revision_conflict"

    # 新修订号也不允许继续失效计划
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 2, "revision": new_rev})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "plan_invalidated"

    rows = client.get("/api/plans").json()["plans"]
    entry = next(p for p in rows if p["plan_id"] == pid)
    assert entry["status"] == "invalidated"
    assert entry["current_revision"] == new_rev


def test_reload_network_invalidates_unfinished_plan(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    r = client.put("/api/network", json=_network())
    assert r.status_code == 200
    assert r.json()["revision"] == loaded + 1
    assert client.get(f"/api/plans/{pid}").json()["status"] == "invalidated"


def test_clear_network_invalidates_plan(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    r = client.delete("/api/network")
    assert r.status_code == 200
    assert r.json()["revision"] == loaded + 1
    # 清空后计划记录仍可查（保留历史），但已失效
    got = client.get(f"/api/plans/{pid}").json()
    assert got["status"] == "invalidated"
    # 管网不存在 → 执行优先报未初始化
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 1, "revision": loaded + 1})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "network_not_initialized"


def test_completed_plan_survives_external_changes_and_replays(client, loaded):
    pid = _create(client, SAFE_STEPS).json()["plan_id"]
    for no in (1, 2, 3):
        assert client.post(f"/api/plans/{pid}/execute",
                           json={"step_no": no, "revision": loaded}).status_code == 200

    # 完成后的计划不受后续阀门变更影响（不被置失效）
    r = client.post("/api/valves/change",
                    json={"changes": [{"valve_id": "V7", "action": "close"}]})
    new_rev = r.json()["revision"]
    assert client.get(f"/api/plans/{pid}").json()["status"] == "completed"

    # 旧修订号重放 → 409；当前修订号重放历史步骤 → 原结果，不再次动作
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 1, "revision": loaded})
    assert r.status_code == 409
    r = client.post(f"/api/plans/{pid}/execute",
                    json={"step_no": 1, "revision": new_rev})
    assert r.status_code == 200
    b = r.json()
    assert b["idempotent_replay"] is True
    assert b["plan_status"] == "completed"
