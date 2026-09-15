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

    # 12) 除冰液配给 —— 场景一：足量单批次覆盖全部需求
    deicing_single = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            {"batch_id": "DZ-01", "available": 120.5,
             "received_at": "2026-09-14T20:00:00Z", "expires_at": "2026-09-16T08:00:00Z"},
        ],
        "demands": [
            {"job_id": "JOB-1", "requested": 40.25},
            {"job_id": "JOB-2", "requested": 30},
        ],
    }
    status, body = request("POST", "/deicing-allocation", deicing_single)
    assert_equal(status, 200, "除冰配给场景一状态码")
    assert_equal(body["calculated_at"], "2026-09-15T02:00:00Z", "计算时刻回显")
    assert_equal(
        body["allocations"],
        [
            {"job_id": "JOB-1", "requested": 40.25,
             "lines": [{"batch_id": "DZ-01", "quantity": 40.25}]},
            {"job_id": "JOB-2", "requested": 30,
             "lines": [{"batch_id": "DZ-01", "quantity": 30}]},
        ],
        "足量单批次逐项明细",
    )
    assert_equal(
        body["remaining"],
        [{"batch_id": "DZ-01", "remaining": 50.25}],
        "足量单批次剩余量",
    )
    assert_equal(body["expired_batches"], [], "场景一无过期批次")

    # 13) 除冰液配给 —— 场景二：跨批次拆分，先失效先用，库存顺序不影响结果
    deicing_split = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            # 故意把后失效的批次放在前面：扣减顺序由失效时间决定
            {"batch_id": "DZ-02", "available": 60,
             "received_at": "2026-09-14T21:00:00Z", "expires_at": "2026-09-17T08:00:00Z"},
            {"batch_id": "DZ-01", "available": 25,
             "received_at": "2026-09-14T20:00:00Z", "expires_at": "2026-09-15T12:00:00Z"},
        ],
        "demands": [{"job_id": "JOB-9", "requested": 70}],
    }
    status, body = request("POST", "/deicing-allocation", deicing_split)
    assert_equal(status, 200, "除冰配给场景二状态码")
    assert_equal(
        body["allocations"][0]["lines"],
        [
            {"batch_id": "DZ-01", "quantity": 25},
            {"batch_id": "DZ-02", "quantity": 45},
        ],
        "跨批次拆分：先失效的 DZ-01 先扣减",
    )
    assert_equal(
        body["remaining"],
        [
            {"batch_id": "DZ-01", "remaining": 0},
            {"batch_id": "DZ-02", "remaining": 15},
        ],
        "跨批次拆分后的批次剩余量",
    )
    deicing_shuffled = dict(deicing_split)
    deicing_shuffled["batches"] = list(reversed(deicing_split["batches"]))
    status, body_shuffled = request("POST", "/deicing-allocation", deicing_shuffled)
    assert_equal(status, 200, "库存倒序提交状态码")
    assert_equal(body_shuffled, body, "库存输入顺序不影响配给结果")

    # 14) 除冰液配给 —— 场景三：过期批次排除（失效时间 <= 计算时刻即已失效）
    deicing_expired = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            # 恰在计算时刻失效：不得参与配给
            {"batch_id": "OLD-1", "available": 50,
             "received_at": "2026-09-13T08:00:00Z", "expires_at": "2026-09-15T02:00:00Z"},
            # 计算时刻之后一秒才失效：仍有效
            {"batch_id": "FR-1", "available": 30,
             "received_at": "2026-09-14T20:00:00Z", "expires_at": "2026-09-15T02:00:01Z"},
        ],
        "demands": [{"job_id": "JOB-1", "requested": 30}],
    }
    status, body = request("POST", "/deicing-allocation", deicing_expired)
    assert_equal(status, 200, "除冰配给场景三状态码")
    assert_equal(
        body["allocations"][0]["lines"],
        [{"batch_id": "FR-1", "quantity": 30}],
        "过期批次不参与扣减",
    )
    assert_equal(
        body["remaining"],
        [{"batch_id": "FR-1", "remaining": 0}],
        "剩余量只含有效批次",
    )
    assert_equal(
        body["expired_batches"],
        [
            {"batch_id": "OLD-1", "available": 50,
             "received_at": "2026-09-13T08:00:00Z",
             "expires_at": "2026-09-15T02:00:00Z"}
        ],
        "过期批次单独报告且数量原样保留",
    )

    # 15) 除冰液配给 —— 场景四：库存不足整次 422，指出总需求、有效库存与缺口
    deicing_short = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            {"batch_id": "DZ-01", "available": 60,
             "received_at": "2026-09-14T20:00:00Z", "expires_at": "2026-09-16T08:00:00Z"},
            # 过期批次的 50 不计入有效库存
            {"batch_id": "OLD-1", "available": 50,
             "received_at": "2026-09-13T08:00:00Z", "expires_at": "2026-09-14T08:00:00Z"},
        ],
        "demands": [
            {"job_id": "JOB-1", "requested": 70},
            {"job_id": "JOB-2", "requested": 30.5},
        ],
    }
    status, body = request("POST", "/deicing-allocation", deicing_short)
    assert_equal(status, 422, "库存不足必须 422")
    assert_equal(set(body.keys()), {"detail"}, "库存不足无部分配给")
    (error,) = body["detail"]
    assert_equal(error["type"], "insufficient_inventory", "缺货错误类型")
    assert_equal(error["ctx"]["total_demand"], 100.5, "缺货反馈总需求")
    assert_equal(error["ctx"]["effective_inventory"], 60, "缺货反馈有效库存（不含过期批次）")
    assert_equal(error["ctx"]["shortfall"], 40.5, "缺货反馈缺口")
    assert all(
        token in error["msg"] for token in ("100.500", "60.000", "40.500")
    ), "缺货错误信息须带出总需求、有效库存与缺口"

    # 15a) 除冰液配给 —— 批次生命周期：计算时刻尚未入库的批次整次 422
    deicing_future = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            {"batch_id": "FUTURE-1", "available": 100,
             "received_at": "2026-09-15T03:00:00Z", "expires_at": "2026-09-17T08:00:00Z"},
        ],
        "demands": [{"job_id": "JOB-1", "requested": 50}],
    }
    status, body = request("POST", "/deicing-allocation", deicing_future)
    assert_equal(status, 422, "尚未入库批次必须 422")
    assert_equal(set(body.keys()), {"detail"}, "尚未入库批次无部分配给")
    (error,) = body["detail"]
    assert_equal(error["type"], "batch_not_received", "尚未入库错误类型")
    assert_equal(
        tuple(error["loc"]),
        ("body", "batches", 0, "received_at"),
        "尚未入库错误定位到 received_at",
    )
    assert_equal(error["ctx"]["batch_id"], "FUTURE-1", "尚未入库错误带出批次编号")

    # 入库恰等于计算时刻视为已入库，可正常配给
    deicing_received_now = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            {"batch_id": "NOW-1", "available": 10,
             "received_at": "2026-09-15T02:00:00Z", "expires_at": "2026-09-17T08:00:00Z"},
        ],
        "demands": [{"job_id": "JOB-1", "requested": 4}],
    }
    status, body = request("POST", "/deicing-allocation", deicing_received_now)
    assert_equal(status, 200, "入库恰等于计算时刻应可配给")
    assert_equal(
        body["allocations"][0]["lines"],
        [{"batch_id": "NOW-1", "quantity": 4}],
        "临界入库批次正常扣减",
    )

    # 15b) 除冰液配给 —— 批次生命周期：入库晚于失效（倒挂时段）整次 422
    deicing_inverted = {
        "calculated_at": "2026-09-15T02:00:00Z",
        "batches": [
            {"batch_id": "BAD-1", "available": 100,
             "received_at": "2026-09-17T08:00:00Z", "expires_at": "2026-09-15T08:00:00Z"},
        ],
        "demands": [{"job_id": "JOB-1", "requested": 50}],
    }
    status, body = request("POST", "/deicing-allocation", deicing_inverted)
    assert_equal(status, 422, "入库晚于失效必须 422")
    assert_equal(
        {(tuple(e["loc"]), e["type"]) for e in body["detail"]},
        {(("body", "batches", 0, "expires_at"), "start_not_before_end")},
        "倒挂时段定位到 expires_at 且只报一个根因",
    )

    # 15c) 除冰液配给 —— 必填清单：省略 batches / demands 指出两个缺失清单
    status, body = request(
        "POST", "/deicing-allocation", {"calculated_at": "2026-09-15T02:00:00Z"}
    )
    assert_equal(status, 422, "省略两个必要清单必须 422")
    assert_equal(
        {(tuple(e["loc"]), e["type"]) for e in body["detail"]},
        {(("body", "batches"), "missing"), (("body", "demands"), "missing")},
        "一次指出 batches 与 demands 两个缺失清单",
    )

    # 显式空清单仍是合法的无库存 / 无需求配给
    status, body = request(
        "POST",
        "/deicing-allocation",
        {"calculated_at": "2026-09-15T02:00:00Z", "batches": [], "demands": []},
    )
    assert_equal(status, 200, "显式空清单应放行")
    assert_equal(
        body,
        {
            "calculated_at": "2026-09-15T02:00:00Z",
            "allocations": [],
            "remaining": [],
            "expired_batches": [],
        },
        "显式空清单返回完整空配给",
    )

    # 16) 摩擦评定 —— 场景一：正常路面，三段中位数都在良好线以上
    friction_good = {
        "batch_id": "FR-20260915-01",
        "runways": ["36L", "18R"],
        "runway": "36L",
        "measured_at": "2026-09-15T04:30:00Z",
        "segments": [
            {"segment": "touchdown", "readings": [
                {"reading_id": "TD-1", "coefficient": 0.512},
                {"reading_id": "TD-2", "coefficient": 0.508},
                {"reading_id": "TD-3", "coefficient": 0.515},
            ]},
            {"segment": "midpoint", "readings": [
                # 偶数条读数：中位数取中间两条均值 0.5115
                {"reading_id": "MP-1", "coefficient": 0.4},
                {"reading_id": "MP-2", "coefficient": 0.511},
                {"reading_id": "MP-3", "coefficient": 0.512},
                {"reading_id": "MP-4", "coefficient": 0.6},
            ]},
            {"segment": "rollout", "readings": [
                {"reading_id": "RO-1", "coefficient": 0.455},
                {"reading_id": "RO-2", "coefficient": 0.460},
                {"reading_id": "RO-3", "coefficient": 0.450},
            ]},
        ],
    }
    status, body = request("POST", "/friction-assessment", friction_good)
    assert_equal(status, 200, "摩擦评定场景一状态码")
    assert_equal(body["batch_id"], "FR-20260915-01", "批次编号回显")
    assert_equal(body["runway"], "36L", "跑道回显")
    assert_equal(body["measured_at"], "2026-09-15T04:30:00Z", "测量时刻回显")
    assert_equal(
        body["segments"],
        [
            {"segment": "touchdown", "median": 0.512},
            {"segment": "midpoint", "median": 0.5115},
            {"segment": "rollout", "median": 0.455},
        ],
        "分段中位数按着陆段、中段、滑跑段固定顺序返回",
    )
    assert_equal(body["overall_coefficient"], 0.455, "全跑道结论取三段最低值")
    assert_equal(body["overall_grade"], "good", "最低值不低于 0.400 为良好")

    # 17) 摩擦评定 —— 场景二：临界等级，阈值恰取到归较高等级，低千分之一即降级
    def friction_with_rollout(coefficients: list[float]) -> dict:
        payload = dict(friction_good)
        payload["segments"] = [
            friction_good["segments"][0],
            friction_good["segments"][1],
            {
                "segment": "rollout",
                "readings": [
                    {"reading_id": f"RO-{index}", "coefficient": coefficient}
                    for index, coefficient in enumerate(coefficients, start=1)
                ],
            },
        ]
        return payload

    boundary_cases = [
        ([0.400, 0.401, 0.399], 0.4, "good"),
        ([0.399, 0.400, 0.398], 0.399, "restricted"),
        ([0.250, 0.251, 0.249], 0.25, "restricted"),
        ([0.249, 0.250, 0.248], 0.249, "poor"),
    ]
    for rollout, expected_coefficient, expected_grade in boundary_cases:
        status, body = request(
            "POST", "/friction-assessment", friction_with_rollout(rollout)
        )
        assert_equal(status, 200, f"临界等级 {rollout} 状态码")
        assert_equal(
            body["overall_coefficient"], expected_coefficient, f"临界等级 {rollout} 最低值"
        )
        assert_equal(body["overall_grade"], expected_grade, f"临界等级 {rollout} 定级")

    # 18) 摩擦评定 —— 场景三：乱序稳定，分段与读数乱序提交响应逐字节一致
    friction_shuffled = dict(friction_good)
    friction_shuffled["segments"] = [
        {"segment": "rollout", "readings": [
            {"reading_id": "RO-3", "coefficient": 0.450},
            {"reading_id": "RO-1", "coefficient": 0.455},
            {"reading_id": "RO-2", "coefficient": 0.460},
        ]},
        {"segment": "midpoint", "readings": [
            {"reading_id": "MP-4", "coefficient": 0.6},
            {"reading_id": "MP-2", "coefficient": 0.511},
            {"reading_id": "MP-1", "coefficient": 0.4},
            {"reading_id": "MP-3", "coefficient": 0.512},
        ]},
        {"segment": "touchdown", "readings": [
            {"reading_id": "TD-3", "coefficient": 0.515},
            {"reading_id": "TD-1", "coefficient": 0.512},
            {"reading_id": "TD-2", "coefficient": 0.508},
        ]},
    ]
    status, body_ordered = request("POST", "/friction-assessment", friction_good)
    assert_equal(status, 200, "摩擦评定正序请求状态码")
    status, body_shuffled = request("POST", "/friction-assessment", friction_shuffled)
    assert_equal(status, 200, "摩擦评定乱序请求状态码")
    assert_equal(body_shuffled, body_ordered, "读数与分段输入顺序不影响评定结果")

    # 19) 摩擦评定 —— 场景四：整批拒绝，多重问题聚合 422 且无部分评定
    friction_bad = {
        "batch_id": "FR-20260915-02",
        "runways": ["36L"],
        "runway": "18L",  # 未声明跑道
        "measured_at": "2026-09-15T04:30:00Z",
        "segments": [
            {"segment": "touchdown", "readings": [
                # 只有两条读数，且第二条系数超出 0 至 1
                {"reading_id": "TD-1", "coefficient": 0.512},
                {"reading_id": "TD-2", "coefficient": 1.5},
            ]},
            {"segment": "sidewalk", "readings": [  # 非法分段
                {"reading_id": "X-1", "coefficient": 0.4},
                {"reading_id": "X-2", "coefficient": 0.4},
                {"reading_id": "X-3", "coefficient": 0.4},
            ]},
            {"segment": "rollout", "readings": [
                {"reading_id": "TD-1", "coefficient": 0.455},  # 编号与着陆段重复
                {"reading_id": "RO-2", "coefficient": 0.460},
                {"reading_id": "RO-3", "coefficient": 0.450},
            ]},
        ],
    }
    status, body = request("POST", "/friction-assessment", friction_bad)
    assert_equal(status, 422, "非法摩擦批次必须整批 422")
    assert_equal(set(body.keys()), {"detail"}, "整批拒绝时无部分评定字段")
    loc_types = {(tuple(e["loc"]), e["type"]) for e in body["detail"]}
    assert (
        ("body", "runway"), "unknown_runway"
    ) in loc_types, f"未声明跑道应为字段级错误，实际: {loc_types}"
    assert (
        ("body", "segments", 0, "readings"), "insufficient_readings"
    ) in loc_types, f"读数不足三条须定位到分段，实际: {loc_types}"
    assert (
        ("body", "segments", 0, "readings", 1, "coefficient"),
        "coefficient_out_of_range",
    ) in loc_types, f"系数超界须定位到读数，实际: {loc_types}"
    assert (
        ("body", "segments", 1, "segment"), "literal_error"
    ) in loc_types, f"非法分段须定位到分段名，实际: {loc_types}"
    assert (
        ("body", "segments", 2, "readings", 0, "reading_id"),
        "duplicate_reading_id",
    ) in loc_types, f"重复编号须定位到读数，实际: {loc_types}"
    # 非法分段导致中段没有任何合法读数，缺失分段聚合报告
    missing = [
        e
        for e in body["detail"]
        if e["type"] == "insufficient_readings" and e["loc"] == ["body", "segments"]
    ]
    assert missing and missing[0]["ctx"]["missing_segments"] == [
        "midpoint"
    ], f"缺失分段须聚合报告，实际: {body['detail']}"

    print("smoke_http: 全部断言通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - 验收脚本需把任何失败转为非零退出
        print(f"smoke_http: 失败 —— {exc}", file=sys.stderr)
        raise SystemExit(1)
