"""HTTP API 的请求/响应模型（Pydantic v2）。"""
from __future__ import annotations

import math
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")  # 多余字段直接报错，防止录入错位


def _finite(v: float) -> float:
    if not math.isfinite(v):
        raise ValueError("必须是有限数值，不能是 NaN 或无穷大")
    return v


class SourceIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="水源编号")
    capacity: float = Field(ge=0, description="供水上限（非负）")

    @field_validator("capacity")
    @classmethod
    def _cap_finite(cls, v: float) -> float:
        return _finite(v)


class DemandIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="用水点编号")
    demand: float = Field(ge=0, description="需求量（非负）")
    priority: int = Field(default=0, description="优先级，数值越大越优先")

    @field_validator("demand")
    @classmethod
    def _demand_finite(cls, v: float) -> float:
        return _finite(v)


class JunctionIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="连接点编号")


class PipeIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="管段编号")
    from_node: str = Field(min_length=1, description="起点节点编号")
    to_node: str = Field(min_length=1, description="终点节点编号（管段单向）")
    capacity: float = Field(ge=0, description="管段容量（非负）")

    @field_validator("capacity")
    @classmethod
    def _cap_finite(cls, v: float) -> float:
        return _finite(v)


class ValveIn(StrictModel):
    id: str = Field(min_length=1, max_length=64, description="阀门编号")
    pipe_id: str = Field(min_length=1, description="所在管段编号")
    is_open: bool = Field(default=True, description="true=开启，false=关闭")


class ForbiddenPairIn(StrictModel):
    source_a: str = Field(min_length=1)
    source_b: str = Field(min_length=1)


class NetworkLoad(StrictModel):
    """整批录入：一次性覆盖式写入全部设施（原子事务）。"""

    sources: list[SourceIn] = Field(default_factory=list)
    demands: list[DemandIn] = Field(default_factory=list)
    junctions: list[JunctionIn] = Field(default_factory=list)
    pipes: list[PipeIn] = Field(default_factory=list)
    valves: list[ValveIn] = Field(default_factory=list)
    forbidden_pairs: list[ForbiddenPairIn] = Field(default_factory=list)


class ValveChangeOp(StrictModel):
    """单只阀门的变更。action 与 is_open 二选一。"""

    valve_id: str = Field(min_length=1)
    action: Optional[Literal["open", "close"]] = None
    is_open: Optional[bool] = None


class ValveChangeBatch(StrictModel):
    changes: list[ValveChangeOp] = Field(min_length=1)


class DrillRequest(StrictModel):
    """隔离预演：拟关闭的阀门编号列表（不写入状态）。"""

    close_valves: list[str] = Field(default_factory=list)


class RestorationStepIn(StrictModel):
    """复水计划中的一步：对某只阀门执行 open/close。

    清单整体必须非空、阀门编号不重复；顺序即操作顺序。
    `action` 只声明为字符串：取值合法性（非法动作定位）在接口层与
    “未知阀门/重复编号”一并返回 400 + steps[i].action 定位。
    """

    valve_id: str = Field(min_length=1, max_length=64, description="阀门编号")
    action: str = Field(description="开阀/关阀动作（仅允许 open / close）")


class RestorationPlanCreate(StrictModel):
    """提交复水阀门操作清单：服务逐步预演，全部安全才保存为不可变计划。"""

    steps: list[RestorationStepIn] = Field(min_length=1)


class PlanExecuteRequest(StrictModel):
    """执行计划的下一步：只接受计划的下一步及所见基准修订号。"""

    step_no: int = Field(ge=1, description="要执行的步骤号（从 1 开始）")
    revision: int = Field(ge=0, description="调用方所见的基准修订号")
