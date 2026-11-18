# 检验报告唯一性保障与质控放行服务

为持续补录的检验报告表 `reports` 提供两项保障：

1. **唯一性**：在线建立 **(实验室号, 报告号)** 唯一索引，防止重复报告入仓
   （`CREATE UNIQUE INDEX CONCURRENTLY`，构建期间补录不阻塞）。
2. **质控放行**：按 **(实验室号, UTC 日期, 整数上下限)** 核对原始测值，
   全部合格才放行；供数接口只返回当前获放行作用域的报告；
   独立 SQL 对 reports 的增/改/删随原事务撤销受影响作用域的放行。

技术栈：FastAPI + PostgreSQL。

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发起请求）
# OpenAPI: http://localhost:8000/openapi.json
```

- 首次启动 PostgreSQL 自动执行 `sql/01_schema.sql` 建表、建索引、部署触发器（幂等）。
- API 启动时也会幂等重放同一脚本，本地运行、升级场景同样可用。
- 健康检查：`GET /health`。

## 2. 数据模型

`reports`（`sql/01_schema.sql`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT IDENTITY 主键 | 主键流水号 |
| `lab_no` | TEXT NOT NULL | 实验室号 |
| `report_no` | TEXT NOT NULL | 实验室内报告号 |
| `measured` | INTEGER NOT NULL | 整数测值 |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | 入库时间；质控按其 **UTC 日历日** 核对 |

`qc_releases`：每个 **(实验室号, UTC 日期)** 作用域至多一行（UPSERT 覆盖），
`status` 为 `released`（当前放行）或 `revoked`（当前撤销），
并记录本次上下限、核验行数、越界报告 id 列表、撤销原因与核验时间。

另有轮次表 `index_build_rounds` 持久化每次索引构建的状态、后端 PID 与错误信息，用于重启接续。

## 3. API

### 3.1 补录与唯一索引

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/reports` | 单条补录；唯一索引生效后重复键返回 `409`，原数据不变 |
| POST | `/reports/batch` | 批量补录，重复键逐条跳过，返回成功/跳过明细 |
| GET | `/reports` | 管理视图：分页查询全部报告（不经质控过滤） |
| POST | `/index/build` | 发起/复用 `CREATE UNIQUE INDEX CONCURRENTLY`（202） |
| GET | `/index/status` | 查询轮次状态 + `pg_index` 中索引的真实有效性 |
| POST | `/index/cancel` | 受理取消（202 `cancel_requested`），SQL 确认停止后才 `cancelled` |

