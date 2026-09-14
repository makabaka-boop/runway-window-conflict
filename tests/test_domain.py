"""领域逻辑测试：十分钟余量、半开区间临界相接、跨日、排序、跑道隔离。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.domain import (
    Conflict,
    Occupancy,
    WorkWindow,
    WorkWindowReport,
    buffered,
    evaluate,
    overlap,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


class TestBufferAndOverlap:
    def test_buffer_extends_ten_minutes_each_side(self) -> None:
        occ = Occupancy("36L", "CA1", dt("2026-09-15T02:30:00"), dt("2026-09-15T02:40:00"))
        assert buffered(occ) == (
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
        )

    def test_touching_ends_are_safe(self) -> None:
        # 施工结束 == 扩展占用开始
        assert overlap(
            dt("2026-09-15T02:00:00"),
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
        ) is None

    def test_touching_starts_are_safe(self) -> None:
        # 施工开始 == 扩展占用结束
        assert overlap(
            dt("2026-09-15T02:50:00"),
            dt("2026-09-15T03:20:00"),
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
        ) is None

    def test_one_second_incursion_is_conflict(self) -> None:
        hit = overlap(
            dt("2026-09-15T02:00:00"),
            dt("2026-09-15T02:20:01"),
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
        )
        assert hit == (dt("2026-09-15T02:20:00"), dt("2026-09-15T02:20:01"))

    def test_overlap_exact_match(self) -> None:
        hit = overlap(
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
            dt("2026-09-15T02:20:00"),
            dt("2026-09-15T02:50:00"),
        )
        assert hit == (dt("2026-09-15T02:20:00"), dt("2026-09-15T02:50:00"))


class TestEvaluate:
    def _occ(self, flight: str, start: str, end: str, runway: str = "36L") -> Occupancy:
        return Occupancy(runway, flight, dt(start), dt(end))

    def _win(self, start: str, end: str, runway: str = "36L") -> WorkWindow:
        return WorkWindow(runway, dt(start), dt(end))

    def test_critical_boundaries_both_sides_safe(self) -> None:
        reports = evaluate(
            [
                self._win("2026-09-15T02:00:00", "2026-09-15T02:20:00"),
                self._win("2026-09-15T02:50:00", "2026-09-15T03:20:00"),
            ],
            [self._occ("CA1001", "2026-09-15T02:30:00", "2026-09-15T02:40:00")],
        )
        assert reports == [
            WorkWindowReport("36L", dt("2026-09-15T02:00:00"), dt("2026-09-15T02:20:00"), ()),
            WorkWindowReport("36L", dt("2026-09-15T02:50:00"), dt("2026-09-15T03:20:00"), ()),
        ]

    def test_cross_midnight_window_vs_occupancy(self) -> None:
        # 施工从 23:50 跨到次日 00:20；占用 00:30-00:40，扩展 00:20-00:50
        # 施工结束 00:20 == 扩展开始 00:20 → 相接安全
        reports = evaluate(
            [self._win("2026-09-14T23:50:00", "2026-09-15T00:20:00")],
            [self._occ("CA9", "2026-09-15T00:30:00", "2026-09-15T00:40:00")],
        )
        assert reports[0].conflicts == ()

    def test_cross_midnight_conflict_uses_real_calendar_datetimes(self) -> None:
        # 跨日施工 23:50 -> 00:25，扩展占用 00:20 -> 00:50，交集 00:20-00:25
        reports = evaluate(
            [self._win("2026-09-14T23:50:00", "2026-09-15T00:25:00")],
            [self._occ("CA9", "2026-09-15T00:30:00", "2026-09-15T00:40:00")],
        )
        assert reports[0].conflicts == (
            Conflict("CA9", dt("2026-09-15T00:20:00"), dt("2026-09-15T00:25:00")),
        )

    def test_different_runways_never_hit(self) -> None:
        reports = evaluate(
            [self._win("2026-09-15T02:00:00", "2026-09-15T03:00:00", runway="18R")],
            [self._occ("CA1", "2026-09-15T02:00:00", "2026-09-15T03:00:00", runway="36L")],
        )
        assert reports[0].conflicts == ()

    def test_conflicts_sorted_by_overlap_start_then_flight_id(self) -> None:
        # 两个航班扩展后都覆盖施工窗：
        # ZZ1 扩展 02:00-01:30 -> 01:50-02:40
        # AA2 扩展 02:20-02:30 -> 02:10-02:40
        # 与施工 [02:00,03:00) 的交集起点：ZZ1 02:00 < AA2 02:10
        reports = evaluate(
            [self._win("2026-09-15T02:00:00", "2026-09-15T03:00:00")],
            [
                self._occ("ZZ1", "2026-09-15T02:00:00", "2026-09-15T02:30:00"),
                self._occ("AA2", "2026-09-15T02:20:00", "2026-09-15T02:30:00"),
            ],
        )
        assert [c.flight_id for c in reports[0].conflicts] == ["ZZ1", "AA2"]

    def test_same_overlap_start_sorts_by_flight_id(self) -> None:
        # 两航班扩展后起点相同（不同占用但相同开始），按航班标识升序
        common = ("2026-09-15T02:30:00", "2026-09-15T02:40:00")
        reports = evaluate(
            [self._win("2026-09-15T02:00:00", "2026-09-15T03:00:00")],
            [
                self._occ("ZZ9", *common),
                self._occ("AA1", *common),
            ],
        )
        assert [c.flight_id for c in reports[0].conflicts] == ["AA1", "ZZ9"]

    def test_reports_keep_window_input_order(self) -> None:
        reports = evaluate(
            [
                self._win("2026-09-15T04:00:00", "2026-09-15T04:30:00"),
                self._win("2026-09-15T02:00:00", "2026-09-15T02:10:00"),
            ],
            [],
        )
        assert [r.start for r in reports] == [
            dt("2026-09-15T04:00:00"),
            dt("2026-09-15T02:00:00"),
        ]

    def test_pure_and_reproducible(self) -> None:
        windows = [self._win("2026-09-15T02:00:00", "2026-09-15T03:00:00")]
        occupancies = [
            self._occ("B", "2026-09-15T02:20:00", "2026-09-15T02:30:00"),
            self._occ("A", "2026-09-15T02:10:00", "2026-09-15T02:20:00"),
        ]
        first = evaluate(windows, occupancies)
        second = evaluate(windows, list(reversed(occupancies)))
        assert first == second
        assert [c.flight_id for c in first[0].conflicts] == ["A", "B"]

    def test_intersection_timestamps_are_exact(self) -> None:
        # 施工夹在扩展区间内部：交集等于施工窗自身
        reports = evaluate(
            [self._win("2026-09-15T02:25:00", "2026-09-15T02:35:00")],
            [self._occ("CA1", "2026-09-15T02:30:00", "2026-09-15T02:40:00")],
        )
        (conflict,) = reports[0].conflicts
        assert conflict.overlap_start == dt("2026-09-15T02:25:00")
        assert conflict.overlap_end == dt("2026-09-15T02:35:00")

    def test_exactly_ten_minutes_gap_is_safe(self) -> None:
        # 施工结束 02:20:00，占用 02:30:00 开始：恰好十分钟余量，安全
        win_end = dt("2026-09-15T02:20:00")
        reports = evaluate(
            [self._win("2026-09-15T02:10:00", "2026-09-15T02:20:00")],
            [
                Occupancy(
                    "36L",
                    "CA1",
                    win_end + timedelta(minutes=10),
                    win_end + timedelta(minutes=15),
                )
            ],
        )
        assert reports[0].conflicts == ()
