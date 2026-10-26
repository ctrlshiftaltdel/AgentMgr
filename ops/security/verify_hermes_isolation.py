#!/usr/bin/env python3
"""Read-only verification of a provisioned Hermes managed-execution host.

Issue #97, Task 9.  This script checks the *installed* deployment surface and
never changes state, starts a unit, calls the helper or reads a prompt, token or
provider key.  It is deliberately standard-library only so it can run on a host
whose application venv is broken, and it fails closed on any mismatch.

Checks:

* deployment record, installed helper/wrapper/workload hashes and modes;
* the sudoers rule is exactly one fixed, argument-less wrapper command for the
  configured adapter user;
* helper install config, launcher config and broker config are root-controlled
  and agree on the fixed paths, runtime root hash, model and provider host;
* the runtime root manifest hash is recomputed and compared with all three
  records (plus the recorded hash file);
* the runtime root has no personal-profile markers and no symlink or bind mount
  escaping to a home/profile path;
* the task root, broker control directory/socket, revocation store and every
  key file have the exact owner, group and mode the sandbox boundary needs;
* the installed helper builds the fixed transient-unit argv and refuses
  caller-supplied systemd properties;
* the sandbox group and adapter-private group memberships are intact.

Usage:
    sudo ops/security/verify_hermes_isolation.sh

The script exits 0 only when every check passes.  Run it after provisioning and
after starting the host egress broker; no task unit is required.
"""

from __future__ import annotations

import argparse
from collections import Counter
import grp
import hashlib
import importlib.util
import json
import os
import pwd
import re
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

# Fixed deployment surface (mirrors provision_hermes_runtime.sh and the helper).
SANDBOX_GROUP = "agentmgr-hermes"
ADAPTER_GROUP = "agentmgr-hermes-adapter"
RUNTIME_ROOT = Path("/var/lib/agentmgr-hermes-runtime")
TASK_ROOT = Path("/var/lib/agentmgr-hermes-tasks")
CONTROL_DIR = Path("/var/lib/agentmgr-hermes-control")
CONTROL_SOCKET = CONTROL_DIR / "egress.sock"
REVOCATION_DIR = Path("/var/lib/agentmgr-hermes-revoked")
AGENTMGR_ETC = Path("/etc/agentmgr")
HERMES_ETC = AGENTMGR_ETC / "hermes"
HELPER_PATH = Path("/usr/local/libexec/agentmgr_hermes_exec.py")
WRAPPER_PATH = Path("/usr/local/libexec/agentmgr-hermes-exec")
WORKLOAD_PATH = Path("/usr/local/libexec/agentmgr_hermes_workload.py")
SUDOERS_PATH = Path("/etc/sudoers.d/agentmgr-hermes")
UNIT_PATH = Path("/etc/systemd/system/agentmgr-hermes-egress.service")
INSTALL_CONFIG = AGENTMGR_ETC / "hermes-exec.json"
CLI_CONFIG = HERMES_ETC / "managed-cli.json"
BROKER_CONFIG = HERMES_ETC / "egress-broker.json"
DEPLOYMENT_RECORD = HERMES_ETC / "deployment.json"
ATTESTATION_KEY = HERMES_ETC / "attestation-key"
EGRESS_TOKEN_KEY = HERMES_ETC / "egress-token-key"
PROVIDER_KEY = HERMES_ETC / "provider-key"
RUNTIME_ATTESTATION = HERMES_ETC / "runtime-attestation.json"
RUNTIME_HASH_FILE = HERMES_ETC / "runtime-root.hash"
BROKER_CREDENTIAL_PREFIX = "/run/credentials/agentmgr-hermes-egress.service"

PERSONAL_PREFIXES = (Path("/home"), Path("/root"))
PROFILE_MARKERS = (".env", "auth.json", "gateway.lock", "locks", "profiles", "sessions")
SECRET_FIELD_NAMES = frozenset(
    {
        "api_key",
        "credential",
        "credentials",
        "password",
        "passwd",
        "provider_key",
        "secret",
        "token",
    }
)
SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
SERVICE_IDENTITY = re.compile(r"^svc_[1-9][0-9]{0,18}$")


