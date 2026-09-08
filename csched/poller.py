"""Samples the usage endpoint on a fixed cadence and emits gate transitions.

Runs independently of the scheduler: it must keep sampling precisely when the
scheduler is blocked, since that is when we are waiting to learn the gate has
reopened.
"""
import json
import signal
import sys
import time
from datetime import datetime, timezone

from . import config, db, gate, usage

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


def record(conn, payload=None, error=None):
    flat = usage.flatten(payload) if payload else {}
    conn.execute(
        """INSERT INTO usage_samples
           (sampled_at, ok, five_hour_pct, five_hour_resets_at,
            seven_day_pct, seven_day_resets_at, extra_usage_enabled,
            error, raw_json)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            db.utcnow(),
            1 if payload else 0,
            flat.get("five_hour_pct"),
            flat.get("five_hour_resets_at"),
            flat.get("seven_day_pct"),
            flat.get("seven_day_resets_at"),
            flat.get("extra_usage_enabled"),
            error,
            json.dumps(payload) if payload else None,
        ),
    )


def queue_depth(conn):
    row = conn.execute(
        """SELECT
             SUM(status='pending') AS pending,
             SUM(status='running') AS running
           FROM jobs"""
    ).fetchone()
    return (row["pending"] or 0), (row["running"] or 0)


def reconcile(conn, g):
    """Emit edge-triggered events only. Level-triggered would push every tick."""
    was_open = db.get_meta(conn, "gate_open") == "1"
    if g.open != was_open:
        db.set_meta(conn, "gate_open", "1" if g.open else "0")
        db.emit(
            conn,
            "gate_opened" if g.open else "gate_closed",
            five_hour_pct=g.five_hour_pct,
            seven_day_pct=g.seven_day_pct,
            reasons=g.reasons,
        )

    pending, running = queue_depth(conn)
    idle_free = g.open and pending == 0 and running == 0
    was_idle_free = db.get_meta(conn, "idle_free") == "1"
    if idle_free != was_idle_free:
        db.set_meta(conn, "idle_free", "1" if idle_free else "0")
        if idle_free:
            db.emit(
                conn,
                "capacity_idle",
                five_hour_pct=g.five_hour_pct,
                seven_day_pct=g.seven_day_pct,
            )


def tick(conn):
    """One sample + reconcile. Returns seconds to wait before the next tick."""
    interval = config.POLL_INTERVAL_S
    try:
        payload = usage.fetch()
        record(conn, payload=payload)
        if db.get_meta(conn, "fetch_error"):
            db.set_meta(conn, "fetch_error", "")
    except usage.UsageError as e:
        record(conn, error=f"{e.kind}: {e}")
        # Emit once per transition, not once per tick.
        if db.get_meta(conn, "fetch_error") != e.kind:
            db.set_meta(conn, "fetch_error", e.kind)
            if e.kind == "auth":
                db.emit(conn, "auth_stale", detail=str(e))
            elif e.kind != "throttled":
                db.emit(conn, "poll_failing", kind=e.kind, detail=str(e))
        if e.kind == "throttled":
            interval = max(interval * 2, 600)

    reconcile(conn, gate.evaluate(db.latest_sample(conn)))
    conn.commit()
    return interval


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    db.init()
    conn = db.connect()
    print(f"csched poller: every {config.POLL_INTERVAL_S}s -> {config.DB_PATH}",
          file=sys.stderr, flush=True)
    while not _stop:
        wait = tick(conn)
        g = gate.evaluate(db.latest_sample(conn))
        print(f"{datetime.now(timezone.utc):%H:%M:%S}  {g.summary()}",
              file=sys.stderr, flush=True)
        for _ in range(wait):
            if _stop:
                break
            time.sleep(1)
    conn.close()


if __name__ == "__main__":
    main()
