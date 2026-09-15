# 夜间跑道施工放行评估 / 灯光巡检快照 / 除冰液配给 API

无状态纯后端服务：

- 一次 **`POST /evaluate`** 请求提交**已声明的跑道代码**、若干**施工窗口**与**航班占用区间**，
  服务逐施工窗口返回与之冲突的航班及交集起止；
- 一次 **`POST /inspection-snapshot`** 请求提交**巡检批次**（应查点位 + 按发生时间记录的
  正常 / 故障 / 已修复事件），服务按批次截止时间把事件折叠为各点位现状；
- 一次 **`POST /deicing-allocation`** 请求提交**计算时刻**、**除冰液库存批次**与
  **按优先级排列的作业需求**，服务返回逐项分配明细、批次剩余量与未参与的过期批次。

响应完全由请求体决定，可复算、顺序稳定，不依赖任何数据库或请求间状态。

- 语言/运行时：Python 3.12
- Web 层：FastAPI + Uvicorn
- 校验：Pydantic v2（字段级 422 错误）
- 领域逻辑：`app/domain.py`，仅依赖标准库，可独立单测

## 判定规则

1. **十分钟安全余量**：每段航班占用区间在两端各扩展 10 分钟
   （进场前 10 分钟、离场后 10 分钟），得到扩展占用 `[start-10m, end+10m)`。
2. **半开区间比较**：施工窗口 `[start, end)` 仅与**同跑道**的扩展占用比较。
3. **仅正长交集算冲突**：交集长度严格大于零才计冲突。
   - 施工结束 **恰等于** 扩展占用开始 → **安全**（端点相接不算冲突）；
   - 施工开始 **恰等于** 扩展占用结束 → **安全**；
   - 侵入哪怕 1 秒也算冲突，交集为真实重叠的 `[max, min)`。
4. **跑道隔离**：不同跑道永不互相命中；窗口/占用引用的跑道必须在 `runways` 中声明。
5. **结果顺序**：按施工窗口在请求中的顺序逐窗返回；同一窗口内冲突先按
   **交集开始时间**、再按**航班标识**（字符串升序）排列。
6. 时间只接受带 `Z` 的 ISO 8601 UTC **秒级**值，例如 `2026-09-15T02:30:00Z`；
   不接受时区偏移（`+00:00`）、小数秒（`.500Z`）、小写 `z` 或不存在的日历时间。
7. 每段区间的 `start` 必须严格早于 `end`。
8. **整体校验**：未知跑道、无 `Z` 时间、非法区间等都让整次评估返回 HTTP 422 字段级错误，
   不产生任何部分结果；同一次请求的多个错误会聚合在一个响应中。

> 跨日区间是普通的日历时间比较：`2026-09-14T23:50:00Z` 到 `2026-09-15T00:25:00Z`
> 天然支持，端点相接规则不变。
>
> 合法的年份范围为 `0001`–`9999`。当占用时间贴近年份上下界（如 `0001-01-01T00:00:00Z`
> 或 `9999-12-31T23:59:59Z`）、十分钟余量会溢出可表达范围时，超出的余量端点被钳制到
> 可表达边界，评估照常返回放行结论而不是报错；超出 `0001`–`9999` 的时间仍按非法时间返回 422。

## 请求示例

`POST /evaluate`

```json
{
  "runways": ["36L", "18R"],
  "work_windows": [
    {"runway": "36L", "start": "2026-09-15T01:40:00Z", "end": "2026-09-15T02:20:00Z"},
    {"runway": "36L", "start": "2026-09-15T02:25:00Z", "end": "2026-09-15T02:45:00Z"},
    {"runway": "36L", "start": "2026-09-15T02:50:00Z", "end": "2026-09-15T02:55:00Z"},
    {"runway": "36L", "start": "2026-09-15T02:45:00Z", "end": "2026-09-15T03:10:00Z"},
    {"runway": "18R", "start": "2026-09-15T02:25:00Z", "end": "2026-09-15T03:10:00Z"}
  ],
  "occupancies": [
    {"runway": "36L", "flight_id": "CA1831", "start": "2026-09-15T02:30:00Z", "end": "2026-09-15T02:40:00Z"},
    {"runway": "36L", "flight_id": "MU5102", "start": "2026-09-15T03:05:00Z", "end": "2026-09-15T03:15:00Z"}
  ]
}
```

手工复算（扩展十分钟）：

