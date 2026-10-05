#!/usr/bin/env bash
# 物业复水（逐步开阀恢复供水）端到端调用示例。
#
# 流程：录入抢修后管网（各支线阀关闭、互锁联络阀关闭）
#   1) 提交有序开关步骤 → 服务以当前管网/阀位逐步预演，不改变实际状态
#   2) 演示首个互锁冲突被定位（步骤位置、连通水源、路径），计划不保存
#   3) 提交全部安全的清单 → 保存不可变计划，返回每步阀位与供水/欠供、基准修订号
#   4) 仅按“下一步 + 所见修订号”逐步执行；演示跳步拒绝、重复提交幂等
#   5) 计划外阀门变更使未完计划失效（修订号 +1），过期执行被拒绝
#
# 用法：bash examples/restoration_demo.sh [BASE_URL]
set -euo pipefail

BASE="${1:-http://localhost:8080}"
JQ="$(command -v jq || true)"
pp() { if [ -n "$JQ" ]; then jq .; else cat; fi; }

echo "== 0. 录入抢修后待复水管网 =="
# S1 --P1(V1,关)--> D1(需求10)
# S2 --P2(cap8)--> J1 --P3(V3,关)--> D2(需求5)
#                        \--P6(V6,关)--> D4(需求4)
# S1 --P5(V5,关,互锁联络)--> S2 ；J1 --P7(V7,开)--> S2
curl -sS -X PUT "$BASE/api/network" \
  -H 'Content-Type: application/json' \
  -d '{
    "sources": [{"id":"S1","capacity":10},{"id":"S2","capacity":8}],
    "demands": [
      {"id":"D1","demand":10,"priority":9},
      {"id":"D2","demand":5,"priority":1},
      {"id":"D4","demand":4,"priority":1}
    ],
    "junctions": [{"id":"J1"}],
    "pipes": [
      {"id":"P1","from_node":"S1","to_node":"D1","capacity":10},
      {"id":"P2","from_node":"S2","to_node":"J1","capacity":8},
      {"id":"P3","from_node":"J1","to_node":"D2","capacity":8},
      {"id":"P6","from_node":"J1","to_node":"D4","capacity":8},
      {"id":"P5","from_node":"S1","to_node":"S2","capacity":5},
      {"id":"P7","from_node":"J1","to_node":"S2","capacity":5}
    ],
    "valves": [
      {"id":"V1","pipe_id":"P1","is_open":false},
      {"id":"V3","pipe_id":"P3","is_open":false},
      {"id":"V5","pipe_id":"P5","is_open":false},
      {"id":"V6","pipe_id":"P6","is_open":false},
      {"id":"V7","pipe_id":"P7","is_open":true}
    ],
    "forbidden_pairs": [{"source_a":"S1","source_b":"S2"}]
  }' | pp
echo

echo "== 1. 提交清单：第 3 步打开互锁联络阀 V5 —— 逐步预演在第 3 步报冲突，计划不保存 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps": [
        {"valve_id":"V6","action":"open"},
        {"valve_id":"V1","action":"open"},
        {"valve_id":"V5","action":"open"}
      ]}'
pp < /tmp/resp.json
echo

echo "== 1b. 未知阀门 / 编号重复 / 非法动作 —— 定位报错（steps[i].valve_id） =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps": [
        {"valve_id":"V6","action":"open"},
        {"valve_id":"GHOST","action":"open"}
      ]}'
pp < /tmp/resp.json
echo

echo "== 2. 提交全部安全的复水清单：先恢复 S2 子系统(V6,V3)，再恢复 S1(V1) =="
CREATE=$(curl -sS -X POST "$BASE/api/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps": [
        {"valve_id":"V6","action":"open"},
        {"valve_id":"V3","action":"open"},
        {"valve_id":"V1","action":"open"}
      ]}')
echo "$CREATE" | pp
PID="$(printf '%s' "$CREATE" | sed -n 's/.*"plan_id": *"\([^"]*\)".*/\1/p')"
REV="$(printf '%s' "$CREATE" | sed -n 's/.*"base_revision": *\([0-9]*\).*/\1/p')"
echo "(plan_id=$PID base_revision=$REV)"
echo

echo "== 3. 跳步执行（直接提交第 2 步）—— 409 拒绝，状态不变 =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/plans/$PID/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 2, \"revision\": $REV}"
pp < /tmp/resp.json
echo

echo "== 4. 执行下一步（第 1 步，带所见修订号）—— 事务内复核、落阀位、推进进度 =="
curl -sS -X POST "$BASE/api/plans/$PID/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 1, \"revision\": $REV}" | pp
echo

echo "== 4b. 重复提交已完成的第 1 步 —— 幂等返回原结果，不再次改变状态 =="
curl -sS -X POST "$BASE/api/plans/$PID/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 1, \"revision\": $REV}" | pp
echo

echo "== 5. 继续执行第 2、3 步直至完成 =="
curl -sS -X POST "$BASE/api/plans/$PID/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 2, \"revision\": $REV}" > /dev/null
curl -sS -X POST "$BASE/api/plans/$PID/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 3, \"revision\": $REV}" | pp
echo

echo "== 6. 查询计划：每步预演(preview)与实际结果(result)、进度 =="
curl -sS "$BASE/api/plans/$PID" | pp
echo

echo "== 7. 再建一个未完计划，演示计划外阀门变更使其失效 =="
P2=$(curl -sS -X POST "$BASE/api/plans" \
  -H 'Content-Type: application/json' \
  -d '{"steps":[{"valve_id":"V7","action":"close"}]}')
PID2="$(printf '%s' "$P2" | sed -n 's/.*"plan_id": *"\([^"]*\)".*/\1/p')"
echo "(新计划 $PID2；V7 当前开启，计划为关闭 V7)"
curl -sS -X POST "$BASE/api/valves/change" \
  -H 'Content-Type: application/json' \
  -d '{"changes":[{"valve_id":"V7","action":"close"}]}' | pp
echo
echo "== 7b. 用旧修订号执行新计划 —— 409 revision_conflict =="
curl -sS -o /tmp/resp.json -w "HTTP %{http_code}\n" -X POST "$BASE/api/plans/$PID2/execute" \
  -H 'Content-Type: application/json' \
  -d "{\"step_no\": 1, \"revision\": $REV}"
pp < /tmp/resp.json
echo
echo "== 7c. 计划已被置为 invalidated（历史仍可查） =="
curl -sS "$BASE/api/plans/$PID2" | pp
echo
echo "复水演示完成。"
