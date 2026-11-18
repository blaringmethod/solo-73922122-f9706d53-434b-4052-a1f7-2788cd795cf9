"""质控放行服务：范围核验、放行/撤销、供数过滤。

并发正确性（核验 vs 补录，含独立 SQL）：

* 每个作用域 (lab_no, report_date) 对应一把事务级咨询锁
  ``pg_advisory_xact_lock(qc_scope_lock_key(...))``。
* 核验事务先取锁，再按 ``reports.created_at`` 的 UTC 日期读取该作用域全部原始行，
  检查 ``lower_bound <= measured <= upper_bound``，最后 UPSERT 放行状态并提交。
  取锁用 ``pg_try_advisory_xact_lock`` + 应用侧截止时间轮询，而不是
  ``lock_timeout`` 阻塞等待：这样在禁止 PostgreSQL 定时器中断的受限环境里
  也能保证有界等待并返回 409，语义与 lock_timeout 完全一致。
* 对 reports 的任何 INSERT/UPDATE/DELETE（无论来自 API 还是独立 SQL）都会触发
  ``qc_revoke_affected()``：在同一事务内先取同一把锁，再把受影响作用域的
  released 行改为 revoked。因此两类操作在同一作用域上严格串行，
  未被本次核验看到的新行不可能被放行；改动随原事务提交生效，回滚则一并回滚。

原子性：一次核验就是一个数据库事务，任一步失败整体回滚，
qc_releases 不留下半成品，reports 原始行永不被本服务修改。
"""
from __future__ import annotations

import logging
import threading
import time

from . import db
from .config import settings

log = logging.getLogger("qc")

# try-lock 轮询间隔：足够小以保证等待精度，又不会把连接刷成忙等。
_LOCK_POLL_INTERVAL_S = 0.05

# 测试钩子：非 None 时，核验事务在“取锁之后、读取原始行之后”会在此事件上等待，
# 用于确定性地构造“核验持锁进行中”的并发交错。生产代码保持 None。
test_stall_after_read: threading.Event | None = None


class ScopeBusyError(Exception):
    """同一作用域的咨询锁在等待窗口内无法取得（有长事务在增改删）。"""


def _view(row: dict) -> dict:
    return {
        "lab_no": row["lab_no"],
        "report_date": row["report_date"],
        "lower_bound": row["lower_bound"],
        "upper_bound": row["upper_bound"],
        "status": row["status"],
        "checked_report_count": row["checked_report_count"],
        "violation_count": row["violation_count"],
        "violation_ids": list(row["violation_ids"] or []),
        "revoke_reason": row.get("revoke_reason"),
        "checked_at": row["checked_at"],
    }


