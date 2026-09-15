"""摩擦批次评定 API 测试：四大验收场景、字段级错误、整批拒绝无部分评定。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

MEASURED = "2026-09-15T04:30:00Z"


def _reading(reading_id: str, coefficient: float) -> dict:
    return {"reading_id": reading_id, "coefficient": coefficient}


def _segment(name: str, prefix: str, coefficients: list[float]) -> dict:
    return {
        "segment": name,
        "readings": [
            _reading(f"{prefix}-{index}", coefficient)
            for index, coefficient in enumerate(coefficients, start=1)
        ],
    }


def _base(**overrides) -> dict:
    payload = {
        "batch_id": "FR-20260915-01",
        "runways": ["36L", "18R"],
        "runway": "36L",
        "measured_at": MEASURED,
        "segments": [
            _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
            _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
            _segment("rollout", "RO", [0.455, 0.460, 0.450]),
        ],
    }
    payload.update(overrides)
    return payload


def _assess(payload: dict) -> tuple[int, object]:
    resp = client.post("/friction-assessment", json=payload)
    return resp.status_code, resp.json()


def _loc_types(payload: dict) -> set[tuple]:
    status, body = _assess(payload)
    assert status == 422, body
    return {(tuple(e["loc"]), e["type"]) for e in body["detail"]}


class TestFrictionAssessmentScenarios:
    def test_normal_pavement_all_good(self) -> None:
        """场景一：正常路面——三段中位数都在良好线以上。"""
        status, body = _assess(_base())
        assert status == 200
        assert body == {
            "batch_id": "FR-20260915-01",
            "runway": "36L",
            "measured_at": MEASURED,
            "segments": [
                {"segment": "touchdown", "median": 0.512},
                {"segment": "midpoint", "median": 0.601},
                {"segment": "rollout", "median": 0.455},
            ],
            "overall_coefficient": 0.455,
            "overall_grade": "good",
        }

    def test_even_count_median_serializes_exactly(self) -> None:
        """偶数条读数的中位数（四位小数）精确序列化。"""
        status, body = _assess(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
                    _segment("midpoint", "MP", [0.4, 0.511, 0.512, 0.6]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert status == 200
        assert body["segments"][1] == {"segment": "midpoint", "median": 0.5115}
        assert body["overall_grade"] == "good"

    def test_boundary_grades(self) -> None:
        """场景二：临界等级——阈值恰取到归较高等级，低千分之一即降级。"""
        boundary_cases = [
            ([0.400, 0.401, 0.399], 0.4, "good"),
            ([0.399, 0.400, 0.398], 0.399, "restricted"),
            ([0.250, 0.251, 0.249], 0.25, "restricted"),
            ([0.249, 0.250, 0.248], 0.249, "poor"),
        ]
        for rollout, expected_coefficient, expected_grade in boundary_cases:
            status, body = _assess(
                _base(
                    segments=[
                        _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
                        _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                        _segment("rollout", "RO", rollout),
                    ]
                )
            )
            assert status == 200, rollout
            assert body["segments"][2]["median"] == expected_coefficient, rollout
            assert body["overall_coefficient"] == expected_coefficient, rollout
            assert body["overall_grade"] == expected_grade, rollout

    def test_shuffled_input_gives_identical_response(self) -> None:
        """场景三：乱序稳定——分段与读数乱序提交，响应逐字节一致。"""
        status, body = _assess(_base())
        assert status == 200
        shuffled = _base(
            segments=[
                _segment("rollout", "RO", [0.450, 0.460, 0.455]),
                _segment("midpoint", "MP", [0.605, 0.598, 0.601]),
                _segment("touchdown", "TD", [0.515, 0.512, 0.508]),
            ]
        )
        status2, body2 = _assess(shuffled)
        assert status2 == 200
        assert body2 == body

    def test_whole_batch_rejection_aggregates_field_errors(self) -> None:
        """场景四：整批拒绝——多重问题一次聚合，无部分评定。"""
        payload = _base(
            runway="18L",  # 未声明跑道
            segments=[
                # 着陆段只有两条读数，且第二条系数超出 0 至 1
                {
                    "segment": "touchdown",
                    "readings": [
                        _reading("TD-1", 0.512),
                        _reading("TD-2", 1.5),
                    ],
                },
                # 非法分段名
                {
                    "segment": "sidewalk",
                    "readings": [
                        _reading("X-1", 0.4),
                        _reading("X-2", 0.4),
                        _reading("X-3", 0.4),
                    ],
                },
                # 滑跑段读数编号与着陆段重复
                {
                    "segment": "rollout",
                    "readings": [
                        _reading("TD-1", 0.455),
                        _reading("RO-2", 0.460),
                        _reading("RO-3", 0.450),
                    ],
                },
            ],
        )
        status, body = _assess(payload)
        assert status == 422
        # 无部分评定：错误响应只含 detail
        assert set(body.keys()) == {"detail"}
        loc_types = {(tuple(e["loc"]), e["type"]) for e in body["detail"]}
        assert (("body", "runway"), "unknown_runway") in loc_types
        assert (
            ("body", "segments", 0, "readings"),
            "insufficient_readings",
        ) in loc_types
        assert (
            ("body", "segments", 0, "readings", 1, "coefficient"),
            "coefficient_out_of_range",
        ) in loc_types
        assert (("body", "segments", 1, "segment"), "literal_error") in loc_types
        assert (
            ("body", "segments", 2, "readings", 0, "reading_id"),
            "duplicate_reading_id",
        ) in loc_types
        # 非法分段导致中段没有任何合法读数，同时报缺失
        assert (("body", "segments"), "insufficient_readings") in loc_types
        missing = [
            e
            for e in body["detail"]
            if e["type"] == "insufficient_readings" and e["loc"] == ["body", "segments"]
        ]
        assert missing[0]["ctx"]["missing_segments"] == ["midpoint"]


class TestFrictionSegmentRules:
    def test_missing_segment_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                ]
            )
        )
        assert (("body", "segments"), "insufficient_readings") in errors

    def test_multiple_missing_segments_aggregated_in_one_error(self) -> None:
        # 多个缺失分段聚合在一条错误里一次返回，而非修一个再冒出下一个
        status, body = _assess(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
                ]
            )
        )
        assert status == 422
        (error,) = body["detail"]
        assert error["type"] == "insufficient_readings"
        assert error["loc"] == ["body", "segments"]
        assert error["ctx"]["missing_segments"] == ["midpoint", "rollout"]
        assert "midpoint" in error["msg"] and "rollout" in error["msg"]

    def test_segment_with_two_readings_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, 0.508]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (
            ("body", "segments", 0, "readings"),
            "insufficient_readings",
        ) in errors

    def test_empty_readings_list_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    {"segment": "touchdown", "readings": []},
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (
            ("body", "segments", 0, "readings"),
            "insufficient_readings",
        ) in errors

    def test_illegal_segment_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("threshold", "TD", [0.512, 0.508, 0.515]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (("body", "segments", 0, "segment"), "literal_error") in errors

    def test_duplicate_segment_is_field_error(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, 0.508, 0.515]),
                    _segment("touchdown", "TX", [0.502, 0.501, 0.503]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (("body", "segments", 1, "segment"), "duplicate_segment") in errors

    def test_duplicate_reading_id_after_strip_is_caught(self) -> None:
        # " TD-1 " 与 "TD-1" 去空白后相同，同样判重
        payload = _base()
        payload["segments"][1]["readings"][0]["reading_id"] = " TD-1 "
        errors = _loc_types(payload)
        assert (
            ("body", "segments", 1, "readings", 0, "reading_id"),
            "duplicate_reading_id",
        ) in errors


class TestFrictionFieldValidation:
    def test_coefficient_out_of_range_rejected(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.512, -0.1, 0.515]),
                    _segment("midpoint", "MP", [0.601, 1.5, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (
            ("body", "segments", 0, "readings", 1, "coefficient"),
            "coefficient_out_of_range",
        ) in errors
        assert (
            ("body", "segments", 1, "readings", 1, "coefficient"),
            "coefficient_out_of_range",
        ) in errors

    def test_coefficient_bounds_zero_and_one_accepted(self) -> None:
        status, body = _assess(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0, 0.0, 0.001]),
                    _segment("midpoint", "MP", [1, 1.0, 0.999]),
                    _segment("rollout", "RO", [0.5, 0.5, 0.5]),
                ]
            )
        )
        assert status == 200
        assert body["segments"][0]["median"] == 0
        assert body["segments"][1]["median"] == 1
        assert body["overall_grade"] == "poor"

    def test_coefficient_more_than_three_decimals_rejected(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.5121, 0.508, 0.515]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (
            ("body", "segments", 0, "readings", 0, "coefficient"),
            "coefficient_too_precise",
        ) in errors

    def test_coefficient_trailing_zeros_accepted(self) -> None:
        # 0.5100 数值上就是 0.51，不超精度
        status, body = _assess(
            _base(
                segments=[
                    _segment("touchdown", "TD", [0.5100, 0.508, 0.515]),
                    _segment("midpoint", "MP", [0.601, 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert status == 200
        assert body["segments"][0]["median"] == 0.51

    def test_boolean_and_string_coefficients_rejected(self) -> None:
        errors = _loc_types(
            _base(
                segments=[
                    _segment("touchdown", "TD", [True, 0.508, 0.515]),
                    _segment("midpoint", "MP", ["0.6", 0.598, 0.605]),
                    _segment("rollout", "RO", [0.455, 0.460, 0.450]),
                ]
            )
        )
        assert (
            ("body", "segments", 0, "readings", 0, "coefficient"),
            "coefficient_not_a_number",
        ) in errors
        assert (
            ("body", "segments", 1, "readings", 0, "coefficient"),
            "coefficient_not_a_number",
        ) in errors

    def test_non_finite_coefficient_rejected_in_place(self) -> None:
        # Infinity / NaN 必须在系数字段处拒绝为 422，不得在回显阶段 500
        for token in ("Infinity", "-Infinity", "NaN"):
            raw = (
                '{"batch_id":"FR-1","runways":["36L"],"runway":"36L",'
                '"measured_at":"2026-09-15T04:30:00Z",'
                '"segments":[{"segment":"touchdown","readings":['
                '{"reading_id":"T1","coefficient":' + token + "},"
                '{"reading_id":"T2","coefficient":0.5},'
                '{"reading_id":"T3","coefficient":0.5}]},'
                '{"segment":"midpoint","readings":['
                '{"reading_id":"M1","coefficient":0.5},'
                '{"reading_id":"M2","coefficient":0.5},'
                '{"reading_id":"M3","coefficient":0.5}]},'
                '{"segment":"rollout","readings":['
                '{"reading_id":"R1","coefficient":0.5},'
                '{"reading_id":"R2","coefficient":0.5},'
                '{"reading_id":"R3","coefficient":0.5}]}]}'
            )
            resp = client.post(
                "/friction-assessment",
                content=raw,
                headers={"Content-Type": "application/json"},
            )
            assert resp.status_code == 422, token
            assert (
                ("body", "segments", 0, "readings", 0, "coefficient"),
                "coefficient_not_finite",
            ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}, token

    def test_invalid_measured_at_rejected(self) -> None:
        errors = _loc_types(_base(measured_at="2026-09-15T04:30:00+00:00"))
        assert (("body", "measured_at"), "not_utc_z_seconds") in errors

    def test_unknown_runway_is_field_error(self) -> None:
        errors = _loc_types(_base(runway="18L"))
        assert (("body", "runway"), "unknown_runway") in errors

    def test_blank_identifiers_rejected(self) -> None:
        payload = _base(batch_id="  ", runway=" ")
        payload["segments"][0]["readings"][0]["reading_id"] = "   "
        errors = _loc_types(payload)
        assert (("body", "batch_id"), "blank_batch_id") in errors
        assert (("body", "runway"), "blank_runway") in errors
        assert (
            ("body", "segments", 0, "readings", 0, "reading_id"),
            "blank_reading_id",
        ) in errors

    def test_extra_field_rejected(self) -> None:
        errors = _loc_types(_base(surprise=1))
        assert (("body", "surprise"), "extra_forbidden") in errors

    def test_duplicate_json_keys_rejected(self) -> None:
        raw = (
            '{"batch_id":"FR-1","batch_id":"FR-2","runways":["36L"],'
            '"runway":"36L","measured_at":"2026-09-15T04:30:00Z",'
            '"segments":[]}'
        )
        resp = client.post(
            "/friction-assessment",
            content=raw,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert (
            ("body", "batch_id"),
            "duplicate_field",
        ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}

    def test_malformed_json_and_non_object_body_are_422(self) -> None:
        resp = client.post(
            "/friction-assessment",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert tuple(resp.json()["detail"][0]["loc"]) == ("body",)

        resp = client.post("/friction-assessment", json=[1, 2, 3])
        assert resp.status_code == 422
        assert ("body",) in {tuple(e["loc"]) for e in resp.json()["detail"]}

    def test_lone_surrogate_in_batch_id_rejected_in_place(self) -> None:
        raw = (
            '{"batch_id":"FR-\\uD800","runways":["36L"],"runway":"36L",'
            '"measured_at":"2026-09-15T04:30:00Z","segments":[]}'
        )
        resp = client.post(
            "/friction-assessment",
            content=raw.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert (
            ("body", "batch_id"),
            "unpaired_surrogate",
        ) in {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}


class TestExistingSurfaceUnchanged:
    def test_health_still_ok(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_existing_business_endpoints_still_work(self) -> None:
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

        resp = client.post(
            "/deicing-allocation",
            json={
                "calculated_at": "2026-09-15T02:00:00Z",
                "batches": [
                    {
                        "batch_id": "DZ-01",
                        "available": 10,
                        "received_at": "2026-09-14T20:00:00Z",
                        "expires_at": "2026-09-16T08:00:00Z",
                    }
                ],
                "demands": [{"job_id": "JOB-1", "requested": 5}],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["remaining"] == [
            {"batch_id": "DZ-01", "remaining": 5}
        ]
