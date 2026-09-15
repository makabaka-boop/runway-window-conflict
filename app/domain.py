"""跑道施工冲突判定、跑道灯光巡检快照、除冰液配给与跑道摩擦批次评定的纯领域逻辑。

本模块只依赖标准库，不感知 HTTP / Pydantic，便于直接单测。

施工放行规则（对应需求）：

- 每段航班占用区间在两端各扩展 :data:`OCCUPANCY_BUFFER`（10 分钟）；
- 施工窗口与“扩展后占用”按半开区间 ``[start, end)`` 比较；
- 仅交集长度严格大于零才计冲突，端点相接（施工结束 == 扩展占用开始，
  或施工开始 == 扩展占用结束）判定安全；
- 不同跑道永不互相命中；
- 冲突先按交集开始时间、再按航班标识升序排列。

巡检快照规则：

- 事件按其发生时间排序后折叠为各点位的最新状态，事件输入顺序不影响结果；
- 截止时间之后（严格大于 cutoff）的记录不参与快照；
- 同一点位同一时刻出现相互矛盾的结论（``ok`` / ``fault`` / ``repaired``
  中不同的两个）时整次快照失败，由调用方转成字段级 422；
- 折叠时 ``ok`` 与 ``repaired`` 都映射为现状正常（``normal``）。

除冰液配给规则：

- 失效时间小于等于计算时刻的批次已失效，不参与扣减，单独列入过期批次；
- 有效批次按（失效时间、入库时间、批次编号）稳定排序，先到先用，
  结果与库存输入顺序无关；
- 需求按给定的优先级顺序逐笔扣减，每项需求可拆分到多个批次；
- 数量统一按三位小数（``Decimal``）精确计算；
- 批次编号 / 作业编号重复、有效库存总量小于总需求时整次失败，
  不返回部分配给。

摩擦批次评定规则：

- 测点读数按分段（着陆段 / 中段 / 滑跑段）分组，分段内按系数排序取中位数，
  读数输入顺序不影响结果；
- 中位数以 ``Decimal`` 精确计算：奇数条取中间值，偶数条取中间两条的均值
  （三位小数系数的均值最多四位小数，除二恒精确）；
- 全跑道结论取三段中位数的最低值，系数不低于 0.400 为良好（``good``）、
  不低于 0.250 为受限（``restricted``）、其余为较差（``poor``）；
- 任一分段缺失或少于三条读数、分段重复、分段名非法、读数编号重复时
  整次失败，不返回部分评定；
- 系数取值域（0 至 1、最多三位小数）由调用方约束（API 层强制），
  与除冰液数量的三位小数约束同一分工。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

#: 航班占用两端各外扩的安全余量（进场前 / 离场后各十分钟）。
OCCUPANCY_BUFFER = timedelta(minutes=10)

#: Python datetime 可表达范围的带 UTC 时区版本。
_MIN_AWARE = datetime.min.replace(tzinfo=timezone.utc)
_MAX_AWARE = datetime.max.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class WorkWindow:
    """一段跑道施工窗口（半开区间）。"""

    runway: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Occupancy:
    """一段航班跑道占用区间（半开区间，尚未外扩）。"""

    runway: str
    flight_id: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class Conflict:
    """施工窗口与单个航班扩展占用的非零交集。"""

    flight_id: str
    overlap_start: datetime
    overlap_end: datetime


@dataclass(frozen=True)
class WorkWindowReport:
    """单个施工窗口的放行结论。

    ``conflicts`` 为空即该窗口对所有同跑道航班满足安全余量，可以放行。
    """

    runway: str
    start: datetime
    end: datetime
    conflicts: tuple[Conflict, ...]


def buffered(occupancy: Occupancy) -> tuple[datetime, datetime]:
    """返回航班占用两端各扩展十分钟后的半开区间。

    占用时间接近年份上下界（``0001`` / ``9999``）时，直接相减/相加会
    溢出；此时把超出可表达范围的端点钳制到 ``datetime`` 的最小/最大值，
    即把无法表示的余量视为一直延伸到可表达边界，从而照常给出放行结论。
    注意必须先按阈值判断再运算——溢出发生在加减那一刻，无法事后比较。
    """

    if occupancy.start <= _MIN_AWARE + OCCUPANCY_BUFFER:
        start = _MIN_AWARE
    else:
        start = occupancy.start - OCCUPANCY_BUFFER

    if occupancy.end >= _MAX_AWARE - OCCUPANCY_BUFFER:
        end = _MAX_AWARE
    else:
        end = occupancy.end + OCCUPANCY_BUFFER
    return start, end


def overlap(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> tuple[datetime, datetime] | None:
    """计算两个半开区间的交集。

    仅当交集长度严格大于零时返回 ``(start, end)``；
    任一端点恰好相接（``max == min``）时返回 ``None``，判定安全。
    """

    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if start < end:
        return start, end
    return None


def evaluate(
    windows: list[WorkWindow],
    occupancies: list[Occupancy],
) -> list[WorkWindowReport]:
    """对全部施工窗口逐窗给出冲突结论。

    输出按施工窗口在请求中的原始顺序排列（响应“按施工窗口返回”）；
    每个窗口内的冲突先按交集开始时间、再按航班标识升序排列。
    航班按跑道分组索引，不同跑道永不比较，且本函数是纯函数：
    相同输入永远得到相同、顺序稳定的输出。
    """

    by_runway: dict[str, list[Occupancy]] = {}
    for occ in occupancies:
        by_runway.setdefault(occ.runway, []).append(occ)

    reports: list[WorkWindowReport] = []
    for window in windows:
        conflicts: list[Conflict] = []
        for occ in by_runway.get(window.runway, ()):
            buf_start, buf_end = buffered(occ)
            hit = overlap(window.start, window.end, buf_start, buf_end)
            if hit is not None:
                conflicts.append(
                    Conflict(
                        flight_id=occ.flight_id,
                        overlap_start=hit[0],
                        overlap_end=hit[1],
                    )
                )

        conflicts.sort(key=lambda c: (c.overlap_start, c.flight_id))
        reports.append(
            WorkWindowReport(
                runway=window.runway,
                start=window.start,
                end=window.end,
                conflicts=tuple(conflicts),
            )
        )
    return reports


# ---------------------------------------------------------------------------
# 跑道灯光巡检快照
# ---------------------------------------------------------------------------

#: 巡检事件结论：正常。
EVENT_OK = "ok"
#: 巡检事件结论：故障。
EVENT_FAULT = "fault"
#: 巡检事件结论：故障已修复。
EVENT_REPAIRED = "repaired"

#: 事件结论的稳定排序（同秒矛盾错误同时按此序报告，不依赖输入顺序）。
EVENT_KINDS: tuple[str, ...] = (EVENT_OK, EVENT_FAULT, EVENT_REPAIRED)

#: 点位现状：正常（最新事件为正常或已修复）。
POINT_NORMAL = "normal"
#: 点位现状：故障（截止前最新事件为故障）。
POINT_FAULT = "fault"
#: 点位现状：未检查（截止前没有任何事件）。
POINT_UNCHECKED = "unchecked"

#: 事件结论到点位现状的映射：修复即恢复正常。
_KIND_TO_STATUS = {
    EVENT_OK: POINT_NORMAL,
    EVENT_FAULT: POINT_FAULT,
    EVENT_REPAIRED: POINT_NORMAL,
}


@dataclass(frozen=True)
class InspectionPoint:
    """一条跑道上应巡检的灯光点位。"""

    runway: str
    code: str


@dataclass(frozen=True)
class InspectionEvent:
    """某点位在某一时刻观察到的一条事件。

    ``index`` 为该事件在请求事件列表中的原始下标，用于把矛盾错误
    定位回具体输入记录；纯领域测试里可留空。
    """

    runway: str
    point: str
    observed_at: datetime
    kind: str
    index: int = -1


@dataclass(frozen=True)
class PointStatus:
    """单个点位折叠后的现状。"""

    runway: str
    point: str
    status: str
    #: 决定现状的最新事件发生时间；未检查点位为 ``None``。
    observed_at: datetime | None


@dataclass(frozen=True)
class InspectionSnapshot:
    """一次巡检批次在截止时间处的快照。"""

    batch_id: str
    cutoff: datetime
    points: tuple[PointStatus, ...]
    unchecked_count: int
    fault_count: int


@dataclass(frozen=True)
class EventContradiction:
    """同一点位同一秒内结论互相矛盾的事件组（已稳定排序）。"""

    runway: str
    point: str
    observed_at: datetime
    events: tuple[InspectionEvent, ...]


class SnapshotContradictionError(ValueError):
    """快照输入存在同秒矛盾事件，整次请求必须失败。"""

    def __init__(self, contradictions: tuple[EventContradiction, ...]) -> None:
        self.contradictions = contradictions
        super().__init__(f"同一点位同一时刻存在矛盾事件 {len(contradictions)} 组")


def _contradiction_key(contradiction: EventContradiction) -> tuple:
    """矛盾分组的稳定排序键：点位、时间、组内首个事件的原始下标。

    排序只依赖输入内容本身，因此无论事件以何种顺序提交，
    422 错误的先后次序都一致。
    """

    first = min(e.index for e in contradiction.events)
    return (contradiction.runway, contradiction.point, contradiction.observed_at, first)


def build_inspection_snapshot(
    batch_id: str,
    cutoff: datetime,
    points: list[InspectionPoint],
    events: list[InspectionEvent],
) -> InspectionSnapshot:
    """按批次截止时间把事件折叠为各点位现状。

    流程（顺序很重要）：

    1. 丢弃发生时间严格晚于 ``cutoff`` 的事件——截止后的修复不参与快照；
    2. 按 (runway, point) 分组，组内事件按 ``(observed_at, kind, index)``
       稳定排序，使结果与事件输入顺序无关；
    3. 同一时刻结论不一致即整次失败（:class:`SnapshotContradictionError`），
       不返回任何部分快照；
    4. 每个点位取截止前最后一条事件作为现状，``ok``/``repaired`` 归为正常，
       没有任何截止前事件的点位记为未检查；
    5. 点位严格按入参顺序返回，同时汇总未检查、故障数量。
    """

    grouped: dict[tuple[str, str], list[InspectionEvent]] = {}
    for event in events:
        if event.observed_at > cutoff:
            continue
        grouped.setdefault((event.runway, event.point), []).append(event)

    contradictions: list[EventContradiction] = []
    latest: dict[tuple[str, str], InspectionEvent] = {}
    for (runway, point), group in grouped.items():
        group.sort(key=lambda e: (e.observed_at, EVENT_KINDS.index(e.kind), e.index))
        timeline: dict[datetime, list[InspectionEvent]] = {}
        for event in group:
            timeline.setdefault(event.observed_at, []).append(event)
        for observed_at, same_second in timeline.items():
            if len({e.kind for e in same_second}) > 1:
                contradictions.append(
                    EventContradiction(
                        runway=runway,
                        point=point,
                        observed_at=observed_at,
                        events=tuple(
                            sorted(
                                same_second,
                                key=lambda e: (EVENT_KINDS.index(e.kind), e.index),
                            )
                        ),
                    )
                )
        last = group[-1]
        latest[(runway, point)] = last

    if contradictions:
        contradictions.sort(key=_contradiction_key)
        raise SnapshotContradictionError(tuple(contradictions))

    statuses: list[PointStatus] = []
    unchecked_count = 0
    fault_count = 0
    for point in points:
        event = latest.get((point.runway, point.code))
        if event is None:
            status = POINT_UNCHECKED
            observed_at: datetime | None = None
            unchecked_count += 1
        else:
            status = _KIND_TO_STATUS[event.kind]
            observed_at = event.observed_at
            if status == POINT_FAULT:
                fault_count += 1
        statuses.append(
            PointStatus(
                runway=point.runway,
                point=point.code,
                status=status,
                observed_at=observed_at,
            )
        )

    return InspectionSnapshot(
        batch_id=batch_id,
        cutoff=cutoff,
        points=tuple(statuses),
        unchecked_count=unchecked_count,
        fault_count=fault_count,
    )


# ---------------------------------------------------------------------------
# 除冰液配给
# ---------------------------------------------------------------------------

#: 数量统一按三位小数（0.001）精确计算。
QUANTUM = Decimal("0.001")

#: 领域加减运算的十进制上下文精度。默认 28 位在极端大数相加时会静默舍入，
#: 这里放宽到 50 位：输入数量经 API 层约束最多 15 位有效数字，
#: 50 位足以让任何现实规模的汇总保持精确。
_DOMAIN_DECIMAL_PRECISION = 50


@dataclass(frozen=True)
class DeicingBatch:
    """一个除冰液库存批次。"""

    batch_id: str
    available: Decimal
    received_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class DeicingDemand:
    """一项按优先级排列的除冰作业需求。"""

    job_id: str
    requested: Decimal


@dataclass(frozen=True)
class AllocationLine:
    """单项需求从某个批次扣减的一笔数量。"""

    batch_id: str
    quantity: Decimal


@dataclass(frozen=True)
class DemandAllocation:
    """单项需求的分配明细；``lines`` 为空表示该需求未获配（仅零申请时）。"""

    job_id: str
    requested: Decimal
    lines: tuple[AllocationLine, ...]


@dataclass(frozen=True)
class BatchRemaining:
    """配给结束后单个有效批次的剩余量（可能为零）。"""

    batch_id: str
    remaining: Decimal


@dataclass(frozen=True)
class ExpiredBatch:
    """计算时刻已失效、未参与配给的批次，数量原样保留。"""

    batch_id: str
    available: Decimal
    received_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class DeicingAllocationReport:
    """一次除冰液配给的完整结果。"""

    calculated_at: datetime
    allocations: tuple[DemandAllocation, ...]
    remaining: tuple[BatchRemaining, ...]
    expired_batches: tuple[ExpiredBatch, ...]


class DuplicateIdentifierError(ValueError):
    """批次编号或作业编号重复，整次请求必须失败。"""

    def __init__(self, kind: str, identifier: str) -> None:
        self.kind = kind
        self.identifier = identifier
        label = "批次编号" if kind == "batch" else "作业编号"
        super().__init__(f"{label}重复: {identifier}")


class InsufficientInventoryError(ValueError):
    """有效库存总量小于总需求，整次请求失败且不返回部分配给。

    反馈必须指出总需求、有效库存与缺口，供调用方转成 422 错误信封。
    """

    def __init__(self, total_demand: Decimal, effective_inventory: Decimal) -> None:
        self.total_demand = total_demand
        self.effective_inventory = effective_inventory
        self.shortfall = total_demand - effective_inventory
        super().__init__(
            "除冰液有效库存不足："
            f"总需求 {total_demand:.3f}，"
            f"有效库存 {effective_inventory:.3f}，"
            f"缺口 {self.shortfall:.3f}"
        )


def _deicing_batch_key(batch: DeicingBatch) -> tuple:
    """批次扣减顺序：失效时间、入库时间、批次编号。

    先失效的批次先扣减，避免先到批次久置过期；编号唯一，排序完全确定，
    与库存输入顺序无关。
    """

    return (batch.expires_at, batch.received_at, batch.batch_id)


def allocate_deicing(
    calculated_at: datetime,
    batches: list[DeicingBatch],
    demands: list[DeicingDemand],
) -> DeicingAllocationReport:
    """按计算时刻对除冰液需求做整次配给。

    流程（顺序很重要）：

    1. 批次编号、作业编号任一重复即整次失败
       （:class:`DuplicateIdentifierError`）；
    2. 失效时间小于等于 ``calculated_at`` 的批次已失效，不参与扣减，
       原样列入 ``expired_batches``；
    3. 有效批次按（失效时间、入库时间、批次编号）稳定排序——
       库存输入顺序不影响结果；
    4. 有效库存总量小于总需求时整次失败
       （:class:`InsufficientInventoryError`），不返回部分配给；
    5. 需求按给定的优先级顺序逐笔扣减，每项需求可拆分到多个批次，
       扣减顺序即第 3 步的批次顺序；
    6. 数量以 ``Decimal`` 精确运算；调用方负责三位小数约束
       （API 层强制），因此全部结果同样精确到三位小数。

    本函数是纯函数：相同输入永远得到相同、顺序稳定的输出。
    """

    seen_batches: set[str] = set()
    for batch in batches:
        if batch.batch_id in seen_batches:
            raise DuplicateIdentifierError("batch", batch.batch_id)
        seen_batches.add(batch.batch_id)
    seen_jobs: set[str] = set()
    for demand in demands:
        if demand.job_id in seen_jobs:
            raise DuplicateIdentifierError("job", demand.job_id)
        seen_jobs.add(demand.job_id)

    with localcontext() as context:
        context.prec = _DOMAIN_DECIMAL_PRECISION

        valid = sorted(
            (b for b in batches if b.expires_at > calculated_at),
            key=_deicing_batch_key,
        )
        expired = sorted(
            (b for b in batches if b.expires_at <= calculated_at),
            key=_deicing_batch_key,
        )

        total_demand = sum((d.requested for d in demands), Decimal(0))
        effective_inventory = sum((b.available for b in valid), Decimal(0))
        if effective_inventory < total_demand:
            raise InsufficientInventoryError(total_demand, effective_inventory)

        pool = {batch.batch_id: batch.available for batch in valid}
        allocations: list[DemandAllocation] = []
        for demand in demands:
            needed = demand.requested
            lines: list[AllocationLine] = []
            for batch in valid:
                if needed <= 0:
                    break
                remaining = pool[batch.batch_id]
                if remaining <= 0:
                    continue
                take = min(remaining, needed)
                pool[batch.batch_id] = remaining - take
                needed -= take
                lines.append(
                    AllocationLine(batch_id=batch.batch_id, quantity=take)
                )
            allocations.append(
                DemandAllocation(
                    job_id=demand.job_id,
                    requested=demand.requested,
                    lines=tuple(lines),
                )
            )

        return DeicingAllocationReport(
            calculated_at=calculated_at,
            allocations=tuple(allocations),
            remaining=tuple(
                BatchRemaining(batch_id=b.batch_id, remaining=pool[b.batch_id])
                for b in valid
            ),
            expired_batches=tuple(
                ExpiredBatch(
                    batch_id=b.batch_id,
                    available=b.available,
                    received_at=b.received_at,
                    expires_at=b.expires_at,
                )
                for b in expired
            ),
        )


# ---------------------------------------------------------------------------
# 跑道摩擦测量批次评定
# ---------------------------------------------------------------------------

#: 法定分段：着陆段。
SEGMENT_TOUCHDOWN = "touchdown"
#: 法定分段：中段。
SEGMENT_MIDPOINT = "midpoint"
#: 法定分段：滑跑段。
SEGMENT_ROLLOUT = "rollout"

#: 三个法定分段的规范顺序，响应固定按此顺序给出中位数。
SEGMENTS: tuple[str, ...] = (SEGMENT_TOUCHDOWN, SEGMENT_MIDPOINT, SEGMENT_ROLLOUT)

#: 总体等级：良好（最低中位数不低于 0.400）。
GRADE_GOOD = "good"
#: 总体等级：受限（最低中位数不低于 0.250 但低于 0.400）。
GRADE_RESTRICTED = "restricted"
#: 总体等级：较差（最低中位数低于 0.250）。
GRADE_POOR = "poor"

#: 良好等级的下限（含）。
FRICTION_GOOD_THRESHOLD = Decimal("0.400")
#: 受限等级的下限（含）。
FRICTION_RESTRICTED_THRESHOLD = Decimal("0.250")

#: 每个分段参与评定的最少读数条数。
MIN_READINGS_PER_SEGMENT = 3


@dataclass(frozen=True)
class FrictionReading:
    """一条测点读数：批次内唯一编号 + 三位小数摩擦系数。"""

    reading_id: str
    coefficient: Decimal


@dataclass(frozen=True)
class SegmentReadings:
    """一个分段提交的全部测点读数。"""

    segment: str
    readings: tuple[FrictionReading, ...]


@dataclass(frozen=True)
class SegmentMedian:
    """一个分段的评定中位数。"""

    segment: str
    median: Decimal


@dataclass(frozen=True)
class FrictionAssessment:
    """一次摩擦测量批次的完整评定结论。

    ``segments`` 固定按 :data:`SEGMENTS` 顺序排列；
    ``overall_coefficient`` 为三段中位数的最低值，``overall_grade``
    是它按阈值划出的全跑道等级。
    """

    batch_id: str
    runway: str
    measured_at: datetime
    segments: tuple[SegmentMedian, ...]
    overall_coefficient: Decimal
    overall_grade: str


class UnknownSegmentError(ValueError):
    """分段名不是三个法定分段之一，整次请求必须失败。"""

    def __init__(self, segment: str) -> None:
        self.segment = segment
        super().__init__(f"分段非法: {segment}")


class DuplicateSegmentError(ValueError):
    """同一分段被重复提交，整次请求必须失败。"""

    def __init__(self, segment: str) -> None:
        self.segment = segment
        super().__init__(f"分段重复提交: {segment}")


class InsufficientReadingsError(ValueError):
    """分段缺失或读数少于三条，整次请求失败且不返回部分评定。"""

    def __init__(self, segment: str, count: int) -> None:
        self.segment = segment
        self.count = count
        super().__init__(f"分段 {segment} 只有 {count} 条读数，少于三条")


class DuplicateReadingError(ValueError):
    """读数编号在批次内重复，整次请求必须失败。"""

    def __init__(self, reading_id: str) -> None:
        self.reading_id = reading_id
        super().__init__(f"读数编号重复: {reading_id}")


def grade_for(coefficient: Decimal) -> str:
    """按阈值把摩擦系数划为等级：>= 0.400 良好，>= 0.250 受限，其余较差。"""

    if coefficient >= FRICTION_GOOD_THRESHOLD:
        return GRADE_GOOD
    if coefficient >= FRICTION_RESTRICTED_THRESHOLD:
        return GRADE_RESTRICTED
    return GRADE_POOR


def _median(coefficients: list[Decimal]) -> Decimal:
    """以 ``Decimal`` 精确计算中位数；输入顺序不影响结果。

    奇数条取排序后的中间值；偶数条取中间两条的均值。三位小数系数
    两两之和仍精确，除以二恒为有限小数（最多四位小数），无需舍入。
    调用方保证列表非空（分段至少三条读数已先行校验）。
    """

    ordered = sorted(coefficients)
    count = len(ordered)
    middle = count // 2
    if count % 2 == 1:
        return ordered[middle]
    with localcontext() as context:
        context.prec = _DOMAIN_DECIMAL_PRECISION
        return (ordered[middle - 1] + ordered[middle]) / 2


def assess_friction(
    batch_id: str,
    runway: str,
    measured_at: datetime,
    segments: list[SegmentReadings],
) -> FrictionAssessment:
    """对一个摩擦测量批次给出整批评定结论。

    流程（顺序很重要）：

    1. 分段名必须是 :data:`SEGMENTS` 之一（:class:`UnknownSegmentError`），
       且同一分段不得重复提交（:class:`DuplicateSegmentError`）；
    2. 三个法定分段都必须出现且各至少三条读数，缺失按零条计
       （:class:`InsufficientReadingsError`）；
    3. 读数编号在整个批次内唯一（:class:`DuplicateReadingError`）；
    4. 各分段读数按系数排序取中位数——读数输入顺序不影响结果；
    5. 三段中位数的最低值即全跑道结论，按阈值划出总体等级。

    系数取值域（0 至 1、最多三位小数）由调用方约束（API 层强制）。
    本函数是纯函数：相同输入永远得到相同、顺序稳定的输出。
    """

    seen_segments: set[str] = set()
    for segment in segments:
        if segment.segment not in SEGMENTS:
            raise UnknownSegmentError(segment.segment)
        if segment.segment in seen_segments:
            raise DuplicateSegmentError(segment.segment)
        seen_segments.add(segment.segment)

    by_segment = {segment.segment: segment for segment in segments}
    for name in SEGMENTS:
        segment = by_segment.get(name)
        count = len(segment.readings) if segment is not None else 0
        if count < MIN_READINGS_PER_SEGMENT:
            raise InsufficientReadingsError(name, count)

    seen_readings: set[str] = set()
    for segment in segments:
        for reading in segment.readings:
            if reading.reading_id in seen_readings:
                raise DuplicateReadingError(reading.reading_id)
            seen_readings.add(reading.reading_id)

    medians = tuple(
        SegmentMedian(
            segment=name,
            median=_median([r.coefficient for r in by_segment[name].readings]),
        )
        for name in SEGMENTS
    )
    overall = min(median.median for median in medians)
    return FrictionAssessment(
        batch_id=batch_id,
        runway=runway,
        measured_at=measured_at,
        segments=medians,
        overall_coefficient=overall,
        overall_grade=grade_for(overall),
    )
