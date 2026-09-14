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

    def test_occupancy_near_year_lower_bound_returns_clearance(self) -> None:
        # 占用从 0001 年第一分钟开始；扩展下溢时服务此前会 500。
        # 离场缓冲到 00:20，施工 [00:20, 00:40) 端点相接 → 放行。
        payload = {
            "runways": ["36L"],
            "work_windows": [
                {"runway": "36L", "start": "0001-01-01T00:20:00Z", "end": "0001-01-01T00:40:00Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "CA1", "start": "0001-01-01T00:00:00Z", "end": "0001-01-01T00:10:00Z"},
            ],
        }
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 200
        assert resp.json()[0]["conflicts"] == []

    def test_occupancy_near_year_lower_bound_conflict_serializes_four_digit_year(self) -> None:
        # 下界冲突交集的年份必须序列化为四位 "0001"，而非 "1"
        payload = {
            "runways": ["36L"],
            "work_windows": [
                {"runway": "36L", "start": "0001-01-01T00:19:59Z", "end": "0001-01-01T00:40:00Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "CA1", "start": "0001-01-01T00:00:00Z", "end": "0001-01-01T00:10:00Z"},
            ],
        }
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 200
        assert resp.json()[0]["conflicts"] == [
            {
                "flight_id": "CA1",
                "overlap_start": "0001-01-01T00:19:59Z",
                "overlap_end": "0001-01-01T00:20:00Z",
            }
        ]
        # 窗口自身回显也必须保持四位年份
        assert resp.json()[0]["start"] == "0001-01-01T00:19:59Z"

    def test_occupancy_near_year_upper_bound_returns_conflict(self) -> None:
        # 占用到 9999 年末；扩展上溢时服务此前会 500
        payload = {
            "runways": ["36L"],
            "work_windows": [
                {"runway": "36L", "start": "9999-12-31T23:50:00Z", "end": "9999-12-31T23:59:59Z"},
            ],
            "occupancies": [
                {"runway": "36L", "flight_id": "CA9", "start": "9999-12-31T23:40:00Z", "end": "9999-12-31T23:59:59Z"},
            ],
        }
        resp = client.post("/evaluate", json=payload)
        assert resp.status_code == 200
        assert resp.json()[0]["conflicts"] == [
            {
                "flight_id": "CA9",
                "overlap_start": "9999-12-31T23:50:00Z",
                "overlap_end": "9999-12-31T23:59:59Z",
            }
        ]


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

    def test_year_outside_representable_range_rejected(self) -> None:
        for bad_time in ("0000-01-01T00:00:00Z", "10000-01-01T00:00:00Z"):
            errors = _conflict_loc_types(
                {
                    "runways": ["36L"],
                    "work_windows": [
                        {"runway": "36L", "start": bad_time, "end": "2026-09-15T03:00:00Z"},
                    ],
                    "occupancies": [],
                }
            )
            assert (
                ("body", "work_windows", 0, "start"),
                "not_utc_z_seconds",
            ) in errors, bad_time

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


def _snapshot(payload: dict) -> tuple[int, object]:
    resp = client.post("/inspection-snapshot", json=payload)
    return resp.status_code, resp.json()


class TestInspectionSnapshotEndpoint:
    def _base(self, **overrides) -> dict:
        payload = {
            "batch_id": "NIGHT-20260914",
            "cutoff": "2026-09-15T03:00:00Z",
            "runways": ["36L", "18R"],
            "points": [
                {"runway": "36L", "code": "EDGE-A"},
                {"runway": "36L", "code": "MID-B"},
                {"runway": "18R", "code": "THR-C"},
            ],
            "events": [],
        }
        payload.update(overrides)
        return payload

    def test_point_without_events_is_unchecked(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
                ]
            )
        )
        assert status == 200
        assert body == {
            "batch_id": "NIGHT-20260914",
            "cutoff": "2026-09-15T03:00:00Z",
            "points": [
                {"runway": "36L", "point": "EDGE-A", "status": "normal",
                 "observed_at": "2026-09-15T02:00:00Z"},
                {"runway": "36L", "point": "MID-B", "status": "unchecked",
                 "observed_at": None},
                {"runway": "18R", "point": "THR-C", "status": "unchecked",
                 "observed_at": None},
            ],
            "unchecked_count": 2,
            "fault_count": 0,
        }

    def test_fault_then_repair_shows_normal(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T01:00:00Z", "kind": "fault"},
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:30:00Z", "kind": "repaired"},
                ]
            )
        )
        assert status == 200
        edge = body["points"][0]
        assert edge["status"] == "normal"
        assert edge["observed_at"] == "2026-09-15T02:30:00Z"
        assert body["unchecked_count"] == 2
        assert body["fault_count"] == 0

    def test_repair_after_cutoff_point_still_faulty(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
                    # 截止时间之后一秒的修复不参与快照
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T03:00:01Z", "kind": "repaired"},
                ]
            )
        )
        assert status == 200
        edge = body["points"][0]
        assert edge["status"] == "fault"
        assert edge["observed_at"] == "2026-09-15T02:00:00Z"
        assert body["fault_count"] == 1
        assert body["unchecked_count"] == 2

    def test_event_exactly_at_cutoff_is_folded(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T03:00:00Z", "kind": "repaired"},
                ]
            )
        )
        assert status == 200
        assert body["points"][0]["status"] == "normal"
        assert body["points"][0]["observed_at"] == "2026-09-15T03:00:00Z"

    def test_contradictory_same_second_events_are_field_level_422_with_no_snapshot(
        self,
    ) -> None:
        payload = self._base(
            events=[
                {"runway": "36L", "point": "EDGE-A",
                 "observed_at": "2026-09-15T02:10:00Z", "kind": "fault"},
                {"runway": "36L", "point": "EDGE-A",
                 "observed_at": "2026-09-15T02:10:00Z", "kind": "ok"},
            ]
        )
        status, body = _snapshot(payload)
        assert status == 422
        # 无部分快照：错误响应只含 detail
        assert set(body.keys()) == {"detail"}
        locs = {(tuple(e["loc"]), e["type"]) for e in body["detail"]}
        assert (
            ("body", "events", 0, "kind"),
            "contradictory_events",
        ) in locs
        assert (
            ("body", "events", 1, "kind"),
            "contradictory_events",
        ) in locs
        # 错误信息能定位到跑道、点位与发生秒
        msg = body["detail"][0]["msg"]
        assert "EDGE-A" in msg and "2026-09-15T02:10:00Z" in msg

    def test_contradiction_result_does_not_depend_on_input_order(self) -> None:
        events_a = [
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:10:00Z", "kind": "fault"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:10:00Z", "kind": "ok"},
        ]
        status_a, body_a = _snapshot(self._base(events=events_a))
        status_b, body_b = _snapshot(self._base(events=list(reversed(events_a))))
        assert (status_a, status_b) == (422, 422)
        locs_a = [tuple(e["loc"]) for e in body_a["detail"]]
        locs_b = [tuple(e["loc"]) for e in body_b["detail"]]
        # 错误始终按事件原始下标升序报告
        assert locs_a == locs_b == [
            ("body", "events", 0, "kind"),
            ("body", "events", 1, "kind"),
        ]

    def test_result_does_not_depend_on_event_input_order(self) -> None:
        events = [
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:40:00Z", "kind": "fault"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:10:00Z", "kind": "ok"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:50:00Z", "kind": "repaired"},
        ]
        status_a, body_a = _snapshot(self._base(events=events))
        status_b, body_b = _snapshot(self._base(events=list(reversed(events))))
        assert (status_a, status_b) == (200, 200)
        assert body_a == body_b
        assert body_a["points"][0] == {
            "runway": "36L", "point": "EDGE-A", "status": "normal",
            "observed_at": "2026-09-15T02:50:00Z",
        }

    def test_same_kind_same_second_is_allowed(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:10:00Z", "kind": "fault"},
                    {"runway": "36L", "point": "EDGE-A",
                     "observed_at": "2026-09-15T02:10:00Z", "kind": "fault"},
                ]
            )
        )
        assert status == 200
        assert body["points"][0]["status"] == "fault"
        assert body["fault_count"] == 1

    def test_points_follow_declared_order_and_runways_isolated(self) -> None:
        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "MID-B",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
                    # 18R 上相同点位代码与 36L 互不影响
                    {"runway": "18R", "point": "MID-B",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
                ]
            )
        )
        assert status == 422
        # 18R/MID-B 未声明为应查点位
        locs = {tuple(e["loc"]) for e in body["detail"]}
        assert ("body", "events", 1, "point") in locs

        status, body = _snapshot(
            self._base(
                events=[
                    {"runway": "36L", "point": "MID-B",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
                    {"runway": "18R", "point": "THR-C",
                     "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
                ]
            )
        )
        assert status == 200
        assert [p["point"] for p in body["points"]] == ["EDGE-A", "MID-B", "THR-C"]
        assert [p["status"] for p in body["points"]] == [
            "unchecked", "fault", "normal",
        ]
        assert body["unchecked_count"] == 1
        assert body["fault_count"] == 1

    def test_empty_events_marks_all_points_unchecked(self) -> None:
        status, body = _snapshot(self._base())
        assert status == 200
        assert all(p["status"] == "unchecked" for p in body["points"])
        assert body["unchecked_count"] == 3
        assert body["fault_count"] == 0


class TestInspectionSnapshotFieldErrors:
    def _base(self) -> dict:
        return {
            "batch_id": "B1",
            "cutoff": "2026-09-15T03:00:00Z",
            "runways": ["36L"],
            "points": [{"runway": "36L", "code": "P1"}],
            "events": [],
        }

    def _locs_types(self, payload: dict) -> set[tuple]:
        status, body = _snapshot(payload)
        assert status == 422
        return {(tuple(e["loc"]), e["type"]) for e in body["detail"]}

    def test_unknown_runway_in_point(self) -> None:
        payload = self._base()
        payload["points"] = [{"runway": "18R", "code": "P1"}]
        errors = self._locs_types(payload)
        assert (("body", "points", 0, "runway"), "unknown_runway") in errors

    def test_unknown_runway_in_event(self) -> None:
        payload = self._base()
        payload["events"] = [
            {"runway": "18R", "point": "P1",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
        ]
        errors = self._locs_types(payload)
        assert (("body", "events", 0, "runway"), "unknown_runway") in errors

    def test_undeclared_point_in_event_is_field_error(self) -> None:
        payload = self._base()
        payload["events"] = [
            {"runway": "36L", "point": "GHOST",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
        ]
        errors = self._locs_types(payload)
        assert (
            ("body", "events", 0, "point"),
            "unknown_inspection_point",
        ) in errors

    def test_duplicate_point_declaration_rejected(self) -> None:
        payload = self._base()
        payload["points"] = [
            {"runway": "36L", "code": "P1"},
            {"runway": "36L", "code": "P1"},
        ]
        errors = self._locs_types(payload)
        assert (
            ("body", "points", 1, "code"),
            "duplicate_inspection_point",
        ) in errors

    def test_invalid_event_kind_rejected(self) -> None:
        payload = self._base()
        payload["events"] = [
            {"runway": "36L", "point": "P1",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "broken"},
        ]
        errors = self._locs_types(payload)
        assert any(loc[-1] == "kind" for loc, _ in errors)

    def test_strict_utc_seconds_enforced(self) -> None:
        payload = self._base()
        payload["cutoff"] = "2026-09-15T03:00:00"
        payload["events"] = [
            {"runway": "36L", "point": "P1",
             "observed_at": "2026-09-15T02:00:00.5Z", "kind": "ok"},
        ]
        errors = self._locs_types(payload)
        assert (("body", "cutoff"), "not_utc_z_seconds") in errors
        assert (
            ("body", "events", 0, "observed_at"),
            "not_utc_z_seconds",
        ) in errors

    def test_blank_batch_id_rejected(self) -> None:
        payload = self._base()
        payload["batch_id"] = "   "
        errors = self._locs_types(payload)
        assert any(loc == ("body", "batch_id") for loc, _ in errors)

    def test_points_required_and_nonempty(self) -> None:
        payload = self._base()
        del payload["points"]
        errors = self._locs_types(payload)
        assert any(loc == ("body", "points") for loc, _ in errors)

        payload = self._base()
        payload["points"] = []
        errors = self._locs_types(payload)
        assert any(loc == ("body", "points") for loc, _ in errors)

    def test_extra_field_rejected(self) -> None:
        payload = self._base()
        payload["surprise"] = 1
        errors = self._locs_types(payload)
        assert (("body", "surprise"), "extra_forbidden") in errors

    def test_runway_rules_shared_with_evaluate(self) -> None:
        payload = self._base()
        payload["runways"] = ["36L", "36L"]
        errors = self._locs_types(payload)
        types = {t for (_, t) in errors}
        assert "duplicate_runway" in types

    def test_malformed_json_and_non_object_body_are_422(self) -> None:
        resp = client.post(
            "/inspection-snapshot",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 422
        assert tuple(resp.json()["detail"][0]["loc"]) == ("body",)

        resp = client.post("/inspection-snapshot", json=[1, 2, 3])
        assert resp.status_code == 422
        assert ("body",) in {tuple(e["loc"]) for e in resp.json()["detail"]}

    def test_unknown_runway_and_bad_time_aggregated_together(self) -> None:
        payload = {
            "batch_id": "B1",
            "cutoff": "2026-09-15T03:00:00Z",
            "runways": ["36L"],
            "points": [{"runway": "18R", "code": "P1"}],
            "events": [
                {"runway": "36L", "point": "P1",
                 "observed_at": "not-a-time", "kind": "ok"},
            ],
        }
        status, body = _snapshot(payload)
        assert status == 422
        locs = {tuple(e["loc"]) for e in body["detail"]}
        assert ("body", "points", 0, "runway") in locs
        assert ("body", "events", 0, "observed_at") in locs
