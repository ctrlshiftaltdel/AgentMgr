#!/bin/sh
# Real isolation verification entry point (Issue #82).
#
# This script only checks the approved test host and then runs the in-process
# harness.  It never starts a gateway process of its own: the harness owns the
# gateway thread and its Unix socket.
#
# Required environment:
#   AGENTMGR_ISOLATION_TEST=1   explicit opt-in for the live isolation tests
#   AGENTMGR_ISOLATION_SUDO=1   explicit opt-in for passwordless sudo -n
# Optional:
#   AGENTMGR_ISOLATION_EVIDENCE=<path outside the repository>
#
# Run it on the owner-approved isolation test host, from the branch worktree:
#   AGENTMGR_ISOLATION_TEST=1 AGENTMGR_ISOLATION_SUDO=1 \
#     AGENTMGR_ISOLATION_EVIDENCE=/tmp/agentmgr-isolation-evidence.json \
#     ops/security/verify_isolation.sh

set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 2
}

[ "${AGENTMGR_ISOLATION_TEST:-}" = "1" ] || \
    fail "AGENTMGR_ISOLATION_TEST=1 is required for the live isolation run"
[ "${AGENTMGR_ISOLATION_SUDO:-}" = "1" ] || \
    fail "AGENTMGR_ISOLATION_SUDO=1 is required for the live isolation run"

command -v systemctl >/dev/null 2>&1 || fail "systemctl is required"
command -v systemd-run >/dev/null 2>&1 || fail "systemd-run is required"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

if ! version_line=$(systemctl --version 2>/dev/null | head -n 1); then
    fail "cannot read the systemd version (systemctl --version failed)"
fi

version=${version_line#systemd }
version=${version%% *}

case "$version" in
    ''|*[!0-9]*)
        fail "cannot parse the systemd version from: $version_line"
        ;;
esac

if [ "$version" -lt 249 ]; then
    fail "systemd >= 249 is required (found $version)"
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/../.." && pwd)

exec env PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}" \
    python3 "$repo_root/ops/security/verify_isolation.py"
