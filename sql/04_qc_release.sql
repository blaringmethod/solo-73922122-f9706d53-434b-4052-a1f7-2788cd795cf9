-- ============================================================
-- 质控放行联动：独立 SQL 对 reports 增/改/删时的撤销语义（可直接在 psql 执行）
--
-- 机制（由 sql/01_schema.sql 自动部署，无需手工创建）：
--   * 表 reports 上的语句级触发器 trg_reports_qc_revoke_ins/upd/del/truncate
--     在每条增改删语句之后，于“同一事务”内把受影响作用域
--     (lab_no, created_at 的 UTC 日期) 的 qc_releases.released 行改为 revoked；
--   * 触发器先取与 API 核验相同的事务级咨询锁
--     pg_advisory_xact_lock(qc_scope_lock_key(lab_no, date))，与核验互斥；
--   * 撤销随该事务的 COMMIT 生效；ROLLBACK 则撤销一并回滚，放行保持不变；
--   * 改实验室号或 created_at（日期）时，旧作用域与新作用域都会被撤销；
--   * TRUNCATE reports 会撤销全部已放行作用域。
--
-- 本文件仅演示与核对，不修改任何数据（演示事务均以 ROLLBACK 收尾）。
-- ============================================================

-- 0) 查看当前放行状态（只读）。
SELECT lab_no, report_date, status, lower_bound, upper_bound,
       checked_report_count, violation_ids, revoke_reason, checked_at
FROM   qc_releases
ORDER  BY report_date DESC, lab_no;

-- 1) 独立 INSERT：提交后该作用域放行被撤销；回滚则放行不变。
BEGIN;
INSERT INTO reports (lab_no, report_no, measured, created_at)
VALUES ('LAB-001', 'RPT-2026-0999', 1024, TIMESTAMPTZ '2026-11-18 08:00:00+00');

-- 事务内即可看到该作用域已被标记撤销（与本语句同事务）。
SELECT lab_no, report_date, status, revoke_reason
FROM   qc_releases
WHERE  lab_no = 'LAB-001'
  AND  report_date = DATE '2026-11-18';
ROLLBACK;   -- 演示回滚：换成 COMMIT 才会真正生效；回滚后放行恢复为 released。

-- 2) 独立 UPDATE 改实验室号/日期：新旧两个作用域的放行都被撤销。
BEGIN;
UPDATE reports
   SET lab_no = 'LAB-002',
       created_at = TIMESTAMPTZ '2026-11-19 09:30:00+00'
 WHERE  lab_no = 'LAB-001'
   AND  report_no = 'RPT-2026-0001';

SELECT lab_no, report_date, status, revoke_reason
FROM   qc_releases
WHERE  (lab_no IN ('LAB-001', 'LAB-002'))
  AND  report_date IN (DATE '2026-11-18', DATE '2026-11-19');
ROLLBACK;

-- 3) 独立 DELETE：被删行所属作用域的放行被撤销（防止“删掉坏行后仍沿用旧放行”）。
BEGIN;
DELETE FROM reports
WHERE  lab_no = 'LAB-001'
  AND  (created_at AT TIME ZONE 'UTC')::date = DATE '2026-11-18';

SELECT lab_no, report_date, status, revoke_reason
FROM   qc_releases
WHERE  lab_no = 'LAB-001'
  AND  report_date = DATE '2026-11-18';
ROLLBACK;

-- 4) TRUNCATE：撤销所有当前放行（同样随事务提交/回滚）。
BEGIN;
TRUNCATE reports;
SELECT count(*) AS released_after_truncate
FROM   qc_releases
WHERE  status = 'released';   -- 事务内应为 0
ROLLBACK;

-- 5) 撤销后恢复放行：数据整改完毕后，重新调用核验接口按当前范围重核即可：
--    POST /qc/verify {"lab_no":"LAB-001","report_date":"2026-11-18",
--                     "lower_bound":0,"upper_bound":1000}
--    全部合格时状态重新变为 released，供数接口 /reports/supply 恢复可见。

-- 6) 核验与补录为何不会放行未检查行（只读解释，可手工查锁）：
--    两者都要先取 pg_advisory_xact_lock(qc_scope_lock_key(lab_no, date))：
SELECT qc_scope_lock_key('LAB-001', DATE '2026-11-18') AS scope_lock_key;
--    谁先持锁，另一方便等待；先提交方的结论对后提交方必然可见，
--    因此“核验通过但漏看并发新行”的交错在该作用域上不可能发生。
