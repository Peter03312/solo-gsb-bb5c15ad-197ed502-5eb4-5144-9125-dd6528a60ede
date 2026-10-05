# 供水管网隔离预演与复水计划服务

物业抢修场景的供水管网关阀隔离预演 API：录入水源供水上限、**不可并联**的水源对、
**单向管段**及容量、管段上的阀门状态、用水点需求与优先级；支持阀门**整批校验、原子生效**，
并在**不写入状态**的前提下预演关阀后各用水点的供水分配，返回供水量、欠供原因与受限管段。

同时支持**物业复水**：操作者提交非空、有序且阀门编号不重复的开关步骤，服务以当前管网和
阀位**逐步预演**（不改变实际状态），每步按无向开启管段规则复核不可并联水源，首个冲突即
定位返回（步骤位置、连通水源、路径）；全部安全才保存**不可变计划**，随后只能按
“**下一步 + 所见基准修订号**”逐步执行，事务内复核修订与安全性、更新阀位与进度。
录网、清空及计划外阀门变更会使未完计划失效（基准修订号 +1）。

技术栈：Python 3.11 · FastAPI · SQLite（标准库 `sqlite3`）· Uvicorn · Dinic 最大流。

---

## 1. 快速开始

### 方式 A：Docker Compose（一键启动，推荐）

```bash
docker compose up --build -d
# 服务地址: http://localhost:8080
# 接口文档(Swagger): http://localhost:8080/docs
curl http://localhost:8080/health
```

容器启动时会自动：安装依赖 → 初始化 SQLite 表结构（`/data/water.db`，幂等）→ 启动服务。
数据持久化在命名卷 `water-data` 中。

停止 / 清空数据：

```bash
docker compose down            # 停止（保留数据卷）
docker compose down -v         # 停止并删除数据库卷
```

### 方式 B：本地直接运行

```bash
python -m pip install -r requirements.txt
# 初始化数据库（生成 ./data/water.db）
python -c "from app.database import init_db; init_db()"
# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 一键演示

```bash
bash examples/demo.sh                 # 默认 http://localhost:8080（关阀隔离预演）
bash examples/demo.sh http://主机:端口
bash examples/restoration_demo.sh     # 物业复水：计划预演/保存/逐步执行/失效
```

---

## 2. 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WATER_HOST` | `0.0.0.0` | 监听地址（Dockerfile 内置） |
| `WATER_PORT` | `8080` | 监听端口（Dockerfile 内置） |
| `WATER_DB_PATH` | `./data/water.db`（本地）/ `/data/water.db`（容器） | SQLite 数据库文件路径，目录自动创建 |

Compose 的端口映射在 `docker-compose.yml` 中修改左侧即可（如 `"18080:8080"`）。

---

## 3. 业务模型

| 实体 | 字段 | 说明 |
| --- | --- | --- |
| 水源 `sources` | `id`、`capacity` | `capacity` 为供水上限，≥ 0 |
| 用水点 `demands` | `id`、`demand`、`priority` | 需求量 ≥ 0；`priority` 整数，**数值越大优先级越高** |
| 连接点 `junctions` | `id` | 无属性的中转节点 |
| 管段 `pipes` | `id`、`from_node`、`to_node`、`capacity` | **单向**（泵/止回语义），容量 ≥ 0 |
| 阀门 `valves` | `id`、`pipe_id`、`is_open` | 挂在管段上；管段上**所有阀门均开启**时该管段才导通 |
| 互锁水源对 `forbidden_pairs` | `source_a`、`source_b` | 二者不允许经开启管段连通 |

### 核心规则

1. **阀门变更整批校验、原子生效**：一个批次里只要存在非法阀门编号、重复条目，或变更后
   任意不可并联水源对经开启管段连通，整批拒绝（HTTP 400/409），库内**原状态保持不变**；
   全部通过才在一个事务内更新。
2. **互锁连通判定按无向图**：关阀隔离只关心物理连通，止回方向不应让互锁水源被误判为已隔离，
   因此即使管段单向，连通性仍按无向遍历“开启管段”（偏保守、更安全）。响应中给出连通路径便于定位。
3. **预演不写入**：`POST /api/drill` 在内存中临时叠加“拟关闭阀门”后计算，库内阀门状态不变。
4. **按优先级贪心 + 最大流分配**：用水点按 `priority` 降序、同级按编号升序依次处理；
   对每个点在“**剩余**水源上限 + **剩余**共享管段容量”约束下用 Dinic 求该点最大可获得流量；
   已被前序点占用的水源额度与管段容量对后续点不再可用（不重复占用）。
