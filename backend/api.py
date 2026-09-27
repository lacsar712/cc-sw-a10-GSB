import asyncio
import os
from datetime import datetime, timedelta, timezone

import psycopg
from jose import JWTError, jwt
from litestar import Litestar, Request, get, post, put
from litestar.exceptions import HTTPException
from litestar.status_codes import HTTP_401_UNAUTHORIZED, HTTP_403_FORBIDDEN
from passlib.context import CryptContext
from psycopg.rows import dict_row
from pydantic import BaseModel

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
SECRET = os.environ.get("JWT_SECRET", "spectrum-dev-secret")
pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
USERS = {
    "calibrator": {"role": "writer", "password_hash": pwd.hash("calib123456")},
    "inspector": {"role": "reader", "password_hash": pwd.hash("insp123456")},
}

# 领取心跳超时：默认 30 秒，可在超时台由校准员调整
DEFAULT_CLAIM_TIMEOUT_SECONDS = 30.0
MIN_CLAIM_TIMEOUT_SECONDS = 0.5
MAX_CLAIM_TIMEOUT_SECONDS = 3600.0
REAPER_INTERVAL_SECONDS = 0.5

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
    created_at timestamptz NOT NULL
);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_by text NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claim_deadline timestamptz;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS claim_timeout_seconds double precision NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS settings (
    key text PRIMARY KEY,
    value text NOT NULL
);
CREATE TABLE IF NOT EXISTS reclaims (
    id serial PRIMARY KEY,
    job_id integer NOT NULL,
    lamp text NOT NULL,
    claimed_by text NOT NULL DEFAULT '',
    claimed_at timestamptz,
    reclaimed_at timestamptz NOT NULL,
    timeout_seconds double precision NOT NULL
);
"""


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def get_timeout_seconds(conn) -> float:
    row = conn.execute("SELECT value FROM settings WHERE key='claim_timeout_seconds'").fetchone()
    if not row:
        return DEFAULT_CLAIM_TIMEOUT_SECONDS
    try:
        return float(row["value"])
    except (TypeError, ValueError):
        return DEFAULT_CLAIM_TIMEOUT_SECONDS


class LoginIn(BaseModel):
    username: str
    password: str


class JobIn(BaseModel):
    lamp: str
    nominal_nm: float
    measured_nm: float


class TimeoutIn(BaseModel):
    seconds: float


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


@get("/api/jobs")
async def list_jobs(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs ORDER BY id DESC"
        ).fetchall()
        return list(rows)


@get("/api/jobs/{job_id:int}")
async def get_job(request: Request, job_id: int) -> dict:
    user_from_request(request)
    with connect() as conn:
        row = conn.execute(
            "SELECT id, lamp, nominal_nm, measured_nm, status, verdict, reason, created_by FROM jobs WHERE id = %s",
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
            INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
            VALUES (%s,%s,%s,'pending','','',%s,%s) RETURNING id
            """,
            (data.lamp.strip(), data.nominal_nm, data.measured_nm, user["username"], datetime.now(timezone.utc)),
        ).fetchone()
        conn.commit()
        return {"id": row["id"], "status": "pending"}


@get("/api/settings/claim-timeout")
async def get_claim_timeout(request: Request) -> dict:
    user_from_request(request)
    with connect() as conn:
        return {
            "seconds": get_timeout_seconds(conn),
            "min": MIN_CLAIM_TIMEOUT_SECONDS,
            "max": MAX_CLAIM_TIMEOUT_SECONDS,
        }


@put("/api/settings/claim-timeout")
async def put_claim_timeout(request: Request, data: TimeoutIn) -> dict:
    user = user_from_request(request)
    if user["role"] != "writer":
        raise HTTPException(status_code=HTTP_403_FORBIDDEN, detail="仅校准员可改秒数")
    if not (MIN_CLAIM_TIMEOUT_SECONDS <= data.seconds <= MAX_CLAIM_TIMEOUT_SECONDS):
        raise HTTPException(
            status_code=400,
            detail=f"秒数需在 {MIN_CLAIM_TIMEOUT_SECONDS}~{MAX_CLAIM_TIMEOUT_SECONDS} 之间",
        )
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO settings(key, value) VALUES ('claim_timeout_seconds', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """,
            (repr(data.seconds),),
        )
        conn.commit()
    # 只约束之后新进领取态的单：已被领取的单沿用领取时的快照，不受影响
    return {"seconds": data.seconds}


@get("/api/claims/active")
async def list_active_claims(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, lamp, claimed_by, claimed_at, claim_deadline, claim_timeout_seconds,
                   EXTRACT(EPOCH FROM (claim_deadline - now()))::float AS remaining_seconds
            FROM jobs
            WHERE status = 'claimed'
            ORDER BY id
            """
        ).fetchall()
        return list(rows)


@get("/api/reclaims")
async def list_reclaims(request: Request) -> list:
    user_from_request(request)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, job_id, lamp, claimed_by, claimed_at, reclaimed_at, timeout_seconds
            FROM reclaims
            ORDER BY id DESC
            LIMIT 200
            """
        ).fetchall()
        return list(rows)


def reclaim_expired_once() -> int:
    """把超时仍无结论的领取态单退回待处理，并记一条回收流水。返回回收条数。"""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, lamp, claimed_by, claimed_at, claim_timeout_seconds
            FROM jobs
            WHERE status = 'claimed' AND claim_deadline < now()
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            """
        ).fetchall()
        for r in rows:
            conn.execute(
                """
                UPDATE jobs
                SET status='pending', claimed_by='', claimed_at=NULL,
                    claim_deadline=NULL, claim_timeout_seconds=0
                WHERE id=%s
                """,
                (r["id"],),
            )
            conn.execute(
                """
                INSERT INTO reclaims(job_id, lamp, claimed_by, claimed_at, reclaimed_at, timeout_seconds)
                VALUES (%s,%s,%s,%s,now(),%s)
                """,
                (r["id"], r["lamp"], r["claimed_by"], r["claimed_at"], r["claim_timeout_seconds"]),
            )
        conn.commit()
        return len(rows)


_reaper_task = None


async def reaper_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(reclaim_expired_once)
        except Exception as exc:
            print("reaper err", exc, flush=True)
        await asyncio.sleep(REAPER_INTERVAL_SECONDS)


async def start_reaper() -> None:
    global _reaper_task
    _reaper_task = asyncio.create_task(reaper_loop())


async def stop_reaper() -> None:
    if _reaper_task:
        _reaper_task.cancel()


def on_startup() -> None:
    with connect() as conn:
        conn.execute(SCHEMA)
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('claim_timeout_seconds', %s) ON CONFLICT (key) DO NOTHING",
            (repr(DEFAULT_CLAIM_TIMEOUT_SECONDS),),
        )
        n = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if n == 0:
            now = datetime.now(timezone.utc)
            conn.execute(
                """
                INSERT INTO jobs(lamp, nominal_nm, measured_nm, status, verdict, reason, created_by, created_at)
                VALUES
                ('氦灯-587', 587.56, 587.50, 'done', '合格', '偏差 0.0600 nm 在允差内', 'seed', %s),
                ('汞灯-546', 546.07, 546.30, 'done', '超差', '偏差 0.2300 nm 超过允差 0.08', 'seed', %s)
                """,
                (now, now),
            )
        conn.commit()


app = Litestar(
    route_handlers=[
        health,
        login,
        list_jobs,
        get_job,
        create_job,
        get_claim_timeout,
        put_claim_timeout,
        list_active_claims,
        list_reclaims,
    ],
    on_startup=[on_startup, start_reaper],
    on_shutdown=[stop_reaper],
)
