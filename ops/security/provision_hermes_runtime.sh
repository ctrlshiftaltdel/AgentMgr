#!/usr/bin/env bash
# Idempotent provisioning for Hermes managed execution (Issue #97, Task 9).
#
# This script installs the *fixed deployment surface* used by Task 2's helper,
# Task 5's adapter and Task 9's launcher.  It does not start, stop, replace or
# inspect any personal/default Hermes gateway, profile, authentication file,
# session or lock, and it never prints a secret.
#
# Fixed paths (the helper, adapter and verifier all pin these):
#   /usr/local/libexec/agentmgr_hermes_exec.py          root:root 0644
#   /usr/local/libexec/agentmgr-hermes-exec             root:root 0755
#   /usr/local/libexec/agentmgr_hermes_workload.py      root:root 0555
#   /etc/sudoers.d/agentmgr-hermes                      root:root 0440
#   /etc/systemd/system/agentmgr-hermes-egress.service  root:root 0644
#   /etc/agentmgr/hermes-exec.json                      root:root 0640
#   /etc/agentmgr/hermes/managed-cli.json               root:<adapter-group> 0440
#   /etc/agentmgr/hermes/egress-broker.json             root:agentmgr-hermes 0440
#   /etc/agentmgr/hermes/deployment.json                root:root 0640
#   /etc/agentmgr/hermes/attestation-key               root:<adapter-group> 0440
#   /etc/agentmgr/hermes/egress-token-key              root:<adapter-group> 0440
#   /etc/agentmgr/hermes/provider-key                  root:root 0400
#   /etc/agentmgr/hermes/runtime-attestation.json      root:<adapter-group> 0440
#   /etc/agentmgr/hermes/runtime-root.hash             root:root 0444
#   /var/lib/agentmgr-hermes-runtime                    root:root 0555 (read-only)
#   /var/lib/agentmgr-hermes-tasks                      root:agentmgr-hermes 0770
#   /var/lib/agentmgr-hermes-control                    <broker>:agentmgr-hermes 0750
#   /var/lib/agentmgr-hermes-revoked                    <adapter>:agentmgr-hermes 0750
#
# Operator-supplied root-only inputs (never committed, never printed):
#   --provider-key-file   dedicated model provider key (copied to provider-key)
#   --source              read-only Hermes runtime root to copy or bind mount
#
# Usage:
#   sudo ops/security/provision_hermes_runtime.sh \
#     --adapter-user <adapter-user> --install-root <install-root> \
#     --venv <venv-bin-dir> --service-identity-id svc_<id> \
#     --source <runtime-source> --runtime-version <version> \
#     --model <model-name> --provider-host <provider-host> \
#     --provider-base-url https://<provider-host> \
#     --provider-key-file <root-only-key-file> \
#     --evidence-ref evidence:hermes:managed:<id>
#
#   sudo ops/security/provision_hermes_runtime.sh --dry-run ...   # validate only
#   sudo ops/security/provision_hermes_runtime.sh --rollback      # remove install
#
# This script never starts a unit.  After a successful install run:
#   systemctl daemon-reload
#   systemctl enable --now agentmgr-hermes-egress.service
#   ops/security/verify_hermes_isolation.sh
set -euo pipefail
umask 077

readonly SANDBOX_GROUP="agentmgr-hermes"
readonly ADAPTER_GROUP="agentmgr-hermes-adapter"
readonly RUNTIME_ROOT="/var/lib/agentmgr-hermes-runtime"
readonly TASK_ROOT="/var/lib/agentmgr-hermes-tasks"
readonly CONTROL_DIR="/var/lib/agentmgr-hermes-control"
readonly CONTROL_SOCKET="${CONTROL_DIR}/egress.sock"
readonly REVOCATION_DIR="/var/lib/agentmgr-hermes-revoked"
readonly AGENTMGR_ETC="/etc/agentmgr"
readonly HERMES_ETC="${AGENTMGR_ETC}/hermes"
readonly HELPER_PATH="/usr/local/libexec/agentmgr_hermes_exec.py"
readonly WRAPPER_PATH="/usr/local/libexec/agentmgr-hermes-exec"
readonly WORKLOAD_PATH="/usr/local/libexec/agentmgr_hermes_workload.py"
readonly SUDOERS_PATH="/etc/sudoers.d/agentmgr-hermes"
readonly UNIT_PATH="/etc/systemd/system/agentmgr-hermes-egress.service"
readonly INSTALL_CONFIG="${AGENTMGR_ETC}/hermes-exec.json"
readonly CLI_CONFIG="${HERMES_ETC}/managed-cli.json"
readonly BROKER_CONFIG="${HERMES_ETC}/egress-broker.json"
readonly DEPLOYMENT_RECORD="${HERMES_ETC}/deployment.json"
readonly ATTESTATION_KEY="${HERMES_ETC}/attestation-key"
readonly EGRESS_TOKEN_KEY="${HERMES_ETC}/egress-token-key"
readonly PROVIDER_KEY="${HERMES_ETC}/provider-key"
readonly RUNTIME_ATTESTATION="${HERMES_ETC}/runtime-attestation.json"
readonly RUNTIME_HASH_FILE="${HERMES_ETC}/runtime-root.hash"
readonly BROKER_CREDENTIAL_PREFIX="/run/credentials/agentmgr-hermes-egress.service"

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
readonly REPO_ROOT="$(CDPATH= cd -- "${SCRIPT_DIR}/../.." && pwd)"

DRY_RUN=0
ROLLBACK=0
REFRESH_RUNTIME=0
PURGE_GROUPS=0
SOURCE=""
MODEL=""
PROVIDER_HOST=""
PROVIDER_BASE_URL=""
PROVIDER_KEY_FILE=""
EVIDENCE_REF=""
RUNTIME_NAME="hermes"
RUNTIME_VERSION=""
RUNTIME_MODE="copy"
ADAPTER_USER=""
BROKER_USER="agentmgr-hermes-broker"
INSTALL_ROOT="${REPO_ROOT}"
VENV=""
SERVICE_IDENTITY_ID=""
WORKER_ID="hermes-managed-1"
RUNTIME_MAX_SEC=900
STAGING=""

log() {
    printf 'provision: %s\n' "$*" >&2
}

