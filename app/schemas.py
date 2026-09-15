"""请求 / 响应的 Pydantic 模型与字段级约束。

时间仅接受形如 ``2026-09-14T22:30:00Z`` 的 ISO 8601 UTC 秒级字符串：
必须带 ``Z``、不接受时区偏移、不接受小数秒；非法时间与
``start >= end`` 都表现为带定位路径（``loc``）的字段级错误。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
import re

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    ValidationInfo,
    field_validator,
)
from pydantic_core import PydanticCustomError
from typing import Annotated, Literal

from app.domain import EVENT_FAULT, EVENT_OK, EVENT_REPAIRED

#: 秒级、仅 Z 结尾的 ISO 8601 UTC 时间（拒绝小数秒与偏移量）。
_Z_SECONDS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)


def validate_utc_z_seconds(value: object) -> datetime:
    """把严格的 ``...Z`` 秒级字符串解析为 UTC ``datetime``。

    请求入口只允许字符串；但响应模型会直接传入已解析的 ``datetime``，
    因此对带 UTC 时区且无小数秒的 ``datetime`` 直接放行。
    字符串先用正则锁死形态，再交给 ``fromisoformat`` 校验日历合法性，
    这样 ``2026-02-30T...``、``25:00:00`` 等不存在的时间也会被拒绝。
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise PydanticCustomError(
                "not_utc_z_string",
                "内部时间缺少 UTC 时区信息",
            )
        aware = value.astimezone(timezone.utc)
        if aware.microsecond:
            raise PydanticCustomError(
                "not_utc_z_seconds",
                "时间必须为秒级，不能包含小数秒",
            )
        return aware

    if not isinstance(value, str):
        # JSON 的 Infinity / NaN（即便作为裸 token 出现）会被解析成
        # float；这类非有限数值无法序列化为合法 JSON，必须在这里显式
        # 拒绝成字段级错误，而不是让错误回显阶段抛内部错误。
        if isinstance(value, float) and not math.isfinite(value):
            raise PydanticCustomError(
                "not_finite_datetime",
                "截止时间必须是有限的带 Z 的 ISO 8601 UTC 秒级字符串，"
                "不能是 Infinity、-Infinity 或 NaN",
            )
        raise PydanticCustomError(
            "not_utc_z_string",
            "时间必须是带 Z 的 ISO 8601 UTC 秒级字符串，例如 2026-09-14T22:30:00Z",
        )
    if not _Z_SECONDS_RE.match(value):
        raise PydanticCustomError(
            "not_utc_z_seconds",
            "时间必须是带 Z 的秒级 ISO 8601 UTC 值（结尾为 Z，无小数秒），收到 {value}",
            {"value": repr(value)},
        )
    try:
        dt = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PydanticCustomError(
            "not_utc_z_seconds",
            "时间不是合法的日历时间，收到 {value}",
            {"value": repr(value)},
        )
    return dt.astimezone(timezone.utc)


def serialize_utc_z_seconds(value: datetime) -> str:
    """序列化为带 ``Z`` 的秒级 UTC 字符串。

    手工拼年份：``strftime('%Y')`` 对公元 1000 年以前的年份不补零，
    会把 0001 年输出成 ``1-...``，必须始终保证四位年份。
    """

    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    aware = aware.astimezone(timezone.utc)
    return (
        f"{aware.year:04d}-{aware.month:02d}-{aware.day:02d}T"
        f"{aware.hour:02d}:{aware.minute:02d}:{aware.second:02d}Z"
    )


#: 请求/响应用：入参只接受严格 Z 秒级字符串，出参序列化为 Z 秒级字符串。
#: 必须用 BeforeValidator —— 在 Pydantic 内置 datetime 解析之前拦截，
#: 否则无 Z 的字符串会被先解析成 naive datetime 而漏过形态校验。
UtcZSecond = Annotated[
    datetime,
    BeforeValidator(validate_utc_z_seconds),
    PlainSerializer(serialize_utc_z_seconds, return_type=str),
]


class _StrictModel(BaseModel):
    """禁止多余字段，保证未知字段也以字段级错误返回。"""

    model_config = ConfigDict(extra="forbid")


