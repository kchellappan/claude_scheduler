#!/usr/bin/env bash
# Install csched as systemd user services.
#
# The checked-in units under systemd/ are templates: they carry a placeholder
# working directory, because a clone can live anywhere. This script resolves
# the real path and writes the units out.
set -euo pipefail

REPO="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
UNIT_DIR="${CSCHED_UNIT_DIR:-$HOME/.config/systemd/user}"
SERVICES=(csched-poller csched-server csched-runner)
DRY_RUN=0

say()  { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Usage: ./install.sh [--dry-run] [--uninstall] [--status]

  (no args)    install and start the poller, dashboard and runner
  --dry-run    write units to a temp dir and print them; change nothing
  --uninstall  stop, disable and remove the services
  --status     show whether the services are running
USAGE
}

check_prereqs() {
    printf '\nChecking prerequisites\n'

    command -v python3 >/dev/null || die "python3 not found"
    local py
    py="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)'; then
        ok "python3 $py"
    else
        die "python3 $py is too old; csched needs 3.11 or newer"
    fi

    # No pip install step: csched is stdlib-only and CI enforces that.
    ok "no dependencies to install"

    if command -v claude >/dev/null; then
        ok "claude $(claude --version 2>/dev/null | awk '{print $1}')"
    else
        warn "claude not on PATH -- the runner cannot launch jobs without it"
    fi

    if [ -r "$HOME/.claude/.credentials.json" ]; then
        ok "claude credentials found"
    else
        warn "no ~/.claude/.credentials.json -- run 'claude' and sign in, or the poller cannot read your usage"
    fi

    if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
        ok "tailscale up ($(tailscale ip -4 | head -1))"
    else
        warn "tailscale not up -- the dashboard will bind to localhost only"
    fi

    command -v systemctl >/dev/null || die "systemctl not found; see the README to run the processes manually"
}

write_units() {
    mkdir -p "$UNIT_DIR"
    for svc in "${SERVICES[@]}"; do
        sed "s|@@REPO@@|$REPO|g" "$REPO/systemd/$svc.service" > "$UNIT_DIR/$svc.service"
        ok "wrote $UNIT_DIR/$svc.service"
    done
}

dashboard_url() {
    local host port
    port="${CSCHED_PORT:-8787}"
    host="$(tailscale status --json 2>/dev/null \
            | python3 -c 'import sys,json; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
    [ -n "$host" ] && printf 'http://%s:%s\n' "$host" "$port" || printf 'http://127.0.0.1:%s\n' "$port"
}

do_install() {
    check_prereqs
    printf '\nInstalling units\n'
    write_units
    systemctl --user daemon-reload
    for svc in "${SERVICES[@]}"; do
        systemctl --user enable --now "$svc.service" >/dev/null 2>&1
        if systemctl --user is-active --quiet "$svc.service"; then
            ok "$svc running"
        else
            warn "$svc did not start -- journalctl --user -u $svc -n 30"
        fi
    done

    printf '\nDone\n'
    say "dashboard: $(dashboard_url)"
    say "status:    $REPO/csched.sh status"
    say "queue:     $REPO/csched.sh add \"...\""
    printf '\n'
    say "For HTTPS (needed later for web push), one-time:"
    say "  sudo tailscale set --operator=\$USER"
    say "  tailscale serve --bg --https=443 http://127.0.0.1:${CSCHED_PORT:-8787}"
    printf '\n'
}

do_dry_run() {
    check_prereqs
    UNIT_DIR="$(mktemp -d)"
    printf '\nUnits that would be installed to %s\n' "${CSCHED_UNIT_DIR:-$HOME/.config/systemd/user}"
    write_units
    for svc in "${SERVICES[@]}"; do
        printf '\n--- %s.service ---\n' "$svc"
        cat "$UNIT_DIR/$svc.service"
    done
    rm -rf "$UNIT_DIR"
    printf '\nNothing was changed.\n\n'
}

do_uninstall() {
    printf '\nRemoving services\n'
    for svc in "${SERVICES[@]}"; do
        systemctl --user disable --now "$svc.service" >/dev/null 2>&1 || true
        rm -f "$UNIT_DIR/$svc.service"
        ok "removed $svc"
    done
    systemctl --user daemon-reload
    printf '\nThe database at ~/.local/state/csched/ was left alone.\n\n'
}

do_status() {
    printf '\n'
    for svc in "${SERVICES[@]}"; do
        if systemctl --user is-active --quiet "$svc.service"; then
            ok "$svc active"
        else
            warn "$svc inactive"
        fi
    done
    printf '\n  dashboard: %s\n\n' "$(dashboard_url)"
}

case "${1:-}" in
    "")           do_install ;;
    --dry-run)    do_dry_run ;;
    --uninstall)  do_uninstall ;;
    --status)     do_status ;;
    -h|--help)    usage ;;
    *)            usage; exit 1 ;;
esac
