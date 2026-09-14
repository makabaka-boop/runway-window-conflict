"""无状态纯后端评估 API。

- ``GET  /health``               存活探针；
- ``POST /evaluate``             提交声明跑道、施工窗口、航班占用，逐窗口返回冲突结论；
- ``POST /inspection-snapshot``  提交巡检批次（应查点位 + 按发生时间记录的事件），
                                  按批次截止时间折叠出点位现状与未检查、故障数量。

服务端不保存任何请求间状态：响应完全由请求体决定，可复算、顺序稳定。
"""

from __future__ import annotations

import json
from typing import Callable, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.domain import (
    EVENT_KINDS,
    InspectionEvent,
    InspectionPoint,
    Occupancy,
    SnapshotContradictionError,
    WorkWindow,
    build_inspection_snapshot,
    evaluate,
)
from app.schemas import (
    EvaluationRequest,
    InspectionSnapshotOut,
    InspectionSnapshotRequest,
    PointStatusOut,
    WorkWindowOut,
    serialize_utc_z_seconds,
)

T = TypeVar("T", bound=BaseModel)

app = FastAPI(
    title="夜间跑道施工放行 / 灯光巡检 API",
    version="1.1.0",
    description=(
        "纯后端、无状态：航班占用两端各扩展十分钟后按半开区间判定冲突；"
        "巡检批次按截止时间把事件折叠为点位现状。"
    ),
)


async def _parse_body(request: Request) -> object:
    """读取原始 JSON 请求体；语法错误走标准 422。"""

    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise RequestValidationError(
            [
                {
                    "type": "json_invalid",
                    "loc": ["body"],
                    "msg": "请求体必须是合法的 JSON",
                    "input": None,
                }
            ]
        )


def _validate_payload(
    payload: object,
    model_cls: type[T],
    extra_errors: Callable[[object], list[dict]],
) -> T:
    """执行字段校验 + 跨字段引用校验，错误聚合为一次 422。

    模型校验失败时也尽力从未经校验的原始结构中提取引用关系错误，
    保证一次请求里的所有字段级错误同时返回，而非修一个再冒出下一个。
    """

    errors: list[dict] = []
    req: T | None = None
    try:
        req = model_cls.model_validate(payload)
    except ValidationError as exc:
        errors.extend(_prefix_loc(exc.errors(), "body"))

    errors.extend(extra_errors(payload))

    if errors:
        # 同一 loc 以模型校验错误为准去重；再按字段定位稳定排序，
        # 使引用关系等附加错误不依赖 set 迭代顺序。
        deduped: dict[tuple, dict] = {}
        for err in errors:
            deduped.setdefault(tuple(err["loc"]), err)
        raise RequestValidationError(
            sorted(deduped.values(), key=lambda e: _loc_key(e["loc"]))
        )
    assert req is not None
    return req


def _loc_key(loc: tuple | list) -> tuple:
    """把 loc 路径统一为可比较的字符串键（int 索引零填充，避免跨类型比较）。"""

    return tuple(
        (f"i:{item:08d}" if isinstance(item, int) else f"s:{item}") for item in loc
    )


def _declared_runways(payload: object) -> set[str] | None:
    """从原始结构提取已声明跑道集合；结构形态非法时返回 ``None`` 让位模型错误。"""

    if not isinstance(payload, dict):
        return None
    raw_runways = payload.get("runways")
    if not isinstance(raw_runways, list) or not all(
        isinstance(r, str) for r in raw_runways
    ):
        return None
    declared = {r.strip() for r in raw_runways if r.strip()}
    return declared


def _unknown_runway_errors(payload: object) -> list[dict]:
    """从未经校验的原始结构中提取“引用未声明跑道”的字段级错误。

    结构本身不合法（runways 非列表、元素非字符串等）时直接跳过：
    这类错误已由模型校验负责报告，这里只补充引用关系错误。
    """

    declared = _declared_runways(payload)
    if declared is None:
        return []

    errors: list[dict] = []
    for field in ("work_windows", "occupancies"):
        items = payload.get(field, [])
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            runway = item.get("runway")
            if isinstance(runway, str) and runway.strip() and runway.strip() not in declared:
                errors.append(
                    {
                        "type": "unknown_runway",
                        "loc": ("body", field, index, "runway"),
                        "msg": f"跑道代码 {runway!r} 未在 runways 中声明",
                        "input": runway,
                        "ctx": {"runway": runway},
                    }
                )
    return errors