5. **欠供原因码**：
   - `ISOLATED`：无任何水源经开启管段可达该点（被隔离），`constrained_pipes` 给出切断它的管段及上面的关闭阀门；
   - `SOURCE_LIMIT`：水源供水上限打满；
   - `PIPE_LIMIT`：共享管段容量打满；
   - `SOURCE_AND_PIPE_LIMIT`：水源与管段同时达到瓶颈。
6. **定位错误**：未知端点、未知管段、未知阀门、负容量、非法编号等返回带 `location`
   （如 `pipes[2].capacity`、`changes[1].valve_id`）的错误详情。
   类型/范围错误（负数、缺字段、NaN）由 Pydantic 层返回 422；语义错误（未知端点、互锁连通）返回 400/409。
7. **复水逐步预演与不可变计划**：见 §5。提交清单必须非空、有序、阀门编号不重复；
   服务从当前阀位起在内存中逐步叠加，每步都做互锁复核，**首个冲突即返回，不保存计划**；
   全部安全才落库为不可变计划。
8. **乐观修订号执行**：执行只接受计划的“下一步”和调用方“所见修订号”；每步在同一事务内
   复核修订与安全性后更新阀位和进度。重复提交已完成步骤幂等返回原结果；修订号过期或跳步
   一律拒绝且不改变状态。录网、清空、阀门变更在同一事务内把修订号 +1 并把未完计划置为
   `invalidated`（计划自身的逐步执行**不**推进修订号）。

---

## 4. HTTP API

服务前缀无特殊前缀；交互式文档见 `/docs`（Swagger）与 `/redoc`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| PUT | `/api/network` | **整批覆盖式录入**管网（事务，失败全回滚；修订号 +1） |
| GET | `/api/network` | 查询当前管网（含当前修订号） |
| DELETE | `/api/network` | 清空管网（修订号 +1，未完计划失效） |
| POST | `/api/valves/change` | 阀门**整批变更**（校验通过才原子生效；修订号 +1，未完计划失效） |
| POST | `/api/drill` | 关阀隔离**预演**（只读，不写入） |
| GET | `/api/status` | 当前真实阀门状态下的分派结果 |
| POST | `/api/plans` | 提交复水清单：**逐步预演**，全部安全才保存不可变计划 |
| GET | `/api/plans` | 计划列表（状态、进度、修订号） |
| GET | `/api/plans/{plan_id}` | 计划详情（初始阀位、每步预演/执行结果、进度） |
| POST | `/api/plans/{plan_id}/execute` | **只执行下一步**（乐观修订号，事务内复核，幂等重放） |

### 4.1 录入管网 `PUT /api/network`

请求体示例见 [`examples/sample_network.json`](examples/sample_network.json)：

```json
{
  "sources":  [{"id": "S1", "capacity": 10}, {"id": "S2", "capacity": 6}],
  "demands":  [
    {"id": "D1", "demand": 10, "priority": 9},
    {"id": "D2", "demand": 8,  "priority": 1}
  ],
  "junctions": [{"id": "J1"}],
  "pipes": [
    {"id": "P1", "from_node": "S1", "to_node": "D1", "capacity": 10},
    {"id": "P2", "from_node": "S2", "to_node": "J1", "capacity": 6},
    {"id": "P5", "from_node": "S1", "to_node": "S2", "capacity": 5}
  ],
  "valves": [
    {"id": "V1", "pipe_id": "P1", "is_open": true},
    {"id": "V5", "pipe_id": "P5", "is_open": false}
  ],
  "forbidden_pairs": [{"source_a": "S1", "source_b": "S2"}]
}
```

错误响应（400，整批未写入）：

```json
{
  "error": {
    "code": "invalid_network",
    "message": "管网数据校验失败，整批录入已拒绝（未写入任何数据）",
    "details": [
      {"location": "pipes[0].to_node", "code": "unknown_endpoint",
       "message": "未知端点：GHOST（节点不存在）"}
    ]
  }
}
```

### 4.2 阀门整批变更 `POST /api/valves/change`

`action`（`open`/`close`）与 `is_open` 二选一；同一批次可包含多只阀门：

```bash
curl -X POST http://localhost:8080/api/valves/change \
  -H 'Content-Type: application/json' \
  -d '{"changes": [
        {"valve_id": "V3", "action": "close"},
        {"valve_id": "V1", "is_open": false}
      ]}'
```

