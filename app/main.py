"""无状态纯后端评估 API。

- ``GET  /health``  存活探针；
- ``POST /evaluate`` 提交声明跑道、施工窗口、航班占用，逐窗口返回冲突结论。

服务端不保存任何请求间状态：响应完全由请求体决定，可复算、顺序稳定。
"""

from __future__ import annotations

import json

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.domain import Occupancy, WorkWindow, evaluate
from app.schemas import EvaluationRequest, WorkWindowOut

app = FastAPI(
    title="夜间跑道施工放行评估 API",
    version="1.0.0",
    description="纯后端、无状态：航班占用两端各扩展十分钟后按半开区间判定冲突。",
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


def _validate_request(payload: object) -> EvaluationRequest:
    """执行字段校验 + 未知跑道交叉校验，错误聚合为一次 422。

    模型校验失败时也尽力从未经校验的原始结构中提取未知跑道引用，
    保证一次请求里的所有字段级错误同时返回，而非修一个再冒出下一个。
    """

    errors: list[dict] = []
    req: EvaluationRequest | None = None
    try:
        req = EvaluationRequest.model_validate(payload)
    except ValidationError as exc:
        errors.extend(_prefix_loc(exc.errors(), "body"))

    errors.extend(_unknown_runway_errors(payload))

    if errors:
        # 同一 loc 以模型校验错误为准去重；再按字段定位稳定排序，
        # 使未知跑道等附加错误不依赖 set 迭代顺序。
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


def _unknown_runway_errors(payload: object) -> list[dict]:
    """从未经校验的原始结构中提取“引用未声明跑道”的字段级错误。

    结构本身不合法（runways 非列表、元素非字符串等）时直接跳过：
    这类错误已由模型校验负责报告，这里只补充引用关系错误。
    """

    if not isinstance(payload, dict):
        return []
    raw_runways = payload.get("runways")
    if not isinstance(raw_runways, list) or not all(
        isinstance(r, str) for r in raw_runways
    ):
        return []
    declared = {r.strip() for r in raw_runways if r.strip()}

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

    req = _validate_request(await _parse_body(request))

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
