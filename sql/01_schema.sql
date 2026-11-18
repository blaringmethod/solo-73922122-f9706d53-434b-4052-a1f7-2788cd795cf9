-- 检验数仓报告表、索引构建轮次表、质控放行表
-- 幂等脚本，容器首次启动时由 PostgreSQL 自动执行（/docker-entrypoint-initdb.d）；
-- API 每次启动也会以幂等方式重放本脚本（见 app/db.ensure_schema），因此可安全升级。

BEGIN;

-- 1) 报告主表
CREATE TABLE IF NOT EXISTS reports (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 主键流水号
    lab_no       TEXT NOT NULL,                                     -- 实验室号（非空）
    report_no    TEXT NOT NULL,                                     -- 实验室内报告号（非空）
    measured     INTEGER NOT NULL,                                  -- 整数测值
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  reports            IS '持续补录的检验报告表';
COMMENT ON COLUMN reports.id         IS '主键流水号';
COMMENT ON COLUMN reports.lab_no     IS '实验室号';
COMMENT ON COLUMN reports.report_no  IS '实验室内报告号';
COMMENT ON COLUMN reports.measured   IS '整数测值';
COMMENT ON COLUMN reports.created_at IS '入库时间（TIMESTAMPTZ）；质控按 UTC 日期核对';

-- 补录期间的普通辅助索引（非唯一），仅用于按实验室号/报告号查询与去重；
-- 唯一索引构建成功前，唯一性由补录 SQL 自行保证。
CREATE INDEX IF NOT EXISTS idx_reports_lab_report
    ON reports (lab_no, report_no);

-- 质控核对/供数按 (实验室号, UTC 日期) 过滤的表达式索引。
-- created_at AT TIME ZONE 'UTC' 把 timestamptz 归一为 UTC 时刻的 timestamp，
-- 再取 ::date 即“该报告所属的 UTC 日历日”。
CREATE INDEX IF NOT EXISTS idx_reports_lab_utc_date
    ON reports (lab_no, ((created_at AT TIME ZONE 'UTC')::date));

-- 2) 唯一索引构建轮次表（状态持久化，API 重启后据此接续跟踪）
CREATE TABLE IF NOT EXISTS index_build_rounds (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id    UUID NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN (
                    'running',        -- 构建中（含等待长事务阶段）
                    'complete',       -- 索引已有效
                    'failed',         -- 构建失败（如重复键），原数据保留，无效索引已清理
                    'cancel_requested', -- 已受理取消，等待后端 SQL 真正停止
                    'cancelled'       -- 后端 SQL 已确认停止且索引无效
                )),
    backend_pid INTEGER,                       -- 执行 CREATE INDEX 的 PostgreSQL 后端 PID
    index_name  TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    error       TEXT,                         -- 失败原因（如重复键明细）
    failure_type TEXT,                        -- duplicate_key / lock_timeout / other / cancelled
    restarted   BOOLEAN NOT NULL DEFAULT FALSE -- 是否为 API 重启后接续跟踪的轮次
);

COMMENT ON TABLE index_build_rounds IS 'CREATE UNIQUE INDEX CONCURRENTLY 构建轮次';

-- 同一时刻只允许一轮活跃构建（running 或 cancel_requested 各最多一条）。
-- 该约束在数据库层面兜底，即使多个 API 实例并发发起也不会出现两轮并发构建。
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_rounds_one_active
    ON index_build_rounds ((1))
    WHERE status IN ('running', 'cancel_requested');

