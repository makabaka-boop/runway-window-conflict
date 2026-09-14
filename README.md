# 夜间跑道施工放行评估 API

无状态纯后端服务：一次请求提交**已声明的跑道代码**、若干**施工窗口**与**航班占用区间**，
服务逐施工窗口返回与之冲突的航班及交集起止。响应完全由请求体决定，可复算、顺序稳定，
不依赖任何数据库或请求间状态。

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

### 一次性验收服务 verify

`verify` 不随默认配置启动（使用 compose profile），跑完 `pytest` 与真实 HTTP 冒烟即退出：

```bash
docker compose run --rm verify
```

它会：

1. 等待 `api` 健康检查通过；
2. 在容器内执行全部 pytest（临界相接、侵入一秒、跨日区间、跑道隔离、排序稳定性、各类 422）；
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
  domain.py     # 纯领域逻辑：十分钟扩展、半开相交、排序（无第三方依赖）
  schemas.py    # Pydantic 请求/响应模型、Z 秒级时间与字段级约束
  main.py       # FastAPI 装配、422 错误聚合、无状态 /evaluate
tests/
  test_domain.py
  test_api.py
scripts/
  smoke_http.py # verify 服务使用的真实 HTTP 冒烟脚本（仅标准库）
Dockerfile
docker-compose.yml
requirements.txt
```
