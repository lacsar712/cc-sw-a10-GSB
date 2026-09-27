import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post, put
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
DEFAULT_TIMEOUT_SECONDS = 30
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 3600
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id serial PRIMARY KEY,
    lamp text NOT NULL,
    nominal_nm double precision NOT NULL,
    measured_nm double precision NOT NULL,
    status text NOT NULL,
    verdict text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz,
    heartbeat_at timestamptz,
    timeout_seconds integer,
    simulate_stall boolean NOT NULL DEFAULT false,
    stalled_until timestamptz
);
CREATE TABLE IF NOT EXISTS app_settings (
    id integer PRIMARY KEY DEFAULT 1,
    claim_timeout_seconds integer NOT NULL,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL,
    CONSTRAINT settings_singleton CHECK (id = 1)
);
CREATE TABLE IF NOT EXISTS recycle_log (
    id serial PRIMARY KEY,
    job_id integer NOT NULL,
    lamp text NOT NULL DEFAULT '',
    timeout_seconds integer,
    claimed_at timestamptz,
    recycled_at timestamptz NOT NULL,
    reason text NOT NULL DEFAULT '',
    forced_by text NOT NULL DEFAULT ''
);
"""

JOB_COLUMNS = (
    "id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, "
    "claimed_at, heartbeat_at, timeout_seconds, simulate_stall"
)


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def now() -> datetime:
    return datetime.now(timezone.utc)


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float
    simulate_stall: bool = False


class SettingsIn(BaseModel):
    claim_timeout_seconds: int = Field(ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)


def user_from_request(request: Request) -> dict:
    auth = request.headers.get("Authorization") or ""
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="未登录")
    try:
        payload = jwt.decode(auth[7:], SECRET, algorithms=["HS256"])
    except JWTError as exc:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌") from exc
    if payload.get("sub") not in USERS:
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="无效令牌")
    return {"username": payload["sub"], "role": payload.get("role")}


def get_settings(conn) -> dict:
    row = conn.execute(
        "SELECT claim_timeout_seconds, updated_by, updated_at FROM app_settings WHERE id=1"
    ).fetchone()
    if not row:
        ts = now()
        conn.execute(
            "INSERT INTO app_settings(id, claim_timeout_seconds, updated_by, updated_at) "
            "VALUES (1, %s, 'system', %s) ON CONFLICT (id) DO NOTHING",
            (DEFAULT_TIMEOUT_SECONDS, ts),
        )
        row = conn.execute(
            "SELECT claim_timeout_seconds, updated_by, updated_at FROM app_settings WHERE id=1"
        ).fetchone()
    return dict(row)


@get("/api/health")
async def health() -> dict:
    return {"status": "ok", "service": "spectrum-wavelength-desk"}


@post("/api/login")
async def login(data: LoginIn) -> dict:
    u = USERS.get(data.username)
    if not u or not pwd.verify(data.password, u["password_hash"]):
        raise HTTPException(status_code=HTTP_401_UNAUTHORIZED, detail="账号或密码错误")
    token = jwt.encode(
        {
            "sub": data.username,
            "role": u["role"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=12),
        },
        SECRET,
        algorithm="HS256",
    )
    return {"access_token": token, "role": u["role"], "username": data.username}


@get("/api/settings")
async def read_settings(request: Request) -> dict:
    user_from_request(request)
    with connect() as conn:
        return get_settings(conn)


@put("/api/settings")
async def update_settings(request: Request, data: SettingsIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可设置超时秒数")
    with connect() as conn:
        get_settings(conn)
        conn.execute(
            "UPDATE app_settings SET claim_timeout_seconds=%s, updated_by=%s, updated_at=%s WHERE id=1",
            (data.claim_timeout_seconds, user["username"], now()),
        )
        conn.commit()
        return get_settings(conn)


@get("/api/recycle-log")
async def list_recycle_log(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, job_id, lamp, timeout_seconds, claimed_at, recycled_at, reason, forced_by "
            "FROM recycle_log ORDER BY id DESC LIMIT 200"
        ).fetchall()
        return list(rows)


@get("/api/jobs")
async def list_jobs(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(f"SELECT {JOB_COLUMNS} FROM jobs ORDER BY id DESC").fetchall()
        return list(rows)


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(
            f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="任务不存在")
        return dict(row)


@post("/api/jobs")
async def create_job(request: Request, data: JobIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可提交")
    with connect() as conn:
        row = conn.execute(
            """
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason,
                             created_by, created_at, simulate_stall)
            VALUES (%s,%s,%s,'pending','','',%s,%s,%s) RETURNING id
            """,
            (
                data.lamp.strip(),
                data.nominal_nm,
                data.measured_nm,
                user["username"],
                now(),
                data.simulate_stall,
            ),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "status": "pending"}


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        # 兼容既有库：补齐基线版本缺失的列
        existing = {r["column_name"] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='jobs'"
        ).fetchall()}
        if "claimed_at" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN claimed_at timestamptz")
        if "heartbeat_at" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN heartbeat_at timestamptz")
        if "timeout_seconds" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN timeout_seconds integer")
        if "simulate_stall" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN simulate_stall boolean NOT NULL DEFAULT false")
        if "stalled_until" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN stalled_until timestamptz")
        get_settings(conn)
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            ts = now()
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason,
                                 created_by, created_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s)
                """,
                (ts, ts),
            )
        conn.commit()


app = Litestar(
    route_handlers=[
        health,
        login,
        read_settings,
        update_settings,
        list_recycle_log,
        list_jobs,
        get_job,
        create_job,
    ],
    on_startup=[on_startup],
)
