import os
import time

import psycopg
from psycopg.rows import dict_row

from domain import judge

DSN = os.environ.get("DATABASE_URL", "postgresql://app:app@localhost:54395/spectrum")
WORKER_ID = os.environ.get("WORKER_ID", "worker-1")
# 模拟判定耗时：领取后这段时间内单子处于领取态，超时未出结论会被回收
JUDGE_DELAY_SECONDS = float(os.environ.get("JUDGE_DELAY_SECONDS", "1.5"))
DEFAULT_CLAIM_TIMEOUT_SECONDS = 30.0


def connect():
    return psycopg.connect(DSN, row_factory=dict_row)


def claim_one(conn):
    """领取一单进入领取态，并按当前秒数设置快照租约截止时间。"""
    row = conn.execute(
        """
        SELECT id, nominal_nm, measured_nm FROM jobs
        WHERE status='pending'
        ORDER BY id
        FOR UPDATE SKIP LOCKED
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    conn.execute(
        """
        UPDATE jobs
        SET status='claimed',
            claimed_by=%s,
            claimed_at=now(),
            claim_timeout_seconds=COALESCE(
                (SELECT value::float FROM settings WHERE key='claim_timeout_seconds'),
                %s
            ),
            claim_deadline=now() + make_interval(
                secs => COALESCE(
                    (SELECT value::float FROM settings WHERE key='claim_timeout_seconds'),
                    %s
                )
            )
        WHERE id=%s
        """,
        (WORKER_ID, DEFAULT_CLAIM_TIMEOUT_SECONDS, DEFAULT_CLAIM_TIMEOUT_SECONDS, row["id"]),
    )
    conn.commit()
    return row


def finalize(conn, job) -> bool:
    """落结论。若租约已超时单被回收，则不再覆盖，返回 False。"""
    verdict, reason = judge(job["nominal_nm"], job["measured_nm"])
    cur = conn.execute(
        """
        UPDATE jobs
        SET status='done', verdict=%s, reason=%s,
            claimed_by='', claimed_at=NULL, claim_deadline=NULL, claim_timeout_seconds=0
        WHERE id=%s AND status='claimed'
        """,
        (verdict, reason, job["id"]),
    )
    conn.commit()
    return cur.rowcount > 0


def main():
    while True:
        try:
            with connect() as conn:
                job = claim_one(conn)
                if job:
                    time.sleep(JUDGE_DELAY_SECONDS)
                    if not finalize(conn, job):
                        print(f"job {job['id']} 超时已被回收，放弃判定", flush=True)
        except Exception as exc:
            print("worker err", exc, flush=True)
        time.sleep(0.4)


if __name__ == "__main__":
    main()
