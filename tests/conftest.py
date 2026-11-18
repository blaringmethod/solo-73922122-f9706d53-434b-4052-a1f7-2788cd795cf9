"""测试夹具：指向本地 PostgreSQL（环境变量 DATABASE_URL），每轮测试清库。"""
from __future__ import annotations

import os
import time
import uuid

import httpx
import psycopg
import pytest
from a2wsgi import ASGIMiddleware

from app import db as dbmod
from app.config import settings
from app.main import app
from app.service import service

# 每次 pytest 进程使用唯一实验室号后缀：受限沙箱无法 kill 残留后端，
# 唯一作用域键可避免与历史进程遗留的事务级咨询锁相撞。
QC_RUN_SUFFIX = uuid.uuid4().hex[:8]
os.environ["QC_RUN_SUFFIX"] = QC_RUN_SUFFIX


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


def _fresh_database() -> str:
    """尽量为本次测试创建独立 database；环境受限（如 seccomp 禁止信号，
    DROP DATABASE 的检查点信号失败）时回退到共享库 + 每用例重建 public schema。

    两种路径都保证每个用例从空库开始；正常 Docker/CI 环境走独立 database 路径。
    """
    base_url = os.getenv(
        "DATABASE_URL", "postgresql://postgres@127.0.0.1:55432/postgres"
    )
    test_db = f"test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(base_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {test_db}")
            admin.execute(f"CREATE DATABASE {test_db}")
    except psycopg.errors.InternalError as exc:
        # 受限沙箱：DROP/CREATE 后的检查点信号被拒绝。回退共享库模式，
        # 由 client 夹具在每个用例前后重建 public schema 清库。
        print(f"[conftest] isolated database unavailable ({exc}); "
              f"falling back to schema reset on {base_url}")
        return base_url
    # 替换连接串中的库名（支持百分号编码的 unix socket host）。
    prefix, _old = base_url.rsplit("/", 1)
    return f"{prefix}/{test_db}"


def _reset_schema(url: str) -> None:
    """回退路径：重建 public schema，等价于空库。"""
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO public")


@pytest.fixture()
def client(monkeypatch):
    original_env_url = os.getenv("DATABASE_URL")
    test_url = _fresh_database()
    monkeypatch.setenv("DATABASE_URL", test_url)
    # settings 是 frozen dataclass，直接改对象属性。
    object.__setattr__(settings, "database_url", test_url)
    dbmod.close_pool()
    # 共享库回退路径：先清掉上一个用例的残留对象。
    if test_url == original_env_url:
        _reset_schema(test_url)
    dbmod.init_pool()
    dbmod.ensure_schema()
    # 复位服务内线程状态。
    with service._lock:
        service._thread = None
        service._watching_round_id = None

    # httpx 0.28 的 ASGITransport 仅支持异步；用 WSGI 适配在同步测试中驱动 ASGI 应用。
    c = httpx.Client(transport=httpx.WSGITransport(app=ASGIMiddleware(app)),
                     base_url="http://test")
    # 手动触发 startup（ASGITransport 不跑 lifespan）。
    service.reconcile_on_startup()
    yield c
    c.close()
    dbmod.close_pool()
    if test_url == original_env_url:
        _reset_schema(test_url)


def poll_status(client: httpx.Client, want: str, timeout_s: float = 45) -> dict:
    deadline = time.time() + timeout_s
    last = {}
    while time.time() < deadline:
        last = client.get("/index/status").json()
        if last.get("status") == want:
            return last
        time.sleep(0.5)
    raise AssertionError(f"status never reached {want}; last={last}")
