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

    # 5) 巡检快照 —— 场景一：缺少事件的点位计为未检查
    snap = {
        "batch_id": "NIGHT-20260914",
        "cutoff": "2026-09-15T03:00:00Z",
        "runways": ["36L", "18R"],
        "points": [
            {"runway": "36L", "code": "EDGE-A"},
            {"runway": "36L", "code": "MID-B"},
            {"runway": "18R", "code": "THR-C"},
        ],
        "events": [
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
        ],
    }
    status, body = request("POST", "/inspection-snapshot", snap)
    assert_equal(status, 200, "巡检快照场景一状态码")
    assert_equal(body["batch_id"], "NIGHT-20260914", "批次标识回显")
    assert_equal(body["cutoff"], "2026-09-15T03:00:00Z", "截止时间回显")
    assert_equal(
        [p["status"] for p in body["points"]],
        ["normal", "unchecked", "unchecked"],
        "缺少事件的点位计为未检查",
    )
    assert_equal(body["points"][1]["observed_at"], None, "未检查点位无事件时间")
    assert_equal(body["unchecked_count"], 2, "未检查数量")
    assert_equal(body["fault_count"], 0, "故障数量")

    # 6) 巡检快照 —— 场景二：故障后修复显示正常
    snap2 = {
        "batch_id": "NIGHT-20260914",
        "cutoff": "2026-09-15T03:00:00Z",
        "runways": ["36L"],
        "points": [{"runway": "36L", "code": "EDGE-A"}],
        "events": [
            # 故意乱序提交，验证结果与输入顺序无关
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:30:00Z", "kind": "repaired"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
        ],
    }
    status, body = request("POST", "/inspection-snapshot", snap2)
    assert_equal(status, 200, "巡检快照场景二状态码")
    assert_equal(
        body["points"][0],
        {"runway": "36L", "point": "EDGE-A", "status": "normal",
         "observed_at": "2026-09-15T02:30:00Z"},
        "故障后修复显示正常",
    )
    assert_equal(body["fault_count"], 0, "修复后故障数量归零")
    assert_equal(body["unchecked_count"], 0, "修复后无未检查点位")

    # 7) 巡检快照 —— 场景三：截止后修复不参与快照，仍显示故障
    snap3 = {
        "batch_id": "NIGHT-20260914",
        "cutoff": "2026-09-15T03:00:00Z",
        "runways": ["36L"],
        "points": [{"runway": "36L", "code": "EDGE-A"}],
        "events": [
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T03:00:01Z", "kind": "repaired"},
        ],
    }
    status, body = request("POST", "/inspection-snapshot", snap3)
    assert_equal(status, 200, "巡检快照场景三状态码")
    assert_equal(body["points"][0]["status"], "fault", "截止后修复仍显示故障")
    assert_equal(
        body["points"][0]["observed_at"],
        "2026-09-15T02:00:00Z",
        "现状时间取自截止前最后一条故障",
    )
    assert_equal(body["fault_count"], 1, "故障数量为 1")

    # 截止时间当秒的记录必须参与（边界为严格大于）
    snap3_ok = dict(snap3)
    snap3_ok["events"] = [
        {"runway": "36L", "point": "EDGE-A",
         "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
        {"runway": "36L", "point": "EDGE-A",
         "observed_at": "2026-09-15T03:00:00Z", "kind": "repaired"},
    ]
    status, body = request("POST", "/inspection-snapshot", snap3_ok)
    assert_equal(status, 200, "截止当秒事件请求状态码")
    assert_equal(body["points"][0]["status"], "normal", "截止当秒的修复生效")

    # 8) 巡检快照 —— 场景四：同秒矛盾事件整体 422，字段级定位且无部分快照
    snap4 = {
        "batch_id": "NIGHT-20260914",
        "cutoff": "2026-09-15T03:00:00Z",
        "runways": ["36L"],
        "points": [{"runway": "36L", "code": "EDGE-A"}],
        "events": [
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:10:00Z", "kind": "fault"},
            {"runway": "36L", "point": "EDGE-A",
             "observed_at": "2026-09-15T02:10:00Z", "kind": "ok"},
        ],
    }
    status, body = request("POST", "/inspection-snapshot", snap4)
    assert_equal(status, 422, "同秒矛盾事件必须 422")
    assert_equal(set(body.keys()), {"detail"}, "矛盾时无部分快照字段")
    locs = {tuple(e["loc"]) for e in body["detail"]}
    assert (
        "body", "events", 0, "kind"
    ) in locs, f"矛盾错误须定位到事件，实际 loc: {locs}"
    assert (
        "body", "events", 1, "kind"
    ) in locs, f"矛盾错误须定位到事件，实际 loc: {locs}"
    assert all(
        "EDGE-A" in e["msg"] and "2026-09-15T02:10:00Z" in e["msg"]
        for e in body["detail"]
    ), "错误信息须带出点位与发生秒"

    # 事件倒序提交时，错误仍按事件原始下标稳定报告
    snap4_rev = dict(snap4)
    snap4_rev["events"] = list(reversed(snap4["events"]))
    status, body_rev = request("POST", "/inspection-snapshot", snap4_rev)
    assert_equal(status, 422, "乱序矛盾事件同样 422")
    assert_equal(
        [tuple(e["loc"]) for e in body_rev["detail"]],
        [("body", "events", 0, "kind"), ("body", "events", 1, "kind")],
        "错误次序与事件输入顺序无关",
    )

    # 引用未声明点位也是字段级 422（引用关系约束）
    bad_ref = {
        "batch_id": "B1",
        "cutoff": "2026-09-15T03:00:00Z",
        "runways": ["36L"],
        "points": [{"runway": "36L", "code": "P1"}],
        "events": [
            {"runway": "36L", "point": "GHOST",
             "observed_at": "2026-09-15T02:00:00Z", "kind": "ok"},
        ],
    }
    status, body = request("POST", "/inspection-snapshot", bad_ref)
    assert_equal(status, 422, "未声明点位引用必须 422")
    assert (
        "body", "events", 0, "point"
    ) in {tuple(e["loc"]) for e in body["detail"]}, "未声明点位须定位到 point 字段"

    # 9) 非有限截止时间（Infinity / NaN）必须在 cutoff 字段处拒绝，不得 500
    raw_request = urllib.request.Request(
        BASE_URL + "/inspection-snapshot",
        data=(
            b'{"batch_id":"B1","cutoff":Infinity,"runways":["36L"],'
            b'"points":[{"runway":"36L","code":"P1"}],"events":[]}'
        ),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(raw_request, timeout=10) as resp:
            raise AssertionError(f"Infinity cutoff 必须 422，实际 {resp.status}")
    except urllib.error.HTTPError as exc:
        assert_equal(exc.code, 422, "Infinity cutoff 必须 422")
        body = json.loads(exc.read().decode("utf-8"))
        assert (
            tuple(body["detail"][0]["loc"]),
            body["detail"][0]["type"],
        ) == (
            ("body", "cutoff"),
            "not_finite_datetime",
        ), "非有限截止时间须定位到 cutoff 字段"

    # 10) 两个不同的 cutoff（重复 JSON 键）必须拒绝，不得静默采用后一个
    raw_request = urllib.request.Request(
        BASE_URL + "/inspection-snapshot",
        data=(
            b'{"batch_id":"B1",'
            b'"cutoff":"2026-09-15T03:00:00Z",'
            b'"cutoff":"2026-09-15T04:00:00Z",'
            b'"runways":["36L"],'
            b'"points":[{"runway":"36L","code":"P1"}],"events":[]}'
        ),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(raw_request, timeout=10) as resp:
            raise AssertionError(f"重复 cutoff 必须 422，实际 {resp.status}")
    except urllib.error.HTTPError as exc:
        assert_equal(exc.code, 422, "重复 cutoff 必须 422")
        body = json.loads(exc.read().decode("utf-8"))
        assert (
            tuple(body["detail"][0]["loc"]),
            body["detail"][0]["type"],
        ) == (
            ("body", "cutoff"),
            "duplicate_field",
        ), "重复截止时间须定位到 cutoff 字段"

    # 11) 批次标识含孤立代理字符（\uD800）必须在 batch_id 处拒绝，不得 500
    raw_request = urllib.request.Request(
        BASE_URL + "/inspection-snapshot",
        data=(
            b'{"batch_id":"BATCH-\\uD800-TAIL",'
            b'"cutoff":"2026-09-15T03:00:00Z","runways":["36L"],'
            b'"points":[{"runway":"36L","code":"P1"}],"events":[]}'
        ),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(raw_request, timeout=10) as resp:
            raise AssertionError(f"孤立代理 batch_id 必须 422，实际 {resp.status}")
    except urllib.error.HTTPError as exc:
        assert_equal(exc.code, 422, "孤立代理批次标识必须 422")
        body = json.loads(exc.read().decode("utf-8"))
        assert (
            tuple(body["detail"][0]["loc"]),
            body["detail"][0]["type"],
        ) == (
            ("body", "batch_id"),
            "unpaired_surrogate",
        ), "孤立代理批次标识须定位到 batch_id 字段"

    print("smoke_http: 全部断言通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - 验收脚本需把任何失败转为非零退出
        print(f"smoke_http: 失败 —— {exc}", file=sys.stderr)
        raise SystemExit(1)