def _reject_unpaired_surrogate(value: object) -> str:
    """拒绝含孤立代理（如 ``\\ud800``）的文本。

    JSON 以 ``\\uXXXX`` 转义形式携带的孤立代理会被解析成 Python 字符串，
    但它无法编码为 UTF-8：若放行，字段回显阶段会直接抛内部错误。
    作为 ``BeforeValidator`` 在内置 str 校验之前拦截，所有会原样回显的
    标识字段统一使用 :data:`SafeText`。
    """

    if not isinstance(value, str):
        # 非字符串交给后续内置校验，按标准类型错误返回。
        return value  # type: ignore[return-value]
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise PydanticCustomError(
            "unpaired_surrogate",
            "文本含孤立代理字符，不是合法的 Unicode 字符串",
        )
    return value


#: 会原样回显的自由文本字段专用 str：入口拒绝孤立代理。
SafeText = Annotated[str, BeforeValidator(_reject_unpaired_surrogate)]


#: 数量上限：12 位整数 + 3 位小数。值域内任何三位小数都能无损转为
#: JSON 数值回显（有效数字不超过 15 位），且 float 化永不溢出。
MAX_QUANTITY = Decimal("999999999999.999")


def validate_quantity(value: object) -> Decimal:
    """把 JSON 数值解析为最多三位小数的正 ``Decimal``。

    非数值类型（含布尔、字符串）、非有限值（Infinity / NaN）、非正数、
    超过三位小数或超出可表示范围的值都在此拒绝为字段级错误。
    三位小数以整数比（``as_integer_ratio``，最低项分数的分母必须整除
    1000）精确判定，不经过 ``quantize``，因此不受十进制上下文精度影响，
    ``1.2300`` 这类末尾带零的写法也按数值本身判定。
    """

    if isinstance(value, bool):
        # bool 是 int 的子类，必须先拦截：True/False 不是数量。
        raise PydanticCustomError(
            "quantity_not_a_number",
            "数量必须是 JSON 数值，不能是布尔值",
        )
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        # 直接模型校验（单测）可能传入 float；API 入口经 parse_float=Decimal
        # 解析，只有 Infinity / NaN 这类常量会以 float 到达这里。
        if not math.isfinite(value):
            raise PydanticCustomError(
                "quantity_not_finite",
                "数量必须是有限数值，不能是 Infinity、-Infinity 或 NaN",
            )
        number = Decimal(str(value))
    else:
        raise PydanticCustomError(
            "quantity_not_a_number",
            "数量必须是 JSON 数值",
        )

    if not number.is_finite():
        raise PydanticCustomError(
            "quantity_not_finite",
            "数量必须是有限数值，不能是 Infinity、-Infinity 或 NaN",
        )
    if number <= 0:
        raise PydanticCustomError(
            "quantity_not_positive",
            "数量必须为正数（严格大于零）",
        )
    _numerator, denominator = number.as_integer_ratio()
    if 1000 % denominator != 0:
        raise PydanticCustomError(
            "quantity_too_precise",
            "数量最多支持三位小数，收到 {value}",
            {"value": str(number)},
        )
    if number > MAX_QUANTITY:
        raise PydanticCustomError(
            "quantity_out_of_range",
            "数量超出可表示范围（最大 {max}）",
            {"max": str(MAX_QUANTITY)},
        )
    return number


def serialize_quantity(value: Decimal) -> float:
    """三位小数数量序列化为 JSON 数值。

    值域已被 :data:`MAX_QUANTITY` 约束，float 化有限且按十进制字面量
    精确往返（如 ``0.001`` 序列化再解析仍是 ``0.001``）。
    """

    return float(value)


#: 请求数量：正、有限、最多三位小数的 Decimal。
RequestQuantity = Annotated[Decimal, BeforeValidator(validate_quantity)]

#: 响应数量：序列化为 JSON 数值。
ResponseQuantity = Annotated[
    Decimal,
    PlainSerializer(serialize_quantity, return_type=float),
]


def _check_order(start: datetime, end: datetime) -> None:
    if not start < end:
        # 挂在 end 字段上，定位到具体是哪一段区间。
        raise PydanticCustomError(
            "start_not_before_end",
            "开始时间必须严格早于结束时间",
        )


def _normalize_runway_codes(values: list[str]) -> list[str]:
    """跑道代码去空白、拒空白与重复（/evaluate 与巡检批次共用）。"""

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise PydanticCustomError(
                "bad_runway",
                "跑道代码必须是字符串",
            )
        code = raw.strip()
        if not code:
            raise PydanticCustomError(
                "blank_runway",
                "跑道代码不能为空白",
            )
        if code in seen:
            raise PydanticCustomError(
                "duplicate_runway",
                "跑道代码重复声明: {code}",
                {"code": code},
            )
        seen.add(code)
        normalized.append(code)
    return normalized