class VerificationError(Exception):
    """One failed check with a stable code and an optional non-secret detail."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code)


@dataclass(frozen=True)
class Deployment:
    install_root: Path
    venv: Path
    adapter_user: str
    adapter_group: str
    broker_user: str
    sandbox_group: str
    runtime_source: Path
    runtime_mode: str
    runtime_root: Path
    runtime_root_hash: str


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def _fail(code: str, detail: str = "") -> None:
    raise VerificationError(code, detail)


def _lstat(path: Path, code: str) -> os.stat_result:
    try:
        return os.lstat(path)
    except OSError:
        _fail(code, str(path))


def _require_regular(path: Path, code: str) -> os.stat_result:
    info = _lstat(path, code)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _fail(code, str(path))
    return info


def _require_dir(path: Path, code: str) -> os.stat_result:
    info = _lstat(path, code)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _fail(code, str(path))
    return info


def _owner(info: os.stat_result) -> str:
    try:
        return pwd.getpwuid(info.st_uid).pw_name
    except KeyError:
        return str(info.st_uid)


def _group(info: os.stat_result) -> str:
    try:
        return grp.getgrgid(info.st_gid).gr_name
    except KeyError:
        return str(info.st_gid)


def _mode(info: os.stat_result) -> int:
    return stat.S_IMODE(info.st_mode)


def _expect_mode(info: os.stat_result, allowed: tuple[int, ...], code: str) -> None:
    if _mode(info) not in allowed:
        _fail(code, oct(_mode(info)))


def _expect_owner_group(
    info: os.stat_result,
    *,
    owner: str | None,
    group: str | None,
    code: str,
) -> None:
    if owner is not None and _owner(info) != owner:
        _fail(code, f"owner={_owner(info)}")
    if group is not None and _group(info) != group:
        _fail(code, f"group={_group(info)}")


def _read_json(path: Path, code: str) -> dict[str, object]:
    _require_regular(path, code)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail(code, str(path))
    if not isinstance(document, dict):
        _fail(code, str(path))
    return document


def _sha256_file(path: Path, code: str) -> str:
    _require_regular(path, code)
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        _fail(code, str(path))
    return digest.hexdigest()


def _same_file_content(first: Path, second: Path, code: str) -> None:
    if _sha256_file(first, code) != _sha256_file(second, code):
        _fail(code, f"{first} does not match {second}")


def _is_inside(path: Path, root: Path) -> bool:
    if path == root:
        return True
    try:
        return path.is_relative_to(root)
    except ValueError:  # pragma: no cover - defensive
        return False


def _personal_path(path: Path) -> bool:
    if any(_is_inside(path, prefix) for prefix in PERSONAL_PREFIXES):
        return True
    parts = path.parts
    for index, part in enumerate(parts):
        if part == ".hermes":
            return True
        if part == ".config" and index + 1 < len(parts) and parts[index + 1] == "hermes":
            return True
    return False


def _group_entry(name: str, code: str) -> grp.struct_group:
    try:
        return grp.getgrnam(name)
    except KeyError:
        _fail(code, name)


def _user_entry(name: str, code: str) -> pwd.struct_passwd:
    try:
        return pwd.getpwnam(name)
    except KeyError:
        _fail(code, name)


# ---------------------------------------------------------------------------
# runtime-root manifest (identical to provision_hermes_runtime.sh)
# ---------------------------------------------------------------------------


def _runtime_marker(name: str) -> bool:
    """Return whether an entry name is a personal-profile marker at any depth."""

    return name in PROFILE_MARKERS


def _runtime_manifest(root: Path) -> str:
    """Return the deterministic runtime-root manifest hash or raise.

    Every directory is walked.  A profile marker at **any** depth, an absolute
    symlink (which could point at host home/profile state) and a relative
    symlink escaping the root are rejected before they can be hashed.  The
    provisioner uses the identical rules; a test pins the two implementations.
    """

    if not root.is_dir():
        _fail("runtime_root_unavailable", str(root))
    entries: list[tuple[str, str, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        for name in list(dirnames):
            candidate = os.path.join(dirpath, name)
            if os.path.islink(candidate):
                dirnames.remove(name)
                filenames.append(name)
        dirnames.sort()
        for name in dirnames:
            if _runtime_marker(name):
                _fail("runtime_root_profile_marker", name)
        if rel_dir != ".":
            entries.append(("d", rel_dir, 0, ""))
        for name in sorted(filenames):
            if _runtime_marker(name):
                _fail("runtime_root_profile_marker", name)
            full = os.path.join(dirpath, name)
            rel = os.path.normpath(os.path.join(rel_dir, name)) if rel_dir != "." else name
            try:
                info = os.lstat(full)
            except OSError:
                _fail("runtime_root_unreadable", rel)
            if stat.S_ISLNK(info.st_mode):
                target = os.readlink(full)
                if os.path.isabs(target):
                    # Even a read-only runtime may not reach an absolute host
                    # path outside itself; the unit only sees this root.
                    _fail("runtime_root_symlink_escape", rel)
                normalized = os.path.normpath(os.path.join(rel_dir, target))
                if normalized == ".." or normalized.startswith("../"):
                    _fail("runtime_root_symlink_escape", rel)
                entries.append(("l", rel, len(target), target))
                continue
            if not stat.S_ISREG(info.st_mode):
                _fail("runtime_root_non_regular", rel)
            digest = hashlib.sha256()
            try:
                with open(full, "rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
            except OSError:
                _fail("runtime_root_unreadable", rel)
            entries.append(("f", rel, info.st_size, digest.hexdigest()))
    total = hashlib.sha256()
    for kind, rel, size, payload in sorted(entries):
        total.update(f"{kind}\0{rel}\0{size}\0{payload}\n".encode("utf-8"))
    return "sha256:" + total.hexdigest()


def _profile_marker(path: Path) -> str | None:
    """Return the first profile marker below ``path`` (any depth), or ``None``."""

    try:
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            for name in (*dirnames, *filenames):
                if _runtime_marker(name):
                    return os.path.normpath(
                        os.path.join(os.path.relpath(dirpath, path), name)
                    )
    except OSError:
        _fail("runtime_root_unreadable", str(path))
    return None


# ---------------------------------------------------------------------------
# deployment record
# ---------------------------------------------------------------------------


def load_deployment() -> Deployment:
    info = _require_regular(DEPLOYMENT_RECORD, "deployment_record_missing")
    _expect_owner_group(info, owner="root", group=None, code="deployment_record_owner")
    _expect_mode(info, (0o640, 0o600, 0o440), "deployment_record_mode")
    document = _read_json(DEPLOYMENT_RECORD, "deployment_record_invalid")
    required = {
        "schema",
        "install_root",
        "venv",
        "adapter_user",
        "adapter_group",
        "broker_user",
        "sandbox_group",
        "runtime_source",
        "runtime_mode",
        "runtime_root",
        "runtime_root_hash",
    }
    if not required <= set(document):
        _fail("deployment_record_invalid", "missing field")
    if document["schema"] != "agentmgr.hermes.deployment.v1":
        _fail("deployment_record_invalid", "schema")
    values = {name: document[name] for name in required if name != "schema"}
    if not all(isinstance(value, str) and value for value in values.values()):
        _fail("deployment_record_invalid", "type")
    if document["sandbox_group"] != SANDBOX_GROUP:
        _fail("deployment_record_invalid", "sandbox_group")
    if document["adapter_group"] != ADAPTER_GROUP:
        _fail("deployment_record_invalid", "adapter_group")
    if document["runtime_mode"] not in {"copy", "bind"}:
        _fail("deployment_record_invalid", "runtime_mode")
    if Path(document["runtime_root"]) != RUNTIME_ROOT:
        _fail("deployment_record_invalid", "runtime_root")
    if not SHA256_REF.fullmatch(document["runtime_root_hash"]):
        _fail("deployment_record_invalid", "runtime_root_hash")
    return Deployment(
        install_root=Path(document["install_root"]),
        venv=Path(document["venv"]),
        adapter_user=document["adapter_user"],
        adapter_group=document["adapter_group"],
        broker_user=document["broker_user"],
        sandbox_group=document["sandbox_group"],
        runtime_source=Path(document["runtime_source"]),
        runtime_mode=document["runtime_mode"],
        runtime_root=Path(document["runtime_root"]),
        runtime_root_hash=document["runtime_root_hash"],
    )


# ---------------------------------------------------------------------------
# individual checks
# ---------------------------------------------------------------------------


Check = Callable[[Deployment], None]
CHECKS: list[tuple[str, Check]] = []


def check(name: str) -> Callable[[Check], Check]:
    def register(function: Check) -> Check:
        CHECKS.append((name, function))
        return function

    return register


@check("deployment-record")
def _check_deployment_record(deployment: Deployment) -> None:
    if not deployment.install_root.is_dir():
        _fail("install_root_missing", str(deployment.install_root))
    if _personal_path(deployment.install_root):
        _fail("install_root_personal", str(deployment.install_root))
    if _personal_path(deployment.venv):
        _fail("venv_personal", str(deployment.venv))
    if not deployment.venv.is_dir():
        _fail("venv_missing", str(deployment.venv))
    if not (deployment.venv / "python").is_file():
        _fail("venv_python_missing", str(deployment.venv / "python"))
    _user_entry(deployment.adapter_user, "adapter_user_missing")
    _user_entry(deployment.broker_user, "broker_user_missing")


@check("installed-helper")
def _check_helper(deployment: Deployment) -> None:
    info = _require_regular(HELPER_PATH, "helper_missing")
    _expect_owner_group(info, owner="root", group="root", code="helper_owner")
    _expect_mode(info, (0o644, 0o444), "helper_mode")
    _same_file_content(
        HELPER_PATH,
        deployment.install_root / "ops" / "security" / "agentmgr_hermes_exec.py",
        "helper_content_mismatch",
    )


@check("installed-wrapper")
def _check_wrapper(deployment: Deployment) -> None:
    info = _require_regular(WRAPPER_PATH, "wrapper_missing")
    _expect_owner_group(info, owner="root", group="root", code="wrapper_owner")
    if _mode(info) & 0o022 or not _mode(info) & 0o111:
        _fail("wrapper_mode", oct(_mode(info)))
    _same_file_content(
        WRAPPER_PATH,
        deployment.install_root / "ops" / "security" / "agentmgr-hermes-exec",
        "wrapper_content_mismatch",
    )


@check("installed-workload")
def _check_workload(deployment: Deployment) -> None:
    info = _require_regular(WORKLOAD_PATH, "workload_missing")
    _expect_owner_group(info, owner="root", group="root", code="workload_owner")
    if _mode(info) & 0o022 or not _mode(info) & 0o444:
        _fail("workload_mode", oct(_mode(info)))
    _same_file_content(
        WORKLOAD_PATH,
        deployment.install_root / "ops" / "security" / "agentmgr_hermes_workload.py",
        "workload_content_mismatch",
    )


@check("sudoers")
def _check_sudoers(deployment: Deployment) -> None:
    info = _require_regular(SUDOERS_PATH, "sudoers_missing")
    _expect_owner_group(info, owner="root", group="root", code="sudoers_owner")
    _expect_mode(info, (0o440,), "sudoers_mode")
    try:
        text = SUDOERS_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _fail("sudoers_invalid")
    rules = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    expected = (
        f'{deployment.adapter_user} ALL=(root) NOPASSWD: '
        '/usr/local/libexec/agentmgr-hermes-exec ""'
    )
    if rules != [expected]:
        _fail("sudoers_rule_mismatch", "expected exactly one fixed wrapper rule")
    for forbidden in ("NOPASSWD: ALL", " sh -c ", "eval ", "*"):
        if forbidden in text:
            _fail("sudoers_rule_mismatch", "forbidden token")


@check("install-config")
def _check_install_config(deployment: Deployment) -> None:
    info = _require_regular(INSTALL_CONFIG, "install_config_missing")
    _expect_owner_group(info, owner="root", group="root", code="install_config_owner")
    _expect_mode(info, (0o600, 0o640), "install_config_mode")
    document = _read_json(INSTALL_CONFIG, "install_config_invalid")
    expected = {
        "task_root": str(TASK_ROOT),
        "runtime_root": str(RUNTIME_ROOT),
        "runtime_root_hash": deployment.runtime_root_hash,
        "broker_socket": str(CONTROL_SOCKET),
        "workload": str(WORKLOAD_PATH),
        "adapter_user": deployment.adapter_user,
        "adapter_group": deployment.adapter_group,
        "attestation_key_path": str(ATTESTATION_KEY),
    }
    for name in (
        "runtime_name",
        "runtime_version",
        "evidence_ref",
        "model_name",
        "adapter_user",
        "adapter_group",
    ):
        value = document.get(name)
        if not isinstance(value, str) or not value:
            _fail("install_config_mismatch", name)
    for name, value in expected.items():
        if document.get(name) != value:
            _fail("install_config_mismatch", name)
    key_path = Path(str(document.get("attestation_key_path", "")))
    if not _is_inside(key_path, AGENTMGR_ETC):
        _fail("install_config_mismatch", "attestation_key_path")


@check("launcher-config")
def _check_launcher_config(deployment: Deployment) -> None:
    info = _require_regular(CLI_CONFIG, "launcher_config_missing")
    _expect_owner_group(info, owner="root", group=None, code="launcher_config_owner")
    _expect_mode(info, (0o440, 0o400), "launcher_config_mode")
    if _mode(info) == 0o440 and _group(info) != deployment.adapter_group:
        _fail("launcher_config_owner", "group")
    document = _read_json(CLI_CONFIG, "launcher_config_invalid")
    if set(document) & SECRET_FIELD_NAMES:
        _fail("launcher_config_invalid", "secret field")
    if document.get("schema") != "agentmgr.hermes.managed-cli.v1":
        _fail("launcher_config_invalid", "schema")
    service_identity = document.get("service_identity_id")
    if not isinstance(service_identity, str) or not SERVICE_IDENTITY.fullmatch(
        service_identity
    ):
        _fail("launcher_config_invalid", "service_identity_id")
    expected = {
        "helper_path": str(WRAPPER_PATH),
        "runtime_root": str(RUNTIME_ROOT),
        "runtime_root_hash": deployment.runtime_root_hash,
        "task_root": str(TASK_ROOT),
        "control_dir": str(CONTROL_DIR),
        "broker_socket": str(CONTROL_SOCKET),
        "revocation_dir": str(REVOCATION_DIR),
        "attestation_key_path": str(ATTESTATION_KEY),
        "egress_token_key_path": str(EGRESS_TOKEN_KEY),
        "runtime_attestation_path": str(RUNTIME_ATTESTATION),
    }
    for name, value in expected.items():
        if document.get(name) != value:
            _fail("launcher_config_mismatch", name)
    install_document = _read_json(INSTALL_CONFIG, "install_config_invalid")
    for name in ("runtime_name", "runtime_version", "model_name", "evidence_ref"):
        if document.get(name) != install_document.get(name):
            _fail("launcher_config_mismatch", name)
    provider_host = document.get("egress_provider_host")
    if not isinstance(provider_host, str) or not provider_host:
        _fail("launcher_config_invalid", "egress_provider_host")
    runtime_max_sec = document.get("runtime_max_sec")
    if type(runtime_max_sec) is not int or runtime_max_sec < 9:
        _fail("launcher_config_invalid", "runtime_max_sec")


@check("runtime-root-hash")
def _check_runtime_hash(deployment: Deployment) -> None:
    recomputed = _runtime_manifest(RUNTIME_ROOT)
    sources = {
        "deployment_record": deployment.runtime_root_hash,
        "install_config": _read_json(INSTALL_CONFIG, "install_config_invalid").get(
            "runtime_root_hash"
        ),
        "launcher_config": _read_json(CLI_CONFIG, "launcher_config_invalid").get(
            "runtime_root_hash"
        ),
        "runtime_root_hash_file": RUNTIME_HASH_FILE.read_text(encoding="utf-8").strip()
        if RUNTIME_HASH_FILE.is_file()
        else None,
    }
    for name, value in sources.items():
        if value != recomputed:
            _fail("runtime_root_hash_mismatch", name)


def _runtime_root_mount_is_read_only() -> bool:
    """True when the runtime root itself is a read-only bind mount."""

    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) < separator + 4:
            continue
        target = Path(_unescape_mountinfo(fields[4]))
        if target != RUNTIME_ROOT:
            continue
        options = set(fields[5].split(","))
        super_options = set(fields[separator + 3].split(","))
        return "ro" in options or "ro" in super_options
    return False


@check("runtime-root-content")
def _check_runtime_content(deployment: Deployment) -> None:
    marker = _profile_marker(RUNTIME_ROOT)
    if marker is not None:
        _fail("runtime_root_profile_marker", marker)
    info = _lstat(RUNTIME_ROOT, "runtime_root_unavailable")
    # A copy provisioned by the installer is 0555.  A bind source may carry its
    # original mode; the read-only remount is then the enforcement.
    if _mode(info) & 0o222 and not _runtime_root_mount_is_read_only():
        _fail("runtime_root_writable", oct(_mode(info)))
    # The managed workload runs Hermes in-process: it needs the source, a
    # runtime Python and the provisioned dependency environment.  A runtime
    # root missing any of these would install cleanly but fail every task.
    oneshot = RUNTIME_ROOT / "hermes-agent" / "hermes_cli" / "oneshot.py"
    if not oneshot.is_file() or oneshot.is_symlink():
        _fail("runtime_root_hermes_incomplete", "oneshot.py")
    pythons = [
        candidate
        for candidate in (RUNTIME_ROOT / "tools").glob("python-*/bin/python3")
        if candidate.is_file() and not candidate.is_symlink()
    ]
    if not pythons:
        _fail("runtime_root_hermes_incomplete", "tools/python-*/bin/python3")
    installs = RUNTIME_ROOT / "installs"
    if not installs.is_dir() or installs.is_symlink():
        _fail("runtime_root_hermes_incomplete", "installs")
    found = False
    try:
        for dirpath, dirnames, _filenames in os.walk(installs, followlinks=False):
            dirnames[:] = [
                name
                for name in dirnames
                if not os.path.islink(os.path.join(dirpath, name))
            ]
            if os.path.basename(dirpath) == "site-packages":
                found = True
                break
    except OSError:
        _fail("runtime_root_hermes_incomplete", "installs")
    if not found:
        _fail("runtime_root_hermes_incomplete", "installs/**/site-packages")


def _unescape_mountinfo(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


@check("runtime-mounts")
def _check_runtime_mounts(deployment: Deployment) -> None:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        _fail("mountinfo_unavailable")
    for line in lines:
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if len(fields) < separator + 3:
            continue
        target = Path(_unescape_mountinfo(fields[4]))
        source = _unescape_mountinfo(fields[separator + 2])
        if not (target == RUNTIME_ROOT or _is_inside(target, RUNTIME_ROOT)):
            continue
        source_path = Path(source) if source.startswith("/") else None
        if source_path is None:
            continue
        if _personal_path(source_path) or any(
            part in {".env", "auth.json", "profiles", "sessions"}
            for part in source_path.parts
        ):
            _fail("runtime_root_personal_mount", source)
        if deployment.runtime_mode == "bind" and target == RUNTIME_ROOT:
            if source_path != deployment.runtime_source:
                _fail("runtime_root_bind_source_mismatch", source)


@check("broker-unit")
def _check_broker_unit(deployment: Deployment) -> None:
    info = _require_regular(UNIT_PATH, "broker_unit_missing")
    _expect_owner_group(info, owner="root", group="root", code="broker_unit_owner")
    _expect_mode(info, (0o644,), "broker_unit_mode")
    try:
        text = UNIT_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        _fail("broker_unit_invalid")
    required = {
        "Group=" + SANDBOX_GROUP,
        "StateDirectory=agentmgr-hermes-control",
        "StateDirectoryMode=0750",
        "ConditionPathExists=" + str(BROKER_CONFIG),
        "WorkingDirectory=" + str(deployment.install_root),
        "ExecStart="
        + str(deployment.venv)
        + "/python -m app.runtime.hermes_egress --config "
        + str(BROKER_CONFIG),
        "LoadCredential=egress-token-key:" + str(EGRESS_TOKEN_KEY),
        "LoadCredential=provider-key:" + str(PROVIDER_KEY),
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "NoNewPrivileges=yes",
        "Restart=no",
    }
    lines = {line.strip() for line in text.splitlines()}
    for expected in required:
        if expected not in lines:
            _fail("broker_unit_mismatch", expected)
    for forbidden in ("PrivateNetwork=yes", "Restart=always", "User=root"):
        if forbidden in lines:
            _fail("broker_unit_mismatch", forbidden)


def _provider_endpoint(value: str) -> tuple[str, int] | None:
    """Return ``(host, port)`` for an https base URL, or ``None`` if invalid."""

    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return parsed.hostname, port


def _provider_host_endpoint(value: str) -> tuple[str, int] | None:
    """Return ``(host, port)`` for the broker's claimed provider host."""

    from urllib.parse import urlsplit

    try:
        parsed = urlsplit("//" + value)
    except ValueError:
        return None
    if not parsed.hostname:
        return None
    try:
        port = parsed.port or 443
    except ValueError:
        return None
    return parsed.hostname, port


