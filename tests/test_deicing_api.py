"""除冰液配给 API 测试：四大验收场景、字段级错误、整次失败无部分配给。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

CALC = "2026-09-15T02:00:00Z"


def _batch(
    batch_id: str,
    available: float,
    received: str = "2026-09-14T20:00:00Z",
    expires: str = "2026-09-16T08:00:00Z",
) -> dict:
    return {
        "batch_id": batch_id,
        "available": available,
        "received_at": received,
        "expires_at": expires,
    }


def _base(**overrides) -> dict:
    payload = {
        "calculated_at": CALC,
        "batches": [_batch("DZ-01", 120.5)],
        "demands": [
            {"job_id": "JOB-1", "requested": 40.25},
            {"job_id": "JOB-2", "requested": 30},
        ],
    }
    payload.update(overrides)
    return payload


def _allocate(payload: dict) -> tuple[int, object]:
    resp = client.post("/deicing-allocation", json=payload)
    return resp.status_code, resp.json()


def _loc_types(payload: dict) -> set[tuple]:
    status, body = _allocate(payload)
    assert status == 422, body
    return {(tuple(e["loc"]), e["type"]) for e in body["detail"]}


class TestDeicingAllocationScenarios:
    def test_single_batch_covers_all_demands(self) -> None:
        """场景一：足量单批次。"""
        status, body = _allocate(_base())
        assert status == 200
        assert body == {
            "calculated_at": CALC,
            "allocations": [
                {
                    "job_id": "JOB-1",
                    "requested": 40.25,
                    "lines": [{"batch_id": "DZ-01", "quantity": 40.25}],
                },
                {
                    "job_id": "JOB-2",
                    "requested": 30,
                    "lines": [{"batch_id": "DZ-01", "quantity": 30}],
                },
            ],
            "remaining": [{"batch_id": "DZ-01", "remaining": 50.25}],
            "expired_batches": [],
        }

    def test_demand_splits_across_batches_by_expiry(self) -> None:
        """场景二：跨批次拆分，先失效先用，与库存输入顺序无关。"""
        payload = _base(
            batches=[
                _batch("DZ-02", 60, "2026-09-14T21:00:00Z", "2026-09-17T08:00:00Z"),
                _batch("DZ-01", 25, "2026-09-14T20:00:00Z", "2026-09-15T12:00:00Z"),
            ],
            demands=[{"job_id": "JOB-9", "requested": 70}],
        )
        status, body = _allocate(payload)
        assert status == 200
        assert body["allocations"] == [
            {
                "job_id": "JOB-9",
                "requested": 70,
                "lines": [
                    {"batch_id": "DZ-01", "quantity": 25},
                    {"batch_id": "DZ-02", "quantity": 45},
                ],
            }
        ]
        assert body["remaining"] == [
            {"batch_id": "DZ-01", "remaining": 0},
            {"batch_id": "DZ-02", "remaining": 15},
        ]

        # 库存倒序提交，响应必须逐字节一致
        shuffled = dict(payload)
        shuffled["batches"] = list(reversed(payload["batches"]))
        status2, body2 = _allocate(shuffled)
        assert status2 == 200
        assert body2 == body

    def test_expired_batches_are_excluded_and_reported(self) -> None:
        """场景三：过期批次排除（失效时间 <= 计算时刻即已失效）。"""
        status, body = _allocate(
            _base(
                batches=[
                    # 恰在计算时刻失效
                    _batch("OLD-1", 50, "2026-09-13T08:00:00Z", CALC),
                    # 计算时刻之后一秒才失效：仍有效
                    _batch("FR-1", 30, "2026-09-14T20:00:00Z", "2026-09-15T02:00:01Z"),
                ],
                demands=[{"job_id": "JOB-1", "requested": 30}],
            )
        )
        assert status == 200
        assert body["allocations"][0]["lines"] == [
            {"batch_id": "FR-1", "quantity": 30}
        ]
        assert body["remaining"] == [{"batch_id": "FR-1", "remaining": 0}]
        assert body["expired_batches"] == [
            {
                "batch_id": "OLD-1",
                "available": 50,
                "received_at": "2026-09-13T08:00:00Z",
                "expires_at": CALC,
            }
        ]

    def test_insufficient_inventory_fails_with_totals_and_no_partial(self) -> None:
        """场景四：库存不足整次 422，指出总需求、有效库存与缺口，无部分配给。"""
        status, body = _allocate(
            _base(
                batches=[
                    _batch("DZ-01", 60),
                    # 过期批次的 50 不计入有效库存
                    _batch("OLD-1", 50, "2026-09-13T08:00:00Z", "2026-09-14T08:00:00Z"),
                ],
                demands=[
                    {"job_id": "JOB-1", "requested": 70},
                    {"job_id": "JOB-2", "requested": 30.5},
                ],
            )
        )
        assert status == 422
        # 无部分配给：错误响应只含 detail
        assert set(body.keys()) == {"detail"}
        (error,) = body["detail"]
        assert error["type"] == "insufficient_inventory"
        assert error["loc"] == ["body"]
        assert error["ctx"] == {
            "total_demand": 100.5,
            "effective_inventory": 60,
            "shortfall": 40.5,
        }
        assert "100.500" in error["msg"]
        assert "60.000" in error["msg"]
        assert "40.500" in error["msg"]

    def test_empty_demands_leave_all_remaining(self) -> None:
        status, body = _allocate(_base(demands=[]))
        assert status == 200
        assert body["allocations"] == []
        assert body["remaining"] == [{"batch_id": "DZ-01", "remaining": 120.5}]

    def test_no_valid_batch_and_positive_demand_is_insufficient(self) -> None:
        status, body = _allocate(_base(batches=[], demands=[{"job_id": "J", "requested": 1}]))
        assert status == 422
        assert body["detail"][0]["type"] == "insufficient_inventory"
        assert body["detail"][0]["ctx"]["effective_inventory"] == 0


class TestDeicingDuplicateIdentifiers:
    def test_duplicate_batch_id_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                batches=[
                    _batch("DZ-01", 10),
                    _batch("DZ-01", 5, "2026-09-14T21:00:00Z"),
                ],
                demands=[],
            )
        )
        assert (("body", "batches", 1, "batch_id"), "duplicate_batch_id") in errors

    def test_duplicate_job_id_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                demands=[
                    {"job_id": "JOB-1", "requested": 1},
                    {"job_id": "JOB-1", "requested": 2},
                ]
            )
        )
        assert (("body", "demands", 1, "job_id"), "duplicate_job_id") in errors

    def test_duplicate_after_strip_is_caught(self) -> None:
        # " DZ-01 " 与 "DZ-01" 去空白后相同，同样判重
        errors = _loc_types(
            _base(
                batches=[_batch("DZ-01", 10), _batch(" DZ-01 ", 5)],
                demands=[],
            )
        )
        assert (("body", "batches", 1, "batch_id"), "duplicate_batch_id") in errors

    def test_duplicates_aggregate_with_other_field_errors(self) -> None:
        # 重复编号与非法时间在同一次请求中一起聚合返回
        status, body = _allocate(
            _base(
                batches=[
                    _batch("DZ-01", 10),
                    _batch("DZ-01", 5, expires="not-a-time"),
                ],
                demands=[],
            )
        )
        assert status == 422
        locs = {tuple(e["loc"]) for e in body["detail"]}
        assert ("body", "batches", 1, "batch_id") in locs
        assert ("body", "batches", 1, "expires_at") in locs


class TestDeicingFieldValidation:
    def test_invalid_times_rejected(self) -> None:
        errors = _loc_types(
            _base(
                calculated_at="2026-09-15T02:00:00",  # 缺 Z
                batches=[
                    _batch(
                        "DZ-01",
                        10,
                        received="2026-09-14T20:00:00+00:00",  # 偏移量
                        expires="2026-09-16T08:00:00.5Z",  # 小数秒
                    )
                ],
            )
        )
        assert (("body", "calculated_at"), "not_utc_z_seconds") in errors
        assert (("body", "batches", 0, "received_at"), "not_utc_z_seconds") in errors
        assert (("body", "batches", 0, "expires_at"), "not_utc_z_seconds") in errors

    def test_nonexistent_calendar_time_rejected(self) -> None:
        errors = _loc_types(_base(calculated_at="2026-02-30T02:00:00Z"))
        assert (("body", "calculated_at"), "not_utc_z_seconds") in errors

    def test_non_positive_quantities_rejected(self) -> None:
        errors = _loc_types(
            _base(
                batches=[_batch("DZ-01", 0)],
                demands=[{"job_id": "JOB-1", "requested": -1.5}],
            )
        )
        assert (("body", "batches", 0, "available"), "quantity_not_positive") in errors
        assert (("body", "demands", 0, "requested"), "quantity_not_positive") in errors

    def test_more_than_three_decimals_rejected(self) -> None:
        errors = _loc_types(
            _base(
                batches=[_batch("DZ-01", 1.0001)],
                demands=[{"job_id": "JOB-1", "requested": 0.0001}],
            )
        )
        assert (("body", "batches", 0, "available"), "quantity_too_precise") in errors
        assert (("body", "demands", 0, "requested"), "quantity_too_precise") in errors

    def test_trailing_zeros_beyond_three_decimals_accepted(self) -> None:
        # 1.2300 数值上就是 1.23，不超精度
        status, body = _allocate(
            _base(
                batches=[_batch("DZ-01", 1.2300)],
                demands=[{"job_id": "JOB-1", "requested": 1.23}],
            )
        )
        assert status == 200
        assert body["remaining"] == [{"batch_id": "DZ-01", "remaining": 0}]

    def test_boolean_and_string_quantities_rejected(self) -> None:
        errors = _loc_types(
            _base(
                batches=[_batch("DZ-01", True)],
                demands=[{"job_id": "JOB-1", "requested": "5"}],
            )
        )
        assert (("body", "batches", 0, "available"), "quantity_not_a_number") in errors
        assert (("body", "demands", 0, "requested"), "quantity_not_a_number") in errors

    def test_non_finite_quantity_rejected_in_place(self) -> None:
        # Infinity / NaN 必须在数量字段处拒绝为 422，不得在回显阶段 500
        for token in ("Infinity", "-Infinity", "NaN"):
            raw = (
                '{"calculated_at":"2026-09-15T02:00:00Z",'
                '"batches":[{"batch_id":"DZ-01","available":' + token + ","
                '"received_at":"2026-09-14T20:00:00Z",'
                '"expires_at":"2026-09-16T08:00:00Z"}],'
                '"demands":[]}'
            )
            resp = client.post(
                "/deicing-allocation",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 422, token
            assert (
                ("body", "batches", 0, "available"),
                "quantity_not_finite",
            ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}, token

    def test_overflow_quantity_rejected_as_out_of_range(self) -> None:
        # 1e999 经 Decimal 解析是有限值，但超出可表示范围，必须 422 而非 500
        raw = (
            '{"calculated_at":"2026-09-15T02:00:00Z",'
            '"batches":[{"batch_id":"DZ-01","available":1e999,'
            '"received_at":"2026-09-14T20:00:00Z",'
            '"expires_at":"2026-09-16T08:00:00Z"}],'
            '"demands":[]}'
        )
        resp = client.post(
            "/deicing-allocation",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert (
            ("body", "batches", 0, "available"),
            "quantity_out_of_range",
        ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}

    def test_blank_identifiers_rejected(self) -> None:
        errors = _loc_types(
            _base(
                batches=[_batch("   ", 10)],
                demands=[{"job_id": "  ", "requested": 1}],
            )
        )
        assert (("body", "batches", 0, "batch_id"), "blank_batch_id") in errors
        assert (("body", "demands", 0, "job_id"), "blank_job_id") in errors

    def test_extra_field_rejected(self) -> None:
        errors = _loc_types(_base(surprise=1))
        assert (("body", "surprise"), "extra_forbidden") in errors

    def test_duplicate_json_keys_rejected(self) -> None:
        raw = (
            '{"calculated_at":"2026-09-15T02:00:00Z",'
            '"calculated_at":"2026-09-15T03:00:00Z",'
            '"batches":[],"demands":[]}'
        )
        resp = client.post(
            "/deicing-allocation",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert (
            ("body", "calculated_at"),
            "duplicate_field",
        ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}

    def test_malformed_json_and_non_object_body_are_422(self) -> None:
        resp = client.post(
            "/deicing-allocation",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert tuple(resp.json()["detail"][0]["loc"]) == ("body",)

        resp = client.post("/deicing-allocation", json=[1, 2, 3])
        assert resp.status_code == 422
        assert ("body",) in {tuple(e["loc"]) for e in resp.json()["detail"]}

    def test_lone_surrogate_in_batch_id_rejected_in_place(self) -> None:
        raw = (
            '{"calculated_at":"2026-09-15T02:00:00Z",'
            '"batches":[{"batch_id":"DZ-\\uD800","available":1,'
            '"received_at":"2026-09-14T20:00:00Z",'
            '"expires_at":"2026-09-16T08:00:00Z"}],'
            '"demands":[]}'
        )
        resp = client.post(
            "/deicing-allocation",
            content=raw.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert (
            ("body", "batches", 0, "batch_id"),
            "unpaired_surrogate",
        ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}


class TestExistingSurfaceUnchanged:
    def test_health_still_ok(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_evaluate_and_snapshot_still_work(self) -> None:
        resp = client.post(
            "/evaluate",
            json={"runways": ["36L"], "work_windows": [], "occupancies": []},
        )
        assert resp.status_code == 200
        assert resp.json() == []

        resp = client.post(
            "/inspection-snapshot",
            json={
                "batch_id": "B1",
                "cutoff": "2026-09-15T03:00:00Z",
                "runways": ["36L"],
                "points": [{"runway": "36L", "code": "P1"}],
                "events": [],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["unchecked_count"] == 1
