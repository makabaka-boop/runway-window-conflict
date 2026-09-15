"""字段校验器与请求解析辅助的单测：非有限数值、孤立代理、重复 JSON 键。"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from app.main import _RawObject, _duplicate_key_errors, _json_safe
from app.schemas import (
    EvaluationRequest,
    InspectionSnapshotRequest,
    validate_utc_z_seconds,
)


def _loc_types(exc: ValidationError) -> set[tuple]:
    return {(tuple(e["loc"]), e["type"]) for e in exc.errors()}


class TestNonFiniteDatetime:
    def test_non_finite_float_rejected_explicitly(self) -> None:
        for value in (math.inf, -math.inf, math.nan):
            with pytest.raises(PydanticCustomError) as exc_info:
                validate_utc_z_seconds(value)
            assert exc_info.value.type == "not_finite_datetime", value

    def test_overflow_literal_1e999_parses_to_inf_and_is_rejected(self) -> None:
        import json

        value = json.loads("1e999")
        assert math.isinf(value)
        with pytest.raises(PydanticCustomError) as exc_info:
            validate_utc_z_seconds(value)
        assert exc_info.value.type == "not_finite_datetime"

    def test_finite_number_still_gets_generic_type_error(self) -> None:
        with pytest.raises(PydanticCustomError) as exc_info:
            validate_utc_z_seconds(123)
        assert exc_info.value.type == "not_utc_z_string"

    def test_valid_string_unchanged(self) -> None:
        dt = validate_utc_z_seconds("2026-09-15T03:00:00Z")
        assert dt.year == 2026


class TestUnpairedSurrogate:
    def _payload(self, **overrides) -> dict:
        payload = {
            "batch_id": "B1",
            "cutoff": "2026-09-15T03:00:00Z",
            "runways": ["36L"],
            "points": [{"runway": "36L", "code": "P1"}],
            "events": [],
        }
        payload.update(overrides)
        return payload

    def test_batch_id_with_lone_surrogate_rejected_at_field(self) -> None:
        payload = self._payload(batch_id="BATCH-\ud800-TAIL")
        with pytest.raises(ValidationError) as exc_info:
            InspectionSnapshotRequest.model_validate(payload)
        assert (
            ("batch_id",),
            "unpaired_surrogate",
        ) in _loc_types(exc_info.value)

    def test_point_code_with_lone_surrogate_rejected_in_place(self) -> None:
        payload = self._payload(
            points=[{"runway": "36L", "code": "P1\udc00"}],
        )
        with pytest.raises(ValidationError) as exc_info:
            InspectionSnapshotRequest.model_validate(payload)
        assert (
            ("points", 0, "code"),
            "unpaired_surrogate",
        ) in _loc_types(exc_info.value)

    def test_runway_declaration_with_lone_surrogate_rejected(self) -> None:
        payload = self._payload(runways=["36L\ud800"])
        with pytest.raises(ValidationError) as exc_info:
            InspectionSnapshotRequest.model_validate(payload)
        assert any(
            t == "unpaired_surrogate" for _, t in _loc_types(exc_info.value)
        )

    def test_flight_id_with_lone_surrogate_rejected_on_evaluate(self) -> None:
        payload = {
            "runways": ["36L"],
            "work_windows": [],
            "occupancies": [
                {
                    "runway": "36L",
                    "flight_id": "CA\ud800",
                    "start": "2026-09-15T02:00:00Z",
                    "end": "2026-09-15T03:00:00Z",
                }
            ],
        }
        with pytest.raises(ValidationError) as exc_info:
            EvaluationRequest.model_validate(payload)
        assert (
            ("occupancies", 0, "flight_id"),
            "unpaired_surrogate",
        ) in _loc_types(exc_info.value)

    def test_properly_paired_surrogate_pair_is_allowed(self) -> None:
        # 高代理 + 低代理组成合法增补平面字符（如 🕯），不得误伤
        payload = self._payload(batch_id="BATCH-\U0001F56F-END")
        req = InspectionSnapshotRequest.model_validate(payload)
        assert req.batch_id == "BATCH-\U0001F56F-END"


class TestDuplicateKeyErrors:
    def test_top_level_duplicate_points_at_second_occurrence(self) -> None:
        import json

        raw = (
            '{"cutoff":"2026-09-15T03:00:00Z",'
            '"cutoff":"2026-09-15T04:00:00Z"}'
        )
        payload = json.loads(raw, object_pairs_hook=_RawObject)
        errors = _duplicate_key_errors(payload)
        assert len(errors) == 1
        assert tuple(errors[0]["loc"]) == ("body", "cutoff")
        assert errors[0]["type"] == "duplicate_field"

    def test_nested_duplicate_inside_list_item(self) -> None:
        import json

        raw = '{"events":[{"point":"P1","point":"P2"}]}'
        payload = json.loads(raw, object_pairs_hook=_RawObject)
        errors = _duplicate_key_errors(payload)
        assert tuple(errors[0]["loc"]) == ("body", "events", 0, "point")

    def test_unique_keys_no_errors(self) -> None:
        payload = _RawObject([("a", 1), ("b", _RawObject([("c", 2)]))])
        assert _duplicate_key_errors(payload) == []


class TestJsonSafe:
    def test_non_finite_floats_become_name_strings(self) -> None:
        safe = _json_safe({"input": math.inf, "x": -math.inf, "y": math.nan})
        assert safe == {"input": "Infinity", "x": "-Infinity", "y": "NaN"}

    def test_lone_surrogate_escaped(self) -> None:
        safe = _json_safe({"input": "B-\ud800-T"})
        import json as _json

        encoded = _json.dumps(safe, ensure_ascii=False, allow_nan=False)
        assert "\\ud800" in encoded

    def test_normal_structure_passes_through(self) -> None:
        data = {"loc": ["body", 0, "x"], "n": 1, "ok": [1, "a", None, True]}
        assert _json_safe(data) == data
