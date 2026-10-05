# 供水管网隔离预演与复水计划服务

物业抢修场景的供水管网关阀隔离预演与**复水（逐步恢复供水）计划** API：录入水源供水上限、
**不可并联**的水源对、**单向管段**及容量、管段上的阀门状态、用水点需求与优先级；支持阀门
**整批校验、原子生效**，并在**不写入状态**的前提下预演关阀后各用水点的供水分配。

复水时必须按**阀门操作清单逐步恢复**：服务对开/关步骤做逐步预演（每一步都按无向开启管段
复核互锁水源），全部安全才保存**不可变计划**；执行端携带**基准修订号**逐步提交，服务在同一
事务内复核修订与安全性、更新阀位与进度，重复提交幂等回放，过期/跳步拒绝。

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
bash examples/demo.sh                 # 关阀隔离预演（原能力），默认 http://localhost:8080
bash examples/demo.sh http://主机:端口
bash examples/restore_demo.sh         # 复水计划：逐步预演 → 保存 → 修订号逐步执行
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

---

## 4. HTTP API

服务前缀无特殊前缀；交互式文档见 `/docs`（Swagger）与 `/redoc`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| PUT | `/api/network` | **整批覆盖式录入**管网（事务，失败全回滚；成功 `revision+1`，未完计划失效） |
| GET | `/api/network` | 查询当前管网（含基准修订号 `revision`） |
| DELETE | `/api/network` | 清空管网（`revision+1`，未完计划失效） |
| POST | `/api/valves/change` | 阀门**整批变更**（原子生效；成功 `revision+1`，未完计划失效） |
| POST | `/api/drill` | 关阀隔离**预演**（只读，不写入） |
| GET | `/api/status` | 当前真实阀位下的分派结果（含 `revision`） |
| POST | `/api/restore/plans` | 复水**逐步预演**；全部安全才保存不可变计划 |
| GET | `/api/restore/plans` | 计划列表（状态/进度） |
| GET | `/api/restore/plans/{plan_id}` | 计划详情（不可变步骤与进度） |
| POST | `/api/restore/plans/{plan_id}/execute` | 执行计划**下一步**（携带所见修订号，同事务提交） |

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
| 422 | `validation_error` | 负数/NaN 容量、缺字段、类型错误、多余字段、步骤清单为空 |
| 400 | `invalid_network` | 未知端点、自环、重复编号、阀门引用不存在管段、水源对非法或录入时已连通 |
| 400 | `invalid_valve_change` | 未知/非法阀门编号、批次内重复、`action` 与 `is_open` 冲突 |
| 409 | `forbidden_sources_connected` | 变更/复水某步会使互锁水源经开启管段连通 |
| 400 | `invalid_drill` | 预演阀门编号未知/重复 |
| 400 | `invalid_restore_plan` | 复水步骤含未知阀门、非法动作或重复阀门（带 `steps[i].*` 定位，计划不保存） |
| 404 | `plan_not_found` | 计划编号不存在 |
| 409 | `revision_conflict` | 执行时携带的修订号已过期（状态不变，需重取后重试） |
| 409 | `step_out_of_order` | 跳步：提交的不是计划的下一步（状态不变） |
| 409 | `plan_invalidated` | 计划已因管网录入/清空或阀门变更失效 |
| 409 | `plan_already_completed` | 计划已全部完成（重放已完成步骤需带 `step`） |
| 409 | `network_not_initialized` | 尚未 `PUT /api/network` |

---

## 5. 复水计划（逐步恢复供水）

复水不能只检验“整批结束后的状态”——必须按阀门操作清单**逐步**恢复，每一步都要保证
不可并联水源不被连通。整体分三段：

1. **逐步预演（不保存、不改状态）**：`POST /api/restore/plans`
   - 请求体为非空、有序（数组顺序即操作顺序）、阀门编号不重复的开/关步骤；
   - 服务以**当前管网与当前阀位**为起点，在内存中逐步叠加，每一步都按现有“无向开启管段”
     规则检查互锁水源；
   - **首个冲突**立即返回：步骤位置 `position`、定位 `steps[i]`、连通水源 `source_a/b`、
     节点路径 `path`，且**不保存计划、不改任何实际状态**；
   - 条目问题（未知阀门、非法动作、重复阀门）一次性带定位返回 400，同样不保存；
   - 全部安全才保存**不可变计划**（步骤此后不可修改），响应含 `plan_id`、基准修订号
     `base_revision`，以及**每一步**的阀位（`valves`/`closed_valves`）和沿用现有口径的
     各用水点供水、欠供、`totals` 与资源占用。
2. **逐步执行（乐观修订号 + 同事务）**：`POST /api/restore/plans/{plan_id}/execute`
   - 请求体 `{"revision": <所见修订号>, "step": <可选，1基位置>}`；
   - 只接受**计划的下一步**与**所见修订号**；服务在**同一事务**内复核修订号与该步安全性、
     更新阀位与计划进度，并把基准修订号 `revision` 推进 1（同事务内还会使其它未完计划失效）；
   - **重复提交已完成步骤**（带该步 `step`）→ 原样回放该步结果
     （`idempotent_replay: true`），阀位/进度/修订号都不再变化；
   - **过期修订号** → 409 `revision_conflict`；**跳步** → 409 `step_out_of_order`；
     二者都拒绝且**不改变任何状态**，客户端重取当前修订号后重试。
3. **失效**：管网重新录入、清空，以及阀门整批变更，都会使所有**未完**计划置为
   `invalidated` 并推进修订号；已完成计划保留结果。重新编排新计划即可。

### 5.1 创建计划（逐步预演 + 保存）