def _inspection_reference_errors(payload: object) -> list[dict]:
    """巡检批次的跨字段引用错误：未声明跑道、未声明点位、重复点位。

    与模型校验同样只在原始结构形态可识别时尽力提取，其余错误让位
    给 Pydantic；跑道未声明的事件不再追加点位错误，避免同一字段
    堆叠多个根因。
    """

    declared = _declared_runways(payload)
    if declared is None or not isinstance(payload, dict):
        return []

    errors: list[dict] = []
    declared_points: set[tuple[str, str]] = set()
    seen_points: set[tuple[str, str]] = set()
    raw_points = payload.get("points")
    if isinstance(raw_points, list):
        for index, item in enumerate(raw_points):
            if not isinstance(item, dict):
                continue
            runway, code = item.get("runway"), item.get("code")
            if isinstance(runway, str) and isinstance(code, str):
                runway_s, code_s = runway.strip(), code.strip()
                if not runway_s or not code_s:
                    continue
                if runway_s not in declared:
                    errors.append(
                        {
                            "type": "unknown_runway",
                            "loc": ("body", "points", index, "runway"),
                            "msg": f"跑道代码 {runway!r} 未在 runways 中声明",
                            "input": runway,
                            "ctx": {"runway": runway},
                        }
                    )
                declared_points.add((runway_s, code_s))
                key = (runway_s, code_s)
                if key in seen_points:
                    errors.append(
                        {
                            "type": "duplicate_inspection_point",
                            "loc": ("body", "points", index, "code"),
                            "msg": (
                                f"点位 {code!r} 在跑道 {runway_s!r} 上重复声明"
                            ),
                            "input": code,
                            "ctx": {"runway": runway_s, "point": code_s},
                        }
                    )
                seen_points.add(key)

    raw_events = payload.get("events", [])
    if isinstance(raw_events, list):
        for index, item in enumerate(raw_events):
            if not isinstance(item, dict):
                continue
            runway, point = item.get("runway"), item.get("point")
            if not (isinstance(runway, str) and runway.strip()):
                continue
            runway_s = runway.strip()
            if runway_s not in declared:
                errors.append(
                    {
                        "type": "unknown_runway",
                        "loc": ("body", "events", index, "runway"),
                        "msg": f"跑道代码 {runway!r} 未在 runways 中声明",
                        "input": runway,
                        "ctx": {"runway": runway},
                    }
                )
                continue
            if isinstance(point, str) and point.strip():
                point_s = point.strip()
                if (runway_s, point_s) not in declared_points:
                    errors.append(
                        {
                            "type": "unknown_inspection_point",
                            "loc": ("body", "events", index, "point"),
                            "msg": (
                                f"点位 {point!r} 未在跑道 {runway_s!r} 的 "
                                "应查点位中声明"
                            ),
                            "input": point,
                            "ctx": {"runway": runway_s, "point": point_s},
                        }
                    )
    return errors


def _contradiction_errors(exc: SnapshotContradictionError) -> list[dict]:
    """把领域层的同秒矛盾事件展开为定位到具体事件的字段级 422 条目。

    每个矛盾组内的每条事件各生成一条错误，整体按事件原始下标排序，
    保证错误次序与事件输入顺序无关；不携带任何部分快照。
    """

    errors: list[dict] = []
    for contradiction in exc.contradictions:
        present = {e.kind for e in contradiction.events}
        kinds = "、".join(kind for kind in EVENT_KINDS if kind in present)
        for event in contradiction.events:
            errors.append(
                {
                    "type": "contradictory_events",
                    "loc": ("body", "events", event.index, "kind"),
                    "msg": (
                        "同一点位同一时刻的事件结论矛盾："
                        f"跑道 {contradiction.runway} 点位 {contradiction.point} "
                        f"{serialize_utc_z_seconds(contradiction.observed_at)} "
                        f"同时出现 {kinds}"
                    ),
                    "input": event.kind,
                    "ctx": {
                        "runway": contradiction.runway,
                        "point": contradiction.point,
                        "observed_at": serialize_utc_z_seconds(
                            contradiction.observed_at
                        ),
                    },
                }
            )
    errors.sort(key=lambda e: _loc_key(e["loc"]))
    return errors


def _prefix_loc(errors: list[dict], prefix: str) -> list[dict]:
    """给 Pydantic 错误的 loc 补 ``body`` 前缀，与 FastAPI 标准定位一致。"""

    fixed: list[dict] = []
    for err in errors:
        err = dict(err)
        err["loc"] = (prefix, *err.get("loc", ()))
        fixed.append(err)
    return fixed


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/evaluate", response_model=list[WorkWindowOut])
async def evaluate_windows(request: Request) -> list[WorkWindowOut]:
    """逐施工窗口返回冲突航班及交集起止；冲突按交集开始、航班标识升序。"""

    req = _validate_payload(
        await _parse_body(request), EvaluationRequest, _unknown_runway_errors
    )

    windows = [
        WorkWindow(runway=w.runway, start=w.start, end=w.end)
        for w in req.work_windows
    ]
    occupancies = [
        Occupancy(
            runway=o.runway,
            flight_id=o.flight_id,
            start=o.start,
            end=o.end,
        )
        for o in req.occupancies
    ]

    reports = evaluate(windows, occupancies)
    return [
        WorkWindowOut(
            runway=report.runway,
            start=report.start,
            end=report.end,
            conflicts=[
                {
                    "flight_id": c.flight_id,
                    "overlap_start": c.overlap_start,
                    "overlap_end": c.overlap_end,
                }
                for c in report.conflicts
            ],
        )
        for report in reports
    ]


@app.post("/inspection-snapshot", response_model=InspectionSnapshotOut)
async def inspection_snapshot(request: Request) -> InspectionSnapshotOut:
    """按批次截止时间折叠各点位最新事件，返回现状与未检查、故障数量。

    截止时间之后的记录不参与快照；同一点位同一时刻结论矛盾时整次
    请求 422 且无部分快照。
    """

    req = _validate_payload(
        await _parse_body(request),
        InspectionSnapshotRequest,
        _inspection_reference_errors,
    )

    points = [
        InspectionPoint(runway=p.runway, code=p.code) for p in req.points
    ]
    events = [
        InspectionEvent(
            runway=e.runway,
            point=e.point,
            observed_at=e.observed_at,
            kind=e.kind,
            index=index,
        )
        for index, e in enumerate(req.events)
    ]

    try:
        snapshot = build_inspection_snapshot(
            batch_id=req.batch_id,
            cutoff=req.cutoff,
            points=points,
            events=events,
        )
    except SnapshotContradictionError as exc:
        raise RequestValidationError(_contradiction_errors(exc))

    return InspectionSnapshotOut(
        batch_id=snapshot.batch_id,
        cutoff=snapshot.cutoff,
        points=[
            PointStatusOut(
                runway=p.runway,
                point=p.point,
                status=p.status,
                observed_at=p.observed_at,
            )
            for p in snapshot.points
        ],
        unchecked_count=snapshot.unchecked_count,
        fault_count=snapshot.fault_count,
    )