- CA1831 扩展占用 `[02:20, 02:50)`；MU5102 扩展占用 `[02:55, 03:25)`。
- 窗口 1 结束于 `02:20`，恰等于 CA1831 扩展开始 → 安全。
- 窗口 2 `[02:25, 02:45)` 与 CA1831 整段相交 → 冲突。
- 窗口 3 `[02:50, 02:55)`：左端接 CA1831 扩展结束、右端接 MU5102 扩展开始 → 安全。
- 窗口 4 同时撞两段扩展占用，冲突按交集开始排序：CA1831（02:45）先于 MU5102（02:55）。
- 窗口 5 在 18R，36L 的航班永不命中 → 安全。

响应（`200 OK`，以下为上述请求的真实输出，由算法实时计算而非固定模板）：

```json
[
  {"runway": "36L", "start": "2026-09-15T01:40:00Z", "end": "2026-09-15T02:20:00Z", "conflicts": []},
  {"runway": "36L", "start": "2026-09-15T02:25:00Z", "end": "2026-09-15T02:45:00Z",
   "conflicts": [
     {"flight_id": "CA1831", "overlap_start": "2026-09-15T02:25:00Z", "overlap_end": "2026-09-15T02:45:00Z"}
   ]},
  {"runway": "36L", "start": "2026-09-15T02:50:00Z", "end": "2026-09-15T02:55:00Z", "conflicts": []},
  {"runway": "36L", "start": "2026-09-15T02:45:00Z", "end": "2026-09-15T03:10:00Z",
   "conflicts": [
     {"flight_id": "CA1831", "overlap_start": "2026-09-15T02:45:00Z", "overlap_end": "2026-09-15T02:50:00Z"},
     {"flight_id": "MU5102", "overlap_start": "2026-09-15T02:55:00Z", "overlap_end": "2026-09-15T03:10:00Z"}
   ]},
  {"runway": "18R", "start": "2026-09-15T02:25:00Z", "end": "2026-09-15T03:10:00Z", "conflicts": []}
]
```

`conflicts` 为空即该窗口对所有同跑道航班满足十分钟余量，可放行。

## 错误示例

未知跑道 + 无 `Z` 时间 + 倒挂区间会一次性聚合返回（HTTP 422），每条错误定位到字段：

```json
{
  "detail": [
    {"type": "unknown_runway", "loc": ["body", "work_windows", 0, "runway"], "msg": "跑道代码 '18R' 未在 runways 中声明", "input": "18R"},
    {"type": "not_utc_z_seconds", "loc": ["body", "work_windows", 1, "start"], "msg": "时间必须是带 Z 的秒级 ISO 8601 UTC 值（结尾为 Z，无小数秒），收到 '2026-09-15T03:00:00+00:00'"},
    {"type": "start_not_before_end", "loc": ["body", "work_windows", 2, "end"], "msg": "开始时间必须严格早于结束时间"}
  ]
}
```

请求体非法时绝不返回任何窗口结论（无部分结果），多余字段也会被拒绝。

## 跑道灯光巡检快照

`POST /inspection-snapshot` 供夜班交接确认跑道灯光巡检是否留有未闭合缺陷：
客户端提交巡检批次、每条跑道应查点位及按发生时间记录的 `ok`（正常）/
`fault`（故障）/ `repaired`（已修复）事件，服务按批次**截止时间**折叠各点位的最新事件。

折叠规则：

1. **截止过滤**：`observed_at` 严格晚于 `cutoff` 的记录不参与快照
   （截止当秒的记录参与；截止后的修复不能消除此前的故障）；
2. **最新事件取胜**：同一点位（按 `runway` + `point` 区分）取截止前最后一条事件，
   事件输入顺序不影响结果；`ok` 与 `repaired` 都折叠为现状 `normal`，
   `fault` 折叠为 `fault`；
3. **未检查**：截止前没有任何事件的点位现状为 `unchecked`，
   `observed_at` 为 `null`；
4. **同秒矛盾**：同一点位同一秒出现不同结论（如同时记录 `fault` 与 `ok`）
   时整次请求返回 HTTP 422，错误定位到每一条矛盾事件的 `events[i].kind`，
   不返回任何部分快照；同一秒结论相同不算矛盾；
5. **引用关系**：点位与事件引用的跑道必须在 `runways` 中声明，
   事件引用的点位必须在本批次 `points` 中声明（同跑道内匹配，跨跑道同名点位互不影响），
   同跑道内点位不可重复声明；
6. 结果中 `points` 严格按请求中的声明顺序返回，并汇总 `unchecked_count`、
   `fault_count`；时间沿用严格的带 `Z` UTC 秒级格式。
