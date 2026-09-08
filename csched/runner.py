"""Launches queued jobs as Remote Control sessions when the gate allows.

Each job becomes a real `claude --bg --remote-control` session rather than a
headless `-p` run, so it shows up in the Claude app's session list and can be
picked up and steered from the phone. Headless would produce a transcript you
cannot join: Remote Control attaches to interactive sessions only.
"""
import signal
import sys
import time
from datetime import datetime, timezone

from . import config, db, gate
from .claudecli import ClaudeCLI, SpawnError, remote_control_url

_stop = False


def _handle_stop(signum, frame):
    global _stop
    _stop = True


def session_name(job):
    """Title shown in the Claude app's session list."""
    head = " ".join(job["prompt"].split())[:48]
    return f"csched #{job['id']}: {head}"


def is_blocked(job, busy):
    """A follow-up waiting for its target session to go idle."""
    return bool(job["resume_session_id"]) and (
        busy is None or job["resume_session_id"] in busy)


def running_jobs(conn):
    return conn.execute(
        "SELECT * FROM jobs WHERE status='running' ORDER BY id").fetchall()


def next_launchable(conn, busy):
    """Highest-priority pending job that can start now.

    A follow-up whose target session is still live is skipped rather than
    launched: Claude Code would fork a copy under a new id instead of
    continuing the conversation. Skipping rather than stopping means one
    blocked follow-up does not hold up everything queued behind it.
    """
    for job in conn.execute(
            "SELECT * FROM jobs WHERE status='pending' "
            "ORDER BY priority, created_at"):
        # busy is None when the session listing is unavailable; hold every
        # follow-up rather than risk forking a live session.
        if job["resume_session_id"] and (busy is None
                                         or job["resume_session_id"] in busy):
            continue
        return job
    return None


def _capture_bridge(conn, cli, job, agent):
    """Record the Remote Control session id if it is available yet.

    Called both while a job runs and again just before it is marked done: a
    short job can finish inside one tick, and without this last attempt its
    URL -- the whole point of launching with Remote Control -- would be lost.
    """
    if job["bridge_session_id"] or not agent or not agent.get("pid"):
        return False
    bridge = cli.bridge_session_id(agent["pid"])
    if not bridge:
        return False
    conn.execute(
        "UPDATE jobs SET bridge_session_id=?, session_id=? WHERE id=?",
        (bridge, agent.get("sessionId"), job["id"]))
    db.emit(conn, "job_reachable", job_id=job["id"],
            url=remote_control_url(bridge))
    return True


def reconcile_running(conn, cli):
    """Mark finished sessions done. A session that has left the listing counts
    as finished too -- we cannot tell the difference, and leaving the job stuck
    in 'running' would wedge the queue permanently."""
    live = running_jobs(conn)
    if not live:
        return
    listing = cli.agents()
    if listing is None:
        return          # cannot ask; do not conclude everything finished
    agents = {a.get("id"): a for a in listing if a.get("id")}
    for job in live:
        agent = agents.get(job["bg_id"])
        _capture_bridge(conn, cli, job, agent)
        finished = agent is None or agent.get("state") == "done"
        if not finished:
            continue
        bridge = conn.execute(
            "SELECT bridge_session_id FROM jobs WHERE id=?", (job["id"],)
        ).fetchone()["bridge_session_id"]
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
            (db.utcnow(), job["id"]))
        db.emit(conn, "job_done", job_id=job["id"],
                name=session_name(job), url=remote_control_url(bridge))

    if not conn.execute(
            "SELECT 1 FROM jobs WHERE status IN ('pending','running') LIMIT 1"
    ).fetchone():
        db.emit(conn, "queue_drained")


def backfill_bridge_ids(conn, cli):
    """Remote Control connects a moment after launch, so the session id is not
    there yet when we spawn. Pick it up on a later tick."""
    missing = conn.execute(
        "SELECT * FROM jobs WHERE status='running' AND bridge_session_id IS NULL"
    ).fetchall()
    if not missing:
        return
    listing = cli.agents()
    if listing is None:
        return
    by_id = {a.get("id"): a for a in listing if a.get("id")}
    for job in missing:
        _capture_bridge(conn, cli, job, by_id.get(job["bg_id"]))


def launch(conn, cli, job):
    attempts = job["attempts"] + 1
    try:
        bg_id = cli.spawn(job["prompt"], job["working_dir"], session_name(job),
                          model=job["model"], resume=job["resume_session_id"])
    except (SpawnError, OSError) as e:
        failed = attempts >= job["max_attempts"]
        conn.execute(
            "UPDATE jobs SET status=?, attempts=?, error=?, finished_at=? "
            "WHERE id=?",
            ("failed" if failed else "pending", attempts, str(e)[:500],
             db.utcnow() if failed else None, job["id"]))
        if failed:
            db.emit(conn, "job_failed", job_id=job["id"], error=str(e)[:200])
        return False

    conn.execute(
        "UPDATE jobs SET status='running', started_at=?, attempts=?, bg_id=?, "
        "error=NULL WHERE id=?",
        (db.utcnow(), attempts, bg_id, job["id"]))
    db.emit(conn, "job_started", job_id=job["id"], bg_id=bg_id,
            name=session_name(job))
    return True


def tick(conn, cli):
    backfill_bridge_ids(conn, cli)
    reconcile_running(conn, cli)

    g = gate.evaluate(db.latest_sample(conn))
    if not g.open:
        conn.commit()
        return g

    busy = cli.busy_sessions()
    while len(running_jobs(conn)) < config.MAX_CONCURRENT:
        job = next_launchable(conn, busy)
        if job is None or not launch(conn, cli, job):
            break
        if job["resume_session_id"] and busy is not None:
            busy.add(job["resume_session_id"])
    conn.commit()
    return g


def main():
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    db.init()
    conn = db.connect()
    cli = ClaudeCLI()
    print(f"csched runner: every {config.RUNNER_INTERVAL_S}s, "
          f"max {config.MAX_CONCURRENT} concurrent", file=sys.stderr, flush=True)
    while not _stop:
        g = tick(conn, cli)
        pending, running = (
            conn.execute("SELECT SUM(status='pending'), SUM(status='running') "
                         "FROM jobs").fetchone())
        print(f"{datetime.now(timezone.utc):%H:%M:%S}  {g.summary()}  "
              f"{pending or 0} pending, {running or 0} running",
              file=sys.stderr, flush=True)
        for _ in range(config.RUNNER_INTERVAL_S):
            if _stop:
                break
            time.sleep(1)
    conn.close()


if __name__ == "__main__":
    main()