fail() {
    printf 'provision: error: %s\n' "$*" >&2
    exit 2
}

usage() {
    sed -n '2,60p' "$0" | sed -n 's/^# \{0,1\}//p'
}

cleanup_staging() {
    if [ -n "${STAGING}" ] && [ -d "${STAGING}" ]; then
        rm -rf --one-file-system -- "${STAGING}"
    fi
}
trap cleanup_staging EXIT

require_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "required command not found: $1"
}

require_root() {
    [ "$(id -u)" -eq 0 ] || fail "this script must run as root"
}

# Print a command, or run it.  No shell evaluation and no shell indirection.
run() {
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '+ %s\n' "$*" >&2
        return 0
    fi
    "$@"
}

is_dangerous_path() {
    # Personal profiles, home directories and the repository checkout are never
    # deployment targets or runtime sources.
    local path=$1
    case "${path}" in
        /|/root|/root/*|/home|/home/*) return 0 ;;
        */.hermes|*/.hermes/*|*/.config/hermes|*/.config/hermes/*) return 0 ;;
        "${REPO_ROOT}"|"${REPO_ROOT}"/*) return 0 ;;
    esac
    return 1
}

require_install_path() {
    local path=$1 label=$2
    case "${path}" in
        /*) ;;
        *) fail "${label} must be an absolute path" ;;
    esac
    case "/${path}/" in
        */../*) fail "${label} must not contain '..'" ;;
    esac
    case "${path}" in
        /|/root|/root/*|/home|/home/*|*/.hermes|*/.hermes/*)
            fail "${label} must not be a personal/home/profile path"
            ;;
    esac
}

require_state_path() {
    local path=$1 label=$2
    require_install_path "${path}" "${label}"
    if is_dangerous_path "${path}"; then
        fail "${label} must not be inside the repository or a personal profile"
    fi
}

require_present_directory() {
    local path=$1 label=$2
    [ -d "${path}" ] || fail "${label} is not a directory: ${path}"
    [ ! -L "${path}" ] || fail "${label} must not be a symlink: ${path}"
}

require_repo_file() {
    local path=$1 label=$2
    [ -f "${path}" ] || fail "${label} is missing: ${path}"
    [ ! -L "${path}" ] || fail "${label} must not be a symlink: ${path}"
}

# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --rollback) ROLLBACK=1; shift ;;
        --refresh-runtime) REFRESH_RUNTIME=1; shift ;;
        --purge-groups) PURGE_GROUPS=1; shift ;;
        --source) [ "$#" -ge 2 ] || fail "--source requires a path"; SOURCE=$2; shift 2 ;;
        --model) [ "$#" -ge 2 ] || fail "--model requires a value"; MODEL=$2; shift 2 ;;
        --provider-host) [ "$#" -ge 2 ] || fail "--provider-host requires a value"; PROVIDER_HOST=$2; shift 2 ;;
        --provider-base-url) [ "$#" -ge 2 ] || fail "--provider-base-url requires a value"; PROVIDER_BASE_URL=$2; shift 2 ;;
        --provider-key-file) [ "$#" -ge 2 ] || fail "--provider-key-file requires a path"; PROVIDER_KEY_FILE=$2; shift 2 ;;
        --evidence-ref) [ "$#" -ge 2 ] || fail "--evidence-ref requires a value"; EVIDENCE_REF=$2; shift 2 ;;
        --runtime-name) [ "$#" -ge 2 ] || fail "--runtime-name requires a value"; RUNTIME_NAME=$2; shift 2 ;;
        --runtime-version) [ "$#" -ge 2 ] || fail "--runtime-version requires a value"; RUNTIME_VERSION=$2; shift 2 ;;
        --runtime-mode) [ "$#" -ge 2 ] || fail "--runtime-mode requires copy or bind"; RUNTIME_MODE=$2; shift 2 ;;
        --adapter-user) [ "$#" -ge 2 ] || fail "--adapter-user requires a name"; ADAPTER_USER=$2; shift 2 ;;
        --broker-user) [ "$#" -ge 2 ] || fail "--broker-user requires a name"; BROKER_USER=$2; shift 2 ;;
        --install-root) [ "$#" -ge 2 ] || fail "--install-root requires a path"; INSTALL_ROOT=$2; shift 2 ;;
        --venv) [ "$#" -ge 2 ] || fail "--venv requires a path"; VENV=$2; shift 2 ;;
        --service-identity-id) [ "$#" -ge 2 ] || fail "--service-identity-id requires a value"; SERVICE_IDENTITY_ID=$2; shift 2 ;;
        --worker-id) [ "$#" -ge 2 ] || fail "--worker-id requires a value"; WORKER_ID=$2; shift 2 ;;
        --runtime-max-sec) [ "$#" -ge 2 ] || fail "--runtime-max-sec requires a value"; RUNTIME_MAX_SEC=$2; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

if [ "${DRY_RUN}" -eq 1 ] && [ "${ROLLBACK}" -eq 1 ]; then
    fail "--dry-run and --rollback are mutually exclusive"
fi
if [ "${ROLLBACK}" -eq 1 ] && [ "${REFRESH_RUNTIME}" -eq 1 ]; then
    fail "--rollback and --refresh-runtime are mutually exclusive"
fi

require_root
require_cmd python3
require_cmd systemctl
require_cmd install

case "${RUNTIME_MODE}" in
    copy|bind) ;;
    *) fail "--runtime-mode must be copy or bind" ;;
esac
case "${RUNTIME_MAX_SEC}" in
    ''|*[!0-9]*) fail "--runtime-max-sec must be an integer" ;;
esac
[ "${RUNTIME_MAX_SEC}" -ge 9 ] || fail "--runtime-max-sec must be at least 9"
[ "${RUNTIME_MAX_SEC}" -le 86400 ] || fail "--runtime-max-sec must be at most 86400"

require_install_path "${INSTALL_ROOT}" "--install-root"
require_present_directory "${INSTALL_ROOT}" "--install-root"
require_repo_file "${INSTALL_ROOT}/ops/security/agentmgr_hermes_exec.py" "helper template"
require_repo_file "${INSTALL_ROOT}/ops/security/agentmgr-hermes-exec" "wrapper template"
require_repo_file "${INSTALL_ROOT}/ops/security/agentmgr_hermes_workload.py" "workload template"
require_repo_file "${INSTALL_ROOT}/ops/security/agentmgr-hermes.sudoers" "sudoers template"
require_repo_file "${INSTALL_ROOT}/ops/security/agentmgr-hermes-egress.service" "broker unit template"

# ---------------------------------------------------------------------------
# runtime root manifest hash (deterministic, same algorithm as the verifier)
# ---------------------------------------------------------------------------

compute_runtime_hash() {
    local root=$1
    python3 - "${root}" <<'PY'
import hashlib
import os
import stat
import sys

root = sys.argv[1]
profile_markers = {"profiles", "auth.json", ".env", "sessions", "gateway.lock", "locks"}

entries = []
for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
    rel_dir = os.path.relpath(dirpath, root)
    for name in list(dirnames):
        candidate = os.path.join(dirpath, name)
        if os.path.islink(candidate):
            dirnames.remove(name)
            filenames.append(name)
    dirnames.sort()
    for name in dirnames:
        if name in profile_markers:
            raise SystemExit("runtime root contains a personal-profile marker: " + name)
    if rel_dir != ".":
        entries.append(("d", rel_dir, 0, ""))
    for name in sorted(filenames):
        if name in profile_markers:
            raise SystemExit("runtime root contains a personal-profile marker: " + name)
        full = os.path.join(dirpath, name)
        rel = os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name
        info = os.lstat(full)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(full)
            if os.path.isabs(target):
                raise SystemExit(
                    "runtime root contains an absolute symlink: " + rel
                )
            normalized = os.path.normpath(os.path.join(rel_dir, target))
            if normalized == ".." or normalized.startswith("../"):
                raise SystemExit(
                    "runtime root symlink escapes the root: " + rel
                )
            entries.append(("l", rel, len(target), target))
            continue
        if not stat.S_ISREG(info.st_mode):
            raise SystemExit(
                "runtime root contains a non-regular file: " + rel
            )
        digest = hashlib.sha256()
        with open(full, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(("f", rel, info.st_size, digest.hexdigest()))

total = hashlib.sha256()
for kind, rel, size, payload in sorted(entries):
    total.update(f"{kind}\0{rel}\0{size}\0{payload}\n".encode("utf-8"))
print("sha256:" + total.hexdigest())
PY
}

# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

managed_tree() {
    case "$1" in
        "${RUNTIME_ROOT}"|"${TASK_ROOT}"|"${CONTROL_DIR}"|"${REVOCATION_DIR}"|"${HERMES_ETC}")
            return 0
            ;;
    esac
    return 1
}

remove_tree() {
    local path=$1
    managed_tree "${path}" || fail "refusing to remove an unexpected path: ${path}"
    [ -e "${path}" ] || [ -L "${path}" ] || return 0
    run rm -rf --one-file-system -- "${path}"
}

remove_managed_units() {
    local units
    units=$(systemctl list-units --all --plain --no-legend 'agentmgr-hermes-*' 2>/dev/null || true)
    if [ -n "${units}" ]; then
        while IFS= read -r line; do
            local unit
            unit=$(printf '%s\n' "${line}" | awk '{print $1}')
            case "${unit}" in
                agentmgr-hermes-[0-9a-f]*.service)
                    log "stopping ${unit}"
                    run systemctl stop "${unit}" || true
                    run systemctl reset-failed "${unit}" || true
                    ;;
            esac
        done <<< "${units}"
    fi
    if systemctl cat agentmgr-hermes-egress.service >/dev/null 2>&1; then
        log "disabling the host egress broker"
        run systemctl disable --now agentmgr-hermes-egress.service || true
    fi
}

rollback() {
    remove_managed_units

    if [ -f "${UNIT_PATH}" ]; then
        run rm -f -- "${UNIT_PATH}"
    fi
    run systemctl daemon-reload || true

    if [ -f "${SUDOERS_PATH}" ]; then
        run rm -f -- "${SUDOERS_PATH}"
        if command -v visudo >/dev/null 2>&1; then
            run visudo -c || log "warning: unrelated sudoers validation failed"
        fi
    fi

    run rm -f -- "${HELPER_PATH}" "${WRAPPER_PATH}" "${WORKLOAD_PATH}"
    run rm -f -- "${INSTALL_CONFIG}"
    run rm -rf --one-file-system -- "${HERMES_ETC}"
    run rmdir --ignore-fail-on-non-empty -- "${AGENTMGR_ETC}" || true

    if [ -d "${RUNTIME_ROOT}" ] && command -v mountpoint >/dev/null 2>&1 && \
        mountpoint -q "${RUNTIME_ROOT}" 2>/dev/null; then
        run umount "${RUNTIME_ROOT}"
    fi
    remove_tree "${RUNTIME_ROOT}"
    remove_tree "${TASK_ROOT}"
    remove_tree "${CONTROL_DIR}"
    remove_tree "${REVOCATION_DIR}"

    if [ "${PURGE_GROUPS}" -eq 1 ]; then
        log "purging groups and the dedicated broker user (--purge-groups)"
        run groupdel "${ADAPTER_GROUP}" || true
        run groupdel "${SANDBOX_GROUP}" || true
        run userdel "${BROKER_USER}" || true
    else
        log "groups/users were kept (use --purge-groups to remove them)"
    fi

    log "rollback complete; personal/default Hermes gateway and profiles were not touched"
}

if [ "${ROLLBACK}" -eq 1 ]; then
    rollback
    exit 0
fi

# ---------------------------------------------------------------------------
# install inputs
# ---------------------------------------------------------------------------

[ -n "${ADAPTER_USER}" ] || fail "--adapter-user is required (the trusted AgentMgr adapter account)"
[ -n "${VENV}" ] || fail "--venv is required (the venv bin directory containing python)"
[ -n "${SOURCE}" ] || fail "--source is required (the read-only Hermes runtime root)"
[ -n "${MODEL}" ] || fail "--model is required"
[ -n "${PROVIDER_HOST}" ] || fail "--provider-host is required"
[ -n "${PROVIDER_BASE_URL}" ] || fail "--provider-base-url is required"
[ -n "${PROVIDER_KEY_FILE}" ] || fail "--provider-key-file is required"
[ -n "${EVIDENCE_REF}" ] || fail "--evidence-ref is required"
[ -n "${RUNTIME_VERSION}" ] || fail "--runtime-version is required"
[ -n "${SERVICE_IDENTITY_ID}" ] || fail "--service-identity-id is required"

require_install_path "${VENV}" "--venv"
[ -d "${VENV}" ] || fail "--venv is not a directory"
[ -x "${VENV}/python" ] || fail "--venv must contain an executable python"

require_state_path "${SOURCE}" "--source"
require_present_directory "${SOURCE}" "--source"
[ "${SOURCE}" != "${RUNTIME_ROOT}" ] || fail "--source must differ from the runtime root"
case "${RUNTIME_ROOT}/" in
    "${SOURCE}/"*) fail "the runtime root must not be inside --source" ;;
esac
case "${SOURCE}/" in
    "${RUNTIME_ROOT}/"*) fail "--source must not be inside the runtime root" ;;
esac

# The runtime source is an installation, never a personal profile.
for marker in profiles auth.json .env sessions gateway.lock locks; do
    if [ -e "${SOURCE}/${marker}" ]; then
        fail "the runtime source contains a personal-profile marker: ${marker}"
    fi
done

if ! [[ "${ADAPTER_USER}" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,31}$ ]]; then
    fail "--adapter-user is not a valid account name"
fi
if ! [[ "${BROKER_USER}" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,31}$ ]]; then
    fail "--broker-user is not a valid account name"
fi
if ! [[ "${SERVICE_IDENTITY_ID}" =~ ^svc_[1-9][0-9]{0,18}$ ]]; then
    fail "--service-identity-id must be svc_<positive integer>"
fi
if ! [[ "${WORKER_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$ ]]; then
    fail "--worker-id is invalid"
fi
if ! [[ "${EVIDENCE_REF}" =~ ^evidence:[A-Za-z0-9][A-Za-z0-9._:-]{0,126}$ ]]; then
    fail "--evidence-ref is invalid"
fi
if ! [[ "${RUNTIME_NAME}" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then
    fail "--runtime-name is invalid"
fi
if ! [[ "${RUNTIME_VERSION}" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$ ]]; then
    fail "--runtime-version is invalid"
fi
if ! [[ "${MODEL}" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._:+/@-]{0,255}$ ]]; then
    fail "--model is invalid"
fi
if ! [[ "${PROVIDER_HOST}" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$ ]]; then
    fail "--provider-host is invalid"
fi
case "${PROVIDER_BASE_URL}" in
    https://*) ;;
    *) fail "--provider-base-url must use https" ;;
esac
case "${PROVIDER_BASE_URL}" in
    *[[:space:]]*|*"@"*) fail "--provider-base-url must not contain whitespace or credentials" ;;
esac
case "${PROVIDER_BASE_URL}" in
    */) PROVIDER_BASE_URL="${PROVIDER_BASE_URL%/}" ;;