7. **唯一截止时间**：请求体每个 JSON 对象的字段名只能出现一次；携带两个
   `cutoff`（即使值相同）等同义不明的批次整体拒绝为 422，错误定位到重复字段，
   不会静默采用后一个值。嵌套对象（如 `events[i]`）中的重复键同样拒绝。
8. **有限且可回显的字段值**：`cutoff` / 时间字段写成 `Infinity`、`-Infinity`、
   `NaN`（含溢出为非有限浮点的 `1e999`）时在该字段处返回 422；批次标识、
   跑道代码、点位、航班标识等文本字段不接受孤立代理字符（如 `\uD800`），
   含孤立代理时在对应字段处明确拒绝，而不是在响应序列化阶段报内部错误。

请求示例：

```json
{
  "batch_id": "NIGHT-20260914",
  "cutoff": "2026-09-15T03:00:00Z",
  "runways": ["36L", "18R"],
  "points": [
    {"runway": "36L", "code": "EDGE-A"},
    {"runway": "36L", "code": "MID-B"},
    {"runway": "18R", "code": "THR-C"}
  ],
  "events": [
    {"runway": "36L", "point": "EDGE-A", "observed_at": "2026-09-15T02:00:00Z", "kind": "fault"},
    {"runway": "36L", "point": "EDGE-A", "observed_at": "2026-09-15T02:30:00Z", "kind": "repaired"},
    {"runway": "36L", "point": "MID-B", "observed_at": "2026-09-15T02:40:00Z", "kind": "fault"},
    {"runway": "18R", "point": "THR-C", "observed_at": "2026-09-15T03:05:00Z", "kind": "repaired"}
  ]
}
```

响应（`200 OK`）：EDGE-A 故障后修复为正常；MID-B 仍故障；
THR-C 的修复发生在截止之后、不参与快照，计为未检查。

```json
{
  "batch_id": "NIGHT-20260914",
  "cutoff": "2026-09-15T03:00:00Z",
  "points": [
    {"runway": "36L", "point": "EDGE-A", "status": "normal", "observed_at": "2026-09-15T02:30:00Z"},
    {"runway": "36L", "point": "MID-B", "status": "fault", "observed_at": "2026-09-15T02:40:00Z"},
    {"runway": "18R", "point": "THR-C", "status": "unchecked", "observed_at": null}
  ],
  "unchecked_count": 1,
  "fault_count": 1
}
```

同秒矛盾事件返回字段级 422（无部分快照，错误次序与事件输入顺序无关）：

```json
{
  "detail": [
    {"type": "contradictory_events", "loc": ["body", "events", 0, "kind"],
     "msg": "同一点位同一时刻的事件结论矛盾：跑道 36L 点位 EDGE-A 2026-09-15T02:10:00Z 同时出现 ok、fault",
     "input": "fault", "ctx": {"runway": "36L", "point": "EDGE-A", "observed_at": "2026-09-15T02:10:00Z"}},
    {"type": "contradictory_events", "loc": ["body", "events", 1, "kind"], "...": "..."}
  ]
}
```

## 除冰液配给

`POST /deicing-allocation` 供寒潮期间地勤在作业前核算除冰液批次能否覆盖各机位需求：
客户端提交**计算时刻**、**库存批次**（编号、可用量、入库时间、失效时间）与
**按优先级排列的作业需求**（作业编号、申请量），服务整次配给并返回逐项分配明细、
批次剩余量与未参与的过期批次。

配给规则：

1. **过期剔除**：失效时间 **小于等于** 计算时刻的批次已失效，不参与扣减，
   原样列入响应的 `expired_batches`（失效时间恰等于计算时刻也算已失效）；
2. **先到期先用**：有效批次按（失效时间、入库时间、批次编号）稳定排序后逐笔扣减，
   避免先到批次久置过期；**库存输入顺序不影响结果**；
3. **优先级扣减**：需求按请求中的顺序逐笔满足，每项需求可拆分到多个批次，
   拆分明细按扣减顺序列入 `lines`；
4. **三位小数精确计算**：数量以十进制 `Decimal` 精确运算，入参最多三位小数
   （`1.2300` 这类末尾带零的写法按数值本身判定，不超精度）；
5. **整次失败**：批次编号 / 作业编号任一重复、时间格式非法、数量非正（或超过
   三位小数、非有限、超出可表示范围）时整次请求返回 HTTP 422 字段级错误；
   **有效库存总量小于总需求**时同样整次 422，不返回任何部分配给，
   缺货反馈同时指出**总需求、有效库存与缺口**（过期批次不计入有效库存）；