### 3.2 质控放行与供数

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/qc/verify` | 提交 `lab_no`、`report_date`（UTC）、`lower_bound`、`upper_bound` 核对并落结论 |
| GET | `/qc/releases` | 分页查看各作用域当前状态（可按 `status=released/revoked` 过滤） |
| GET | `/qc/releases/{lab_no}/{report_date}` | 查询单个作用域当前结论；从未核验返回 404 |
| GET | `/reports/supply` | **供数接口**：只返回当前获放行作用域内的报告（可按 lab/日期过滤） |

`POST /qc/verify` 的裁决规则（单事务原子完成）：

```
该作用域无报告            -> revoked（撤销原放行；violation_ids 为空，原因 no reports found）
存在 measured 越界的报告  -> revoked（撤销原放行；返回全部越界报告 id）
全部落在 [lower, upper]  -> released（记录/覆盖放行，供数接口立即可见）
重复提交（范围改变）       -> 按当前数据与新范围重新核验，UPSERT 覆盖旧结论
同作用域有长事务占锁超时   -> 409 qc_scope_busy，不写半成品，调用方可重试
```

## 4. 质控放行的关键设计如何满足需求

- **按 `created_at` 的 UTC 日期核对原始行**：过滤条件统一为
  `(created_at AT TIME ZONE 'UTC')::date = report_date`，
  配表达式索引 `idx_reports_lab_utc_date`；供数 JOIN 用同一表达式，跨 UTC 日边界行为确定。
- **无报告 / 越界即撤销，并返回越界报告 id**：结论与越界 id 在同一事务写入 `qc_releases`；
  撤销作用域在供数接口中立即不可见。
- **范围改变必须重核**：每作用域唯一约束 `uq_qc_releases_scope` + UPSERT，
  每次调用都覆盖旧上下限与旧结论，不存在“沿用旧放行”。
- **独立 SQL 增改删随原事务撤销、回滚不能撤销**：reports 上的语句级触发器
  （INSERT/UPDATE/DELETE/TRUNCATE，过渡表按作用域去重）在**触发它的同一事务内**
  把受影响作用域的 released 行改为 revoked；改实验室号或日期时新旧两个作用域都撤销；
  TRUNCATE 撤销全部放行。事务 ROLLBACK 则触发器的 UPDATE 一并回滚，放行保持不变。
- **并发核验与补录不会放行未经检查的行**：核验事务与触发器都先取同一把
  事务级咨询锁 `pg_advisory_xact_lock(qc_scope_lock_key(lab, date))`，
  同作用域严格串行。两种交错都安全：
  1. 核验先持锁：补录等待；核验提交后补录才提交，触发器随即撤销——新行不可能被旧放行覆盖；
  2. 补录先持锁：核验等待；补录提交后核验才读行，新行必然纳入本次检查。
  取锁统一用 `pg_try_advisory_xact_lock` + 有界轮询（应用侧截止时间 / 触发器内 30s 窗口），
  在禁止进程信号的受限环境里也不会无限挂起；窗口耗尽则明确报错回滚，绝不跳过锁。
- **执行故障不留下部分核验结果**：一次核验 = 一个数据库事务（取锁、读行、UPSERT、提交），
  任一步异常整体回滚，`qc_releases` 无半成品、咨询锁随事务自动释放、reports 原始行永不被修改。
- **在线建索引期间补录照常提交**：唯一索引使用 CONCURRENTLY 不持表锁；
  质控核验/供数与索引构建互不干扰（有自测覆盖）。

## 5. 独立 SQL

- `sql/02_backfill_examples.sql`：不依赖 API 的补录示例（单条/批量/外部表，含 `ON CONFLICT DO NOTHING`）。
- `sql/03_dedupe.sql`：重复键排查与去重（保留每组最早一条）。
- `sql/04_qc_release.sql`：触发器撤销语义的可执行核对脚本（全部用 ROLLBACK 收尾，不改数据），
  含 INSERT/UPDATE（改实验室号、改日期）/DELETE/TRUNCATE 与回滚不撤销的演示查询。

## 6. 演示

```bash
# 前置：一个运行中的 API 与 PostgreSQL（本地或 Compose）。
# 本地无 Docker 时可用环境变量覆盖连接，见 demo/common.sh。
bash demo/01_long_transaction.sh   # 长事务拖慢构建 + 构建期补录照常提交
bash demo/02_duplicate_key.sh      # 重复键失败->原数据保留->去重重试成功->409
bash demo/03_restart_recovery.sh   # API 重启接续跟踪，不重跑、不虚报
bash demo/04_cancel.sh             # 取消受理/确认、不误伤补录、有效索引不删除
bash demo/05_qc_release.sh         # 质控放行全链路：核验/撤销/供数/触发器/并发交错/建索引共存
```

> 受限沙箱若禁止进程信号，`pg_cancel_backend` 与 PostgreSQL 锁定时器中断无法生效；
> 质控链路不依赖这些机制（try-lock 轮询 + 应用侧截止时间），在受限与标准环境行为一致。
> 演示 4 会如实展示“取消已受理、索引最终建成则报 complete 且保留”，并用等价的
> 「后端已停止」现场演示 `cancelled` 裁决与清理。

## 7. 运行测试

```bash
export DATABASE_URL=postgresql://postgres@<host>:<port>/<db>
pip install -r requirements.txt pytest httpx a2wsgi
pytest -q
```

测试集（24 个用例）：

- `tests/test_api.py`（9 个）：补录、并发构建、单轮复用、重复键、取消、重启接续；
- `tests/test_qc_release.py`（15 个）：放行/撤销/越界 id/无报告/范围重核、供数过滤与
  UTC 边界、触发器增改删/TRUNCATE 与回滚、两种并发交错、锁超时 409、故障原子性、
  与在线建索引共存。

## 8. 目录结构

```
app/            FastAPI 应用（main 路由、service 构建编排、qc 质控服务、db 访问层、schemas）
sql/            建表/触发器、补录示例、去重脚本、质控联动核对脚本
demo/           五个端到端演示 + 长事务/咨询锁持有助手
tests/          pytest 关键功能自测（24 个用例）
Dockerfile, docker-compose.yml, requirements.txt
```