esac
while [ "${PROVIDER_BASE_URL}" != "${PROVIDER_BASE_URL%/v1}" ]; do
    PROVIDER_BASE_URL="${PROVIDER_BASE_URL%/v1}"
done
[ -n "${PROVIDER_BASE_URL}" ] || fail "--provider-base-url is invalid"
case "${PROVIDER_BASE_URL}" in
    https://?*) ;;
    *) fail "--provider-base-url is invalid" ;;
esac

# The broker injects the real provider key into this URL.  A token claims
# --provider-host, so the URL host and port must be exactly the same endpoint.
if ! python3 - "${PROVIDER_BASE_URL}" "${PROVIDER_HOST}" <<'PY'
import sys
from urllib.parse import urlsplit


def url_endpoint(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SystemExit(1)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SystemExit(1)
    try:
        port = parsed.port or 443
    except ValueError:
        raise SystemExit(1)
    return parsed.hostname, port


def provider_endpoint(value):
    parsed = urlsplit("//" + value)
    if not parsed.hostname:
        raise SystemExit(1)
    try:
        port = parsed.port or 443
    except ValueError:
        raise SystemExit(1)
    return parsed.hostname, port


if url_endpoint(sys.argv[1]) != provider_endpoint(sys.argv[2]):
    raise SystemExit(1)
PY
then
    fail "--provider-base-url host/port must equal --provider-host"
fi

require_repo_file "${PROVIDER_KEY_FILE}" "--provider-key-file"
[ ! -L "${PROVIDER_KEY_FILE}" ] || fail "--provider-key-file must not be a symlink"
case "${PROVIDER_KEY_FILE}" in
    "${REPO_ROOT}"|"${REPO_ROOT}"/*)
        fail "--provider-key-file must not be inside the repository"
        ;;
esac
provider_size=$(wc -c < "${PROVIDER_KEY_FILE}")
[ "${provider_size}" -ge 1 ] || fail "--provider-key-file is empty"
[ "${provider_size}" -le 4096 ] || fail "--provider-key-file is too large"
if python3 - "${PROVIDER_KEY_FILE}" <<'PY'
import sys
raw = open(sys.argv[1], "rb").read()
value = raw.rstrip(b"\r\n")
if not value or b"\n" in value or b"\r" in value:
    raise SystemExit(1)
PY
then
    :
else
    fail "--provider-key-file must contain exactly one non-empty line"
fi

id -u "${ADAPTER_USER}" >/dev/null 2>&1 || \
    fail "--adapter-user must already exist (this script never creates the adapter account)"

# ---------------------------------------------------------------------------
# groups, users and directories
# ---------------------------------------------------------------------------

ensure_group() {
    local group=$1
    if getent group "${group}" >/dev/null 2>&1; then
        return 0
    fi
    log "creating group ${group}"
    run groupadd --system "${group}"
}

add_to_group() {
    local user=$1 group=$2
    if id -nG "${user}" | tr ' ' '\n' | grep -qx "${group}"; then
        return 0
    fi
    log "adding ${user} to ${group}"
    run usermod -aG "${group}" "${user}"
}

# ensure_directory PATH OWNER GROUP MODE
#
# Fixed deployment directories are fail-closed against symlinks and non-
# directories, and are repaired to the expected owner/group/mode.  A repair is
# logged, so a pre-existing world-writable or wrong-owner directory cannot be
# silently reused by the helper or broker.
ensure_directory() {
    local path=$1 owner=$2 group=$3 mode=$4
    if [ -L "${path}" ]; then
        fail "refusing a symlinked directory: ${path}"
    fi
    if [ -e "${path}" ]; then
        [ -d "${path}" ] || fail "not a directory: ${path}"
        local current_owner current_group current_mode expected_mode
        current_owner=$(stat -c '%U' "${path}")
        current_group=$(stat -c '%G' "${path}")
        current_mode=$(stat -c '%a' "${path}")
        expected_mode=$(printf '%o' "$((8#${mode}))")
        if [ "${current_owner}" != "${owner}" ] || \
            [ "${current_group}" != "${group}" ] || \
            [ "${current_mode}" != "${expected_mode}" ]; then
            log "repairing directory ${path} (${current_owner}:${current_group} ${current_mode})"
            run chown "${owner}:${group}" "${path}"
            run chmod "${mode}" "${path}"
        fi
        return 0
    fi
    run install -d -o "${owner}" -g "${group}" -m "${mode}" "${path}"
}

# install_file SRC DST OWNER GROUP MODE
#
# A destination symlink would make `install` write through it, potentially into
# a personal profile or another host path.  Refuse anything that is not an
# absent path or a regular file before installing.
install_file() {
    local src=$1 dst=$2 owner=$3 group=$4 mode=$5
    if [ -L "${dst}" ]; then
        fail "refusing a symlinked install target: ${dst}"
    fi
    if [ -e "${dst}" ] && [ ! -f "${dst}" ]; then
        fail "install target is not a regular file: ${dst}"
    fi
    run install -o "${owner}" -g "${group}" -m "${mode}" -- "${src}" "${dst}"
}

ensure_group "${SANDBOX_GROUP}"
ensure_group "${ADAPTER_GROUP}"
add_to_group "${ADAPTER_USER}" "${SANDBOX_GROUP}"
add_to_group "${ADAPTER_USER}" "${ADAPTER_GROUP}"

if ! id -u "${BROKER_USER}" >/dev/null 2>&1; then
    log "creating dedicated broker user ${BROKER_USER}"
    run useradd --system --gid "${SANDBOX_GROUP}" --home-dir /nonexistent \
        --no-create-home --shell /usr/sbin/nologin "${BROKER_USER}"
fi
add_to_group "${BROKER_USER}" "${SANDBOX_GROUP}"

ensure_directory "${AGENTMGR_ETC}" root root 0755
ensure_directory "${HERMES_ETC}" root root 0755
ensure_directory "${TASK_ROOT}" root "${SANDBOX_GROUP}" 0770
ensure_directory "${CONTROL_DIR}" "${BROKER_USER}" "${SANDBOX_GROUP}" 0750
ensure_directory "${REVOCATION_DIR}" "${ADAPTER_USER}" "${SANDBOX_GROUP}" 0750
ensure_directory /usr/local/libexec root root 0755

# ---------------------------------------------------------------------------
# read-only runtime root
# ---------------------------------------------------------------------------

prepare_runtime_root() {
    local source_hash installed_hash
    source_hash=$(compute_runtime_hash "${SOURCE}")

    if [ "${RUNTIME_MODE}" = "bind" ]; then
        require_cmd mount
        require_cmd mountpoint
        if [ -L "${RUNTIME_ROOT}" ]; then
            fail "refusing a symlinked runtime root: ${RUNTIME_ROOT}"
        fi
        if [ "${DRY_RUN}" -eq 1 ]; then
            log "dry run: would ensure a read-only bind mount of --source"
        elif ! mountpoint -q "${RUNTIME_ROOT}" 2>/dev/null; then
            # Repair/create the mount point before mounting over it.
            ensure_directory "${RUNTIME_ROOT}" root root 0555
            log "bind-mounting the runtime source read-only"
            run mount --bind "${SOURCE}" "${RUNTIME_ROOT}"
            run mount -o remount,bind,ro "${RUNTIME_ROOT}"
        elif [ "${REFRESH_RUNTIME}" -eq 1 ]; then
            current_hash=$(compute_runtime_hash "${RUNTIME_ROOT}")
            if [ "${current_hash}" != "${source_hash}" ]; then
                log "remounting the runtime source read-only (--refresh-runtime)"
                run umount "${RUNTIME_ROOT}"
                run mount --bind "${SOURCE}" "${RUNTIME_ROOT}"
                run mount -o remount,bind,ro "${RUNTIME_ROOT}"
            fi
        fi
        if [ "${DRY_RUN}" -eq 1 ]; then
            installed_hash="${source_hash}"
        else
            installed_hash=$(compute_runtime_hash "${RUNTIME_ROOT}")
            [ "${installed_hash}" = "${source_hash}" ] || \
                fail "the bind-mounted runtime root does not match the source manifest"
        fi
    else
        if [ -L "${RUNTIME_ROOT}" ]; then
            fail "refusing a symlinked runtime root: ${RUNTIME_ROOT}"
        fi
        if [ -d "${RUNTIME_ROOT}" ] && mountpoint -q "${RUNTIME_ROOT}" 2>/dev/null; then
            fail "the runtime root is a mount point in copy mode; use --runtime-mode bind or unmount it"
        fi
        if [ -d "${RUNTIME_ROOT}" ] && [ -n "$(ls -A "${RUNTIME_ROOT}" 2>/dev/null || true)" ]; then
            installed_hash=$(compute_runtime_hash "${RUNTIME_ROOT}")
            if [ "${installed_hash}" != "${source_hash}" ]; then
                if [ "${REFRESH_RUNTIME}" -eq 1 ]; then
                    log "replacing the runtime root (--refresh-runtime)"
                    run rm -rf --one-file-system -- "${RUNTIME_ROOT}"
                else
                    fail "the runtime root already exists with a different manifest; use --refresh-runtime"
                fi
            fi
        fi
        if [ ! -d "${RUNTIME_ROOT}" ] || [ -z "$(ls -A "${RUNTIME_ROOT}" 2>/dev/null || true)" ]; then
            ensure_directory "${RUNTIME_ROOT}" root root 0755
            if [ "${DRY_RUN}" -eq 0 ] && \
                { [ -L "${RUNTIME_ROOT}" ] || [ ! -d "${RUNTIME_ROOT}" ]; }; then
                fail "the runtime root is not a real directory: ${RUNTIME_ROOT}"
            fi
            log "copying the runtime source into the read-only runtime root"
            run cp -a --no-preserve=ownership -- "${SOURCE}/." "${RUNTIME_ROOT}/"
        fi
        # The runtime root is read-only host code: no owner write, no group or
        # world write.  Executables stay executable for the sandbox unit.
        run find "${RUNTIME_ROOT}" -xdev -type d -exec chmod 0555 {} +
        run find "${RUNTIME_ROOT}" -xdev -type f -perm /111 -exec chmod 0555 {} +
        run find "${RUNTIME_ROOT}" -xdev -type f ! -perm /111 -exec chmod 0444 {} +
        run chown -R root:root -- "${RUNTIME_ROOT}"
        if [ "${DRY_RUN}" -eq 1 ]; then
            installed_hash="${source_hash}"
        else
            installed_hash=$(compute_runtime_hash "${RUNTIME_ROOT}")
            [ "${installed_hash}" = "${source_hash}" ] || \
                fail "the copied runtime root does not match the source manifest"
        fi
    fi

    RUNTIME_ROOT_HASH="${installed_hash}"
}

RUNTIME_ROOT_HASH=""
prepare_runtime_root
[ -n "${RUNTIME_ROOT_HASH}" ] || fail "could not compute the runtime root manifest hash"

# ---------------------------------------------------------------------------
# keys and secrets
# ---------------------------------------------------------------------------

# verify_key_file PATH OWNER GROUP MIN_BYTES ALLOWED_MODES...
verify_key_file() {
    local path=$1 expected_owner=$2 expected_group=$3 min_bytes=$4
    shift 4
    [ -f "${path}" ] || fail "key file is missing: ${path}"
    [ ! -L "${path}" ] || fail "key file must not be a symlink: ${path}"
    local owner group mode size allowed
    owner=$(stat -c '%U' "${path}")
    group=$(stat -c '%G' "${path}")
    mode=$(stat -c '%a' "${path}")
    size=$(stat -c '%s' "${path}")
    [ "${owner}" = "${expected_owner}" ] || fail "key file has the wrong owner: ${path}"
    if [ "${group}" != "${expected_group}" ]; then
        # The design also allows a root-only 0400 variant for the HMAC keys.
        if [ "${mode}" != "400" ] || [ "${group}" != "root" ]; then
            fail "key file has the wrong group: ${path}"
        fi
    fi
    for allowed in "$@"; do
        [ "${mode}" = "${allowed}" ] && break
    done
    [ "${mode}" = "${allowed}" ] || fail "key file has an unsafe mode: ${path}"
    [ "${size}" -ge "${min_bytes}" ] || fail "key file is too short: ${path}"
    [ "${size}" -le 4096 ] || fail "key file is too large: ${path}"
}

generate_key() {
    local path=$1
    if [ -e "${path}" ]; then
        log "key ${path} already exists; keeping it (rotation is an explicit operator action)"
        verify_key_file "${path}" root "${ADAPTER_GROUP}" 32 400 440
        return 0
    fi
    log "generating ${path}"
    if [ "${DRY_RUN}" -eq 1 ]; then
        printf '+ generate %s\n' "${path}" >&2
        return 0
    fi
    python3 - "${path}" <<'PY'
import os
import secrets
import sys

path = sys.argv[1]
data = secrets.token_hex(32).encode("ascii")
descriptor = os.open(
    path,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o440,
)
try:
    os.fchmod(descriptor, 0o440)
    os.write(descriptor, data)
finally:
    os.close(descriptor)
PY
    chown root:"${ADAPTER_GROUP}" "${path}"
}

generate_key "${ATTESTATION_KEY}"
generate_key "${EGRESS_TOKEN_KEY}"

if [ "${DRY_RUN}" -eq 0 ]; then
    if cmp -s "${ATTESTATION_KEY}" "${EGRESS_TOKEN_KEY}"; then
        fail "the attestation and egress-token keys must be different"
    fi
fi

if [ -e "${PROVIDER_KEY}" ]; then
    verify_key_file "${PROVIDER_KEY}" root root 1 400
    if ! cmp -s -- "${PROVIDER_KEY}" "${PROVIDER_KEY_FILE}"; then
        fail "the existing provider key differs from --provider-key-file; remove ${PROVIDER_KEY} explicitly to rotate it"
    fi
    log "provider key already installed; keeping it (rotation is an explicit operator action)"
else
    log "installing the dedicated provider key (contents are never printed)"
    install_file "${PROVIDER_KEY_FILE}" "${PROVIDER_KEY}" root root 0400
fi

# ---------------------------------------------------------------------------
# render configuration, sudoers and the broker unit
# ---------------------------------------------------------------------------

PY_ARGS=()
if [ "${DRY_RUN}" -eq 0 ]; then
    STAGING=$(mktemp -d "${HERMES_ETC}/.provision.XXXXXX")
    chmod 0700 "${STAGING}"
    PY_ARGS=(--staging "${STAGING}")
fi

export AGENTMGR_PROVISION_INSTALL_ROOT="${INSTALL_ROOT}"
export AGENTMGR_PROVISION_VENV="${VENV}"
export AGENTMGR_PROVISION_SANDBOX_GROUP="${SANDBOX_GROUP}"
export AGENTMGR_PROVISION_ADAPTER_GROUP="${ADAPTER_GROUP}"
export AGENTMGR_PROVISION_ADAPTER_USER="${ADAPTER_USER}"
export AGENTMGR_PROVISION_BROKER_USER="${BROKER_USER}"
export AGENTMGR_PROVISION_RUNTIME_ROOT="${RUNTIME_ROOT}"
export AGENTMGR_PROVISION_RUNTIME_ROOT_HASH="${RUNTIME_ROOT_HASH}"
export AGENTMGR_PROVISION_TASK_ROOT="${TASK_ROOT}"
export AGENTMGR_PROVISION_CONTROL_DIR="${CONTROL_DIR}"
export AGENTMGR_PROVISION_CONTROL_SOCKET="${CONTROL_SOCKET}"
export AGENTMGR_PROVISION_REVOCATION_DIR="${REVOCATION_DIR}"
export AGENTMGR_PROVISION_INSTALL_CONFIG="${INSTALL_CONFIG}"
export AGENTMGR_PROVISION_HELPER_PATH="${HELPER_PATH}"
export AGENTMGR_PROVISION_WORKLOAD_PATH="${WORKLOAD_PATH}"
export AGENTMGR_PROVISION_ATTESTATION_KEY="${ATTESTATION_KEY}"
export AGENTMGR_PROVISION_EGRESS_TOKEN_KEY="${EGRESS_TOKEN_KEY}"
export AGENTMGR_PROVISION_PROVIDER_KEY="${PROVIDER_KEY}"
export AGENTMGR_PROVISION_RUNTIME_ATTESTATION="${RUNTIME_ATTESTATION}"
export AGENTMGR_PROVISION_CLI_CONFIG="${CLI_CONFIG}"
export AGENTMGR_PROVISION_BROKER_CONFIG="${BROKER_CONFIG}"
export AGENTMGR_PROVISION_BROKER_CREDENTIAL_PREFIX="${BROKER_CREDENTIAL_PREFIX}"
export AGENTMGR_PROVISION_DEPLOYMENT_RECORD="${DEPLOYMENT_RECORD}"
export AGENTMGR_PROVISION_SUDOERS_PATH="${SUDOERS_PATH}"
export AGENTMGR_PROVISION_UNIT_PATH="${UNIT_PATH}"
export AGENTMGR_PROVISION_WRAPPER_PATH="${WRAPPER_PATH}"
export AGENTMGR_PROVISION_SERVICE_IDENTITY_ID="${SERVICE_IDENTITY_ID}"
export AGENTMGR_PROVISION_WORKER_ID="${WORKER_ID}"
export AGENTMGR_PROVISION_RUNTIME_MAX_SEC="${RUNTIME_MAX_SEC}"
export AGENTMGR_PROVISION_RUNTIME_NAME="${RUNTIME_NAME}"
export AGENTMGR_PROVISION_RUNTIME_VERSION="${RUNTIME_VERSION}"
export AGENTMGR_PROVISION_MODEL="${MODEL}"
export AGENTMGR_PROVISION_PROVIDER_HOST="${PROVIDER_HOST}"
export AGENTMGR_PROVISION_PROVIDER_BASE_URL="${PROVIDER_BASE_URL}"
export AGENTMGR_PROVISION_EVIDENCE_REF="${EVIDENCE_REF}"
export AGENTMGR_PROVISION_RUNTIME_MODE="${RUNTIME_MODE}"
export AGENTMGR_PROVISION_SOURCE="${SOURCE}"
export AGENTMGR_PROVISION_REPO_ROOT="${REPO_ROOT}"
export AGENTMGR_PROVISION_DRY_RUN="${DRY_RUN}"

python3 - "${PY_ARGS[@]}" <<'PY'
import datetime
import json
import os
import re
import sys
from pathlib import Path

env = os.environ
dry_run = env["AGENTMGR_PROVISION_DRY_RUN"] == "1"
staging = Path(sys.argv[sys.argv.index("--staging") + 1]) if "--staging" in sys.argv else None

CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def value(name):
    raw = env["AGENTMGR_PROVISION_" + name]
    if not raw or CONTROL_CHARS.search(raw):
        raise SystemExit("invalid provisioning value: " + name)
    return raw


def write(name, text, description):
    if dry_run:
        print("+ would write " + description, file=sys.stderr)
        return
    assert staging is not None
    target = staging / name
    descriptor = os.open(
        target,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, text.encode("utf-8"))
    finally:
        os.close(descriptor)


install_config = {
    "task_root": value("TASK_ROOT"),
    "runtime_root": value("RUNTIME_ROOT"),
    "runtime_root_hash": value("RUNTIME_ROOT_HASH"),
    "broker_socket": value("CONTROL_SOCKET"),
    "workload": value("WORKLOAD_PATH"),
    "adapter_user": value("ADAPTER_USER"),
    "adapter_group": value("ADAPTER_GROUP"),
    "attestation_key_path": value("ATTESTATION_KEY"),
    "evidence_ref": value("EVIDENCE_REF"),
    "runtime_name": value("RUNTIME_NAME"),
    "runtime_version": value("RUNTIME_VERSION"),
    "model_name": value("MODEL"),
    "stop_timeout_sec": 30,
    "start_timeout_sec": 30,
}
write(
    "hermes-exec.json",
    json.dumps(install_config, sort_keys=True, indent=2) + "\n",
    value("INSTALL_CONFIG"),
)

cli_config = {
    "schema": "agentmgr.hermes.managed-cli.v1",
    "service_identity_id": value("SERVICE_IDENTITY_ID"),
    "helper_path": value("WRAPPER_PATH"),
    "runtime_root": value("RUNTIME_ROOT"),
    "runtime_root_hash": value("RUNTIME_ROOT_HASH"),
    "task_root": value("TASK_ROOT"),
    "control_dir": value("CONTROL_DIR"),
    "broker_socket": value("CONTROL_SOCKET"),
    "revocation_dir": value("REVOCATION_DIR"),
    "attestation_key_path": value("ATTESTATION_KEY"),
    "egress_token_key_path": value("EGRESS_TOKEN_KEY"),
    "runtime_attestation_path": value("RUNTIME_ATTESTATION"),
    "evidence_ref": value("EVIDENCE_REF"),
    "runtime_name": value("RUNTIME_NAME"),
    "runtime_version": value("RUNTIME_VERSION"),
    "model_name": value("MODEL"),
    "egress_provider_host": value("PROVIDER_HOST"),
    "worker_id": value("WORKER_ID"),
    "runtime_max_sec": int(value("RUNTIME_MAX_SEC")),
    "lease_seconds": 60,
    "poll_interval": 1.0,
    "health_backoff_seconds": 5.0,
}
write(
    "managed-cli.json",
    json.dumps(cli_config, sort_keys=True, indent=2) + "\n",
    value("CLI_CONFIG"),
)

broker_config = {
    "socket_path": value("CONTROL_SOCKET"),
    "upstream_base_url": value("PROVIDER_BASE_URL"),
    "provider_host": value("PROVIDER_HOST"),
    "token_key_path": value("BROKER_CREDENTIAL_PREFIX") + "/egress-token-key",
    "provider_key_path": value("BROKER_CREDENTIAL_PREFIX") + "/provider-key",
    "revocation_dir": value("REVOCATION_DIR"),
    "task_root": value("TASK_ROOT"),
}
write(
    "egress-broker.json",
    json.dumps(broker_config, sort_keys=True, indent=2) + "\n",
    value("BROKER_CONFIG"),
)

deployment = {
    "schema": "agentmgr.hermes.deployment.v1",
    "install_root": value("INSTALL_ROOT"),
    "venv": value("VENV"),
    "adapter_user": value("ADAPTER_USER"),
    "adapter_group": value("ADAPTER_GROUP"),
    "broker_user": value("BROKER_USER"),
    "sandbox_group": value("SANDBOX_GROUP"),
    "runtime_source": value("SOURCE"),
    "runtime_mode": value("RUNTIME_MODE"),
    "runtime_root": value("RUNTIME_ROOT"),
    "runtime_root_hash": value("RUNTIME_ROOT_HASH"),
    "provisioned_at": datetime.datetime.now(datetime.UTC)
    .isoformat()
    .replace("+00:00", "Z"),
}
write(
    "deployment.json",
    json.dumps(deployment, sort_keys=True, indent=2) + "\n",
    value("DEPLOYMENT_RECORD"),
)

templates = Path(value("INSTALL_ROOT")) / "ops" / "security"
sudoers_template = (templates / "agentmgr-hermes.sudoers").read_text(encoding="utf-8")
placeholder = (
    'agentmgr ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-hermes-exec ""'
)
replacement = (
    value("ADAPTER_USER")
    + ' ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-hermes-exec ""'
)
if placeholder not in sudoers_template:
    raise SystemExit("the sudoers template lost its fixed rule")
sudoers = sudoers_template.replace(placeholder, replacement)
write("sudoers", sudoers, value("SUDOERS_PATH"))

unit_template = (templates / "agentmgr-hermes-egress.service").read_text(
    encoding="utf-8"
)
unit = (
    unit_template.replace("<broker-user>", value("BROKER_USER"))
    .replace("<install-root>", value("INSTALL_ROOT"))
    .replace("<venv>", value("VENV"))
)
for marker in ("<broker-user>", "<install-root>", "<venv>"):
    if marker in unit:
        raise SystemExit("the broker unit template has an unrendered placeholder")
write("agentmgr-hermes-egress.service", unit, value("UNIT_PATH"))
PY

if [ "${DRY_RUN}" -eq 1 ]; then
    log "dry run: validated inputs and planned writes; nothing was installed"
    exit 0
fi

# The sudoers rule is validated before it is installed.
if ! command -v visudo >/dev/null 2>&1; then
    fail "visudo is required to validate the sudoers rule"
fi
visudo -cf "${STAGING}/sudoers" >/dev/null

# ---------------------------------------------------------------------------
# install files and the read-only helper assets
# ---------------------------------------------------------------------------

install_file "${INSTALL_ROOT}/ops/security/agentmgr_hermes_exec.py" "${HELPER_PATH}" root root 0644
install_file "${INSTALL_ROOT}/ops/security/agentmgr-hermes-exec" "${WRAPPER_PATH}" root root 0755
install_file "${INSTALL_ROOT}/ops/security/agentmgr_hermes_workload.py" "${WORKLOAD_PATH}" root root 0555

install_file "${STAGING}/hermes-exec.json" "${INSTALL_CONFIG}" root root 0640
install_file "${STAGING}/managed-cli.json" "${CLI_CONFIG}" root "${ADAPTER_GROUP}" 0440
install_file "${STAGING}/egress-broker.json" "${BROKER_CONFIG}" root "${SANDBOX_GROUP}" 0440
install_file "${STAGING}/deployment.json" "${DEPLOYMENT_RECORD}" root root 0640
install_file "${STAGING}/sudoers" "${SUDOERS_PATH}" root root 0440
visudo -c >/dev/null
install_file "${STAGING}/agentmgr-hermes-egress.service" "${UNIT_PATH}" root root 0644

printf '%s\n' "${RUNTIME_ROOT_HASH}" > "${STAGING}/runtime-root.hash"
install_file "${STAGING}/runtime-root.hash" "${RUNTIME_HASH_FILE}" root root 0444

# The runtime-attestation record is the provisioning snapshot the launcher
# preflight checks; the live helper issue remains the verification authority.
python3 - "${HELPER_PATH}" "${INSTALL_CONFIG}" "${ATTESTATION_KEY}" "${STAGING}/runtime-attestation.json" <<'PY'
import importlib.util
import json
import os
import sys
from pathlib import Path

helper_path, install_config, key_path, output = sys.argv[1:5]
spec = importlib.util.spec_from_file_location("agentmgr_hermes_exec_installed", helper_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
config = module.load_install_config(Path(install_config))
key = Path(key_path).read_bytes()
document = module.runtime_attestation(config, key=key)
raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
descriptor = os.open(
    output,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
    0o600,
)
try:
    os.write(descriptor, raw)
finally:
    os.close(descriptor)
PY
install_file "${STAGING}/runtime-attestation.json" "${RUNTIME_ATTESTATION}" root "${ADAPTER_GROUP}" 0440

# The attestation snapshot carries broker health at provisioning time; the
# launcher only checks static bindings, so a not-yet-started broker does not
# block the record.  Re-sign it after the broker is verified if desired.

# ---------------------------------------------------------------------------
# summary (no secrets, no personal paths)
# ---------------------------------------------------------------------------

rm -rf --one-file-system -- "${STAGING}"
STAGING=""

log "installed helper, wrapper, workload, sudoers, broker unit and launcher config"
log "runtime root hash: ${RUNTIME_ROOT_HASH}"
log "next: systemctl daemon-reload"
log "next: systemctl enable --now agentmgr-hermes-egress.service"
log "next: ops/security/verify_hermes_isolation.sh"
log "the personal/default Hermes gateway and profiles were not touched"