6. 响应中 `remaining` 覆盖全部有效批次（含被扣减至零的批次），
   与 `expired_batches` 一样按（失效时间、入库时间、批次编号）排序；
   时间沿用严格的带 `Z` UTC 秒级格式，数量为 JSON 数值。

请求示例：

```json
{
  "calculated_at": "2026-09-15T02:00:00Z",
  "batches": [
    {"batch_id": "DZ-02", "available": 60, "received_at": "2026-09-14T21:00:00Z", "expires_at": "2026-09-17T08:00:00Z"},
    {"batch_id": "DZ-01", "available": 25, "received_at": "2026-09-14T20:00:00Z", "expires_at": "2026-09-15T12:00:00Z"},
    {"batch_id": "OLD-1", "available": 50, "received_at": "2026-09-13T08:00:00Z", "expires_at": "2026-09-15T02:00:00Z"}
  ],
  "demands": [
    {"job_id": "JOB-1", "requested": 70}
  ]
}
```

响应（`200 OK`）：OLD-1 恰在计算时刻失效不参与配给；DZ-01 先失效先扣减，
需求 70 拆分为 DZ-01 出 25、DZ-02 出 45。

```json
{
  "calculated_at": "2026-09-15T02:00:00Z",
  "allocations": [
    {"job_id": "JOB-1", "requested": 70,
     "lines": [
       {"batch_id": "DZ-01", "quantity": 25},
       {"batch_id": "DZ-02", "quantity": 45}
     ]}
  ],
  "remaining": [
    {"batch_id": "DZ-01", "remaining": 0},
    {"batch_id": "DZ-02", "remaining": 15}
  ],
  "expired_batches": [
    {"batch_id": "OLD-1", "available": 50,
     "received_at": "2026-09-13T08:00:00Z", "expires_at": "2026-09-15T02:00:00Z"}
  ]
}
```

库存不足时返回字段级 422（无部分配给，反馈指出总需求、有效库存与缺口）：

```json
{
  "detail": [
    {"type": "insufficient_inventory", "loc": ["body"],
     "msg": "除冰液有效库存不足：总需求 100.500，有效库存 60.000，缺口 40.500",
     "input": null,
     "ctx": {"total_demand": 100.5, "effective_inventory": 60, "shortfall": 40.5}}
  ]
}
```

## 运行

仅启动 API（容器内监听 8000，默认宿主端口 8000）：

```bash
docker compose up --build
# 自定义宿主端口：
API_PORT=18080 docker compose up --build
```

- 健康检查：`GET http://localhost:8000/health` → `{"status": "ok"}`
- 交互式文档：`http://localhost:8000/docs`
- 评估接口：`POST http://localhost:8000/evaluate`
- 巡检快照接口：`POST http://localhost:8000/inspection-snapshot`
- 除冰液配给接口：`POST http://localhost:8000/deicing-allocation`

### 一次性验收服务 verify

`verify` 不随默认配置启动（使用 compose profile），跑完 `pytest` 与真实 HTTP 冒烟即退出：

```bash
docker compose run --rm verify
```

它会：

1. 等待 `api` 健康检查通过；
2. 在容器内执行全部 pytest（临界相接、侵入一秒、跨日区间、跑道隔离、排序稳定性、各类 422，
   以及巡检快照四组确定性场景：未检查 / 故障后修复 / 截止后修复仍故障 / 同秒矛盾 422，
   以及除冰液配给四组确定性场景：足量单批次 / 跨批次拆分 / 过期批次排除 / 库存不足）；
3. 对运行中的 API 执行 `scripts/smoke_http.py`（零第三方依赖，断言真实 HTTP 响应）。

### 本地直接运行（不用 Docker）

需要 Python 3.12：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
pytest -q
API_BASE_URL=http://127.0.0.1:8000 python scripts/smoke_http.py
```

## 目录结构

```
app/
  domain.py     # 纯领域逻辑：十分钟扩展、半开相交、排序、巡检事件折叠、除冰液配给（无第三方依赖）
  schemas.py    # Pydantic 请求/响应模型、Z 秒级时间、三位小数数量与字段级约束
  main.py       # FastAPI 装配、422 错误聚合、无状态 /evaluate、/inspection-snapshot 与 /deicing-allocation
tests/
  test_domain.py
  test_api.py
  test_deicing_domain.py
  test_deicing_api.py
  test_schemas.py
scripts/
  smoke_http.py # verify 服务使用的真实 HTTP 冒烟脚本（仅标准库）
Dockerfile
docker-compose.yml
requirements.txt
```
