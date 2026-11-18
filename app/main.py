"""FastAPI 入口：补录、唯一索引构建、质控放行与供数。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date

import psycopg
from fastapi import FastAPI, HTTPException, Query, status

from . import db, qc
from .qc import ScopeBusyError
from .responses import AppJSONResponse
from .schemas import (
    BatchReportIn,
    QcReleaseOut,
    QcVerifyIn,
    ReportIn,
    ReportOut,
    SupplyListOut,
)
from .service import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 幂等建表/建触发器：首次启动由 initdb 执行，这里保证本地、测试与升级场景同样可用。
    db.ensure_schema()
    db.init_pool()
    service.reconcile_on_startup()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="检验报告唯一性保障与质控放行 API",
    description=(
        "对持续补录的 reports 表在线构建 (lab_no, report_no) 唯一索引；"
        "支持发起、查询、取消 CREATE UNIQUE INDEX CONCURRENTLY，构建期间补录不阻塞，"
        "API 重启后接续跟踪。另提供质控放行：按 (实验室号, UTC 日期, 整数上下限) "
        "核对原始测值，全部合格才放行，供数接口只返回当前获放行的报告；"
        "独立 SQL 对 reports 的增改删随原事务撤销受影响作用域的放行。"
    ),
    version="1.1.0",
    default_response_class=AppJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ------------------------------ 补录 ------------------------------------

@app.post("/reports", response_model=ReportOut, status_code=201)
def insert_report(payload: ReportIn):
    """单条补录。构建期间（CONCURRENTLY）可正常提交；唯一索引生效后重复键返回 409。"""
    try:
        with db.pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO reports (lab_no, report_no, measured) "
                "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                (payload.lab_no, payload.report_no, payload.measured),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_report",
                "message": "同一实验室下报告号已存在，本条补录被拒绝，原有数据保持不变",
                "lab_no": payload.lab_no,
                "report_no": payload.report_no,
                "pg_error": str(exc).strip(),
            },
        )
    return dict(row)


@app.post("/reports/batch")
def insert_reports_batch(payload: BatchReportIn):
    """批量补录：逐条保存，重复键跳过且不影响其它行，返回成功/跳过明细。"""
    inserted, skipped = [], []
    with db.pool().connection() as conn:
        for item in payload.items:
            try:
                row = conn.execute(
                    "INSERT INTO reports (lab_no, report_no, measured) "
                    "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                    (item.lab_no, item.report_no, item.measured),
                ).fetchone()
                inserted.append(dict(row))
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                skipped.append(
                    {"lab_no": item.lab_no, "report_no": item.report_no,
                     "reason": "duplicate_report"}
                )
        conn.commit()
    return {"inserted_count": len(inserted), "skipped_count": len(skipped),
            "inserted": inserted, "skipped": skipped}


@app.get("/reports")
def list_reports(limit: int = 50, offset: int = 0):
    """管理视图：分页查询全部报告（不经质控过滤；供数请用 /reports/supply）。"""
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, lab_no, report_no, measured FROM reports "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


@app.get("/reports/supply", response_model=SupplyListOut)
def supply_reports(
    lab_no: str | None = Query(None, description="可选：按实验室号过滤"),
    report_date: date | None = Query(None, description="可选：按 UTC 日期过滤 (YYYY-MM-DD)"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """供数接口：只返回**当前获放行**作用域内的报告。

    报告按其 ``created_at`` 的 UTC 日历日匹配 ``qc_releases`` 中
    ``status='released'`` 的行；撤销或从未核验的作用域一律不返回。
    """
    return qc.supply_reports(limit, offset, lab_no, report_date)


# ------------------------------ 质控放行 ---------------------------------

@app.post("/qc/verify", response_model=QcReleaseOut)
def qc_verify(payload: QcVerifyIn):
    """提交实验室号、UTC 日期与整数上下限，按 reports.created_at 核对该作用域原始行。

    * 全部合格 -> 记录/更新为 ``released``，供数接口立即可见；
    * 无报告或存在越界值 -> 撤销该作用域原放行，返回越界报告 id；
    * 范围改变（重复提交）-> 按新范围重新核验并覆盖旧结论；
    * 同一作用域有长事务正在增改删 reports、锁等待超时 -> 409（不跳过、不放行）。

    整个核验是单事务：执行故障整体回滚，不留下部分核验结果。
    """
    try:
        view = qc.verify_and_release(
            payload.lab_no,
            payload.report_date,
            payload.lower_bound,
            payload.upper_bound,
        )
    except ScopeBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "qc_scope_busy",
                "message": "该作用域正被长事务占用（reports 增改删未提交），请稍后重试",
                "lab_no": payload.lab_no,
                "report_date": payload.report_date.isoformat(),
                "pg_error": str(exc),
            },
        )
    return view


# ------------------------------ 质控查询 ---------------------------------

@app.get("/qc/releases")
def qc_list_releases(
    status_filter: str | None = Query(None, alias="status",
                                      description="可选：released / revoked"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """分页查看各作用域当前放行/撤销状态。"""
    if status_filter and status_filter not in ("released", "revoked"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_status",
                    "message": "status 仅支持 released / revoked"},
        )
    return qc.list_releases(limit, offset, status_filter)


@app.get("/qc/releases/{lab_no}/{report_date}", response_model=QcReleaseOut)
def qc_get_release(lab_no: str, report_date: date):
    """查询单个作用域的当前质控结论；从未核验返回 404。"""
    view = qc.get_release(lab_no, report_date)
    if view is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "qc_release_not_found",
                    "message": "该作用域尚未进行质控核验",
                    "lab_no": lab_no,
                    "report_date": report_date.isoformat()},
        )
    return view


# --------------------------- 唯一索引构建 --------------------------------

@app.post("/index/build", status_code=202)
def start_build():
    """发起（或复用）CREATE UNIQUE INDEX CONCURRENTLY。

    * 已有活跃轮次：复用同一轮，不新开；
    * 索引已有效：直接返回 complete；
    * 失败后的无效残留索引会先安全清理，绝不删除有效索引。
    """
    view, code = service.start_build()
    return AppJSONResponse(view, status_code=code)


@app.get("/index/status")
def index_status(round_id: str | None = None):
    """查询构建轮次状态，并附带 pg_index 中的真实索引有效性。"""
    return service.get_status(round_id)


@app.post("/index/cancel", status_code=202)
def cancel_build(round_id: str | None = None):
    """取消构建：先受理（cancel_requested），后端 SQL 确认停止后才转 cancelled。

    只向记录在案的构建后端 PID 发 pg_cancel_backend，不影响补录连接；
    若索引已有效则报 complete。
    """
    view, code = service.request_cancel(round_id)
    return AppJSONResponse(view, status_code=code)