def _provider_target_matches(upstream_base_url: str, provider_host: str) -> bool:
    """True only when the real-key destination equals the token's host claim."""

    target = _provider_endpoint(upstream_base_url)
    claim = _provider_host_endpoint(provider_host)
    return target is not None and claim is not None and target == claim


@check("broker-config")
def _check_broker_config(deployment: Deployment) -> None:
    info = _require_regular(BROKER_CONFIG, "broker_config_missing")
    _expect_owner_group(
        info, owner="root", group=SANDBOX_GROUP, code="broker_config_owner"
    )
    _expect_mode(info, (0o440, 0o400), "broker_config_mode")
    document = _read_json(BROKER_CONFIG, "broker_config_invalid")
    if set(document) & SECRET_FIELD_NAMES:
        _fail("broker_config_invalid", "inline secret")
    expected = {
        "socket_path": str(CONTROL_SOCKET),
        "token_key_path": BROKER_CREDENTIAL_PREFIX + "/egress-token-key",
        "provider_key_path": BROKER_CREDENTIAL_PREFIX + "/provider-key",
        "revocation_dir": str(REVOCATION_DIR),
        "task_root": str(TASK_ROOT),
    }
    for name, value in expected.items():
        if document.get(name) != value:
            _fail("broker_config_mismatch", name)
    launcher = _read_json(CLI_CONFIG, "launcher_config_invalid")
    provider_host = document.get("provider_host")
    launcher_host = launcher.get("egress_provider_host")
    if (
        not isinstance(provider_host, str)
        or not isinstance(launcher_host, str)
        or provider_host != launcher_host
    ):
        _fail("broker_config_mismatch", "provider_host")
    upstream = document.get("upstream_base_url")
    if not isinstance(upstream, str) or not _provider_target_matches(
        upstream, provider_host
    ):
        _fail("broker_config_mismatch", "upstream_base_url/provider_host")
    if not _provider_target_matches(upstream, launcher_host):
        _fail("broker_config_mismatch", "upstream_base_url/launcher-host")