-- 3) 质控放行表：每个“实验室号 + UTC 日期”作用域至多一条当前记录（UPSERT 覆盖）。
--    released = 当前放行；revoked = 当前撤销（含越界、无报告、被独立 SQL 改动牵连）。
CREATE TABLE IF NOT EXISTS qc_releases (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    lab_no             TEXT NOT NULL,                          -- 实验室号
    report_date        DATE NOT NULL,                          -- 核对的 UTC 日历日
    lower_bound        INTEGER NOT NULL,                       -- 测值下限（含）
    upper_bound        INTEGER NOT NULL,                       -- 测值上限（含）
    status             TEXT NOT NULL CHECK (status IN ('released', 'revoked')),
    checked_report_count INTEGER NOT NULL DEFAULT 0,          -- 本次核验覆盖的原始行数
    violation_count    INTEGER NOT NULL DEFAULT 0,            -- 越界行数
    violation_ids      BIGINT[] NOT NULL DEFAULT '{}',        -- 越界报告 id（有序）
    revoke_reason      TEXT,                                  -- 撤销原因
    checked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),    -- 本次核验时间
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_qc_releases_scope UNIQUE (lab_no, report_date)
);

COMMENT ON TABLE qc_releases IS '质控放行：按 (lab_no, report_date UTC) 记录当前放行/撤销状态';
COMMENT ON COLUMN qc_releases.status IS 'released=当前放行（供数可见）；revoked=当前撤销';
COMMENT ON COLUMN qc_releases.violation_ids IS '越界报告 id 列表；无报告或被独立 SQL 撤销时为空';

CREATE INDEX IF NOT EXISTS idx_qc_releases_status
    ON qc_releases (status, lab_no, report_date);

COMMIT;

-- 4) 作用域咨询锁键：同一 (实验室号, UTC 日期) 的“核验”与“报告增改删”串行化。
--    咨询锁为事务级（pg_advisory_xact_lock），事务回滚/提交即自动释放，
--    因此 ROLLBACK 不可能留下锁，也不可能把撤销“带出来”。
CREATE OR REPLACE FUNCTION qc_scope_lock_key(p_lab_no TEXT, p_report_date DATE)
RETURNS BIGINT
LANGUAGE sql IMMUTABLE PARALLEL SAFE
-- hashtextextended 为 64 位哈希，碰撞概率可忽略；即便碰撞也只是多等待，
-- 供数过滤仍以 qc_releases 真实行状态为准，不会错误放行。
RETURN hashtextextended(p_lab_no || '|' || p_report_date::text, 0);

-- 5) 独立 SQL 对 reports 增/改/删（含改实验室号或日期）时，随原事务撤销受影响作用域的放行。
--    * 语句级触发器 + 过渡表：一条批量语句只对去重后的作用域各撤销一次；
--    * 先取同一把事务级咨询锁，与 API 核验互斥，杜绝“核验漏看未检查行”的交错；
--    * 取锁用 pg_try_advisory_xact_lock + 有界重试（而非无限阻塞等待）：
--      正常环境下与阻塞取锁语义等价；在禁止进程信号的受限环境里，锁等待者
--      无法被信号唤醒，try-lock 轮询保证写事务仍可在短暂等待后取得锁，
--      不会因环境限制而挂起；最终等待窗口耗尽则明确报错并整体回滚，
--      绝不跳过锁、绝不让未串行化的修改提交；
--    * 撤销在触发它的同一事务内完成并随其提交生效；该事务回滚则撤销一并回滚；
--    * 仅更新 status='released' 的行，重复触发无副作用。
CREATE OR REPLACE FUNCTION qc_acquire_scope_lock(p_lab_no TEXT, p_report_date DATE)
RETURNS VOID
LANGUAGE plpgsql AS $$
DECLARE
    -- 等待窗口 30s、每 50ms 试一次，覆盖正常的核验事务时长。
    v_deadline CONSTANT TIMESTAMPTZ := clock_timestamp() + INTERVAL '30 seconds';
    v_got BOOLEAN;
BEGIN
    LOOP
        SELECT pg_try_advisory_xact_lock(
                   qc_scope_lock_key(p_lab_no, p_report_date)) INTO v_got;
        EXIT WHEN v_got;
        IF clock_timestamp() >= v_deadline THEN
            RAISE EXCEPTION
                'qc scope lock for (lab=%, date=%) unavailable within wait window; '
                'transaction rolled back to preserve release integrity',
                p_lab_no, p_report_date
                USING ERRCODE = 'lock_not_available';
        END IF;
        PERFORM pg_sleep(0.05);
    END LOOP;
END;
$$;