def verify_and_release(
    lab_no: str, report_date, lower_bound: int, upper_bound: int
) -> dict:
    """核验一个作用域并记录当前放行/撤销结论（单事务，原子）。

    结论规则：
      * 该作用域无任何报告          -> revoked（撤销原放行；无越界 id）
      * 存在 measured 越界的报告    -> revoked（撤销原放行；返回越界报告 id）
      * 全部落在 [lower, upper]    -> released（记录放行，供数接口可见）
    每次调用都按当前数据与新范围重核，并覆盖该作用域的旧记录。
    """
    with db.pool().connection() as conn:
        try:
            conn.execute("BEGIN")
            # 取作用域咨询锁。try-lock + 应用侧截止时间轮询：
            # 不依赖 PostgreSQL 的 lock_timeout/statement_timeout 定时器
            # （受限沙箱可能屏蔽其中断），任何环境下等待都有界。
            deadline = (
                None if settings.qc_lock_timeout_ms <= 0
                else time.monotonic() + settings.qc_lock_timeout_ms / 1000.0
            )
            while True:
                got = conn.execute(
                    "SELECT pg_try_advisory_xact_lock("
                    "qc_scope_lock_key(%s, %s)) AS ok",
                    (lab_no, report_date),
                ).fetchone()["ok"]
                if got:
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    raise ScopeBusyError(
                        f"scope lock busy after "
                        f"{settings.qc_lock_timeout_ms}ms"
                    )
                time.sleep(_LOCK_POLL_INTERVAL_S)

            # 按 created_at 的 UTC 日历日核对原始行；行级排序保证越界 id 稳定。
            rows = conn.execute(
                """
                SELECT id, measured
                  FROM reports
                 WHERE lab_no = %s
                   AND (created_at AT TIME ZONE 'UTC')::date = %s
                 ORDER BY id
                """,
                (lab_no, report_date),
            ).fetchall()

            # 测试钩子：模拟“核验进行中（已持锁、已读行、尚未写结论）”。
            if test_stall_after_read is not None:
                test_stall_after_read.wait(30)

            violation_ids = [
                r["id"] for r in rows
                if not (lower_bound <= r["measured"] <= upper_bound)
            ]
            checked = len(rows)

            if checked == 0:
                status, reason = "revoked", "no reports found for scope"
            elif violation_ids:
                status, reason = "revoked", "measured value outside allowed range"
            else:
                status, reason = "released", None

            # UPSERT：范围改变/重新核验都覆盖同一作用域行，历史结论以当前行为准。
            row = conn.execute(
                """
                INSERT INTO qc_releases (
                    lab_no, report_date, lower_bound, upper_bound, status,
                    checked_report_count, violation_count, violation_ids,
                    revoke_reason, checked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (lab_no, report_date) DO UPDATE SET
                    lower_bound        = EXCLUDED.lower_bound,
                    upper_bound        = EXCLUDED.upper_bound,
                    status             = EXCLUDED.status,
                    checked_report_count = EXCLUDED.checked_report_count,
                    violation_count    = EXCLUDED.violation_count,
                    violation_ids      = EXCLUDED.violation_ids,
                    revoke_reason      = EXCLUDED.revoke_reason,
                    checked_at         = now()
                RETURNING *
                """,
                (lab_no, report_date, lower_bound, upper_bound, status,
                 checked, len(violation_ids), violation_ids, reason),
            ).fetchone()
            conn.commit()
            return _view(dict(row))
        except ScopeBusyError:
            conn.rollback()
            log.warning("qc scope busy: lab=%s date=%s", lab_no, report_date)
            raise
        except Exception:
            conn.rollback()
            raise


def get_release(lab_no: str, report_date) -> dict | None:
    with db.pool().connection() as conn:
        row = conn.execute(
            "SELECT * FROM qc_releases WHERE lab_no = %s AND report_date = %s",
            (lab_no, report_date),
        ).fetchone()
    return _view(dict(row)) if row else None


def list_releases(limit: int, offset: int, status: str | None = None) -> dict:
    with db.pool().connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM qc_releases WHERE status = %s "
                "ORDER BY report_date DESC, lab_no LIMIT %s OFFSET %s",
                (status, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT count(*) AS n FROM qc_releases WHERE status = %s",
                (status,),
            ).fetchone()["n"]
        else:
            rows = conn.execute(
                "SELECT * FROM qc_releases "
                "ORDER BY report_date DESC, lab_no LIMIT %s OFFSET %s",
                (limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT count(*) AS n FROM qc_releases"
            ).fetchone()["n"]
    return {"total": total, "items": [_view(dict(r)) for r in rows]}


def supply_reports(
    limit: int, offset: int, lab_no: str | None = None, report_date=None
) -> dict:
    """供数接口：只返回当前获放行（qc_releases.status='released'）作用域内的报告。

    JOIN 条件按报告自身的 UTC 日历日匹配放行行；撤销/从未核验的作用域一律不可见。
    """
    where = ["q.status = 'released'"]
    params: list = []
    if lab_no:
        where.append("r.lab_no = %s")
        params.append(lab_no)
    if report_date is not None:
        where.append("q.report_date = %s")
        params.append(report_date)
    where_sql = " AND ".join(where)

    with db.pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.id, r.lab_no, r.report_no, r.measured, r.created_at,
                   q.report_date AS report_date
              FROM reports r
              JOIN qc_releases q
                ON q.lab_no = r.lab_no
               AND q.report_date = (r.created_at AT TIME ZONE 'UTC')::date
               AND q.status = 'released'
             WHERE {where_sql}
             ORDER BY r.id DESC
             LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
        total = conn.execute(
            f"""
            SELECT count(*) AS n
              FROM reports r
              JOIN qc_releases q
                ON q.lab_no = r.lab_no
               AND q.report_date = (r.created_at AT TIME ZONE 'UTC')::date
               AND q.status = 'released'
             WHERE {where_sql}
            """,
            params,
        ).fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}