@check("control-directory")
def _check_control_directory(deployment: Deployment) -> None:
    info = _require_dir(CONTROL_DIR, "control_dir_missing")
    _expect_owner_group(
        info, owner=deployment.broker_user, group=SANDBOX_GROUP, code="control_dir_owner"
    )
    _expect_mode(info, (0o750,), "control_dir_mode")
    socket_info = _lstat(CONTROL_SOCKET, "broker_socket_missing")
    if stat.S_ISLNK(socket_info.st_mode) or not stat.S_ISSOCK(socket_info.st_mode):
        _fail("broker_socket_invalid", str(CONTROL_SOCKET))
    _expect_owner_group(
        socket_info,
        owner=deployment.broker_user,
        group=SANDBOX_GROUP,
        code="broker_socket_owner",
    )
    if _mode(socket_info) != 0o660:
        _fail("broker_socket_mode", oct(_mode(socket_info)))


@check("task-directory")
def _check_task_directory(deployment: Deployment) -> None:
    info = _require_dir(TASK_ROOT, "task_root_missing")
    _expect_owner_group(
        info, owner="root", group=SANDBOX_GROUP, code="task_root_owner"
    )
    _expect_mode(info, (0o770,), "task_root_mode")


@check("revocation-directory")
def _check_revocation_directory(deployment: Deployment) -> None:
    info = _require_dir(REVOCATION_DIR, "revocation_dir_missing")
    _expect_owner_group(
        info,
        owner=deployment.adapter_user,
        group=SANDBOX_GROUP,
        code="revocation_dir_owner",
    )
    _expect_mode(info, (0o750,), "revocation_dir_mode")


