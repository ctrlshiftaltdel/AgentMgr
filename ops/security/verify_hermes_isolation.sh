#!/usr/bin/env bash
# Read-only verification of a provisioned Hermes managed-execution host
# (Issue #97, Task 9).
#
# The checks live in verify_hermes_isolation.py and only read installed files,
# modes, groups and the runtime-root manifest.  They never start/stop a unit,
# call the privileged helper, read a prompt/token/provider key or touch a
# personal/default Hermes gateway or profile.
#
# Run it as root after provisioning and after starting the host egress broker:
#   sudo ops/security/verify_hermes_isolation.sh
#
# Exit codes: 0 all checks pass, 1 at least one check failed, 2 bad invocation.
set -euo pipefail

fail() {
    printf 'verify: error: %s\n' "$*" >&2
    exit 2
}

[ "$(id -u)" -eq 0 ] || fail "this read-only verification must run as root"
command -v python3 >/dev/null 2>&1 || fail "python3 is required"

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec python3 "${script_dir}/verify_hermes_isolation.py" "$@"
