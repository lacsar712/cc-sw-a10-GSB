import os
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg.rows import dict_row

from domain import judge

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
REAP_INTERVAL = 0.5
CLAIM_IDLE = 0.4
# 拖住领取时，冻结时长 = 当时快照秒数 + 该宽限；回收后单子会先在待处理区停留这么久
STALL_EXTRA_SECONDS = 8


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def now() -> datetime:
    return datetime.now(timezone.utc)


def wait_for_db():
    while True:
        try:
            with connect() as conn:
                conn.execute("SELECT 1")
            return
        except Exception as exc:
            print("waiting for db:", exc, flush=True)
            time.sleep(1)


def current_timeout(conn) -> int:
    row = conn.execute(
        "SELECT claim_timeout_seconds FROM app_settings WHERE id=1"
    ).fetchone()
    return int(row["claim_timeout_seconds"]) if row else 30


def claim_one(conn):
    """领取一张待处理单：pending -> claimed，并快照当前超时秒数。

    被回收后仍在冻结宽限期内的单不会被别的领取动作拿走，
    要等原进程醒来自行重新领取，从而在待处理区留得住、看得见。
    """
    row = conn.execute(
        """
        SELECT id, lamp, nominal_nm, measured_nm, simulate_stall
        FROM jobs
        WHERE status='pending'
          AND (stalled_until IS NULL OR stalled_until <= now())
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    ts = now()
    timeout = current_timeout(conn)
    stalled_until = (
        ts + timedelta(seconds=timeout + STALL_EXTRA_SECONDS)
        if row["simulate_stall"]
        else None
    )
    conn.execute(
        """
        UPDATE jobs
        SET status='claimed', claimed_at=%s, heartbeat_at=%s,
            timeout_seconds=%s, stalled_until=%s
        WHERE id=%s
        """,
        (ts, ts, timeout, stalled_until, row["id"]),
    )
    conn.commit()
    claim = dict(row)
    claim["timeout_seconds"] = timeout
    return claim


def heartbeat(conn, job_id: int) -> None:
    conn.execute("UPDATE jobs SET heartbeat_at=%s WHERE id=%s AND status='claimed'", (now(), job_id))
    conn.commit()


def finish(conn, job_id: int, nominal: float, measured: float) -> None:
    verdict, reason = judge(nominal, measured)
    conn.execute(
        "UPDATE jobs SET status='done', verdict=%s, reason=%s WHERE id=%s",
        (verdict, reason, job_id),
    )
    conn.commit()


def process_claim(claim) -> None:
    job_id = claim["id"]
    if claim["simulate_stall"]:
        # 模拟领取进程被拖住：期间没有任何心跳，只能等超时回收
        stall_seconds = claim["timeout_seconds"] + STALL_EXTRA_SECONDS
        print(f"job {job_id} claimed but stalled for {stall_seconds}s (no heartbeat)", flush=True)
        time.sleep(stall_seconds)
        # 醒来后单子已被回收并在待处理区停留了一个宽限期；自行重新领取并补结论
        with connect() as conn:
            claimed = conn.execute(
                """
                UPDATE jobs
                SET status='claimed', claimed_at=%s, heartbeat_at=%s,
                    timeout_seconds=%s, stalled_until=NULL
                WHERE id=%s AND status='pending'
                  AND (stalled_until IS NULL OR stalled_until <= now())
                """,
                (now(), now(), current_timeout(conn), job_id),
            ).rowcount
            conn.commit()
            if claimed == 1:
                time.sleep(0.2)
                heartbeat(conn, job_id)
                finish(conn, job_id, claim["nominal_nm"], claim["measured_nm"])
                print(f"job {job_id} re-processed after reclaim", flush=True)
        return
    # 正常校准：处理期间持续心跳，再写出结论
    with connect() as conn:
        time.sleep(0.3)
        heartbeat(conn, job_id)
        time.sleep(0.3)
        finish(conn, job_id, claim["nominal_nm"], claim["measured_nm"])
        print(f"job {job_id} done", flush=True)


def process_claim_safe(claim) -> None:
    try:
        process_claim(claim)
    except Exception as exc:
        print(f"process job {claim['id']} err:", exc, flush=True)


def reclaim_loop():
    while True:
        try:
            with connect() as conn:
                claim = claim_one(conn)
            if claim:
                # 每张领取单独立处理（含独立连接与心跳），冻结单不阻塞后续领取
                threading.Thread(
                    target=process_claim_safe, args=(claim,), name=f"job-{claim['id']}", daemon=True
                ).start()
                time.sleep(0.1)
                continue
        except Exception as exc:
            print("claim err:", exc, flush=True)
        time.sleep(CLAIM_IDLE)


def reap_once(conn) -> int:
    """把心跳超过该单快照秒数仍无结论的领取单退回待处理，并记回收流水。"""
    rows = conn.execute(
        """
        SELECT id, lamp, timeout_seconds, claimed_at, heartbeat_at
        FROM jobs
        WHERE status='claimed'
          AND timeout_seconds IS NOT NULL
          AND heartbeat_at IS NOT NULL
          AND heartbeat_at + make_interval(secs => timeout_seconds) < now()
        FOR UPDATE SKIP LOCKED
        """
    ).fetchall()
    recycled = 0
    ts = now()
    for row in rows:
        conn.execute(
            """
            INSERT INTO recycle_log(job_id, lamp, timeout_seconds, claimed_at, recycled_at,
                                    reason, forced_by)
            VALUES (%s,%s,%s,%s,%s,%s,'system')
            """,
            (
                row["id"],
                row["lamp"],
                row["timeout_seconds"],
                row["claimed_at"],
                ts,
                f"领取后 {row['timeout_seconds']} 秒心跳超时，自动退回待处理",
            ),
        )
        conn.execute(
            """
            UPDATE jobs
            SET status='pending', claimed_at=NULL, heartbeat_at=NULL,
                timeout_seconds=NULL, simulate_stall=false
            WHERE id=%s
            """,
            (row["id"],),
        )
        recycled += 1
        print(f"job {row['id']} reclaimed to pending after {row['timeout_seconds']}s timeout", flush=True)
    if recycled:
        conn.commit()
    return recycled


def reaper_loop():
    while True:
        try:
            with connect() as conn:
                reap_once(conn)
        except Exception as exc:
            print("reaper err:", exc, flush=True)
        time.sleep(REAP_INTERVAL)


def main():
    wait_for_db()
    threading.Thread(target=reaper_loop, name="reaper", daemon=True).start()
    reclaim_loop()


if __name__ == "__main__":
    main()
