"""Local HTTP server for the dashboard. Stdlib only.

Binds to localhost; `tailscale serve` fronts it with TLS for the tailnet.
Never bind this to 0.0.0.0 -- there is no auth, by design: the tailnet is the
auth boundary.
"""
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import config, db, gate, poller
from .claudecli import ClaudeCLI, remote_control_url

_CLI = ClaudeCLI()
# `claude agents --json` takes a second or two; the page polls every 10s, so
# cache it rather than shelling out on every request.
_AGENTS_TTL_S = 15
_agents_cache = {"at": 0.0, "value": []}


def agents():
    now = time.monotonic()
    if now - _agents_cache["at"] > _AGENTS_TTL_S:
        _agents_cache.update(at=now, value=_CLI.agents())
    return _agents_cache["value"]


def busy_sessions():
    """Live session ids, or None when the listing is unavailable."""
    listing = agents()
    if listing is None:
        return None
    return {a["sessionId"] for a in listing
            if a.get("sessionId") and a.get("state") != "done"}


def resumable_sessions(conn):
    """Sessions a follow-up could continue: whatever is live, plus sessions
    previous jobs left behind."""
    seen, out = set(), []
    for a in agents() or []:
        sid = a.get("sessionId")
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({"session_id": sid, "name": a.get("name") or sid[:8],
                    "cwd": a.get("cwd"), "busy": a.get("state") != "done",
                    "source": a.get("kind") or "session"})
    for r in conn.execute(
            "SELECT DISTINCT session_id, prompt, working_dir FROM jobs "
            "WHERE session_id IS NOT NULL ORDER BY id DESC LIMIT 25"):
        if r["session_id"] in seen:
            continue
        seen.add(r["session_id"])
        out.append({"session_id": r["session_id"],
                    "name": " ".join(r["prompt"].split())[:48],
                    "cwd": r["working_dir"], "busy": False, "source": "csched"})
    return out

HTML = (Path(__file__).parent / "dashboard.html").read_bytes()
PORT = int(os.environ.get("CSCHED_PORT", "8787"))