打开会使互锁水源连通时返回 **409**（原状态不变）：

```json
{
  "error": {
    "code": "forbidden_sources_connected",
    "message": "阀门变更被拒绝：将使不可并联水源经开启管段连通，原阀门状态保持不变",
    "details": [{
      "location": "forbidden_pairs",
      "code": "forbidden_connected",
      "source_a": "S1", "source_b": "S2",
      "path": ["S1", "S2"],
      "message": "水源 S1 与 S2 不可并联，但变更后将经开启管段连通：S1 -> S2"
    }]
  }
}
```

### 4.3 关阀隔离预演 `POST /api/drill`

```bash
curl -X POST http://localhost:8080/api/drill \
  -H 'Content-Type: application/json' \
  -d '{"close_valves": ["V1"]}'
```

响应（节选）：

```json
{
  "closed_valves": ["V1", "V5"],
  "simulated_closed_valves": ["V1"],
  "message": "预演完成：本次结果为临时计算，未写入任何阀门状态",
  "allocations": [
    {
      "demand_id": "D1", "priority": 9,
      "requested": 10.0, "supplied": 0.0, "shortfall": 10.0,
      "fulfilled": false,
      "shortfall_reason_code": "ISOLATED",
      "shortfall_reason": "用水点 D1 已被隔离：无任何水源经开启管段可达该点",
      "constrained_pipes": [
        {"pipe_id": "P1", "from_node": "S1", "to_node": "D1",
         "capacity": 10.0, "remaining_capacity": 10.0, "closed_valves": ["V1"]}
      ],
      "by_source": {}
    },
    {
      "demand_id": "D2", "priority": 1,
      "requested": 8.0, "supplied": 4.0, "shortfall": 4.0,
      "fulfilled": false,
      "shortfall_reason_code": "PIPE_LIMIT",
      "shortfall_reason": "用水点 D2 欠供 4.000：共享管段 P3 容量已占满",
      "constrained_pipes": [
        {"pipe_id": "P3", "from_node": "J1", "to_node": "D2",
         "capacity": 4.0, "remaining_capacity": 0.0, "closed_valves": []}
      ],
      "by_source": {"S2": 4.0}
    }
  ],
  "totals": {"requested": 24.0, "supplied": 10.0, "shortfall": 14.0},
  "source_utilization": [
    {"source_id": "S1", "capacity": 10.0, "used": 0.0, "remaining": 10.0},
    {"source_id": "S2", "capacity": 6.0, "used": 6.0, "remaining": 0.0}
  ],
  "pipe_utilization": [
    {"pipe_id": "P3", "capacity": 4.0, "used": 4.0, "remaining": 0.0, "conducting": true}
  ]
}
```

字段含义：

- `supplied` / `shortfall`：该点实际获得水量 / 欠供量；
- `shortfall_reason_code` + `shortfall_reason`：欠供原因码与中文说明；
- `constrained_pipes`：受限管段（端点、容量、剩余容量、导致关断的阀门编号）；
- `by_source`：该点水量分别来自哪些水源；
- `source_utilization` / `pipe_utilization`：全局占用情况，可核对“容量不重复占用”。

### 4.4 错误码速查

| HTTP | `error.code` | 触发场景 |
| --- | --- | --- |
| 422 | `validation_error` | 负数/NaN 容量、缺字段、类型错误、多余字段、清单为空、阀门编号为空 |
| 400 | `invalid_network` | 未知端点、自环、重复编号、阀门引用不存在管段、水源对非法或录入时已连通 |
| 400 | `invalid_valve_change` | 未知/非法阀门编号、批次内重复、`action` 与 `is_open` 冲突 |
| 409 | `forbidden_sources_connected` | 变更会使互锁水源经开启管段连通 |
| 400 | `invalid_drill` | 预演阀门编号未知/重复 |
| 400 | `invalid_plan` | 复水清单中阀门未知/重复、动作为非法值（定位到 `steps[i].valve_id` / `steps[i].action`） |
| 409 | `plan_forbidden_sources_connected` | 逐步预演某步将连通互锁水源（返回步骤位置、水源、路径；不保存） |
| 409 | `revision_conflict` | 执行时所见修订号已过期（计划外发生过录网/清空/阀门变更） |
| 409 | `plan_invalidated` | 计划已被录网/清空/阀门变更置为失效 |
| 409 | `step_out_of_order` | 跳步：提交的不是计划的下一步 |
| 409 | `plan_step_unsafe` | 执行事务内复核发现该步将连通互锁水源（已回滚） |
| 409 | `plan_completed` | 向已完成计划提交不存在的下一步 |
| 404 | `plan_not_found` | 计划编号不存在 |
| 409 | `network_not_initialized` | 尚未 `PUT /api/network` |

