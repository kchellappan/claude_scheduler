# csched

Usage-aware job queue for Claude Code on a Pro plan. Polls your real limit
utilisation, holds queued prompts until there is capacity to run them, and
pushes your phone when capacity frees up.

## Status

- **Phase 1 (done)** — poller, SQLite store, gate logic, CLI readout. Zero deps.
- **Phase 1.5 (done)** — web dashboard + JSON API over Tailscale. Still zero deps.
- **Phase 2a (done)** — job runner: queued work runs as Remote Control sessions.
- **Phase 2b** — dashboard deep links to those sessions.
- **Phase 2c** — web push for gate events (needs the cert grant above).

## Dashboard

    http://<machine>.<tailnet>.ts.net:8787

Find your own hostname with `tailscale status`. Reachable from any device
on the tailnet, including the phone. The server binds
to the Tailscale interface only -- never `0.0.0.0` -- so the tailnet is the auth
boundary and there is no login to build.

Plain HTTP is fine for viewing. Installing it as a PWA and receiving web push
(Phase 2) needs a real certificate, which requires a one-time grant:

    sudo tailscale set --operator=$USER
    tailscale serve --bg --https=443 http://127.0.0.1:8787
    # then: https://<machine>.<tailnet>.ts.net

## Quick start

    ./csched.sh status      # gauges, gate state, queue depth
    ./csched.sh add "..."   # queue a prompt (-C workdir, -p priority)
    ./csched.sh ls          # what is queued
    ./csched.sh events      # gate transitions
    ./csched.sh poll        # run the poller in the foreground

Install the poller as a user service:

    systemctl --user enable --now $(pwd)/systemd/csched-poller.service
    systemctl --user enable --now $(pwd)/systemd/csched-server.service

## How queued jobs run

A job is launched as a **background Remote Control session**, not a headless
run:

    claude --bg --remote-control "csched #12: <prompt>" "<prompt>"

`--bg` detaches so no TTY is needed, and `--remote-control` still applies. The
session then appears in the Claude app's session list and can be opened and
steered from a phone mid-flight. Headless `-p` cannot do this: Remote Control
attaches to interactive sessions only, so a `-p` job would be a transcript you
can read but not join.

The phone-openable URL is `https://claude.ai/code/<bridgeSessionId>`, found by:

    claude agents --json --all  ->  pid  ->  ~/.claude/sessions/<pid>.json
                                             .bridgeSessionId

Remote Control connects a moment *after* launch, so the id is absent when the
job starts and gets picked up on a later tick. It is also captured one last
time just before a job is marked done, because a short job can finish inside a
single tick and would otherwise lose its URL entirely.

A session missing from `claude agents --json --all` is treated as finished. We
cannot distinguish "crashed" from "cleaned up", and leaving the job `running`
forever would wedge the queue behind it.

### Sessions you start yourself are not gated

Sessions launched from the Claude app or `claude remote-control` bypass the
gate entirely and run immediately. That is intended -- they are you asking for
work now, not the queue pacing itself -- but it does mean the gauges can move
for reasons the queue cannot explain.

## Tests

    python3 -m unittest discover -s tests -v

Stdlib `unittest`, no test dependencies. CI runs them on 3.11 and 3.12 and
fails the build if a third-party import appears in `csched/`.

## Where the numbers come from

`GET https://api.anthropic.com/api/oauth/usage`, authenticated with the OAuth
token in `~/.claude/.credentials.json`. Unlike the statusline `rate_limits`
payload this works with no Claude Code session running, which is the whole
point — we need to know the gate reopened while you are away.

Two things that will bite you if changed carelessly:

- **The `User-Agent: claude-code/<version>` header is required.** Without it the
  endpoint drops you in an aggressively throttled bucket and returns persistent
  429s. 180s is the safe polling floor; `poller.tick` backs off to 600s on 429.
- **The token expires and Claude Code rotates the file.** `usage.read_token`
  re-reads on every call and never caches. On a stale token the poller emits a
  single `auth_stale` event rather than failing every tick — running any
  `claude` command refreshes it.

## The gate

Two windows with opposite economics, so they get opposite treatment.

**The 5-hour window refills.** Holding a reserve costs nothing, so it is a
hard cap (`FIVE_HOUR_MAX_PCT`, default 50) that keeps headroom for interactive
sessions.

**The 7-day window is use-it-or-lose-it.** Unspent budget evaporates at reset,
so capping it would forfeit budget. Instead we *pace*: spend may run ahead of
the fraction of the week elapsed, but only by `WEEKLY_PACE_SLACK` points
(default 15).

    allowance = min(week_elapsed% + slack, WEEKLY_HARD_MAX_PCT)
    hold if seven_day% > allowance

The gate therefore **widens as the week runs out**, which is correct when
leftovers are worthless — but never past `WEEKLY_HARD_MAX_PCT` (default 95),
because a job started on the last of the budget dies mid-flight and wastes
everything it spent getting there.

A reading older than `STALE_AFTER_S` (default 600) holds the gate shut and
renders greyed in the UI. A stale gauge that looks live is worse than an
obviously stale one, because you would queue against it.

## Events are edge-triggered

`gate_opened`, `gate_closed`, and `capacity_idle` (gate open + queue empty)
fire on *transitions*, recorded in the `events` table with a `notified_at`
column. The notifier drains that table. Level-triggered checks would push you
every 180 seconds.

## Schema changes

`schema.sql` is the only source of truth and there is no migration path. To
change the schema, edit it and delete the database:

    rm ~/.local/state/csched/csched.db

The only thing lost is usage-sample history, which the poller rebuilds. Nothing
here is worth the cost of migrations.

## Known limitation

If this machine sleeps, nothing polls and nothing pushes — worst overnight,
exactly when a 5-hour window would have freed up. Moving the poller to a Pi or
VPS would fix it but requires shipping the OAuth *refresh* token off-box, which
grants full account access. Not worth it; the staleness indicator makes the gap
visible instead.