def default_host():
    """Bind to the Tailscale interface so the phone can reach us; the tailnet
    is the auth boundary. Falls back to loopback if Tailscale is down.
    Deliberately never 0.0.0.0 -- that would expose the queue to the LAN."""
    try:
        import subprocess
        out = subprocess.run(["tailscale", "ip", "-4"],
                             capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip().splitlines()[0].strip()
        if ip.startswith("100."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


HOST = os.environ.get("CSCHED_HOST") or default_host()


def state():
    conn = db.connect()
    try:
        row = db.latest_sample(conn)
        g = gate.evaluate(row)
        pending, running = poller.queue_depth(conn)
        # finished_at is an ISO string with an offset; SQLite's datetime()
        # renders "YYYY-MM-DD HH:MM:SS", and comparing the two as text puts
        # 'T' above ' ', so anything finishing earlier on the cutoff date
        # compared greater. Pass a matching ISO cutoff instead.
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        jobs = [dict(r) for r in conn.execute(
            "SELECT id,prompt,working_dir,priority,status,created_at,started_at,"
            "finished_at,attempts,error,bridge_session_id,model,"
            "resume_session_id FROM jobs "
            "WHERE status IN ('pending','running') OR finished_at > ? "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'pending' THEN 1 "
            "  ELSE 2 END, priority, created_at LIMIT 50", (cutoff,))]
        busy = busy_sessions()
        for job in jobs:
            # The URL shape lives in one place; the page just renders it.
            job["rc_url"] = remote_control_url(job.pop("bridge_session_id"))
            # A follow-up waiting for its target session to go idle.
            job["blocked"] = bool(
                job["status"] == "pending" and job["resume_session_id"]
                and (busy is None or job["resume_session_id"] in busy))
        events = [dict(r) for r in conn.execute(
            "SELECT id,created_at,kind,payload FROM events "
            "ORDER BY id DESC LIMIT 15")]
        history = [dict(r) for r in conn.execute(
            "SELECT sampled_at, five_hour_pct, seven_day_pct FROM usage_samples "
            "WHERE ok=1 ORDER BY id DESC LIMIT 240")][::-1]
        return {
            "now": datetime.now(timezone.utc).isoformat(),
            "usage": {
                "five_hour_pct": g.five_hour_pct,
                "five_hour_resets_at": row["five_hour_resets_at"] if row else None,
                "seven_day_pct": g.seven_day_pct,
                "seven_day_resets_at": row["seven_day_resets_at"] if row else None,
                "week_elapsed_pct": g.week_elapsed_pct,
                "weekly_allowance_pct": g.weekly_allowance_pct,
                "sampled_at": row["sampled_at"] if row else None,
                "age_s": g.age_s,
            },
            "gate": {"open": g.open, "stale": g.stale, "reasons": g.reasons},
            "config": {
                "five_hour_max": config.FIVE_HOUR_MAX_PCT,
                "weekly_slack": config.WEEKLY_PACE_SLACK,
                "weekly_hard_max": config.WEEKLY_HARD_MAX_PCT,
                "poll_interval_s": config.POLL_INTERVAL_S,
            },
            "queue": {"pending": pending, "running": running, "jobs": jobs},
            "sessions": resumable_sessions(conn),
            "events": events,
            "history": history,
        }
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "csched"

    def log_message(self, *a):
        pass  # quiet; systemd journal does not need a line per poll

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            return self._send(200, HTML, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(200, json.dumps(state()).encode())
        if path == "/api/sessions":
            conn = db.connect()
            try:
                return self._send(
                    200, json.dumps(resumable_sessions(conn)).encode())
            finally:
                conn.close()
        self._send(404, b'{"error":"not found"}')

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, b'{"error":"bad json"}')

        conn = db.connect()
        try:
            if path == "/api/jobs":
                prompt = (body.get("prompt") or "").strip()
                if not prompt:
                    return self._send(400, b'{"error":"empty prompt"}')

                resume = (body.get("resume_session_id") or "").strip() or None
                # Refuse rather than fork. The page disables live sessions in
                # the picker, but it refreshes on a timer, so its view can be
                # a few seconds stale -- this is the check that actually holds.
                busy = busy_sessions()
                if resume and busy is None:
                    return self._send(409, json.dumps({
                        "error": "cannot reach `claude` to check whether that "
                                 "session is running; refusing rather than "
                                 "risk forking it"}).encode())
                if resume and resume in busy:
                    return self._send(409, json.dumps({
                        "error": "that session is running right now; "
                                 "resuming it would fork a copy rather than "
                                 "continue it"}).encode())

                model = (body.get("model") or "").strip() or None
                if model and len(model) > 64:
                    return self._send(400, b'{"error":"model name too long"}')

                cwd = os.path.abspath(
                    body.get("working_dir") or os.path.expanduser("~"))
                cur = conn.execute(
                    "INSERT INTO jobs(prompt,working_dir,priority,created_at,"
                    "source,model,resume_session_id) VALUES (?,?,?,?,?,?,?)",
                    (prompt, cwd, int(body.get("priority", 100)),
                     db.utcnow(), body.get("source", "phone"), model, resume))
                conn.commit()
                return self._send(200, json.dumps({"id": cur.lastrowid}).encode())

            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = int(path.split("/")[3])
                n = conn.execute(
                    "UPDATE jobs SET status='cancelled', finished_at=? "
                    "WHERE id=? AND status='pending'",
                    (db.utcnow(), job_id)).rowcount
                conn.commit()
                return self._send(200, json.dumps({"cancelled": bool(n)}).encode())
        except (ValueError, KeyError) as e:
            return self._send(400, json.dumps({"error": str(e)}).encode())
        finally:
            conn.close()
        self._send(404, b'{"error":"not found"}')


def main():
    db.init()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"csched dashboard on http://{HOST}:{PORT}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
