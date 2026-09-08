"""Terminal front-end. Reads the same SQLite seam the phone will."""
import argparse
import os
import sys
from datetime import datetime, timezone

from . import config, db, gate, poller, runner
from .claudecli import remote_control_url

RESET, DIM, BOLD = "\033[0m", "\033[2m", "\033[1m"
GREEN, YELLOW, RED, GREY = "\033[32m", "\033[33m", "\033[31m", "\033[90m"


def bar(pct, width=28, marker=None):
    """Horizontal gauge. `marker` draws the pace allowance as a tick."""
    if pct is None:
        return GREY + "?" * width + RESET
    filled = int(round(min(pct, 100) / 100 * width))
    colour = GREEN if pct < 50 else YELLOW if pct < 80 else RED
    cells = [colour + "█" + RESET if i < filled else GREY + "░" + RESET
             for i in range(width)]
    if marker is not None:
        m = int(round(min(marker, 100) / 100 * width))
        if 0 <= m < width:
            cells[m] = BOLD + "│" + RESET
    return "".join(cells)


def humanise(iso):
    if not iso:
        return "unknown"
    delta = datetime.fromisoformat(iso) - datetime.now(timezone.utc)
    s = int(delta.total_seconds())
    if s < 0:
        return "now"
    h, m = divmod(s // 60, 60)
    d, h = divmod(h, 24)
    return f"{d}d {h}h" if d else (f"{h}h {m}m" if h else f"{m}m")


def cmd_status(args):
    conn = db.connect()
    row = db.latest_sample(conn)
    g = gate.evaluate(row)

    if row is None:
        print("No usage sample yet. Run:  csched poll")
        return 1

    print()
    print(f"  {BOLD}5-hour{RESET}   {bar(g.five_hour_pct)} "
          f"{g.five_hour_pct:5.0f}%   {DIM}cap {config.FIVE_HOUR_MAX_PCT:.0f}%, "
          f"resets in {humanise(row['five_hour_resets_at'])}{RESET}")

    pace = g.weekly_allowance_pct
    print(f"  {BOLD}weekly{RESET}   {bar(g.seven_day_pct, marker=pace)} "
          f"{g.seven_day_pct:5.0f}%   {DIM}pace allows {pace:.0f}%, "
          f"resets in {humanise(row['seven_day_resets_at'])}{RESET}")

    if g.week_elapsed_pct is not None:
        ratio = (g.seven_day_pct / g.week_elapsed_pct) if g.week_elapsed_pct else 0
        verdict = ("under-spending" if ratio < 0.85
                   else "on pace" if ratio <= 1.15 else "ahead of pace")
        print(f"  {DIM}         week {g.week_elapsed_pct:.0f}% elapsed, "
              f"{g.seven_day_pct:.0f}% spent -> {verdict} ({ratio:.2f}x){RESET}")

    pending, running = poller.queue_depth(conn)
    colour = GREEN if g.open else YELLOW
    print()
    print(f"  gate     {colour}{g.summary()}{RESET}")
    print(f"  queue    {pending} pending, {running} running")
    if g.stale:
        print(f"  {RED}stale{RESET}    reading is {int(g.age_s // 60)}m old "
              f"{DIM}(is the poller running?){RESET}")
    print()
    return 0


def cmd_add(args):
    conn = db.connect()
    prompt = " ".join(args.prompt) if args.prompt else sys.stdin.read().strip()
    if not prompt:
        print("empty prompt", file=sys.stderr)
        return 1
    cur = conn.execute(
        """INSERT INTO jobs(prompt, working_dir, priority, created_at, source)
           VALUES (?,?,?,?,?)""",
        (prompt, os.path.abspath(args.cwd), args.priority, db.utcnow(), "cli"),
    )
    conn.commit()
    print(f"queued job {cur.lastrowid} (priority {args.priority}) in {args.cwd}")
    return 0


def cmd_ls(args):
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM jobs WHERE (? OR status IN ('pending','running')) "
        "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,"
        " priority, created_at",
        (1 if args.all else 0,),
    ).fetchall()
    if not rows:
        print("queue empty")
        return 0
    marks = {"pending": f"{DIM}○{RESET}", "running": f"{YELLOW}◐{RESET}",
             "done": f"{GREEN}●{RESET}", "failed": f"{RED}✕{RESET}",
             "cancelled": f"{GREY}—{RESET}"}
    for r in rows:
        prompt = r["prompt"].replace("\n", " ")
        prompt = prompt[:58] + "…" if len(prompt) > 59 else prompt
        print(f"  {marks.get(r['status'], '?')} {r['id']:>4}  {prompt:<60} "
              f"{DIM}{os.path.basename(r['working_dir'])}{RESET}")
        url = remote_control_url(r["bridge_session_id"])
        if url and r["status"] == "running":
            print(f"       {DIM}↳ open on any device: {url}{RESET}")
    return 0


def cmd_cancel(args):
    conn = db.connect()
    n = conn.execute(
        "UPDATE jobs SET status='cancelled', finished_at=? "
        "WHERE id=? AND status='pending'",
        (db.utcnow(), args.id),
    ).rowcount
    conn.commit()
    print(f"cancelled job {args.id}" if n else
          f"job {args.id} not pending (already running or finished)")
    return 0 if n else 1


def cmd_events(args):
    conn = db.connect()
    rows = conn.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,)
    ).fetchall()
    for r in reversed(rows):
        when = datetime.fromisoformat(r["created_at"]).astimezone()
        sent = "" if r["notified_at"] else f"  {DIM}(unsent){RESET}"
        print(f"  {DIM}{when:%m-%d %H:%M}{RESET}  {r['kind']:<16}"
              f"{DIM}{r['payload'] or ''}{RESET}{sent}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="csched",
                                description="Usage-aware Claude job queue")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="usage gauges, gate state, queue depth")

    a = sub.add_parser("add", help="queue a prompt")
    a.add_argument("prompt", nargs="*", help="prompt text (or pipe on stdin)")
    a.add_argument("-C", "--cwd", default=".", help="working dir for the job")
    a.add_argument("-p", "--priority", type=int, default=100,
                   help="lower runs first (default 100)")

    l = sub.add_parser("ls", help="list queued jobs")
    l.add_argument("-a", "--all", action="store_true", help="include finished")

    c = sub.add_parser("cancel", help="cancel a pending job")
    c.add_argument("id", type=int)

    e = sub.add_parser("events", help="recent domain events")
    e.add_argument("-n", "--limit", type=int, default=20)

    sub.add_parser("poll", help="run the poller in the foreground")
    sub.add_parser("run", help="run the job runner in the foreground")

    args = p.parse_args(argv)
    db.init()
    if args.cmd == "poll":
        poller.main()
        return 0
    if args.cmd == "run":
        runner.main()
        return 0
    return {"status": cmd_status, "add": cmd_add, "ls": cmd_ls,
            "cancel": cmd_cancel, "events": cmd_events}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
