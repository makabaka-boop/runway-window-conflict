"""除冰液配给领域逻辑测试：过期剔除、稳定排序扣减、跨批次拆分、缺货与三位小数精度。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.domain import (
    AllocationLine,
    BatchNotReceivedError,
    BatchRemaining,
    DeicingBatch,
    DeicingDemand,
    DuplicateIdentifierError,
    ExpiredBatch,
    InsufficientInventoryError,
    InvalidBatchPeriodError,
    allocate_deicing,
)


def dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def q(value: str) -> Decimal:
    return Decimal(value)


def batch(batch_id: str, available: str, received: str, expires: str) -> DeicingBatch:
    return DeicingBatch(batch_id, q(available), dt(received), dt(expires))


def demand(job_id: str, requested: str) -> DeicingDemand:
    return DeicingDemand(job_id, q(requested))


CALC = "2026-09-15T02:00:00"


class TestSingleBatch:
    """场景一：足量单批次覆盖全部需求。"""

    def test_single_batch_covers_all_demands(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [batch("DZ-01", "120.500", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
            [demand("JOB-1", "40.250"), demand("JOB-2", "30.000")],
        )
        assert report.calculated_at == dt(CALC)
        assert [a.job_id for a in report.allocations] == ["JOB-1", "JOB-2"]
        assert report.allocations[0].requested == q("40.250")
        assert report.allocations[0].lines == (
            AllocationLine("DZ-01", q("40.250")),
        )
        assert report.allocations[1].lines == (
            AllocationLine("DZ-01", q("30.000")),
        )
        assert report.remaining == (BatchRemaining("DZ-01", q("50.250")),)
        assert report.expired_batches == ()

    def test_exact_fit_leaves_zero_remaining(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [batch("DZ-01", "10", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
            [demand("JOB-1", "10")],
        )
        assert report.allocations[0].lines == (AllocationLine("DZ-01", q("10")),)
        assert report.remaining == (BatchRemaining("DZ-01", q("0")),)


class TestCrossBatchSplit:
    """场景二：需求跨批次拆分，先失效的批次先扣减。"""

    def test_demand_splits_across_batches_in_expiry_order(self) -> None:
        # 库存输入顺序与扣减顺序相反：DZ-01 先失效必须先扣
        report = allocate_deicing(
            dt(CALC),
            [
                batch("DZ-02", "60", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
                batch("DZ-01", "25", "2026-09-14T20:00:00", "2026-09-15T12:00:00"),
            ],
            [demand("JOB-9", "70")],
        )
        assert report.allocations[0].lines == (
            AllocationLine("DZ-01", q("25")),
            AllocationLine("DZ-02", q("45")),
        )
        # 剩余量按同一规范顺序（失效、入库、编号）报告
        assert report.remaining == (
            BatchRemaining("DZ-01", q("0")),
            BatchRemaining("DZ-02", q("15")),
        )

    def test_demand_priority_order_is_respected(self) -> None:
        # 先提交的需求优先占用先失效的批次
        report = allocate_deicing(
            dt(CALC),
            [
                batch("DZ-01", "10", "2026-09-14T20:00:00", "2026-09-15T12:00:00"),
                batch("DZ-02", "10", "2026-09-14T20:00:00", "2026-09-15T18:00:00"),
            ],
            [demand("JOB-A", "15"), demand("JOB-B", "5")],
        )
        assert report.allocations[0].lines == (
            AllocationLine("DZ-01", q("10")),
            AllocationLine("DZ-02", q("5")),
        )
        assert report.allocations[1].lines == (AllocationLine("DZ-02", q("5")),)
        assert report.remaining == (
            BatchRemaining("DZ-01", q("0")),
            BatchRemaining("DZ-02", q("0")),
        )

    def test_tie_breaks_by_received_time_then_batch_id(self) -> None:
        # 失效时间相同 → 先入库先用；入库也相同 → 编号升序
        report = allocate_deicing(
            dt(CALC),
            [
                batch("B3", "1", "2026-09-14T10:00:00", "2026-09-16T08:00:00"),
                batch("B2", "1", "2026-09-14T08:00:00", "2026-09-16T08:00:00"),
                batch("B1", "1", "2026-09-14T08:00:00", "2026-09-16T08:00:00"),
            ],
            [demand("JOB-1", "2.5")],
        )
        assert report.allocations[0].lines == (
            AllocationLine("B1", q("1")),
            AllocationLine("B2", q("1")),
            AllocationLine("B3", q("0.5")),
        )
        assert [r.batch_id for r in report.remaining] == ["B1", "B2", "B3"]

    def test_batch_input_order_does_not_affect_result(self) -> None:
        batches = [
            batch("DZ-01", "25", "2026-09-14T20:00:00", "2026-09-15T12:00:00"),
            batch("DZ-02", "60", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
            batch("DZ-03", "10", "2026-09-14T19:00:00", "2026-09-15T12:00:00"),
        ]
        demands = [demand("JOB-1", "40"), demand("JOB-2", "20.5")]
        first = allocate_deicing(dt(CALC), batches, demands)
        second = allocate_deicing(dt(CALC), list(reversed(batches)), demands)
        assert first == second


class TestExpiredBatches:
    """场景三：计算时刻已失效的批次不参与配给，单独报告。"""

    def test_expired_batches_never_participate(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [
                # 恰在计算时刻失效（expires_at <= calculated_at 即已失效）
                batch("OLD-1", "50", "2026-09-13T08:00:00", CALC),
                # 更早失效
                batch("OLD-2", "50", "2026-09-13T09:00:00", "2026-09-14T23:59:59"),
                # 计算时刻之后一秒才失效：仍有效
                batch("FR-1", "30", "2026-09-14T20:00:00", "2026-09-15T02:00:01"),
            ],
            [demand("JOB-1", "30")],
        )
        assert report.allocations[0].lines == (AllocationLine("FR-1", q("30")),)
        assert report.remaining == (BatchRemaining("FR-1", q("0")),)
        # 过期批次按（失效、入库、编号）排序报告，数量原样保留
        assert report.expired_batches == (
            ExpiredBatch(
                "OLD-2", q("50"), dt("2026-09-13T09:00:00"), dt("2026-09-14T23:59:59")
            ),
            ExpiredBatch("OLD-1", q("50"), dt("2026-09-13T08:00:00"), dt(CALC)),
        )

    def test_expired_stock_does_not_count_toward_effective_inventory(self) -> None:
        # 过期批次的 50 不计入有效库存：有效 60 < 需求 70 → 整次失败
        with pytest.raises(InsufficientInventoryError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [
                    batch("DZ-01", "60", "2026-09-14T20:00:00", "2026-09-16T08:00:00"),
                    batch("OLD", "50", "2026-09-13T08:00:00", "2026-09-14T08:00:00"),
                ],
                [demand("JOB-1", "70")],
            )
        assert exc_info.value.effective_inventory == q("60")


class TestInsufficientInventory:
    """场景四：总可用量不足时整次失败，反馈指出总需求、有效库存与缺口。"""

    def test_insufficient_inventory_raises_with_totals(self) -> None:
        with pytest.raises(InsufficientInventoryError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [batch("DZ-01", "60", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
                [demand("JOB-1", "70"), demand("JOB-2", "30.5")],
            )
        exc = exc_info.value
        assert exc.total_demand == q("100.5")
        assert exc.effective_inventory == q("60")
        assert exc.shortfall == q("40.5")
        assert "100.500" in str(exc)
        assert "60.000" in str(exc)
        assert "40.500" in str(exc)

    def test_no_valid_batch_at_all_is_insufficient(self) -> None:
        with pytest.raises(InsufficientInventoryError) as exc_info:
            allocate_deicing(dt(CALC), [], [demand("JOB-1", "0.001")])
        assert exc_info.value.effective_inventory == q("0")
        assert exc_info.value.shortfall == q("0.001")

    def test_total_exactly_sufficient_succeeds(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [
                batch("DZ-01", "30", "2026-09-14T20:00:00", "2026-09-16T08:00:00"),
                batch("DZ-02", "40", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
            ],
            [demand("JOB-1", "70")],
        )
        assert all(r.remaining == q("0") for r in report.remaining)


class TestBatchLifecycle:
    """库存批次生命周期：计算时刻尚未入库、入库/失效时段非法都必须整次拒绝。"""

    def test_batch_received_after_calculated_at_is_rejected(self) -> None:
        # 计算时刻 02:00，批次 03:00 才入库：未来库存不得分给当前作业
        with pytest.raises(BatchNotReceivedError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [batch("FUTURE-1", "100", "2026-09-15T03:00:00", "2026-09-17T08:00:00")],
                [demand("JOB-1", "50")],
            )
        assert exc_info.value.batch_id == "FUTURE-1"
        assert exc_info.value.calculated_at == dt(CALC)

    def test_batch_received_one_second_after_calculated_at_is_rejected(self) -> None:
        with pytest.raises(BatchNotReceivedError):
            allocate_deicing(
                dt(CALC),
                [batch("FUTURE-1", "100", "2026-09-15T02:00:01", "2026-09-17T08:00:00")],
                [demand("JOB-1", "1")],
            )

    def test_batch_received_exactly_at_calculated_at_is_available(self) -> None:
        # 入库恰等于计算时刻：视为已入库，可参与配给
        report = allocate_deicing(
            dt(CALC),
            [batch("B1", "10", CALC, "2026-09-17T08:00:00")],
            [demand("JOB-1", "4")],
        )
        assert report.allocations[0].lines == (AllocationLine("B1", q("4")),)
        assert report.remaining == (BatchRemaining("B1", q("6")),)

    def test_future_batch_does_not_count_toward_effective_inventory(self) -> None:
        # 未来批次的 100 既不能配给，也不能“凑数”让缺货检查通过后产生空明细
        with pytest.raises(BatchNotReceivedError):
            allocate_deicing(
                dt(CALC),
                [
                    batch("FUTURE-1", "100", "2026-09-15T03:00:00", "2026-09-17T08:00:00"),
                    batch("DZ-01", "5", "2026-09-14T20:00:00", "2026-09-16T08:00:00"),
                ],
                [demand("JOB-1", "10")],
            )

    def test_future_batch_is_rejected_before_any_partial_allocation(self) -> None:
        # 即使只有空需求，未来批次同样整次拒绝而非原样出现在结果中
        with pytest.raises(BatchNotReceivedError):
            allocate_deicing(
                dt(CALC),
                [batch("FUTURE-1", "100", "2026-09-15T03:00:00", "2026-09-17T08:00:00")],
                [],
            )

    def test_received_after_expires_is_rejected(self) -> None:
        # 入库时间晚于失效时间：合法库存时段为空
        with pytest.raises(InvalidBatchPeriodError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [batch("BAD-1", "100", "2026-09-17T08:00:00", "2026-09-15T08:00:00")],
                [demand("JOB-1", "50")],
            )
        assert exc_info.value.batch_id == "BAD-1"

    def test_received_equal_to_expires_is_rejected(self) -> None:
        # 入库等于失效：零长时段同样非法（必须严格早于）
        with pytest.raises(InvalidBatchPeriodError):
            allocate_deicing(
                dt(CALC),
                [batch("ZERO-1", "100", "2026-09-16T08:00:00", "2026-09-16T08:00:00")],
                [demand("JOB-1", "50")],
            )

    def test_invalid_period_is_rejected_before_lifecycle_and_shortage(self) -> None:
        # 同时倒挂且尚未入库、且库存不足时，先报非法时段
        with pytest.raises(InvalidBatchPeriodError):
            allocate_deicing(
                dt(CALC),
                [batch("BAD-1", "1", "2026-09-17T08:00:00", "2026-09-16T08:00:00")],
                [demand("JOB-1", "50")],
            )

    def test_lifecycle_check_is_independent_of_batch_input_order(self) -> None:
        # 拒绝结论与库存输入顺序无关（按批次编号稳定判定）
        good = batch("DZ-01", "10", "2026-09-14T20:00:00", "2026-09-16T08:00:00")
        future = batch("FUTURE-1", "10", "2026-09-15T03:00:00", "2026-09-17T08:00:00")
        for ordering in ([good, future], [future, good]):
            with pytest.raises(BatchNotReceivedError) as exc_info:
                allocate_deicing(dt(CALC), ordering, [demand("JOB-1", "5")])
            assert exc_info.value.batch_id == "FUTURE-1"

    def test_expired_batch_that_was_received_long_ago_still_works(self) -> None:
        # 回归：早已入库、在计算时刻失效的批次仍按过期批次处理
        report = allocate_deicing(
            dt(CALC),
            [batch("OLD-1", "50", "2026-09-13T08:00:00", CALC)],
            [],
        )
        assert report.expired_batches == (
            ExpiredBatch("OLD-1", q("50"), dt("2026-09-13T08:00:00"), dt(CALC)),
        )


class TestDuplicateIdentifiers:
    def test_duplicate_batch_id_raises(self) -> None:
        with pytest.raises(DuplicateIdentifierError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [
                    batch("DZ-01", "10", "2026-09-14T20:00:00", "2026-09-16T08:00:00"),
                    batch("DZ-01", "5", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
                ],
                [],
            )
        assert exc_info.value.kind == "batch"
        assert exc_info.value.identifier == "DZ-01"

    def test_duplicate_job_id_raises(self) -> None:
        with pytest.raises(DuplicateIdentifierError) as exc_info:
            allocate_deicing(
                dt(CALC),
                [batch("DZ-01", "10", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
                [demand("JOB-1", "1"), demand("JOB-1", "2")],
            )
        assert exc_info.value.kind == "job"
        assert exc_info.value.identifier == "JOB-1"

    def test_batch_and_job_may_share_the_same_identifier(self) -> None:
        # 批次编号与作业编号属于不同命名空间，不互相判重
        report = allocate_deicing(
            dt(CALC),
            [batch("X-1", "10", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
            [demand("X-1", "10")],
        )
        assert report.allocations[0].lines == (AllocationLine("X-1", q("10")),)


class TestThreeDecimalPrecision:
    def test_decimal_arithmetic_is_exact(self) -> None:
        # 1.005 - 0.005 在二进制浮点下会有残差，Decimal 下精确等于 1.000
        report = allocate_deicing(
            dt(CALC),
            [batch("DZ-01", "1.005", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
            [demand("JOB-1", "0.005")],
        )
        assert report.remaining == (BatchRemaining("DZ-01", q("1.000")),)

    def test_tenth_sums_stay_exact(self) -> None:
        # 0.1 + 0.2 == 0.3 精确成立，总量判定不受浮点误差影响
        report = allocate_deicing(
            dt(CALC),
            [batch("DZ-01", "0.300", "2026-09-14T20:00:00", "2026-09-16T08:00:00")],
            [demand("JOB-1", "0.100"), demand("JOB-2", "0.200")],
        )
        assert report.remaining == (BatchRemaining("DZ-01", q("0.000")),)
        assert report.remaining[0].remaining == q("0")

    def test_thousandth_split_across_batches(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [
                batch("DZ-01", "0.001", "2026-09-14T20:00:00", "2026-09-16T08:00:00"),
                batch("DZ-02", "0.002", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
            ],
            [demand("JOB-1", "0.003")],
        )
        assert report.allocations[0].lines == (
            AllocationLine("DZ-01", q("0.001")),
            AllocationLine("DZ-02", q("0.002")),
        )
        assert all(r.remaining == 0 for r in report.remaining)


class TestEmptyInputs:
    def test_empty_demands_leave_all_batches_untouched(self) -> None:
        report = allocate_deicing(
            dt(CALC),
            [
                batch("DZ-02", "60", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
                batch("DZ-01", "25", "2026-09-14T20:00:00", "2026-09-15T12:00:00"),
            ],
            [],
        )
        assert report.allocations == ()
        # 剩余量仍按规范顺序报告
        assert report.remaining == (
            BatchRemaining("DZ-01", q("25")),
            BatchRemaining("DZ-02", q("60")),
        )

    def test_empty_batches_and_demands_yield_empty_report(self) -> None:
        report = allocate_deicing(dt(CALC), [], [])
        assert report.allocations == ()
        assert report.remaining == ()
        assert report.expired_batches == ()

    def test_pure_and_reproducible(self) -> None:
        batches = [
            batch("DZ-01", "25", "2026-09-14T20:00:00", "2026-09-15T12:00:00"),
            batch("DZ-02", "60", "2026-09-14T21:00:00", "2026-09-17T08:00:00"),
        ]
        demands = [demand("JOB-1", "40")]
        assert allocate_deicing(dt(CALC), batches, demands) == allocate_deicing(
            dt(CALC), batches, demands
        )
