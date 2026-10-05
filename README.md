# 供水管网隔离预演服务

物业抢修场景的供水管网关阀隔离预演 API：录入水源供水上限、**不可并联**的水源对、
**单向管段**及容量、管段上的阀门状态、用水点需求与优先级；支持阀门**整批校验、原子生效**，
并在**不写入状态**的前提下预演关阀后各用水点的供水分配，返回供水量、欠供原因与受限管段。

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
bash examples/demo.sh                 # 默认 http://localhost:8080
bash examples/demo.sh http://主机:端口
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
| PUT | `/api/network` | **整批覆盖式录入**管网（事务，失败全回滚） |
| GET | `/api/network` | 查询当前管网 |
| DELETE | `/api/network` | 清空管网 |
| POST | `/api/valves/change` | 阀门**整批变更**（校验通过才原子生效） |
| POST | `/api/drill` | 关阀隔离**预演**（只读，不写入） |
| GET | `/api/status` | 当前真实阀门状态下的分派结果 |

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
| 422 | `validation_error` | 负数/NaN 容量、缺字段、类型错误、多余字段 |
| 400 | `invalid_network` | 未知端点、自环、重复编号、阀门引用不存在管段、水源对非法或录入时已连通 |
| 400 | `invalid_valve_change` | 未知/非法阀门编号、批次内重复、`action` 与 `is_open` 冲突 |
| 409 | `forbidden_sources_connected` | 变更会使互锁水源经开启管段连通 |
| 400 | `invalid_drill` | 预演阀门编号未知/重复 |
| 409 | `network_not_initialized` | 尚未 `PUT /api/network` |

---

## 5. 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由、错误处理、整批事务
│   ├── engine.py      # 校验、互锁连通(BFS)、Dinic 最大流、优先级贪心预演
│   ├── schemas.py     # Pydantic 请求模型
│   ├── database.py    # SQLite 连接与建表
│   └── repository.py  # DB <-> 内存模型
├── tests/             # pytest：19 项端到端 + 引擎测试
├── examples/
│   ├── sample_network.json
│   └── demo.sh        # curl 端到端调用示例
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
