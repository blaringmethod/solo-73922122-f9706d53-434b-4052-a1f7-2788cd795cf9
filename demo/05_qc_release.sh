#!/usr/bin/env bash
# 演示 5：质控放行全链路。
#
# 覆盖：
#   1) 全量合格 -> released，供数接口 /reports/supply 仅返回放行作用域的报告；
#   2) 收紧范围重核 -> 越界撤销并返回越界报告 id；无报告作用域同样撤销；
#   3) 独立 SQL 对 reports 增/改/删（含改实验室号、改日期）随原事务撤销放行，
#      ROLLBACK 不能撤销（放行保持）；
#   4) 撤销后数据整改、重新核验恢复放行；
#   5) 并发核验与补录的关键交错：补录在核验持锁期间等待，提交后触发器撤销，
#      供数接口从不出现未经检查的行；
#   6) 在线建索引（CONCURRENTLY）期间补录照常提交、核验照常进行。
set -euo pipefail
cd "$(dirname "$0")"
source ./common.sh

DAY="2026-11-18"
LAB="DEMO-LAB"
verify() {  # verify <下限> <上限>
  c -X POST "$API_BASE/qc/verify" -d \
    "{\"lab_no\":\"$LAB\",\"report_date\":\"$DAY\",\"lower_bound\":$1,\"upper_bound\":$2}"
}

echo "== 1. 清理并补录当日报告（R1=5, R2=8；另有次日 R3=99） =="
psql_exec -c "TRUNCATE reports RESTART IDENTITY;" \
          -c "DELETE FROM qc_releases;" >/dev/null
psql_exec -c "
INSERT INTO reports(lab_no, report_no, measured, created_at) VALUES
 ('$LAB','R1',5, TIMESTAMPTZ '$DAY 00:30:00+00'),
 ('$LAB','R2',8, TIMESTAMPTZ '$DAY 23:59:00+00'),
 ('$LAB','R3',99,TIMESTAMPTZ '2026-11-19 00:00:00+00');"

echo "== 2. 按 [0,10] 核验：全部合格 -> released =="
verify 0 10 | python3 -m json.tool
echo "-- 供数接口只返回放行作用域（当日 2 行，次日 R3 不可见）："
c "$API_BASE/reports/supply" | python3 -m json.tool

echo "== 3. 收紧为 [6,10] 重核：R1 越界 -> revoked，返回越界报告 id，供数归零 =="
verify 6 10 | python3 -m json.tool
c "$API_BASE/reports/supply"

echo "== 4. 无报告的作用域核验：同样撤销（不允许对空作用域放行） =="
c -X POST "$API_BASE/qc/verify" -d \
  "{\"lab_no\":\"NO-SUCH-LAB\",\"report_date\":\"$DAY\",\"lower_bound\":0,\"upper_bound\":10}" \
  | python3 -m json.tool

echo "== 5. 放宽回 [0,10] 重核：恢复放行 =="
verify 0 10 | python3 -m json.tool

echo "== 6. 独立 SQL INSERT 后放行随原事务撤销；ROLLBACK 不撤销 =="
psql_exec -c "
BEGIN;
INSERT INTO reports(lab_no,report_no,measured,created_at)
VALUES ('$LAB','R9',1,TIMESTAMPTZ '$DAY 12:00:00+00');
SELECT lab_no,report_date,status,revoke_reason FROM qc_releases
 WHERE lab_no='$LAB' AND report_date=DATE '$DAY';
COMMIT;"
echo "-- 提交后供数应为 0："
c "$API_BASE/reports/supply"
echo "-- 重新放行后再做一次 INSERT 并 ROLLBACK："
verify 0 100 >/dev/null
psql_exec -c "
BEGIN;
INSERT INTO reports(lab_no,report_no,measured,created_at)
VALUES ('$LAB','RB',1,TIMESTAMPTZ '$DAY 13:00:00+00');
ROLLBACK;"
psql_exec -c "SELECT status FROM qc_releases WHERE lab_no='$LAB' AND report_date=DATE '$DAY';"