CREATE OR REPLACE FUNCTION qc_revoke_affected() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    scope RECORD;
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        -- TRUNCATE 无过渡表；全表清空会使所有已放行作用域失去依据，全部撤销。
        UPDATE qc_releases
           SET status = 'revoked',
               revoke_reason = 'revoked by reports TRUNCATE trigger (same transaction)',
               checked_at = now()
         WHERE status = 'released';
        RETURN NULL;
    END IF;

    IF TG_OP = 'INSERT' THEN
        FOR scope IN
            SELECT DISTINCT lab_no,
                   (created_at AT TIME ZONE 'UTC')::date AS report_date
              FROM new_rows
             ORDER BY lab_no, report_date
        LOOP
            PERFORM qc_acquire_scope_lock(scope.lab_no, scope.report_date);
            UPDATE qc_releases
               SET status = 'revoked',
                   revoke_reason = 'revoked by reports INSERT trigger (same transaction)',
                   checked_at = now()
             WHERE lab_no = scope.lab_no
               AND report_date = scope.report_date
               AND status = 'released';
        END LOOP;

    ELSIF TG_OP = 'DELETE' THEN
        FOR scope IN
            SELECT DISTINCT lab_no,
                   (created_at AT TIME ZONE 'UTC')::date AS report_date
              FROM old_rows
             ORDER BY lab_no, report_date
        LOOP
            PERFORM qc_acquire_scope_lock(scope.lab_no, scope.report_date);
            UPDATE qc_releases
               SET status = 'revoked',
                   revoke_reason = 'revoked by reports DELETE trigger (same transaction)',
                   checked_at = now()
             WHERE lab_no = scope.lab_no
               AND report_date = scope.report_date
               AND status = 'released';
        END LOOP;

    ELSIF TG_OP = 'UPDATE' THEN
        -- 改实验室号或日期会同时影响“旧作用域”和“新作用域”，两者都要撤销。
        FOR scope IN
            SELECT DISTINCT lab_no, report_date
              FROM (
                  SELECT lab_no,
                         (created_at AT TIME ZONE 'UTC')::date AS report_date
                    FROM new_rows
                  UNION
                  SELECT lab_no,
                         (created_at AT TIME ZONE 'UTC')::date AS report_date
                    FROM old_rows
              ) affected
             ORDER BY lab_no, report_date
        LOOP
            PERFORM qc_acquire_scope_lock(scope.lab_no, scope.report_date);
            UPDATE qc_releases
               SET status = 'revoked',
                   revoke_reason = 'revoked by reports UPDATE trigger (same transaction)',
                   checked_at = now()
             WHERE lab_no = scope.lab_no
               AND report_date = scope.report_date
               AND status = 'released';
        END LOOP;
    END IF;
    RETURN NULL;
END;
$$;

-- 过渡表名按语句类型分别声明（INSERT 无 OLD TABLE、DELETE 无 NEW TABLE）。
DROP TRIGGER IF EXISTS trg_reports_qc_revoke_ins ON reports;
CREATE TRIGGER trg_reports_qc_revoke_ins
    AFTER INSERT ON reports
    REFERENCING NEW TABLE AS new_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION qc_revoke_affected();

DROP TRIGGER IF EXISTS trg_reports_qc_revoke_upd ON reports;
CREATE TRIGGER trg_reports_qc_revoke_upd
    AFTER UPDATE ON reports
    REFERENCING NEW TABLE AS new_rows OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION qc_revoke_affected();

DROP TRIGGER IF EXISTS trg_reports_qc_revoke_del ON reports;
CREATE TRIGGER trg_reports_qc_revoke_del
    AFTER DELETE ON reports
    REFERENCING OLD TABLE AS old_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION qc_revoke_affected();

DROP TRIGGER IF EXISTS trg_reports_qc_revoke_truncate ON reports;
CREATE TRIGGER trg_reports_qc_revoke_truncate
    AFTER TRUNCATE ON reports
    FOR EACH STATEMENT
    EXECUTE FUNCTION qc_revoke_affected();
