"""一次性 HTTP 冒烟验收：面向运行中的 API 发真实请求，零第三方依赖。

环境变量 ``API_BASE_URL`` 指向 API（Compose 中为 http://api:8000）。
退出码 0 表示全部断言通过。
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = os.environ.get("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def request(method: str, path: str, payload: object) -> tuple[int, object]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def assert_equal(actual: object, expected: object, label: str) -> None:
    if actual != expected:
        raise AssertionError(f"{label}: 期望 {expected!r}，实际 {actual!r}")


def main() -> int:
    # 1) 存活探针
    status, body = request("GET", "/health", None)
    assert_equal(status, 200, "GET /health 状态码")
    assert_equal(body, {"status": "ok"}, "GET /health 响应")

    # 2) 合法评估：包含端点相接（安全）、侵入一秒（冲突）、跨日窗口
    payload = {
        "runways": ["36L"],
        "work_windows": [
            # 02:20:00 结束 == 02:30:00 扩展占用开始 → 安全
            {"runway": "36L", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T02:20:00Z"},
            # 跨日：03:00:00 开始 == 02:50:00 扩展占用结束 → 安全
            {"runway": "36L", "start": "2026-09-15T03:00:00Z", "end": "2026-09-15T03:30:00Z"},
            # 侵入一秒：02:19:59 结束晚于 02:20:00 的扩展开始 → 冲突一秒
            {"runway": "36L", "start": "2026-09-15T02:10:00Z", "end": "2026-09-15T02:20:01Z"},
        ],
        "occupancies": [
            {"runway": "36L", "flight_id": "CA1001", "start": "2026-09-15T02:30:00Z", "end": "2026-09-15T02:40:00Z"},
        ],
    }
    status, body = request("POST", "/evaluate", payload)
    assert_equal(status, 200, "POST /evaluate 合法请求状态码")
    assert_equal(len(body), 3, "按窗口返回三条结论")
    assert_equal(body[0]["conflicts"], [], "施工结束恰等于扩展占用开始：安全")
    assert_equal(body[1]["conflicts"], [], "跨日相接：安全")
    assert_equal(
        body[2]["conflicts"],
        [
            {
                "flight_id": "CA1001",
                "overlap_start": "2026-09-15T02:20:00Z",
                "overlap_end": "2026-09-15T02:20:01Z",
            }
        ],
        "侵入一秒：冲突一秒",
    )

    # 3) 非法请求：未知跑道 + 无 Z 时间 + 倒挂区间，整体 422，无部分结果
    bad = {
        "runways": ["36L"],
        "work_windows": [
            {"runway": "18R", "start": "2026-09-15T02:00:00Z", "end": "2026-09-15T02:30:00Z"},
            {"runway": "36L", "start": "2026-09-15T03:00:00+00:00", "end": "2026-09-15T03:30:00Z"},
            {"runway": "36L", "start": "2026-09-15T03:00:00Z", "end": "2026-09-15T02:00:00Z"},
        ],
        "occupancies": [],
    }
    status, body = request("POST", "/evaluate", bad)
    assert_equal(status, 422, "非法请求必须整体 422")
    locs = {tuple(e["loc"]) for e in body["detail"]}
    assert (
        "body", "work_windows", 0, "runway"
    ) in locs, f"未知跑道应为字段级错误，实际 loc 集合: {locs}"
    assert (
        "body", "work_windows", 1, "start"
    ) in locs, f"无 Z 时间应为字段级错误，实际 loc 集合: {locs}"
    assert (
        "body", "work_windows", 2, "end"
    ) in locs, f"倒挂区间应为字段级错误，实际 loc 集合: {locs}"

    # 4) 年份上下界：占用扩展余量会溢出可表达 datetime 范围，
    #    服务必须钳制后照常返回结论（下界相接安全，上界窗落在缓冲内冲突）
    edge = {
        "runways": ["36L"],
        "work_windows": [
            {"runway": "36L", "start": "0001-01-01T00:20:00Z", "end": "0001-01-01T00:40:00Z"},
            {"runway": "36L", "start": "9999-12-31T23:50:00Z", "end": "9999-12-31T23:59:59Z"},
        ],
        "occupancies": [
            {"runway": "36L", "flight_id": "CA0001", "start": "0001-01-01T00:00:00Z", "end": "0001-01-01T00:10:00Z"},
            {"runway": "36L", "flight_id": "CA9999", "start": "9999-12-31T23:40:00Z", "end": "9999-12-31T23:59:59Z"},
        ],
    }
    status, body = request("POST", "/evaluate", edge)
    assert_equal(status, 200, "年份边界请求状态码")
    assert_equal(body[0]["conflicts"], [], "下界离场端相接：安全")
    assert_equal(
        body[1]["conflicts"],
        [
            {
                "flight_id": "CA9999",
                "overlap_start": "9999-12-31T23:50:00Z",
                "overlap_end": "9999-12-31T23:59:59Z",
            }
        ],
        "上界施工窗落在缓冲内：冲突",
    )

    print("smoke_http: 全部断言通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - 验收脚本需把任何失败转为非零退出
        print(f"smoke_http: 失败 —— {exc}", file=sys.stderr)
        raise SystemExit(1)
