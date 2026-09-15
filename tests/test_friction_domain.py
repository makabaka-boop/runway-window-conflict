"""摩擦批次评定领域逻辑测试：分段中位数、最低值定级、乱序稳定与整批拒绝。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain import (
    GRADE_GOOD,
    GRADE_POOR,
    GRADE_RESTRICTED,
    SEGMENT_MIDPOINT,
    SEGMENT_ROLLOUT,
    SEGMENT_TOUCHDOWN,
    DuplicateReadingError,
    DuplicateSegmentError,
    FrictionReading,
    InsufficientReadingsError,
    SegmentMedian,
    SegmentReadings,
    UnknownSegmentError,
    assess_friction,
    grade_for,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def q(value: str) -> Decimal:
    return Decimal(value)


def readings(prefix: str, coefficients: list[str]) -> tuple[FrictionReading, ...]:
    return tuple(
        FrictionReading(f"{prefix}-{index}", q(coefficient))
        for index, coefficient in enumerate(coefficients, start=1)
    )


def segment(name: str, prefix: str, coefficients: list[str]) -> SegmentReadings:
    return SegmentReadings(name, readings(prefix, coefficients))


MEASURED = "2026-09-15T04:30:00"


def good_segments() -> list[SegmentReadings]:
    """三段各三条读数、全部良好的基准批次。"""

    return [
        segment(SEGMENT_TOUCHDOWN, "TD", ["0.512", "0.508", "0.515"]),
        segment(SEGMENT_MIDPOINT, "MP", ["0.601", "0.598", "0.605"]),
        segment(SEGMENT_ROLLOUT, "RO", ["0.455", "0.460", "0.450"]),
    ]


class TestNormalPavement:
    """场景一：正常路面——三段中位数都在良好线以上。"""

    def test_medians_and_overall_grade(self) -> None:
        assessment = assess_friction(
            "FR-01", "36L", dt(MEASURED), good_segments()
        )
        assert assessment.batch_id == "FR-01"
        assert assessment.runway == "36L"
        assert assessment.measured_at == dt(MEASURED)
        # 响应固定按着陆段、中段、滑跑段顺序
        assert assessment.segments == (
            SegmentMedian(SEGMENT_TOUCHDOWN, q("0.512")),
            SegmentMedian(SEGMENT_MIDPOINT, q("0.601")),
            SegmentMedian(SEGMENT_ROLLOUT, q("0.455")),
        )
        # 全跑道结论取三段最低值 0.455，仍不低于 0.400 → 良好
        assert assessment.overall_coefficient == q("0.455")
        assert assessment.overall_grade == GRADE_GOOD

    def test_even_count_median_is_exact_decimal(self) -> None:
        # 偶数条读数：中位数为中间两条的均值，0.5115 四位小数精确表示
        segments = good_segments()
        segments[1] = segment(
            SEGMENT_MIDPOINT, "MP", ["0.400", "0.511", "0.512", "0.600"]
        )
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.segments[1].median == q("0.5115")

    def test_extreme_coefficients_zero_and_one(self) -> None:
        # 取值域端点 0 与 1 都是合法系数
        segments = [
            segment(SEGMENT_TOUCHDOWN, "TD", ["0", "0.000", "0.001"]),
            segment(SEGMENT_MIDPOINT, "MP", ["1", "1.000", "0.999"]),
            segment(SEGMENT_ROLLOUT, "RO", ["0.500", "0.500", "0.500"]),
        ]
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.segments[0].median == q("0.000")
        assert assessment.segments[1].median == q("1.000")
        assert assessment.overall_coefficient == q("0.000")
        assert assessment.overall_grade == GRADE_POOR


class TestBoundaryGrades:
    """场景二：临界等级——阈值恰取到即归入较高等级。"""

    def test_median_exactly_0400_is_good(self) -> None:
        segments = good_segments()
        segments[2] = segment(SEGMENT_ROLLOUT, "RO", ["0.400", "0.401", "0.399"])
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.overall_coefficient == q("0.400")
        assert assessment.overall_grade == GRADE_GOOD

    def test_median_just_below_0400_is_restricted(self) -> None:
        segments = good_segments()
        segments[2] = segment(SEGMENT_ROLLOUT, "RO", ["0.399", "0.400", "0.398"])
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.overall_coefficient == q("0.399")
        assert assessment.overall_grade == GRADE_RESTRICTED

    def test_median_exactly_0250_is_restricted(self) -> None:
        segments = good_segments()
        segments[2] = segment(SEGMENT_ROLLOUT, "RO", ["0.250", "0.251", "0.249"])
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.overall_coefficient == q("0.250")
        assert assessment.overall_grade == GRADE_RESTRICTED

    def test_median_just_below_0250_is_poor(self) -> None:
        segments = good_segments()
        segments[2] = segment(SEGMENT_ROLLOUT, "RO", ["0.249", "0.250", "0.248"])
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.overall_coefficient == q("0.249")
        assert assessment.overall_grade == GRADE_POOR

    def test_grade_for_thresholds(self) -> None:
        assert grade_for(q("1")) == GRADE_GOOD
        assert grade_for(q("0.400")) == GRADE_GOOD
        assert grade_for(q("0.399")) == GRADE_RESTRICTED
        assert grade_for(q("0.250")) == GRADE_RESTRICTED
        assert grade_for(q("0.249")) == GRADE_POOR
        assert grade_for(q("0")) == GRADE_POOR

    def test_overall_follows_the_lowest_segment(self) -> None:
        # 最低值出现在中段而非滑跑段时，结论同样跟随最低值
        segments = good_segments()
        segments[1] = segment(SEGMENT_MIDPOINT, "MP", ["0.300", "0.310", "0.290"])
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.overall_coefficient == q("0.300")
        assert assessment.overall_grade == GRADE_RESTRICTED


class TestOrderIndependence:
    """场景三：乱序稳定——读数与分段的输入顺序不改变结果。"""

    def test_shuffled_readings_and_segments_give_identical_result(self) -> None:
        base = good_segments()
        shuffled = [
            segment(SEGMENT_ROLLOUT, "RO", ["0.450", "0.460", "0.455"]),
            segment(SEGMENT_MIDPOINT, "MP", ["0.605", "0.598", "0.601"]),
            segment(SEGMENT_TOUCHDOWN, "TD", ["0.515", "0.512", "0.508"]),
        ]
        first = assess_friction("FR-01", "36L", dt(MEASURED), base)
        second = assess_friction("FR-01", "36L", dt(MEASURED), shuffled)
        assert first == second

    def test_median_sorts_regardless_of_submission_order(self) -> None:
        segments = [
            segment(SEGMENT_TOUCHDOWN, "TD", ["0.900", "0.100", "0.500"]),
            segment(SEGMENT_MIDPOINT, "MP", ["0.500", "0.500", "0.500"]),
            segment(SEGMENT_ROLLOUT, "RO", ["0.500", "0.500", "0.500"]),
        ]
        assessment = assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert assessment.segments[0].median == q("0.500")

    def test_pure_and_reproducible(self) -> None:
        segments = good_segments()
        assert assess_friction(
            "FR-01", "36L", dt(MEASURED), segments
        ) == assess_friction("FR-01", "36L", dt(MEASURED), segments)


class TestWholeBatchRejection:
    """场景四：整批拒绝——分段缺失/不足、分段非法/重复、编号重复。"""

    def test_segment_with_fewer_than_three_readings_raises(self) -> None:
        segments = good_segments()
        segments[0] = segment(SEGMENT_TOUCHDOWN, "TD", ["0.512", "0.508"])
        with pytest.raises(InsufficientReadingsError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.segment == SEGMENT_TOUCHDOWN
        assert exc_info.value.count == 2

    def test_missing_segment_counts_as_zero_readings(self) -> None:
        segments = good_segments()[:2]  # 缺滑跑段
        with pytest.raises(InsufficientReadingsError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.segment == SEGMENT_ROLLOUT
        assert exc_info.value.count == 0

    def test_first_deficient_segment_in_canonical_order_reported(self) -> None:
        # 着陆段与滑跑段都不足三条时，按规范顺序先报着陆段
        segments = [
            segment(SEGMENT_TOUCHDOWN, "TD", ["0.512"]),
            segment(SEGMENT_MIDPOINT, "MP", ["0.601", "0.598", "0.605"]),
            segment(SEGMENT_ROLLOUT, "RO", ["0.455", "0.460"]),
        ]
        with pytest.raises(InsufficientReadingsError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.segment == SEGMENT_TOUCHDOWN

    def test_unknown_segment_raises(self) -> None:
        segments = good_segments()
        segments[0] = segment("sidewalk", "TD", ["0.512", "0.508", "0.515"])
        with pytest.raises(UnknownSegmentError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.segment == "sidewalk"

    def test_duplicate_segment_raises(self) -> None:
        segments = good_segments()
        segments.append(segment(SEGMENT_ROLLOUT, "RX", ["0.1", "0.2", "0.3"]))
        with pytest.raises(DuplicateSegmentError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.segment == SEGMENT_ROLLOUT

    def test_duplicate_reading_id_across_segments_raises(self) -> None:
        segments = good_segments()
        # 复用着陆段第一条读数的编号到滑跑段
        segments[2] = SegmentReadings(
            SEGMENT_ROLLOUT,
            (
                FrictionReading("TD-1", q("0.455")),
                FrictionReading("RO-2", q("0.460")),
                FrictionReading("RO-3", q("0.450")),
            ),
        )
        with pytest.raises(DuplicateReadingError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.reading_id == "TD-1"

    def test_duplicate_reading_id_within_segment_raises(self) -> None:
        segments = good_segments()
        segments[1] = SegmentReadings(
            SEGMENT_MIDPOINT,
            (
                FrictionReading("MP-1", q("0.601")),
                FrictionReading("MP-1", q("0.598")),
                FrictionReading("MP-3", q("0.605")),
            ),
        )
        with pytest.raises(DuplicateReadingError) as exc_info:
            assess_friction("FR-01", "36L", dt(MEASURED), segments)
        assert exc_info.value.reading_id == "MP-1"