---

## 5. 物业复水：阀门操作清单 → 不可变计划 → 逐步执行

> 一键脚本：`bash examples/restoration_demo.sh`

### 5.1 提交清单与逐步预演 `POST /api/plans`

请求体为**非空、有序、阀门编号不重复**的开关步骤；`action` 仅允许 `open` / `close`。
服务以当前管网和阀位为起点，在内存中逐步叠加阀位（**不写库**），每步落地后立即按
**无向开启管段**规则复核互锁水源；全部安全才保存不可变计划。

```bash
curl -X POST http://localhost:8080/api/plans \
  -H 'Content-Type: application/json' \
  -d '{"steps": [
        {"valve_id": "V6", "action": "open"},
        {"valve_id": "V3", "action": "open"},
        {"valve_id": "V1", "action": "open"}
      ]}'
```

条目级错误（清单为空/字段缺失/类型错误由 422 拦截；非法动作、未知阀门、编号重复返回
400，并分别定位到 `steps[i].action` 与 `steps[i].valve_id`）：

```json
{
  "error": {
    "code": "invalid_plan",
    "message": "复水操作清单校验失败，未保存计划",
    "details": [
      {"location": "steps[1].valve_id", "code": "unknown_valve",
       "message": "非法/未知阀门编号：GHOST"}
    ]
  }
}
```

逐步预演中的**首个**互锁冲突返回 **409，且不保存计划**。`details[0]` 给出步骤位置
（`step_no` 从 1 开始、`location` 为 `steps[i]`）、连通水源与节点路径，
`safe_previews` 给出冲突之前各安全步骤的逐步预演结果：

```json
{
  "error": {
    "code": "plan_forbidden_sources_connected",
    "message": "复水计划预演未通过：存在步骤将使不可并联水源经开启管段连通，计划未保存",
    "details": [{
      "step_no": 3,
      "location": "steps[2]",
      "valve_id": "V5",
      "action": "open",
      "source_a": "S1", "source_b": "S2",
      "path": ["S1", "S2"],
      "safe_steps_before": 2,
      "safe_previews": [ /* 第 1、2 步安全落地后的逐步结果，结构同下 */ ]
    }]
  }
}
```

### 5.2 计划保存响应（不可变，含基准修订号）

```json
{
  "plan_id": "PL-2dbdf0408eb9",
  "status": "pending",
  "base_revision": 1,
  "current_revision": 1,
  "revision_stale": false,
  "progress": 0,
  "total_steps": 3,
  "next_step_no": 1,
  "initial_valves": [
    {"valve_id": "V1", "pipe_id": "P1", "is_open": false}
  ],
  "steps": [
    {
      "step_no": 1,
      "valve_id": "V6",
      "action": "open",
      "state": "pending",
      "preview": {
        "changed": true,
        "valves": [ /* 该步落地后的“每步阀位”全量快照，按编号排序 */ ],
        "closed_valves": ["V1", "V3", "V5"],
        "allocations": [ /* 沿用 /api/status 现有口径的各用水点供水、欠供、受限管段 */ ],
        "totals": {"requested": 19.0, "supplied": 4.0, "shortfall": 15.0},
        "source_utilization": [ /* 同现有口径 */ ],
        "pipe_utilization": [ /* 同现有口径 */ ]
      },
      "result": null,
      "executed_at": null
    }
  ]
}
```

说明：

- `preview` 是创建时的逐步预演结果，**计划保存后不再变化**；
- `result` 在该步执行成功后填入，字段结构与 `preview` 一致（执行结果与预演同口径）；
- `changed=false` 表示该步的动作与当前阀位一致（冗余操作，允许但不改变状态）。

### 5.3 逐步执行 `POST /api/plans/{plan_id}/execute`

只接受计划的**下一步**与调用方**所见修订号**（即创建/查询响应中的 `base_revision` 或
`current_revision`）。服务在同一事务内：复核修订号 → 复核计划状态 → 复核当前阀位与
互锁安全 → 更新阀位 → 写入该步结果并推进进度。

