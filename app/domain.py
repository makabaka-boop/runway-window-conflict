"""跑道施工与航班占用冲突判定的纯领域逻辑。

本模块只依赖标准库，不感知 HTTP / Pydantic，便于直接单测。

规则（对应需求）：

- 每段航班占用区间在两端各扩展 :data:`OCCUPANCY_BUFFER`（10 分钟）；
- 施工窗口与“扩展后占用”按半开区间 ``[start, end)`` 比较；
- 仅交集长度严格大于零才计冲突，端点相接（施工结束 == 扩展占用开始，
  或施工开始 == 扩展占用结束）判定安全；
- 不同跑道永不互相命中；
- 冲突先按交集开始时间、再按航班标识升序排列。
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