@check("key-files")
def _check_keys(deployment: Deployment) -> None:
    for path, code in (
        (ATTESTATION_KEY, "attestation_key_invalid"),
        (EGRESS_TOKEN_KEY, "egress_token_key_invalid"),
    ):
        info = _require_regular(path, code + "_missing")
        _expect_owner_group(info, owner="root", group=None, code=code)
        _expect_mode(info, (0o400, 0o440), code)
        if _mode(info) == 0o440 and _group(info) != deployment.adapter_group:
            _fail(code, "group")
        if _group(info) == SANDBOX_GROUP:
            _fail(code, "sandbox group can read the key")
        if not 32 <= info.st_size <= 4096:
            _fail(code, "size")

    provider = _require_regular(PROVIDER_KEY, "provider_key_missing")
    _expect_owner_group(provider, owner="root", group="root", code="provider_key_invalid")
    if _mode(provider) != 0o400:
        _fail("provider_key_invalid", "mode")
    if not 1 <= provider.st_size <= 4096:
        _fail("provider_key_invalid", "size")

    if _sha256_file(ATTESTATION_KEY, "attestation_key_invalid") == _sha256_file(
        EGRESS_TOKEN_KEY, "egress_token_key_invalid"
    ):
        _fail("key_files_not_distinct")

    record = _require_regular(RUNTIME_ATTESTATION, "runtime_attestation_missing")
    _expect_owner_group(
        record, owner="root", group=None, code="runtime_attestation_invalid"
    )
    _expect_mode(record, (0o400, 0o440), "runtime_attestation_invalid")
    if _mode(record) == 0o440 and _group(record) != deployment.adapter_group:
        _fail("runtime_attestation_invalid", "group")
    document = _read_json(RUNTIME_ATTESTATION, "runtime_attestation_invalid")
    expected = {
        "schema": "agentmgr.hermes.runtime.v1",
        "runtime_root_hash": deployment.runtime_root_hash,
    }
    for name, value in expected.items():
        if document.get(name) != value:
            _fail("runtime_attestation_invalid", name)
    signature = document.get("hmac")
    if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
        _fail("runtime_attestation_invalid", "hmac")


