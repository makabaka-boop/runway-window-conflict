"""跑道施工冲突判定与跑道灯光巡检快照的纯领域逻辑。

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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
