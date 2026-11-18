"""质控放行关键功能自测。

覆盖：
* 全量合格放行、越界撤销并返回报告 id、无报告撤销、范围改变重核、撤销后重核恢复；
* 供数接口只返回当前获放行作用域的报告（UTC 日期边界、过滤参数）；
* 独立 SQL 增/改/删/TRUNCATE 随原事务撤销放行，ROLLBACK 不撤销；
* 并发核验与补录的两种交错都不会放行未经检查的行；
* 核验执行故障不留下部分结果；锁冲突返回 409；与在线建索引互不干扰。
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timezone

import psycopg
import pytest

from app import db as dbmod
from app.config import settings
from app.qc import verify_and_release
import os

D = date(2026, 11, 18)
# 每次进程唯一的实验室号：避开受限沙箱中无法清理的残留咨询锁作用域。
LAB = f"LAB-QC-{os.environ['QC_RUN_SUFFIX']}"


def _insert(rows, conn=None):
    """rows: list of (lab_no, report_no, measured, created_at_iso)。"""
    own = conn is None
    conn = conn or psycopg.connect(settings.database_url)
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO reports(lab_no, report_no, measured, created_at) "
                "VALUES (%s,%s,%s,%s)", rows,
            )
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()


def _seed_day(lab=LAB, day=D):
    _insert([
        (lab, "R1", 5, datetime(2026, 11, 18, 0, 30, tzinfo=timezone.utc)),
        (lab, "R2", 8, datetime(2026, 11, 18, 23, 59, tzinfo=timezone.utc)),
        # UTC 已跨日：属于 11-19，不应计入 11-18 的核验
        (lab, "R3", 99, datetime(2026, 11, 19, 0, 0, tzinfo=timezone.utc)),
    ])


def _verify(client, lo=0, hi=10, lab=LAB, day=D):
    return client.post("/qc/verify", json={
        "lab_no": lab, "report_date": day.isoformat(),
        "lower_bound": lo, "upper_bound": hi,
    })


# ------------------------------ 基础核验语义 ------------------------------

def test_verify_all_in_range_releases_and_supply(client):
    _seed_day()
    r = _verify(client, 0, 10)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "released"
    assert body["checked_report_count"] == 2
    assert body["violation_count"] == 0 and body["violation_ids"] == []
    assert body["revoke_reason"] is None

    # 供数接口：只返回放行作用域内的两条；R3（11-19）未放行不可见
    supply = client.get("/reports/supply").json()
    assert supply["total"] == 2
    assert {i["report_no"] for i in supply["items"]} == {"R1", "R2"}
    assert all(i["lab_no"] == LAB for i in supply["items"])

    # 管理视图仍可见全部三行
    assert client.get("/reports").json()["total"] == 3


def test_verify_out_of_range_revokes_and_returns_ids(client):
    _seed_day()
    r = _verify(client, 6, 10)  # R1=5 越界
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"
    assert body["violation_count"] == 1
    with dbmod.pool().connection() as conn:
        rid = conn.execute(
            "SELECT id FROM reports WHERE report_no='R1'").fetchone()["id"]
    assert body["violation_ids"] == [rid]
    assert body["revoke_reason"] == "measured value outside allowed range"
    # 撤销后供数不可见
    assert client.get("/reports/supply").json()["total"] == 0


def test_verify_no_reports_revokes(client):
    r = client.post("/qc/verify", json={
        "lab_no": "NOPE", "report_date": D.isoformat(),
        "lower_bound": 0, "upper_bound": 10,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"
    assert body["checked_report_count"] == 0
    assert body["violation_ids"] == []
    assert "no reports" in body["revoke_reason"]
    assert client.get("/reports/supply").json() == {"total": 0, "items": []}


def test_range_change_requires_recheck_and_overwrites(client):
    _seed_day()
    assert _verify(client, 0, 10).json()["status"] == "released"
    # 同作用域收紧范围 -> 重核 -> 撤销
    body = _verify(client, 6, 10).json()
    assert body["status"] == "revoked" and body["violation_count"] == 1
    # qc_releases 每个作用域只有一行（UPSERT 覆盖，不产生重复行）
    with dbmod.pool().connection() as conn:
        n = conn.execute(
            "SELECT count(*) AS n FROM qc_releases WHERE lab_no=%s AND report_date=%s",
            (LAB, D)).fetchone()["n"]
        row = conn.execute(
            "SELECT lower_bound, upper_bound FROM qc_releases "
            "WHERE lab_no=%s AND report_date=%s", (LAB, D)).fetchone()
    assert n == 1 and (row["lower_bound"], row["upper_bound"]) == (6, 10)
    # 放宽范围重新核验 -> 恢复放行
    assert _verify(client, 0, 10).json()["status"] == "released"
    assert client.get("/reports/supply").json()["total"] == 2


def test_reverify_restores_release_after_data_fix(client):
    _seed_day()
    assert _verify(client, 6, 10).json()["status"] == "revoked"
    assert _verify(client, 0, 10).json()["status"] == "released"
    assert client.get("/reports/supply").json()["total"] == 2


def test_invalid_bounds_rejected(client):
    r = client.post("/qc/verify", json={
        "lab_no": LAB, "report_date": D.isoformat(),
        "lower_bound": 10, "upper_bound": 1,
    })
    assert r.status_code == 422


def test_get_and_list_release_endpoints(client):
    _seed_day()
    _verify(client, 0, 10)
    got = client.get(f"/qc/releases/{LAB}/{D.isoformat()}")
    assert got.status_code == 200 and got.json()["status"] == "released"

    missing = client.get(f"/qc/releases/UNKNOWN/{D.isoformat()}")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "qc_release_not_found"

    listed = client.get("/qc/releases").json()
    assert listed["total"] == 1
    revoked_only = client.get("/qc/releases", params={"status": "revoked"}).json()
    assert revoked_only["total"] == 0
    bad = client.get("/qc/releases", params={"status": "weird"})
    assert bad.status_code == 400


def test_supply_filters_and_utc_date_boundary(client):
    _seed_day()
    _verify(client, 0, 100)  # R1,R2 合格；R3=99 在 11-19
    # 放行 11-19（R3 合格）
    r19 = client.post("/qc/verify", json={
        "lab_no": LAB, "report_date": "2026-11-19",
        "lower_bound": 0, "upper_bound": 100}).json()
    assert r19["status"] == "released" and r19["checked_report_count"] == 1

    supply_all = client.get("/reports/supply").json()
    assert supply_all["total"] == 3
    by_day = client.get("/reports/supply",
                        params={"report_date": "2026-11-18"}).json()
    assert by_day["total"] == 2
    by_lab = client.get("/reports/supply", params={"lab_no": LAB}).json()
    assert by_lab["total"] == 3
    other_lab = client.get("/reports/supply", params={"lab_no": "OTHER"}).json()
    assert other_lab["total"] == 0
    # 返回行带 UTC report_date
    dates = {i["report_no"]: i["report_date"] for i in supply_all["items"]}
    assert dates["R3"] == "2026-11-19" and dates["R1"] == "2026-11-18"


# ------------------------ 独立 SQL 触发器：随事务撤销 ------------------------

def test_trigger_insert_delete_update_truncate_and_rollback(client):
    _seed_day()
    assert _verify(client, 0, 100).json()["status"] == "released"

    # INSERT 同事务撤销，COMMIT 生效
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO reports(lab_no,report_no,measured,created_at) "
            "VALUES (%s,'RN',1,%s)",
            (LAB, datetime(2026, 11, 18, 12, tzinfo=timezone.utc)))
        st = conn.execute(
            "SELECT status FROM qc_releases WHERE lab_no=%s AND report_date=%s",
            (LAB, D)).fetchone()[0]
        assert st == "revoked"  # 同事务内已撤销
        conn.commit()
    assert client.get("/reports/supply").json()["total"] == 0

    # 重新放行后，INSERT + ROLLBACK 不得撤销
    assert _verify(client, 0, 100).json()["status"] == "released"
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO reports(lab_no,report_no,measured,created_at) "
            "VALUES (%s,'RB',1,%s)",
            (LAB, datetime(2026, 11, 18, 12, tzinfo=timezone.utc)))
        conn.rollback()
    assert client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()["status"] == "released"

    # DELETE 撤销（删后仍有 R2 等行，重核可再次放行）
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("DELETE FROM reports WHERE report_no='R1'")
        conn.commit()
    assert client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()["status"] == "revoked"

    # 重新放行；UPDATE 改实验室号：旧作用域撤销（R2 移走后该日仅剩次日 R3）
    assert _verify(client, 0, 100).json()["status"] == "released"
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("UPDATE reports SET lab_no='LAB-NEW' WHERE report_no='R2'")
        conn.commit()
    assert client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()["status"] == "revoked"
    # 新作用域本就没有放行行，不影响结果；旧作用域确认已撤销即可

    # UPDATE 改日期（created_at）：先补一行回到当日，再移动它，当日作用域撤销
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO reports(lab_no,report_no,measured,created_at) "
            "VALUES (%s,'R4',6,%s)",
            (LAB, datetime(2026, 11, 18, 10, tzinfo=timezone.utc)))
        conn.commit()
    assert _verify(client, 0, 100).json()["status"] == "released"
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "UPDATE reports SET created_at=%s WHERE report_no='R4'",
            (datetime(2026, 11, 20, 9, tzinfo=timezone.utc),))
        conn.commit()
    assert client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()["status"] == "revoked"

    # TRUNCATE 撤销全部放行（先放行另一个作用域，验证全量撤销不限于本实验室）
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(
            "INSERT INTO reports(lab_no,report_no,measured,created_at) "
            "VALUES ('OTHER-LAB','X',1,%s)",
            (datetime(2026, 11, 18, 10, tzinfo=timezone.utc),))
        conn.commit()
    client.post("/qc/verify", json={
        "lab_no": "OTHER-LAB", "report_date": D.isoformat(),
        "lower_bound": 0, "upper_bound": 100})
    with dbmod.pool().connection() as conn:
        assert conn.execute(
            "SELECT count(*) AS n FROM qc_releases WHERE status='released'"
        ).fetchone()["n"] >= 1
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("TRUNCATE reports")
        conn.commit()
    with dbmod.pool().connection() as conn:
        n_rel = conn.execute(
            "SELECT count(*) AS n FROM qc_releases WHERE status='released'"
        ).fetchone()["n"]
    assert n_rel == 0


def test_trigger_rollback_restores_release(client):
    _seed_day()
    _verify(client, 0, 100)
    # UPDATE/DELETE/TRUNCATE 的回滚同样不撤销
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("UPDATE reports SET measured=99999 WHERE report_no='R1'")
        conn.rollback()
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("DELETE FROM reports")
        conn.rollback()
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("TRUNCATE reports")
        conn.rollback()
    assert client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()["status"] == "released"
    # 回滚后数据原样
    assert client.get("/reports").json()["total"] == 3


# ------------------------------ 并发交错 ------------------------------

def test_concurrent_verify_and_insert_never_releases_unchecked(client, monkeypatch):
    """核验持锁期间，补录（独立 SQL 连接）阻塞等待；提交后触发器撤销该作用域。

    全程供数接口不得出现未检查行。
    """
    from app import qc as qcmod

    _seed_day()
    proceed = threading.Event()
    monkeypatch.setattr(qcmod, "test_stall_after_read", proceed)
    try:
        t = threading.Thread(target=verify_and_release,
                             args=(LAB, D, 0, 100), daemon=True,
                             name="qc-worker")
        t.start()
        time.sleep(1.0)  # 等核验事务拿到咨询锁并停在读取 reports 之后
        # 此时尚未有放行
        assert client.get("/reports/supply").json()["total"] == 0

        insert_done = threading.Event()

        def do_insert():
            # 独立连接：触发器会在同一事务内等待同一把作用域咨询锁。
            with psycopg.connect(settings.database_url) as ic:
                ic.execute(
                    "INSERT INTO reports(lab_no,report_no,measured,created_at) "
                    "VALUES (%s,'R-CONC',999,%s)",
                    (LAB, datetime(2026, 11, 18, 10, tzinfo=timezone.utc)))
                ic.commit()
            insert_done.set()

        it = threading.Thread(target=do_insert, daemon=True)
        it.start()
        time.sleep(1.0)
        assert not insert_done.is_set(), "补录必须等待核验持有的作用域锁"
        # 核验在飞期间供数仍为 0
        assert client.get("/reports/supply").json()["total"] == 0

        proceed.set()
        t.join(15)
        assert not t.is_alive()
        it.join(30)
        assert insert_done.is_set()
    finally:
        monkeypatch.setattr(qcmod, "test_stall_after_read", None)

    # 核验先提交 -> released；补录后提交 -> 触发器撤销；供数看不到未检查的 R-CONC
    rel = client.get(f"/qc/releases/{LAB}/{D.isoformat()}").json()
    assert rel["status"] == "revoked"
    assert "INSERT" in (rel["revoke_reason"] or "")
    supply = client.get("/reports/supply").json()
    assert supply["total"] == 0


def test_concurrent_insert_first_then_verify_sees_new_row(client):
    """反向交错：补录事务先持锁（触发器），核验 try-lock 等待；
    补录提交后核验拿到锁、看到新行并判越界。"""
    _seed_day()
    object.__setattr__(settings, "qc_lock_timeout_ms", 30000)
    ic = psycopg.connect(settings.database_url)
    ic.execute("BEGIN")
    ic.execute(
        "INSERT INTO reports(lab_no,report_no,measured,created_at) "
        "VALUES (%s,'R-CONC2',999,%s)",
        (LAB, datetime(2026, 11, 18, 10, tzinfo=timezone.utc)))
    time.sleep(0.5)  # 触发器已在该事务内取锁并撤销

    result: dict = {}

    def do_verify():
        result["view"] = verify_and_release(LAB, D, 0, 100)

    t = threading.Thread(target=do_verify, daemon=True)
    t.start()
    time.sleep(1.0)
    assert t.is_alive(), "核验必须等待未提交的补录事务"
    ic.commit()
    ic.close()
    t.join(20)
    assert not t.is_alive()
    object.__setattr__(settings, "qc_lock_timeout_ms", 10000)

    view = result["view"]
    assert view["status"] == "revoked"
    assert view["checked_report_count"] == 3
    with dbmod.pool().connection() as conn:
        rid = conn.execute(
            "SELECT id FROM reports WHERE report_no='R-CONC2'").fetchone()["id"]
    assert rid in view["violation_ids"]


def test_verify_lock_timeout_returns_409(client):
    """长事务占用作用域锁且等待窗口耗尽：API 返回 409，不跳过等待、不写半成品。"""
    _seed_day()
    object.__setattr__(settings, "qc_lock_timeout_ms", 500)
    # 用全新独立连接（不经过应用连接池）持锁，避免池内空闲连接被占导致请求等池。
    holder = psycopg.connect(settings.database_url, autocommit=True)
    try:
        holder.execute("BEGIN")
        holder.execute(
            "SELECT pg_advisory_xact_lock(qc_scope_lock_key(%s,%s))",
            (LAB, D))
        r = _verify(client, 0, 100)
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "qc_scope_busy"
        # 超时路径没有写入任何 qc_releases 行
        with dbmod.pool().connection() as conn:
            n = conn.execute(
                "SELECT count(*) AS n FROM qc_releases "
                "WHERE lab_no=%s AND report_date=%s", (LAB, D)).fetchone()["n"]
        assert n == 0
        # 锁释放后重试成功
        holder.rollback()
        assert _verify(client, 0, 100).json()["status"] == "released"
    finally:
        holder.close()
        object.__setattr__(settings, "qc_lock_timeout_ms", 10000)


# ------------------------------ 故障原子性 ------------------------------

def test_verify_failure_leaves_no_partial_result(client, monkeypatch):
    """核验中途故障：整事务回滚，qc_reports 无残留，reports 原样。"""
    _seed_day()
    real_execute = psycopg.Cursor.execute

    def _query_of(sql):
        return sql if isinstance(sql, str) else (sql.decode() if sql else "")

    def boom(cursor, sql, params=None, *a, **kw):
        if "INSERT INTO qc_releases" in _query_of(sql):
            raise psycopg.errors.AdminShutdown("simulated failure during UPSERT")
        return real_execute(cursor, sql, params, *a, **kw)

    monkeypatch.setattr(psycopg.Cursor, "execute", boom)
    with pytest.raises(psycopg.errors.AdminShutdown):
        verify_and_release(LAB, D, 0, 10)
    monkeypatch.setattr(psycopg.Cursor, "execute", real_execute)

    with dbmod.pool().connection() as conn:
        n_qc = conn.execute("SELECT count(*) AS n FROM qc_releases").fetchone()["n"]
        n_rep = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
        # 无锁残留：同键可立即再次取得
        lock_ok = conn.execute(
            "SELECT pg_try_advisory_xact_lock(qc_scope_lock_key(%s,%s)) AS ok",
            (LAB, D)).fetchone()["ok"]
    assert n_qc == 0 and n_rep == 3 and lock_ok is True
    # 事后正常核验成功
    assert _verify(client, 0, 10).json()["status"] == "released"


# ------------------------ 与在线唯一索引构建共存 ------------------------

def test_qc_works_during_concurrent_index_build(client):
    """在线建索引（CONCURRENTLY）期间：补录照常、质控核验与供数正常。"""
    _insert([
        ("LAB-IX", f"R{i}", i,
         datetime(2026, 11, 18, 8, tzinfo=timezone.utc))
        for i in range(10)
    ])
    build = client.post("/index/build")
    assert build.status_code == 202
    # 不等待构建结束，立即核验与补录
    r = client.post("/qc/verify", json={
        "lab_no": "LAB-IX", "report_date": D.isoformat(),
        "lower_bound": 0, "upper_bound": 9}).json()
    assert r["status"] == "released" and r["checked_report_count"] == 10
    ins = client.post("/reports",
                      json={"lab_no": "LAB-IX", "report_no": "RLIVE", "measured": 5})
    assert ins.status_code == 201
    # 新补录在已放行作用域 -> 触发器即时撤销，供数归零
    assert client.get("/reports/supply",
                      params={"lab_no": "LAB-IX"}).json()["total"] == 0
    # 重新核验（含新行，范围内）恢复放行
    r2 = client.post("/qc/verify", json={
        "lab_no": "LAB-IX", "report_date": D.isoformat(),
        "lower_bound": 0, "upper_bound": 10}).json()
    assert r2["status"] == "released" and r2["checked_report_count"] == 11

    from conftest import poll_status
    final = poll_status(client, "complete", timeout_s=60)
    assert final["index"]["is_valid"] is True