@check("group-membership")
def _check_groups(deployment: Deployment) -> None:
    sandbox = _group_entry(SANDBOX_GROUP, "sandbox_group_missing")
    adapter = _group_entry(deployment.adapter_group, "adapter_group_missing")
    if sandbox.gr_gid == adapter.gr_gid or deployment.adapter_group == SANDBOX_GROUP:
        _fail("groups_not_distinct")
    adapter_uid = _user_entry(deployment.adapter_user, "adapter_user_missing").pw_uid
    broker_uid = _user_entry(deployment.broker_user, "broker_user_missing").pw_uid
    adapter_groups = os.getgrouplist(deployment.adapter_user, adapter_uid)
    broker_groups = os.getgrouplist(deployment.broker_user, broker_uid)
    if sandbox.gr_gid not in adapter_groups:
        _fail("adapter_not_in_sandbox_group", deployment.adapter_user)
    if adapter.gr_gid not in adapter_groups:
        _fail("adapter_not_in_private_group", deployment.adapter_user)
    if sandbox.gr_gid not in broker_groups:
        _fail("broker_not_in_sandbox_group", deployment.broker_user)


# ---------------------------------------------------------------------------
# installed helper argv builder
# ---------------------------------------------------------------------------


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "agentmgr_hermes_exec_verify", HELPER_PATH
    )
    if spec is None or spec.loader is None:
        _fail("helper_import_failed")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - any import failure is a stable failure
        _fail("helper_import_failed")
    return module