echo "== 7. 独立 SQL 改实验室号 / 改日期：受影响的新旧作用域都撤销 =="
psql_exec -c "UPDATE reports SET lab_no='MOVED-LAB' WHERE report_no='R2';"
psql_exec -c "SELECT lab_no,report_date,status,revoke_reason FROM qc_releases WHERE status='revoked';"
verify 0 100 >/dev/null
psql_exec -c "UPDATE reports SET created_at=TIMESTAMPTZ '2026-11-20 09:00:00+00' WHERE report_no='R1';"
psql_exec -c "SELECT status FROM qc_releases WHERE lab_no='$LAB' AND report_date=DATE '$DAY';"

echo "== 8. 关键交错：核验事务持锁期间，新核验有界等待并返回 409，供数始终为 0 =="
psql_exec -c "TRUNCATE reports RESTART IDENTITY;" -c "DELETE FROM qc_releases;" >/dev/null
psql_exec -c "
INSERT INTO reports(lab_no,report_no,measured,created_at)
VALUES ('$LAB','R1',5,TIMESTAMPTZ '$DAY 08:00:00+00');"
# 用独立助手事务持有该作用域的咨询锁（与 API 核验、reports 触发器同一把锁）。
SIG_DIR="$(mktemp -d)"
python3 ./qc_lock_holder.py "$PSQL_URL" "$SIG_DIR" "$LAB" "$DAY" </dev/null &
HOLDER_PID=$!
for _ in $(seq 1 20); do [[ -f "$SIG_DIR/pid" ]] && break; sleep 0.3; done
echo "持锁后端 PID = $(cat "$SIG_DIR/pid")（核验与补录此刻都无法取得该作用域锁）"
echo "-- 持锁期间供数接口始终为 0："
c "$API_BASE/reports/supply"
echo "-- 核验请求在 QC_LOCK_TIMEOUT_MS 窗口内拿不到锁 -> 409 qc_scope_busy（不写半成品）："
c -X POST "$API_BASE/qc/verify" -d \
  "{\"lab_no\":\"$LAB\",\"report_date\":\"$DAY\",\"lower_bound\":0,\"upper_bound\":100}" \
  | python3 -m json.tool
psql_exec -c "SELECT count(*) AS qc_rows_during_busy FROM qc_releases;"
touch "$SIG_DIR/commit"
wait "$HOLDER_PID" || true
# 锁释放后核验成功：此时只有 R1，合格放行。
verify 0 100 | python3 -m json.tool
# 随后独立 SQL 补录 LATE：触发器在同一事务取锁并立即撤销，供数归零。
psql_exec -c "
INSERT INTO reports(lab_no,report_no,measured,created_at)
VALUES ('$LAB','LATE',999,TIMESTAMPTZ '$DAY 09:00:00+00');"
psql_exec -c "SELECT status, revoke_reason FROM qc_releases
              WHERE lab_no='$LAB' AND report_date=DATE '$DAY';"
c "$API_BASE/reports/supply"

echo "== 9. 在线建索引期间补录照常提交，且新行会即时撤销旧放行 =="
c -X POST "$API_BASE/index/build" >/dev/null
sleep 2
c -X POST "$API_BASE/reports" -d \
  "{\"lab_no\":\"$LAB\",\"report_no\":\"LIVE\",\"measured\":7}"
psql_exec -c "SELECT status FROM qc_releases WHERE lab_no='$LAB' AND report_date=DATE '$DAY';"
verify 0 1000 | python3 -m json.tool
wait_status complete 60
psql_exec -c "SELECT relname, indisunique, indisvalid FROM pg_index i
              JOIN pg_class c ON c.oid=i.indexrelid
              WHERE relname='uq_reports_lab_no_report_no';"
echo "DEMO-5 OK"
