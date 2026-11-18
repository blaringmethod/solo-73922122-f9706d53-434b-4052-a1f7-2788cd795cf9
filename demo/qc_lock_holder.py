#!/usr/bin/env python3
"""演示用质控作用域锁持有器（模拟“核验进行中”的事务）。

用法：
  python3 qc_lock_holder.py <DATABASE_URL> <信号目录> <lab_no> <report_date>

行为：
  1. 打开连接并 BEGIN，调用 pg_advisory_xact_lock(qc_scope_lock_key(lab,date))，
     即与 API 核验事务、reports 触发器完全相同的那把事务级咨询锁；
  2. 把后端 PID 写入 <信号目录>/pid；
  3. 等待 <信号目录>/commit 或 <信号目录>/rollback 出现后执行对应动作并退出，
     锁随事务结束自动释放（演示“回滚不能撤销”的同一机制）。

该进程独立于演示脚本，避免 bash coproc 因管道/SIGHUP 提前结束。
"""
import pathlib
import sys
import time

import psycopg


def main() -> None:
    dsn, signal_dir, lab_no, report_date = sys.argv[1:5]
    sig = pathlib.Path(signal_dir)
    sig.mkdir(parents=True, exist_ok=True)
    for name in ("commit", "rollback"):
        p = sig / name
        if p.exists():
            p.unlink()

    conn = psycopg.connect(dsn)
    conn.execute("BEGIN")
    conn.execute(
        "SELECT pg_advisory_xact_lock(qc_scope_lock_key(%s, %s::date))",
        (lab_no, report_date),
    )
    pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
    (sig / "pid").write_text(str(pid))
    sys.stderr.write(
        f"qc scope lock held: lab={lab_no} date={report_date} backend pid={pid}\n"
    )
    sys.stderr.flush()

    deadline = time.time() + 300
    while time.time() < deadline:
        if (sig / "commit").exists():
            conn.commit()
            sys.stderr.write("lock holder committed; lock released\n")
            break
        if (sig / "rollback").exists():
            conn.rollback()
            sys.stderr.write("lock holder rolled back; lock released\n")
            break
        time.sleep(0.2)
    conn.close()


if __name__ == "__main__":
    main()