class WorkWindowIn(_StrictModel):
    """单段施工窗口入参。"""

    runway: SafeText = Field(..., min_length=1, description="已声明的跑道代码")
    start: UtcZSecond
    end: UtcZSecond

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_runway",
                "跑道代码不能为空白",
            )
        return stripped

    @field_validator("end")
    @classmethod
    def _order(cls, value: datetime, info: ValidationInfo) -> datetime:
        start = info.data.get("start")
        if start is not None:
            _check_order(start, value)
        return value


class OccupancyIn(_StrictModel):
    """单段航班占用入参（端点在领域层外扩十分钟）。"""

    runway: SafeText = Field(..., min_length=1, description="已声明的跑道代码")
    flight_id: SafeText = Field(..., min_length=1, description="航班标识")
    start: UtcZSecond
    end: UtcZSecond

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_runway",
                "跑道代码不能为空白",
            )
        return stripped

    @field_validator("flight_id")
    @classmethod
    def _strip_flight(cls, value: str) -> str:
        if not value.strip():
            raise PydanticCustomError(
                "blank_flight_id",
                "航班标识不能为空白",
            )
        return value

    @field_validator("end")
    @classmethod
    def _order(cls, value: datetime, info: ValidationInfo) -> datetime:
        start = info.data.get("start")
        if start is not None:
            _check_order(start, value)
        return value


class EvaluationRequest(_StrictModel):
    """一次评估请求：声明跑道 + 施工窗口 + 航班占用。

    窗口/占用是否引用了未声明跑道属于跨字段约束，由
    :func:`app.main.unknown_runway_errors` 聚合为字段级 422 错误。
    """

    runways: list[SafeText] = Field(
        ...,
        min_length=1,
        description="本请求已声明的跑道代码集合，窗口/占用引用的跑道必须在此声明",
    )
    work_windows: list[WorkWindowIn] = Field(default_factory=list)
    occupancies: list[OccupancyIn] = Field(default_factory=list)

    @field_validator("runways")
    @classmethod
    def _normalize_runways(cls, values: list[str]) -> list[str]:
        return _normalize_runway_codes(values)


class ConflictOut(_StrictModel):
    """单个冲突航班及其与施工窗口的交集。"""

    flight_id: str
    overlap_start: UtcZSecond
    overlap_end: UtcZSecond


class WorkWindowOut(_StrictModel):
    """单段施工窗口的评估结论；``conflicts`` 为空即安全放行。"""

    runway: str
    start: UtcZSecond
    end: UtcZSecond
    conflicts: list[ConflictOut]


# ---------------------------------------------------------------------------
# 跑道灯光巡检快照：POST /inspection-snapshot
# ---------------------------------------------------------------------------

#: 巡检事件结论取值，与领域层常量保持一致。
EventKind = Literal[EVENT_OK, EVENT_FAULT, EVENT_REPAIRED]


class InspectionPointIn(_StrictModel):
    """一条跑道上应巡检的灯光点位。"""

    runway: SafeText = Field(..., min_length=1, description="已声明的跑道代码")
    code: SafeText = Field(..., min_length=1, description="点位标识，同一批次内跑道内唯一")

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_runway",
                "跑道代码不能为空白",
            )
        return stripped

    @field_validator("code")
    @classmethod
    def _strip_code(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_point_code",
                "点位标识不能为空白",
            )
        return stripped


class InspectionEventIn(_StrictModel):
    """按发生时间记录的一条点位事件（正常 / 故障 / 已修复）。"""

    runway: SafeText = Field(..., min_length=1, description="已声明的跑道代码")
    point: SafeText = Field(..., min_length=1, description="本批次声明过的点位标识")
    observed_at: UtcZSecond = Field(..., description="事件发生时间（严格 UTC 秒级）")
    kind: EventKind

    @field_validator("runway")
    @classmethod
    def _strip_runway(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_runway",
                "跑道代码不能为空白",
            )
        return stripped

    @field_validator("point")
    @classmethod
    def _strip_point(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_point_code",
                "点位标识不能为空白",
            )
        return stripped