```bash
curl -X POST http://localhost:8080/api/plans/PL-2dbdf0408eb9/execute \
  -H 'Content-Type: application/json' \
  -d '{"step_no": 1, "revision": 1}'
```

响应即该步结果（同 `preview` 结构）并附带执行元数据：

```json
{
  "step_no": 1,
  "valve_id": "V6",
  "action": "open",
  "changed": true,
  "valves": [ /* 执行后每步阀位（已落库） */ ],
  "allocations": [ /* 各用水点供水、欠供（现有口径） */ ],
  "totals": {"requested": 19.0, "supplied": 4.0, "shortfall": 15.0},
  "plan_id": "PL-2dbdf0408eb9",
  "plan_status": "pending",
  "revision": 1,
  "next_step_no": 2,
  "idempotent_replay": false,
  "message": "第 1 步已执行"
}
```

执行语义：

| 提交情形 | 结果 | 是否改变状态 |
| --- | --- | --- |
| `step_no` = 下一步且 `revision` = 当前修订号 | 执行，返回该步阀位与供水/欠供 | 是（事务内） |
| 重复提交**已完成**的步骤（含已完成计划的历史步骤） | **幂等**返回原结果，`idempotent_replay=true` | 否 |
| `revision` ≠ 当前修订号（录网/清空/阀门变更后） | 409 `revision_conflict` | 否 |
| 计划已 `invalidated`（即便带了新修订号） | 409 `plan_invalidated` | 否 |
| `step_no` > 下一步（跳步） | 409 `step_out_of_order`（带期望/收到步号） | 否 |
| 向已完成计划提交不存在的下一步 | 409 `plan_completed` | 否 |

### 5.4 查询与失效联动

- `GET /api/plans`：列出计划摘要（`status`、`progress`、`base/current_revision`、
  `revision_stale`、失效原因与时间）。
- `GET /api/plans/{plan_id}`：完整计划（含 `initial_valves`、每步 `preview`/`result`）。
  已失效或已完成计划的历史记录仍可查询。
- `PUT /api/network`、`DELETE /api/network`、`POST /api/valves/change` 在**同一事务内**
  把全局基准修订号 +1 并将所有 `pending` 计划置为 `invalidated`（带原因）；已完成计划
  不受影响。上述接口响应中新增 `revision` 字段、`GET /api/network` 新增 `revision` 字段，
  均为**附加字段，不改变原有契约**。

---

## 6. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、错误处理、整批事务、复水计划与乐观锁执行
│   ├── engine.py      # 校验、互锁连通(BFS)、Dinic 最大流、优先级贪心预演、复水逐步预演
│   ├── schemas.py     # Pydantic 请求模型
│   ├── database.py    # SQLite 连接与建表（含 meta/plans/plan_steps）
│   └── repository.py  # DB <-> 内存模型、修订号、计划持久化
├── tests/             # pytest：39 项端到端 + 引擎测试
├── examples/
│   ├── sample_network.json
│   ├── demo.sh                 # 关阀隔离预演 curl 端到端示例
│   └── restoration_demo.sh     # 物业复水 curl 端到端示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 7. 运行测试

```bash
python -m pip install pytest httpx
python -m pytest -q
```

## 8. 设计说明与边界

- 管段单向用于**供水方向**的流量计算；互锁水源的**连通性**检查按无向（见 §3 规则 2）。
- 容量为浮点（内部以 1e-9 容差判定饱和）；需求为 0 的点直接判定为已满足。
- 未挂阀门的管段视为常通；管段上任一阀门关闭即不导通。
- 预演仅支持“拟关闭”阀门（抢修预演的实际需求）；库内已关闭阀门自然叠加生效。
- **复水清单**允许 `open`/`close` 任意动作与冗余动作（阀位不变时 `changed=false`）；
  清单内同一阀门只能出现一次（有序、不重复）。
- **基准修订号**为全局单调整数，初值为 0；仅录网、清空、阀门变更会 +1，计划执行不会。
  修订号递增与未完计划失效在变更的同一事务内完成，崩溃/回滚不会出现“只增号不失效”。
- 计划执行与所有写操作共用进程内写锁串行化；执行事务内还会重新加载当前管网并再次
  做互锁安全检查（防御性复核），任何复核失败都回滚，阀位与进度不变。
- 预演/执行的逐步结果由引擎同一函数 `step_result` 生成，保证保存的 `preview` 与
  执行后的 `result` 口径完全一致。
