# 离线潮汐排班服务

该实现为既有的船舶排班流程增加**离线潮汐预报版本治理**。核心保证是：

- 求解请求必须显式提供 `forecast_version_id`，且只能绑定状态为 `published` 的完整版本。
- 每次锁定计划都会保存计划输入快照、完整预报快照、求解结果和可重放哈希。
- 新预报发布不会静默改写任何已锁定计划，只会在显式发起影响分析时生成可审查报告。
- 只有对 `open` 且 `feasible=true` 的报告执行显式采纳，才会锁定下一修订版；旧修订版变为 `superseded`。
- 不可行报告会列出船舶/任务、原可行潮窗、新潮窗和原因，不能被采纳。
- 预报和影响报告按内容哈希与 `(计划, 基线修订版, 目标预报)` 幂等；乱序发布不产生重复影响。
- 所有状态变化写入 append-only、前序哈希链接的审计表。

## 运行

仅依赖 Python 3.11 标准库：

```bash
python -m tide_scheduler --database data/tide-scheduler.sqlite3 --host 127.0.0.1 --port 8080
```

OpenAPI 文档：

```text
GET /openapi.json
GET /api/v1/openapi
```

健康检查：`GET /healthz`

## 主要 API

1. `POST /api/v1/vessels`：登记船舶。
2. `POST /api/v1/forecasts`：创建完整草稿预报（含生效区间和所有潮窗）。
3. `POST /api/v1/forecasts/{id}/publish`：原子发布；重复发布幂等。
4. `POST /api/v1/plans:solve`：用显式指定的已发布预报求解并锁定修订版 1。
5. `POST /api/v1/plans/{id}/impact-reports`：对新预报生成可审查影响报告，不改变锁定计划。
6. `POST /api/v1/impact-reports/{id}:adopt`：显式采纳可行报告并锁定下一修订版。
7. `POST /api/v1/impact-reports/{id}:dismiss`：显式放弃开放报告。
8. `GET /api/v1/plans/{id}/revisions/{n}:replay`：从持久化历史快照确定性回放。
9. `GET /api/v1/audit-events`：查看哈希链审计记录。

## 示例流程

```bash
# 发布 v1 后求解
curl -s localhost:8080/api/v1/plans:solve -H 'content-type: application/json' -d '{
  "plan_id": "plan_1",
  "forecast_version_id": "f_v1",
  "tasks": [{
    "id": "berth_alpha",
    "vessel_id": "v_alpha",
    "duration_minutes": 120,
    "earliest_start": "2026-01-01T23:00:00Z",
    "latest_start": "2026-01-02T01:00:00Z"
  }]
}'

# v2 发布后只生成影响报告
curl -s -X POST localhost:8080/api/v1/plans/plan_1/impact-reports \
  -H 'content-type: application/json' \
  -d '{"target_forecast_version_id":"f_v2"}'

# 人工审查可行后才采纳
curl -s -X POST localhost:8080/api/v1/impact-reports/impact_xxx:adopt
```

## 持久化模型

- `forecast_versions`：版本、生效区间、发布状态、内容哈希。
- `forecast_windows`：版本内的完整潮窗集合；未完整写入的版本仍为草稿，不能求解。
- `plans` / `plan_revisions`：计划当前修订版指针及不可变修订版。
- `plan_assignments`：每个锁定修订版的排程结果。
- `impact_reports` / `impact_report_items`：可审查的影响及逐船/逐任务原因。
- `audit_events`：只追加的哈希链事件表。

SQLite 写事务统一使用 `BEGIN IMMEDIATE`。因此发布与求解并发时，求解只能看到发布事务前的草稿（拒绝）或提交后的完整发布版本（成功锁定），不会绑定半成品。

## 确定性求解与回放

求解器按 `(vessel_id, ordinal, starts_at)` 顺序选择每个任务最早可行的潮窗；任务按 `id` 排序输出。计划修订版记录：

- `input_snapshot_json`
- `forecast_snapshot_json`
- `result_json`
- `replay_hash`

重启服务后，回放接口只使用修订版中的快照重新求解，并比对结果和哈希，不依赖当前最新预报。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

- 无关修订不静默影响既有计划；
- 跨午夜潮窗收窄时精确列出船舶、原窗口、新窗口和 `tide_window_too_short`；
- 重复/乱序发布与重复影响分析幂等；
- 发布与求解并发时不存在半成品版本绑定；
- 重启后快照可回放；
- 原跨午夜可行潮窗求解测试、API/OpenAPI 和审计链。