class InspectionSnapshotRequest(_StrictModel):
    """一次巡检快照请求：批次标识、截止时间、应查点位与已记录事件。

    点位 / 事件对未声明跑道、未声明点位的引用关系属于跨字段约束，
    由 :func:`app.main.inspection_reference_errors` 聚合为字段级 422；
    同秒矛盾事件由领域层判定。
    """

    batch_id: SafeText = Field(..., min_length=1, description="巡检批次标识")
    cutoff: UtcZSecond = Field(..., description="批次截止时间，之后的记录不参与快照")
    runways: list[SafeText] = Field(
        ...,
        min_length=1,
        description="本批次已声明的跑道代码集合，点位/事件引用的跑道必须在此声明",
    )
    points: list[InspectionPointIn] = Field(..., min_length=1)
    events: list[InspectionEventIn] = Field(default_factory=list)

    @field_validator("batch_id")
    @classmethod
    def _strip_batch_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_batch_id",
                "巡检批次标识不能为空白",
            )
        return stripped

    @field_validator("runways")
    @classmethod
    def _normalize_runways(cls, values: list[str]) -> list[str]:
        return _normalize_runway_codes(values)


class PointStatusOut(_StrictModel):
    """单个点位折叠后的现状。"""

    runway: str
    point: str
    status: Literal["normal", "fault", "unchecked"]
    observed_at: UtcZSecond | None = Field(
        ..., description="决定现状的最新事件时间；未检查点位为 null"
    )


class InspectionSnapshotOut(_StrictModel):
    """巡检批次快照：点位现状及未检查 / 故障汇总数量。"""

    batch_id: str
    cutoff: UtcZSecond
    points: list[PointStatusOut]
    unchecked_count: int = Field(..., ge=0)
    fault_count: int = Field(..., ge=0)


# ---------------------------------------------------------------------------
# 除冰液配给：POST /deicing-allocation
# ---------------------------------------------------------------------------


class DeicingBatchIn(_StrictModel):
    """一个除冰液库存批次入参。"""

    batch_id: SafeText = Field(..., min_length=1, description="库存批次编号，请求内唯一")
    available: RequestQuantity = Field(..., description="可用量，正数，最多三位小数")
    received_at: UtcZSecond = Field(..., description="入库时间（严格 UTC 秒级）")
    expires_at: UtcZSecond = Field(..., description="失效时间（严格 UTC 秒级）")

    @field_validator("batch_id")
    @classmethod
    def _strip_batch_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_batch_id",
                "库存批次编号不能为空白",
            )
        return stripped


class DeicingDemandIn(_StrictModel):
    """一项按优先级排列的除冰作业需求入参。"""

    job_id: SafeText = Field(..., min_length=1, description="作业编号，请求内唯一")
    requested: RequestQuantity = Field(..., description="申请量，正数，最多三位小数")

    @field_validator("job_id")
    @classmethod
    def _strip_job_id(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError(
                "blank_job_id",
                "作业编号不能为空白",
            )
        return stripped


class DeicingAllocationRequest(_StrictModel):
    """一次除冰液配给请求：计算时刻、库存批次与按优先级排列的作业需求。

    批次编号 / 作业编号的唯一性属于跨条目约束，由
    :func:`app.main._deicing_duplicate_errors` 聚合为字段级 422；
    总可用量是否足以覆盖总需求由领域层判定。
    """

    calculated_at: UtcZSecond = Field(
        ..., description="计算时刻，该时刻已失效的批次不参与配给"
    )
    batches: list[DeicingBatchIn] = Field(default_factory=list)
    demands: list[DeicingDemandIn] = Field(default_factory=list)


class AllocationLineOut(_StrictModel):
    """单项需求从某个批次扣减的一笔数量。"""

    batch_id: str
    quantity: ResponseQuantity


class DemandAllocationOut(_StrictModel):
    """单项需求的分配明细（可拆分到多个批次）。"""

    job_id: str
    requested: ResponseQuantity
    lines: list[AllocationLineOut]


class BatchRemainingOut(_StrictModel):
    """配给结束后单个有效批次的剩余量。"""

    batch_id: str
    remaining: ResponseQuantity


class ExpiredBatchOut(_StrictModel):
    """计算时刻已失效、未参与配给的批次。"""

    batch_id: str
    available: ResponseQuantity
    received_at: UtcZSecond
    expires_at: UtcZSecond


class DeicingAllocationOut(_StrictModel):
    """除冰液配给结果：逐项分配明细、批次剩余量与未参与的过期批次。"""

    calculated_at: UtcZSecond
    allocations: list[DemandAllocationOut]
    remaining: list[BatchRemainingOut]
    expired_batches: list[ExpiredBatchOut]