def _request_document(helper: ModuleType) -> dict[str, object]:
    task_id = "task_verifymanaged01"
    idempotency_key = "idem_verifymanaged01"
    unit = helper.unit_for_task(task_id, idempotency_key)
    return {
        "operation": "status",
        "task_id": task_id,
        "grant_id": "grnt_verifymanaged01",
        "unit": unit,
        "runtime_handle": helper.runtime_handle_for(unit),
        "idempotency_key": idempotency_key,
        "prompt_sha256": "a" * 64,
        "prompt_bytes": 10,
        "profile_template_sha256": "b" * 64,
        "egress_token_sha256": "c" * 64,
        "ttl_seconds": 300,
        "runtime_max_sec": 60,
        "memory_max_mb": 256,
        "cpu_quota_percent": 50,
        "tasks_max": 16,
    }


def _normalize_sandbox_argv(
    argv: object,
) -> tuple[str | None, str | None, list[str], tuple[str, ...]]:
    """Split the fixed helper argv into unit, description, properties, workload.

    Task 2 emits ``--property=<name>=<value>`` entries; comparing the bare
    ``name=value`` part is what the systemd unit actually receives.  Unknown
    options or duplicate unit/description entries fail closed.
    """

    if not isinstance(argv, list) or not argv:
        _fail("helper_argv_failed", "argv")
    unit: str | None = None
    description: str | None = None
    properties: list[str] = []
    workload: list[str] = []
    for index, value in enumerate(argv):
        if not isinstance(value, str):
            _fail("helper_argv_failed", "arg-type")
        if index == 0:
            if value != "/usr/bin/systemd-run":
                _fail("helper_argv_failed", "systemd-run")
            continue
        if value.startswith("--property="):
            properties.append(value[len("--property=") :])
        elif value.startswith("--unit="):
            if unit is not None:
                _fail("helper_argv_failed", "duplicate-unit")
            unit = value[len("--unit=") :]
        elif value.startswith("--description="):
            if description is not None:
                _fail("helper_argv_failed", "duplicate-description")
            description = value[len("--description=") :]
        elif value.startswith("--"):
            _fail("helper_argv_failed", "unexpected-option")
        else:
            workload.append(value)
    return unit, description, properties, tuple(workload)


def _expected_sandbox_properties(
    *,
    task_dir: Path,
    runtime_root: Path,
    control_socket: Path,
    workload: Path,
    document: dict[str, object],
    stop_timeout_sec: int = 30,
) -> Counter[str]:
    """Return the exact property multiset Task 2 must emit for the request."""

    task_root = task_dir.parent
    hidden_roots = [task_root]
    if control_socket.parent != task_root:
        hidden_roots.append(control_socket.parent)
    properties = [
        "DynamicUser=yes",
        "Group=" + SANDBOX_GROUP,
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "TemporaryFileSystem=/run",
        "PrivateNetwork=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "SystemCallFilter=@system-service",
        "SystemCallArchitectures=native",
        "SystemCallErrorNumber=EPERM",
        "RestrictNamespaces=yes",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "RemoveIPC=yes",
        "ProtectClock=yes",
        "ProtectKernelTunables=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelLogs=yes",
        "ProtectControlGroups=yes",
        "ProtectHostname=yes",
        "ProtectProc=invisible",
        "ProcSubset=pid",
        "InaccessiblePaths=-" + str(AGENTMGR_ETC),
        "InaccessiblePaths=-/var/lib/agentmgr",
        "InaccessiblePaths=-/var/lib/postgresql",
        "InaccessiblePaths=-/srv",
        "InaccessiblePaths=-/var/lib/docker",
        "InaccessiblePaths=-/var/lib/containers",
        "InaccessiblePaths=-/var/lib/containerd",
        *(f"InaccessiblePaths=-{root}" for root in hidden_roots),
        "UMask=0007",
        "KillMode=control-group",
        "SendSIGKILL=yes",
        "ExitType=cgroup",
        f"TimeoutStopSec={stop_timeout_sec}",
        f"RuntimeMaxSec={document['runtime_max_sec']}s",
        f"MemoryMax={document['memory_max_mb']}M",
        f"CPUQuota={document['cpu_quota_percent']}%",
        f"TasksMax={document['tasks_max']}",
        "LimitNOFILE=1024",
        "WorkingDirectory=/task",
        "BindPaths=" + str(task_dir) + ":/task",
        "BindReadOnlyPaths=" + str(runtime_root) + ":/runtime",
        "BindReadOnlyPaths=" + str(control_socket) + ":/task/egress.sock",
        "BindReadOnlyPaths=" + str(workload) + ":/workload.py",
    ]
    return Counter(properties)


