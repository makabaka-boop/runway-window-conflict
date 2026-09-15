"""无状态纯后端评估 API。

- ``GET  /health``               存活探针；
- ``POST /evaluate``             提交声明跑道、施工窗口、航班占用，逐窗口返回冲突结论；
- ``POST /inspection-snapshot``  提交巡检批次（应查点位 + 按发生时间记录的事件），
                                  按批次截止时间折叠出点位现状与未检查、故障数量；
- ``POST /deicing-allocation``   提交计算时刻、库存批次与按优先级排列的作业需求，
                                  返回逐项分配明细、批次剩余量与未参与的过期批次；
- ``POST /friction-assessment``  提交测量批次（批次编号、跑道、测量时刻与三个分段的
                                  测点读数），返回各分段中位数与全跑道总体等级。

服务端不保存任何请求间状态：响应完全由请求体决定，可复算、顺序稳定。
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from typing import Any, Callable, TypeVar

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from app.domain import (
    EVENT_KINDS,
    MIN_READINGS_PER_SEGMENT,
    SEGMENTS,
    BatchNotReceivedError,
    DeicingBatch,
    DeicingDemand,
    DuplicateIdentifierError,
    DuplicateReadingError,
    DuplicateSegmentError,
    FrictionReading,
    InspectionEvent,
    InspectionPoint,
    InsufficientInventoryError,
    InsufficientReadingsError,
    InvalidBatchPeriodError,
    Occupancy,
    SegmentReadings,
    SnapshotContradictionError,
    UnknownSegmentError,
    WorkWindow,
    allocate_deicing,
    assess_friction,
    build_inspection_snapshot,
    evaluate,
)
from app.schemas import (
    AllocationLineOut,
    BatchRemainingOut,
    DeicingAllocationOut,
    DeicingAllocationRequest,
    DemandAllocationOut,
    EvaluationRequest,
    ExpiredBatchOut,
    FrictionAssessmentOut,
    FrictionAssessmentRequest,
    InspectionSnapshotOut,
    InspectionSnapshotRequest,
    PointStatusOut,
    SegmentMedianOut,
    WorkWindowOut,
    _Z_SECONDS_RE,
    serialize_utc_z_seconds,
)

T = TypeVar("T", bound=BaseModel)

app = FastAPI(
    title="夜间跑道施工放行 / 灯光巡检 / 除冰液配给 / 摩擦评定 API",
    version="1.4.0",
    description=(
        "纯后端、无状态：航班占用两端各扩展十分钟后按半开区间判定冲突；"
        "巡检批次按截止时间把事件折叠为点位现状；"
        "除冰液按计算时刻剔除过期批次后先到期先用、逐项配给；"
        "摩擦批次按分段中位数的最低值评定全跑道等级。"
    ),
)


async def _parse_body(
    request: Request,
    *,
    parse_float: Callable[[str], Any] | None = None,
) -> object:
    """读取原始 JSON 请求体；语法错误走标准 422。

    使用 ``object_pairs_hook`` 把对象解析为 :class:`_RawObject`，从而保留
    同名键的全部出现——标准解析会静默采用最后一个值，让同一字段携带两个
    不同值（如两个 ``cutoff``）的请求含义不唯一。重复键由
    :func:`_duplicate_key_errors` 汇总成字段级错误。

    ``parse_float`` 供需要精确十进制数量的端点（除冰液配给）把 JSON 小数
    解析为 :class:`decimal.Decimal`；默认 ``None`` 保持标准 float 行为，
    既有端点的非有限浮点（Infinity / 1e999）判定不受影响。
    """

    raw = await request.body()
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_RawObject,
            parse_float=parse_float,
        )
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


class _RawObject(dict):
    """保留同名键全部出现的 JSON 对象（行为与普通 dict 一致，取最后一个值）。"""

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs


def _duplicate_key_errors(payload: object) -> list[dict]:
    """提取请求体内所有重复 JSON 键的字段级错误，loc 指向后一个重复键。

    嵌套对象（如 ``events[i]``）中的重复键同样拒绝；列表与普通对象
    都递归遍历，而 :class:`_RawObject` 以输入的键值对序列为准。
    """

    errors: list[dict] = []

    def walk(node: object, path: tuple[str | int, ...]) -> None:
        if isinstance(node, _RawObject):
            seen: set[str] = set()
            for key, value in node.pairs:
                if key in seen:
                    errors.append(
                        {
                            "type": "duplicate_field",
                            "loc": ("body", *path, key),
                            "msg": f"字段 {key!r} 在同一对象中重复出现，请求含义不唯一",
                            "input": key,
                        }
                    )
                seen.add(key)
                walk(value, (*path, key))
        elif isinstance(node, dict):
            for key, value in node.items():
                walk(value, (*path, key))
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, (*path, index))

    walk(payload, ())
    return errors


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

    # 重复 JSON 键在语义层对两个端点都非法，与具体模型无关，统一在此聚合。
    errors.extend(_duplicate_key_errors(payload))
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


def _deicing_duplicate_errors(payload: object) -> list[dict]:
    """除冰液配给的跨条目约束：批次编号、作业编号在请求内不得重复。

    与模型校验同样只在原始结构形态可识别时尽力提取（非字典条目、
    非字符串编号等让位给 Pydantic 的字段级错误）；编号先按服务端
    同样的规则去空白再比较，避免 ``" B1 "`` 与 ``"B1"`` 漏判。
    """

    if not isinstance(payload, dict):
        return []

    errors: list[dict] = []
    for field, id_key, error_type, label in (
        ("batches", "batch_id", "duplicate_batch_id", "批次编号"),
        ("demands", "job_id", "duplicate_job_id", "作业编号"),
    ):
        items = payload.get(field, [])
        if not isinstance(items, list):
            continue
        seen: dict[str, int] = {}
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            raw_id = item.get(id_key)
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            identifier = raw_id.strip()
            if identifier in seen:
                errors.append(
                    {
                        "type": error_type,
                        "loc": ("body", field, index, id_key),
                        "msg": (
                            f"{label} {identifier!r} 重复声明，"
                            f"首次出现于 {field}[{seen[identifier]}]"
                        ),
                        "input": raw_id,
                        "ctx": {id_key: identifier, "first_index": seen[identifier]},
                    }
                )
            else:
                seen[identifier] = index
    return errors


def _deicing_lifecycle_errors(payload: object) -> list[dict]:
    """除冰液批次生命周期约束：计算时刻尚未入库的批次必须整次拒绝。

    入库时间严格晚于计算时刻的库存属于未来库存，不能分配给当前作业
    （入库恰等于计算时刻视为已入库）。批次自身的入库/失效时段倒挂
    由模型字段校验器（``start_not_before_end``，定位到 ``expires_at``）
    负责；这里只补充需要同时读取 ``calculated_at`` 与各批次的跨对象
    约束，因此与其它字段级错误一起聚合返回，不产生任何部分配给。
    形态不合法（时间非严格 Z 秒级字符串、批次非对象等）时跳过，
    让位给 Pydantic 的字段级错误。
    """

    if not isinstance(payload, dict):
        return []

    raw_calculated_at = payload.get("calculated_at")
    if not isinstance(raw_calculated_at, str) or not _Z_SECONDS_RE.match(
        raw_calculated_at
    ):
        return []

    raw_batches = payload.get("batches")
    if not isinstance(raw_batches, list):
        return []

    errors: list[dict] = []
    for index, item in enumerate(raw_batches):
        if not isinstance(item, dict):
            continue
        raw_received_at = item.get("received_at")
        if not isinstance(raw_received_at, str) or not _Z_SECONDS_RE.match(
            raw_received_at
        ):
            continue
        raw_expires_at = item.get("expires_at")
        if isinstance(raw_expires_at, str) and _Z_SECONDS_RE.match(raw_expires_at):
            # 入库不严格早于失效的非法时段已由字段校验器定位到
            # expires_at 报告，按“先拒绝批次自身非法时段”的次序
            # 不再叠加相对计算时刻的入库状态错误。
            if raw_received_at >= raw_expires_at:
                continue
        # 固定宽度的 Z 秒级时间戳按字符串字典序与时间先后完全一致，
        # 无需解析即可安全比较；非法日历时间已被模型层拒绝。
        if raw_received_at > raw_calculated_at:
            raw_batch_id = item.get("batch_id")
            identifier = (
                raw_batch_id.strip()
                if isinstance(raw_batch_id, str) and raw_batch_id.strip()
                else raw_batch_id
            )
            errors.append(
                {
                    "type": "batch_not_received",
                    "loc": ("body", "batches", index, "received_at"),
                    "msg": (
                        f"批次 {identifier!r} 在计算时刻 "
                        f"{raw_calculated_at} 尚未入库（入库时间 "
                        f"{raw_received_at}），未来库存不得参与本次配给"
                    ),
                    "input": raw_received_at,
                    "ctx": {
                        "batch_id": identifier,
                        "received_at": raw_received_at,
                        "calculated_at": raw_calculated_at,
                    },
                }
            )
    return errors


def _deicing_validation_errors(payload: object) -> list[dict]:
    """除冰液配给的全部跨条目 / 跨对象原始结构校验。"""

    errors = _deicing_duplicate_errors(payload)
    errors.extend(_deicing_lifecycle_errors(payload))
    return errors


def _friction_assessment_errors(payload: object) -> list[dict]:
    """摩擦测量批次的跨条目约束：跑道已声明、分段齐备、读数编号唯一。

    与模型校验同样只在原始结构形态可识别时尽力提取（非字典条目、
    非字符串编号等让位给 Pydantic 的字段级错误）；编号先按服务端
    同样的规则去空白再比较，避免 ``" R-1 "`` 与 ``"R-1"`` 漏判。
    分段名非法的条目由 Pydantic 的 Literal 校验报告，这里只按合法
    分段名统计齐备性——因此非法分段对应的法定分段会同时报缺失，
    两个根因一次返回。
    """

    if not isinstance(payload, dict):
        return []

    errors: list[dict] = []

    declared = _declared_runways(payload)
    runway = payload.get("runway")
    if (
        declared is not None
        and isinstance(runway, str)
        and runway.strip()
        and runway.strip() not in declared
    ):
        errors.append(
            {
                "type": "unknown_runway",
                "loc": ("body", "runway"),
                "msg": f"跑道代码 {runway!r} 未在 runways 中声明",
                "input": runway,
                "ctx": {"runway": runway},
            }
        )

    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return errors

    counts: dict[str, int] = {}
    seen_segments: dict[str, int] = {}
    seen_readings: dict[str, tuple[int, int]] = {}
    for seg_index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            continue
        segment = item.get("segment")
        if isinstance(segment, str) and segment in SEGMENTS:
            if segment in seen_segments:
                errors.append(
                    {
                        "type": "duplicate_segment",
                        "loc": ("body", "segments", seg_index, "segment"),
                        "msg": (
                            f"分段 {segment!r} 重复提交，"
                            f"首次出现于 segments[{seen_segments[segment]}]"
                        ),
                        "input": segment,
                        "ctx": {
                            "segment": segment,
                            "first_index": seen_segments[segment],
                        },
                    }
                )
            else:
                seen_segments[segment] = seg_index
            raw_readings = item.get("readings")
            if isinstance(raw_readings, list):
                counts[segment] = counts.get(segment, 0) + len(raw_readings)
                for reading_index, reading in enumerate(raw_readings):
                    if not isinstance(reading, dict):
                        continue
                    raw_id = reading.get("reading_id")
                    if not isinstance(raw_id, str) or not raw_id.strip():
                        continue
                    identifier = raw_id.strip()
                    if identifier in seen_readings:
                        first_seg, first_reading = seen_readings[identifier]
                        errors.append(
                            {
                                "type": "duplicate_reading_id",
                                "loc": (
                                    "body",
                                    "segments",
                                    seg_index,
                                    "readings",
                                    reading_index,
                                    "reading_id",
                                ),
                                "msg": (
                                    f"读数编号 {identifier!r} 重复，首次出现于 "
                                    f"segments[{first_seg}].readings[{first_reading}]"
                                ),
                                "input": raw_id,
                                "ctx": {
                                    "reading_id": identifier,
                                    "first_segment_index": first_seg,
                                    "first_reading_index": first_reading,
                                },
                            }
                        )
                    else:
                        seen_readings[identifier] = (seg_index, reading_index)

    for segment in SEGMENTS:
        count = counts.get(segment, 0)
        if count >= MIN_READINGS_PER_SEGMENT or segment not in seen_segments:
            continue
        errors.append(
            {
                "type": "insufficient_readings",
                "loc": ("body", "segments", seen_segments[segment], "readings"),
                "msg": f"分段 {segment!r} 只有 {count} 条读数，少于三条",
                "input": count,
                "ctx": {
                    "segment": segment,
                    "count": count,
                    "min_readings": MIN_READINGS_PER_SEGMENT,
                },
            }
        )

    # 缺失的分段无法定位到具体条目，聚合为一条错误列出全部缺失分段，
    # 保证一次请求里的所有字段级错误同时返回（loc 相同会被去重折叠）。
    missing = [segment for segment in SEGMENTS if segment not in seen_segments]
    if missing:
        names = "、".join(repr(segment) for segment in missing)
        errors.append(
            {
                "type": "insufficient_readings",
                "loc": ("body", "segments"),
                "msg": f"分段 {names} 缺失，读数 0 条少于三条",
                "input": 0,
                "ctx": {
                    "missing_segments": missing,
                    "count": 0,
                    "min_readings": MIN_READINGS_PER_SEGMENT,
                },
            }
        )
    return errors


def _friction_domain_errors(
    exc: UnknownSegmentError
    | DuplicateSegmentError
    | InsufficientReadingsError
    | DuplicateReadingError,
) -> list[dict]:
    """领域层摩擦校验失败的兜底 422 条目。

    原始结构检查已先行拒绝全部同类问题，此处仅为兜底，正常不可达。
    """

    return [
        {
            "type": "invalid_friction_batch",
            "loc": ("body",),
            "msg": str(exc),
            "input": None,
        }
    ]


def _insufficient_inventory_errors(exc: InsufficientInventoryError) -> list[dict]:
    """把领域层的库存不足展开为指出总需求、有效库存与缺口的 422 条目。

    整次请求失败、不返回任何部分配给；三个关键数量同时写进 ``msg``
    （三位小数）与 ``ctx``（数值），便于人工阅读与程序处理。
    """

    return [
        {
            "type": "insufficient_inventory",
            "loc": ("body",),
            "msg": str(exc),
            "input": None,
            "ctx": {
                "total_demand": float(exc.total_demand),
                "effective_inventory": float(exc.effective_inventory),
                "shortfall": float(exc.shortfall),
            },
        }
    ]


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


def _json_safe(value: Any) -> Any:
    """把无法编进合法 JSON 的错误回显值替换为可序列化表示。

    最后一道防线：校验错误的 ``input`` / ``ctx`` 会原样回显请求值，其中
    可能携带 Infinity / NaN（非有限 float）或含孤立代理的字符串；直接交给
    ``JSONResponse``（``allow_nan=False``、UTF-8）会把本应 422 的请求
    渲染成 500。非有限浮点替换为名字符串，孤立代理转义为 ``\\uXXXX``。
    """

    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Decimal):
        # 除冰液配给以 parse_float=Decimal 解析请求体，校验错误的 input/ctx
        # 可能携带 Decimal；JSONResponse 无法直接序列化，按 float 同样的
        # 规则转换（非有限或 float 化溢出时替换为名字符串）。
        if value.is_nan():
            return "NaN"
        if value.is_infinite():
            return "Infinity" if value > 0 else "-Infinity"
        as_float = float(value)
        if math.isinf(as_float):
            return "Infinity" if as_float > 0 else "-Infinity"
        return as_float
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="backslashreplace").decode("utf-8")
        return value
    if isinstance(value, dict):
        return {_json_safe(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422, content=_json_safe({"detail": exc.errors()})
    )


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


@app.post("/deicing-allocation", response_model=DeicingAllocationOut)
async def deicing_allocation(request: Request) -> DeicingAllocationOut:
    """按计算时刻配给除冰液：过期批次剔除，有效批次先到期先用。

    请求体的小数按 ``Decimal`` 精确解析，数量统一按三位小数计算；
    任一编号重复、时间格式非法、批次入库晚于失效（时段非法）、
    批次在计算时刻尚未入库、数量非正或总可用量不足时整次 422，
    不返回部分配给；库存输入顺序不影响结果。
    """

    req = _validate_payload(
        await _parse_body(request, parse_float=Decimal),
        DeicingAllocationRequest,
        _deicing_validation_errors,
    )

    batches = [
        DeicingBatch(
            batch_id=b.batch_id,
            available=b.available,
            received_at=b.received_at,
            expires_at=b.expires_at,
        )
        for b in req.batches
    ]
    demands = [
        DeicingDemand(job_id=d.job_id, requested=d.requested)
        for d in req.demands
    ]

    try:
        report = allocate_deicing(req.calculated_at, batches, demands)
    except InsufficientInventoryError as exc:
        raise RequestValidationError(_insufficient_inventory_errors(exc))
    except (BatchNotReceivedError, InvalidBatchPeriodError) as exc:
        # 原始结构检查 / 字段校验器已先行拒绝全部同类问题，此处仅为兜底。
        raise RequestValidationError(
            [
                {
                    "type": "invalid_deicing_batch",
                    "loc": ("body",),
                    "msg": str(exc),
                    "input": None,
                }
            ]
        )
    except DuplicateIdentifierError as exc:
        # 原始结构检查已先行拒绝全部重复编号，此处仅为兜底，正常不可达。
        raise RequestValidationError(
            [
                {
                    "type": "duplicate_identifier",
                    "loc": ("body",),
                    "msg": str(exc),
                    "input": exc.identifier,
                }
            ]
        )

    return DeicingAllocationOut(
        calculated_at=report.calculated_at,
        allocations=[
            DemandAllocationOut(
                job_id=allocation.job_id,
                requested=allocation.requested,
                lines=[
                    AllocationLineOut(
                        batch_id=line.batch_id,
                        quantity=line.quantity,
                    )
                    for line in allocation.lines
                ],
            )
            for allocation in report.allocations
        ],
        remaining=[
            BatchRemainingOut(batch_id=r.batch_id, remaining=r.remaining)
            for r in report.remaining
        ],
        expired_batches=[
            ExpiredBatchOut(
                batch_id=e.batch_id,
                available=e.available,
                received_at=e.received_at,
                expires_at=e.expires_at,
            )
            for e in report.expired_batches
        ],
    )


@app.post("/friction-assessment", response_model=FrictionAssessmentOut)
async def friction_assessment(request: Request) -> FrictionAssessmentOut:
    """按批次评定跑道摩擦：分段中位数取最低值，按阈值划总体等级。

    请求体的小数按 ``Decimal`` 精确解析；任一分段缺失或少于三条读数、
    分段重复或非法、读数编号重复、系数超出 0 至 1 或超过三位小数时
    整批 422，不返回部分评定；读数输入顺序不影响结果。
    """

    req = _validate_payload(
        await _parse_body(request, parse_float=Decimal),
        FrictionAssessmentRequest,
        _friction_assessment_errors,
    )

    segments = [
        SegmentReadings(
            segment=segment.segment,
            readings=tuple(
                FrictionReading(
                    reading_id=reading.reading_id,
                    coefficient=reading.coefficient,
                )
                for reading in segment.readings
            ),
        )
        for segment in req.segments
    ]

    try:
        assessment = assess_friction(
            batch_id=req.batch_id,
            runway=req.runway,
            measured_at=req.measured_at,
            segments=segments,
        )
    except (
        UnknownSegmentError,
        DuplicateSegmentError,
        InsufficientReadingsError,
        DuplicateReadingError,
    ) as exc:
        # 原始结构检查已先行拒绝全部同类问题，此处仅为兜底，正常不可达。
        raise RequestValidationError(_friction_domain_errors(exc))

    return FrictionAssessmentOut(
        batch_id=assessment.batch_id,
        runway=assessment.runway,
        measured_at=assessment.measured_at,
        segments=[
            SegmentMedianOut(segment=median.segment, median=median.median)
            for median in assessment.segments
        ],
        overall_coefficient=assessment.overall_coefficient,
        overall_grade=assessment.overall_grade,
    )
