"""API 层测试：字段级错误、整次失败无部分结果、排序与跨日响应序列化。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def _conflict_loc_types(payload: dict) -> set[tuple]:
    resp = client.post("/evaluate", json=payload)
    assert resp.status_code == 422
    return {(tuple(e["loc"]), e["type"]) for e in resp.json()["detail"]}


class TestEvaluateEndpoint:
    def test_touching_boundaries_and_cross_day_end_to_end(self) -> None:
        payload = {
            "runways": ["36L"],
            "work_windows": [
                # 左相接：结束 == 扩展开始
                {"runway": "36L", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T02:20:00Z"},
                # 右相接：开始 == 扩展结束
                {"runway": "36L", "start": "2026-09-15T02:50:00Z", "end": "2026-09-15T03:20:00Z"},
                # 跨日，侵入扩展区间一秒
                {"runway": "36L", "start": "2026-09-14T23:59:59Z", "end": "2026-09-15T00:20:01Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "CA1001", "start": "2026-09-15T02:30:00Z", "end": "2026-09-15T02:40:00Z"},
                {"runway": "36L", "flight_id": "MU2002", "start": "2026-09-15T00:30:00Z", "end": "2026-09-15T00:40:00Z"},
            ],
        }
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 200
        body = resp.json()

        assert body[0]["conflicts"] == []
        assert body[1]["conflicts"] == []
        assert body[2]["conflicts"] == [
            {
                "flight_id": "MU2002",
                "overlap_start": "2026-09-15T00:20:00Z",
                "overlap_end": "2026-09-15T00:20:01Z",
            }
        ]

    def test_different_runways_never_hit(self) -> None:
        payload = {
            "runways": ["36L", "18R"],
            "work_windows": [
                {"runway": "18R", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "CA1", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
            ],
        }
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 200
        assert resp.json()[0]["conflicts"] == []

    def test_sorting_by_overlap_start_then_flight_id(self) -> None:
        payload = {
            "runways": ["36L"],
            "work_windows": [
                {"runway": "36L", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "ZZ1", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T02:30:00Z"},
                {"runway": "36L", "flight_id": "AA2", "start": "2026-09-15T02:20:00Z", "end": "2026-09-15T02:30:00Z"},
            ],
        }
        body = client.post("/evaluate", json=payload).json()
        assert [c["flight_id"] for c in body[0]["conflicts"]] == ["ZZ1", "AA2"]
        assert body[0]["conflicts"][0]["overlap_start"] == "2026-09-15T02:00:00Z"

    def test_empty_windows_returns_empty_list(self) -> None:
        resp = client.post(
            "/evaluate",
            json={"runways": ["36L"], "work_windows": [], "occupancies": []},
        )
        assert resp.status_code == 200
        assert resp.json() == []


class TestFieldLevelErrors:
    def test_unknown_runway_is_field_error_with_no_partial_result(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "18R", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "runway"), "unknown_runway") in errors

    def test_unknown_runway_in_occupancy(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [],
                "occupancies": [
                    {"runway": "18R", "flight_id": "CA1", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                ],
            }
        )
        assert (("body", "occupancies", 0, "runway"), "unknown_runway") in errors

    def test_time_without_z_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T02:00:00", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "start"), "not_utc_z_seconds") in errors

    def test_offset_time_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T03:00:00+00:00", "end": "2026-09-15T03:30:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "start"), "not_utc_z_seconds") in errors

    def test_fractional_seconds_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T02:00:00.5Z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "start"), "not_utc_z_seconds") in errors

    def test_lowercase_z_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T02:00:00z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "start"), "not_utc_z_seconds") in errors

    def test_nonexistent_calendar_date_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-02-30T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "start"), "not_utc_z_seconds") in errors

    def test_start_equals_end_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T02:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "end"), "start_not_before_end") in errors

    def test_start_after_end_rejected(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "36L", "start": "2026-09-15T03:00:00Z", "end": "2026-09-15T02:00:00Z"},
                ],
                "occupancies": [],
            }
        )
        assert (("body", "work_windows", 0, "end"), "start_not_before_end") in errors

    def test_occupancy_order_validated_too(self) -> None:
        errors = _conflict_loc_types(
            {
                "runways": ["36L"],
                "work_windows": [],
                "occupancies": [
                    {"runway": "36L", "flight_id": "CA1", "start": "2026-09-15T03:00:00Z", "end": "2026-09-15T02:00:00Z"},
                ],
            }
        )
        assert (("body", "occupancies", 0, "end"), "start_not_before_end") in errors

    def test_blank_and_duplicate_runway_declarations(self) -> None:
        errors = _conflict_loc_types(
            {"runways": ["36L", "  ", "36L"], "work_windows": [], "occupancies": []}
        )
        types = {t for (_loc, t) in errors}
        assert "blank_runway" in types or "duplicate_runway" in types

    def test_extra_field_rejected(self) -> None:
        resp = client.post(
            "/evaluate",
            json={
                "runways": ["36L"],
                "work_windows": [],
                "occupancies": [],
                "surprise": 1,
            },
        )
        assert resp.status_code == 422
        assert tuple(resp.json()["detail"][0]["loc"]) == ("body", "surprise")

    def test_multiple_errors_aggregated_in_one_response(self) -> None:
        # 一次请求里同时有未知跑道和非法时间：错误必须聚合，且只有 422、无部分结果
        resp = client.post(
            "/evaluate",
            json={
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "18R", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                    {"runway": "36L", "start": "bad", "end": "2026-09-15T02:00:00Z"},
                ],
                "occupancies": [],
            },
        )
        assert resp.status_code == 422
        locs = {tuple(e["loc"]) for e in resp.json()["detail"]}
        assert ("body", "work_windows", 0, "runway") in locs
        assert ("body", "work_windows", 1, "start") in locs

    def test_health(self) -> None:
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_malformed_json_body_is_422(self) -> None:
        resp = client.post(
            "/evaluate",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert tuple(resp.json()["detail"][0]["loc"]) == ("body",)

    def test_non_object_body_is_422_with_no_partial_result(self) -> None:
        resp = client.post("/evaluate", json=[1, 2, 3])
        assert resp.status_code == 422
        locs = {tuple(e["loc"]) for e in resp.json()["detail"]}
        assert ("body",) in locs

    def test_duplicate_unknown_runway_loc_not_duplicated(self) -> None:
        # runways 结构非法时，未知跑道检查应让位给模型错误且不产生重复 loc
        resp = client.post(
            "/evaluate",
            json={
                "runways": "36L",
                "work_windows": [
                    {"runway": "18R", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            },
        )
        assert resp.status_code == 422
        locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
        assert len(locs) == len(set(locs))

    def test_multiple_unknown_runways_all_reported_in_stable_order(self) -> None:
        resp = client.post(
            "/evaluate",
            json={
                "runways": ["36L"],
                "work_windows": [
                    {"runway": "ZZ", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                    {"runway": "AA", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T03:00:00Z"},
                ],
                "occupancies": [],
            },
        )
        assert resp.status_code == 422
        locs = [tuple(e["loc"]) for e in resp.json()["detail"]]
        # 按窗口索引稳定排序，而非集合迭代顺序
        assert locs == [
            ("body", "work_windows", 0, "runway"),
            ("body", "work_windows", 1, "runway"),
        ]