def _verify_sandbox_argv(
    argv: object,
    *,
    unit: str,
    task_dir: Path,
    runtime_root: Path,
    control_socket: Path,
    workload: Path,
    document: dict[str, object],
    stop_timeout_sec: int = 30,
) -> None:
    """Fail unless ``argv`` is exactly the fixed Task 2 transient-unit argv."""

    normalized_unit, description, properties, workload_argv = _normalize_sandbox_argv(
        argv
    )
    if normalized_unit != unit:
        _fail("helper_argv_failed", "unit")
    if description != "agentmgr-hermes-sandbox":
        _fail("helper_argv_failed", "description")

    task_bind = "BindPaths=" + str(task_dir) + ":/task"
    allowed_read_only = {
        "BindReadOnlyPaths=" + str(runtime_root) + ":/runtime",
        "BindReadOnlyPaths=" + str(control_socket) + ":/task/egress.sock",
        "BindReadOnlyPaths=" + str(workload) + ":/workload.py",
    }
    for property_name in properties:
        if property_name.startswith(
            ("User=", "Environment=", "EnvironmentFile=", "ExecStart=")
        ):
            _fail("helper_argv_failed", property_name)
        if property_name.startswith("BindPaths=") and property_name != task_bind:
            _fail("helper_argv_failed", "extra-bind-path")
        if (
            property_name.startswith("BindReadOnlyPaths=")
            and property_name not in allowed_read_only
        ):
            _fail("helper_argv_failed", "extra-read-only-bind")

    expected = _expected_sandbox_properties(
        task_dir=task_dir,
        runtime_root=runtime_root,
        control_socket=control_socket,
        workload=workload,
        document=document,
        stop_timeout_sec=stop_timeout_sec,
    )
    actual = Counter(properties)
    if actual != expected:
        missing = sorted((expected - actual).elements())
        extra = sorted((actual - expected).elements())
        _fail(
            "helper_argv_failed",
            f"properties missing={missing} extra={extra}",
        )
    if workload_argv != ("/usr/bin/python3", "/workload.py"):
        _fail("helper_argv_failed", "workload-argv")


@check("sandbox-argv")
def _check_sandbox_argv(deployment: Deployment) -> None:
    del deployment
    helper = _load_helper()
    document = _request_document(helper)
    try:
        request = helper.validate_request(document)
    except Exception:  # noqa: BLE001 - stable failure only
        _fail("helper_request_rejected")
    unit = str(document["unit"])
    task_dir = TASK_ROOT / unit
    try:
        argv = helper.build_systemd_run_argv(
            request,
            task_dir=task_dir,
            runtime_root=RUNTIME_ROOT,
            broker_socket=CONTROL_SOCKET,
            workload=WORKLOAD_PATH,
            stop_timeout_sec=30,
        )
    except Exception:  # noqa: BLE001 - stable failure only
        _fail("helper_argv_failed")
    _verify_sandbox_argv(
        argv,
        unit=unit,
        task_dir=task_dir,
        runtime_root=RUNTIME_ROOT,
        control_socket=CONTROL_SOCKET,
        workload=WORKLOAD_PATH,
        document=document,
        stop_timeout_sec=30,
    )

    for forbidden in ("ExecStart", "BindPaths", "Environment", "PrivateNetwork"):
        try:
            helper.validate_request({**document, forbidden: "caller-value"})
        except Exception:  # noqa: BLE001 - expected rejection
            continue
        _fail("helper_accepts_caller_property", forbidden)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="verify_hermes_isolation.py",
        description=(
            "Read-only verification of the provisioned Hermes managed-execution "
            "host.  No task unit is started and no secret is printed."
        ),
    )
    parser.parse_args(list(argv) if argv is not None else None)

    try:
        deployment = load_deployment()
    except VerificationError as failure:
        print(f"FAIL deployment-record: {failure.code} {failure.detail}".rstrip())
        print("verify: 1 check failed")
        return 1

    failures = 0
    for name, function in CHECKS:
        try:
            function(deployment)
        except VerificationError as failure:
            failures += 1
            detail = f" {failure.detail}" if failure.detail else ""
            print(f"FAIL {name}: {failure.code}{detail}")
        except Exception as exc:  # noqa: BLE001 - never print a traceback
            failures += 1
            print(f"FAIL {name}: unexpected {type(exc).__name__}")
        else:
            print(f"ok {name}")

    if failures:
        print(f"verify: {failures} of {len(CHECKS)} checks failed")
        return 1
    print(f"verify: all {len(CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