```bash
curl -X POST http://localhost:8080/api/restore/plans \
  -H 'Content-Type: application/json' \
  -d '{"steps": [
        {"valve_id": "V3", "action": "open"},
        {"valve_id": "V1", "action": "open"}
      ]}'
```

响应（节选；`steps[]` 为每一步预演快览）：

```json
{
  "message": "复水计划已保存（步骤不可变）；请携带修订号逐步执行",
  "plan_id": "PLAN-9f2c...",
  "status": "active",
  "base_revision": 2,
  "revision": 2,
  "total_steps": 2,
  "next_step": 1,
  "steps": [
    {
      "position": 1, "valve_id": "V3", "action": "open", "is_open": true,
      "closed_valves": ["V1"],
      "valves": [{"valve_id": "V1", "pipe_id": "P1", "is_open": false}, "..."],
      "allocations": [{"demand_id": "D1", "supplied": 0.0,
                       "shortfall_reason_code": "ISOLATED", "...": "..."}],
      "totals": {"requested": 18.0, "supplied": 4.0, "shortfall": 14.0},
      "source_utilization": ["..."], "pipe_utilization": ["..."]
    }
  ]
}
```

首个冲突的 409 响应（第 2 步打开 V5 会连通互锁水源；计划不保存）：

```json
{
  "error": {
    "code": "forbidden_sources_connected",
    "message": "复水预演被拒绝：某一步将使不可并联水源经开启管段连通，计划未保存（未写入任何阀门状态）",
    "details": [{
      "location": "steps[1]", "code": "forbidden_connected", "position": 2,
      "valve_id": "V5", "action": "open",
      "source_a": "S1", "source_b": "S2", "path": ["S1", "S2"],
      "message": "第 2 步对阀门 V5 执行 open 后，不可并联水源 S1 与 S2 将经开启管段连通：S1 -> S2"
    }]
  }
}
```

### 5.2 执行下一步

```bash
# revision 取自创建响应的 base_revision，或 GET /api/status、GET /api/network 的 revision
curl -X POST http://localhost:8080/api/restore/plans/PLAN-9f2c.../execute \
  -H 'Content-Type: application/json' \
  -d '{"revision": 2}'
```

```json
{
  "message": "步骤已执行并提交",
  "plan_id": "PLAN-9f2c...", "status": "active",
  "position": 1, "total_steps": 2, "next_step": 2,
  "seen_revision": 2, "revision": 3,
  "idempotent_replay": false,
  "step": {"position": 1, "valve_id": "V3", "action": "open", "is_open": true,
           "allocations": ["...同 /api/status 口径..."], "totals": {"...": "..."}}
}
```

随后用上一次响应的 `revision`（此处 3）继续执行下一步；最后一步 `status` 变为
`completed`、`next_step` 为 `null`。重放已完成步骤：`{"revision": 3, "step": 1}`。

### 5.3 修订号（乐观并发）

- `revision` 是当前管网/阀位的**基准修订号**，非负整数，单调递增；
- 以下操作成功后各 `+1`：`PUT /api/network`、`DELETE /api/network`、
  `POST /api/valves/change`，以及复水计划**每执行一步**（重复回放不递增）；
- 前三者同时使所有**未完**计划失效——因为计划所依据的管网/阀位前提已变化；
- 读取侧（`GET /api/network`、`GET /api/status`、计划创建/查询/列表响应）都带当前
  `revision`，作为下一次执行的“所见修订号”。

> 与旧版的兼容：以上均为**新增字段/新增接口**；`PUT/GET/DELETE /api/network`、
> `/api/valves/change` 的原有字段与错误码不变，老客户端无需改造。

---

## 5. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、错误处理、整批事务、复水计划执行
│   ├── engine.py      # 校验、互锁连通(BFS)、Dinic 最大流、优先级贪心预演
│   ├── plans.py       # 复水步骤校验、逐步预演、逐步结果快照
│   ├── schemas.py     # Pydantic 请求模型
│   ├── database.py    # SQLite 连接、建表、修订号
│   └── repository.py  # DB <-> 内存模型；计划/步骤/结果持久化
├── tests/             # pytest：25 项端到端 + 引擎测试
├── examples/
│   ├── sample_network.json
│   ├── demo.sh            # 关阀隔离 curl 端到端示例
│   └── restore_demo.sh    # 复水计划 curl 端到端示例
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 6. 运行测试

```bash
python -m pip install pytest httpx
python -m pytest -q
```

## 7. 设计说明与边界

- 管段单向用于**供水方向**的流量计算；互锁水源的**连通性**检查按无向（见 §3 规则 2）。
- 容量为浮点（内部以 1e-9 容差判定饱和）；需求为 0 的点直接判定为已满足。
- 未挂阀门的管段视为常通；管段上任一阀门关闭即不导通。
- 预演仅支持“拟关闭”阀门（抢修预演的实际需求）；库内已关闭阀门自然叠加生效。
- **复水预演不写入**：逐步开/关只在内存中的 `Network` 副本上叠加，库内阀位不变；
  计划保存只写入 `plans/plan_steps`，也不动阀位。
- **计划不可变**：保存后步骤、动作、顺序不可修改；执行只推进 `status/next_step` 与
  追加 `plan_results`。执行某步前会在同一事务内重新做一次互锁连通检查（不依赖创建时的
  预演结论），保证“所见修订号”前提下的安全性。
- **逐步安全而整批也安全**：规则只在“开启管段”上按无向判连通，关阀不会制造连通，
  因此若每一步都不连通，最终状态必然不连通；逐步检查是更严格的作业约束（防止中途短暂并联）。
- **修订号与失效**：录网、清空、阀门变更、复水每步都在写事务内推进 `revision`；前三者
  连同复水执行（针对其它计划）会把未完计划置为 `invalidated`。写操作经应用级互斥锁串行化。
