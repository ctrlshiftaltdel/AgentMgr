"""Live model egress, real adapter and dispatcher E2E (Issue #97).

**Test-only, environment-gated, Master-run.**  Nothing in this module runs by
default: :data:`live_only` skips every live body unless *both*
``AGENTMGR_HERMES_LIVE=1`` and ``AGENTMGR_HERMES_SUDO=1`` are set, and each body
adds its own skip reason when a required path, group, helper or provider input
is absent.  A skip is never acceptance evidence; the implementer must never
claim a live run.

The module proves the last hop the sandbox matrix cannot: a real
:class:`~app.runtime.hermes_egress.EgressBroker` on a dedicated Unix socket,
the real :class:`~app.runtime.hermes_managed.HermesManagedAdapter` /
:class:`~app.runtime.hermes_managed.HelperSandboxSupervisor` and the real #96
:class:`~app.tasks.execution.RuntimeDispatcher` on real PostgreSQL task
fixtures.

Live bodies
-----------

``TestRealModelArtifact`` (module fixture, one real-key run with at most one
retry)
    Starts the broker with the **dedicated provider key** and the configured
    provider base URL.  A managed task carries an ``artifact_hash`` goal for
    exactly ``b"OK"`` and a prompt that requires writing ``out/result.txt`` in
    the task directory.  The dispatcher must stop at ``verifying``; the host
    hashes the artifact; an independent ``complete_task`` with an
    ``artifact_store`` evidence record moves the task to ``succeeded``.  The
    second test scans every captured unit file, journal line, child argv and
    child environment for the provider key and asserts only the broker holds it.
    A file listed by ``find`` must be readable (directly or with ``sudo -n``),
    and a launched run must expose the full finished-run file set; an empty,
    partial or unreadable set fails instead of proving absence.

``TestEgressTokenBoundaries``
    Uses a loopback stub upstream (no real key) and a real managed unit whose
    task token is read from the task directory.  The broker accepts the real
    token and rejects an expired token, a token for another unit/task and a
    token whose revocation markers were written, without reaching upstream.

``TestCancelConfirmed``
    Uses the stub upstream (holding the model answer) and requests a real core
    cancel while the unit is running.  A helper-confirmed stop escalates to
    ``needs_human`` and is never written as ``cancelled`` or as business
    reconciliation.

``TestTimeout``
    Uses the stub upstream and a short unit runtime budget.  A fixture-written
    *partial* artifact gives the dispatcher's terminal path result evidence, so
    the confirmed timeout is recorded as ``timed_out`` -- never ``verifying``
    and never ``succeeded``.

Environment (all paths absolute, on a persistent filesystem; never committed)
----------------------------------------------------------------------------

* ``AGENTMGR_HERMES_LIVE=1``, ``AGENTMGR_HERMES_SUDO=1`` -- the gates.
* ``AGENTMGR_HERMES_RUNTIME_ROOT`` -- read-only Hermes runtime root.
* ``AGENTMGR_HERMES_RUNTIME_ROOT_HASH`` -- ``sha256:`` + 64 lowercase hex.
* ``AGENTMGR_HERMES_TASK_ROOT`` -- provisioned task root (group-shared, 0770).
* ``AGENTMGR_HERMES_EGRESS_SOCKET`` -- the provisioned, test-dedicated broker
  socket.  It must equal the helper install configuration's fixed socket,
  because the helper binds *its* path into the unit.  Bodies run sequentially
  and each one owns this socket; a pre-existing socket fails rather than being
  replaced.
* ``AGENTMGR_HERMES_HELPER`` -- the fixed ``agentmgr-hermes-exec`` wrapper.
* ``AGENTMGR_HERMES_ATTESTATION_KEY`` -- HMAC key readable by the adapter user.
* ``AGENTMGR_HERMES_EGRESS_TOKEN_KEY`` -- at least 32 bytes.
* ``AGENTMGR_HERMES_EVIDENCE_REF``, ``AGENTMGR_HERMES_RUNTIME_VERSION``.
* ``AGENTMGR_HERMES_MODEL`` -- the provider model; required for the real run,
  optional (``stub-model`` fallback) for the stub bodies.
* ``AGENTMGR_HERMES_GROUP`` (default ``agentmgr-hermes``),
  ``AGENTMGR_HERMES_RUNTIME_NAME`` (default ``hermes``),
  ``AGENTMGR_HERMES_RUNTIME_MAX_SEC`` (default 180),
  ``AGENTMGR_HERMES_PROVIDER_HOST`` (token claim override; normally derived).
* Real body only: ``AGENTMGR_HERMES_PROVIDER_KEY_FILE`` (readable by the adapter
  user/broker, never printed), ``AGENTMGR_HERMES_PROVIDER_BASE_URL`` (the
  broker upstream base; a trailing ``/v1`` is normalised away because the
  broker appends the allowlisted ``/v1/...`` route).

The suite must run as the trusted adapter user, which must be a member of the
sandbox group and allowed to run the helper with ``sudo -n``.  Every unit and
task directory this module creates is removed in ``finally``; the personal or
default Hermes profile, gateway, auth, ``.env`` and sessions are never touched.
"""

from __future__ import annotations

import contextlib
import dataclasses
import grp
import hashlib
import hmac
import http.client
import http.server
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.governance.models import ExecutionGrant
from app.governance.principal import (
    GovernancePrincipal,
    resolve_employee_principal,
    resolve_service_principal,
)
from app.identity.seed import seed_permission_catalog
from app.runtime.contract import (
    CapabilityStatus,
    HealthStatus,
    ResultOutcome,
    RuntimeCapability,
    RuntimeGrant,
    RuntimeState,
    SubmitOutcome,
)
from app.runtime.hermes_egress import (
    EGRESS_TOKEN_HEADER,
    BrokerConfig,
    EgressBroker,
    egress_token_hash,
    mint_egress_token,
    verify_egress_token,
    write_revocation_markers,
)
from app.runtime.hermes_managed import (
    MODEL_NAME_PATTERN,
    HermesManagedAdapter,
    HermesManagedConfig,
    HelperSandboxSupervisor,
    unit_name_for,
)
from app.tasks import service as task_service
from app.tasks.execution import (
    RUNTIME_STOP_CONFIRMED,
    DispatcherConfig,
    RuntimeDispatcher,
    build_runtime_idempotency_key,
)
from app.tasks.models import Task
from app.tasks.verification import EvidenceRecord, validate_goal
from app.tests.governance.helpers import (
    make_org,
    make_service_identity,
    publish_policy,
)
from app.tests.tasks.conftest import (
    TaskActors,
    TaskWorld,
    reset_task_state,
)
from app.tests.tasks.helpers import submit_managed

REPO_ROOT = Path(__file__).resolve().parents[3]
OPS_HELPER = REPO_ROOT / "ops" / "security" / "agentmgr_hermes_exec.py"

LIVE_ENV_VARS = ("AGENTMGR_HERMES_LIVE", "AGENTMGR_HERMES_SUDO")
LIVE_ENABLED = all(os.environ.get(name) == "1" for name in LIVE_ENV_VARS)
LIVE_SKIP_REASON = (
    "requires the owner-approved #97 live test host: set "
    "AGENTMGR_HERMES_LIVE=1 and AGENTMGR_HERMES_SUDO=1"
)

MANAGED_ENV_VARS = (
    "AGENTMGR_HERMES_RUNTIME_ROOT",
    "AGENTMGR_HERMES_RUNTIME_ROOT_HASH",
    "AGENTMGR_HERMES_TASK_ROOT",
    "AGENTMGR_HERMES_EGRESS_SOCKET",
    "AGENTMGR_HERMES_HELPER",
    "AGENTMGR_HERMES_ATTESTATION_KEY",
    "AGENTMGR_HERMES_EGRESS_TOKEN_KEY",
    "AGENTMGR_HERMES_EVIDENCE_REF",
    "AGENTMGR_HERMES_RUNTIME_VERSION",
)
PROVIDER_ENV_VARS = (
    "AGENTMGR_HERMES_PROVIDER_KEY_FILE",
    "AGENTMGR_HERMES_PROVIDER_BASE_URL",
    "AGENTMGR_HERMES_MODEL",
)

WORKER_ID = "dispatcher-hermes-live-1"

DEFAULT_GROUP = "agentmgr-hermes"
DEFAULT_RUNTIME_NAME = "hermes"
DEFAULT_RUNTIME_MAX_SEC = 180
STUB_PROVIDER_HOST = "stub-provider.invalid"
STUB_MODEL_FALLBACK = "stub-model"

#: The dedicated provider key is only compared/scan for; it is never printed.
MIN_PROVIDER_KEY_BYTES = 16
MIN_TOKEN_KEY_BYTES = 32

#: The stub broker's provider key.  It is deliberately not a real credential.
STUB_PROVIDER_KEY = b"stub-provider-key-never-real"
STUB_ASSISTANT_TEXT = "STUB-ASSISTANT-OK"
STUB_HOLD_TIMEOUT_SECONDS = 300

#: The single allowed artifact and the exact goal it satisfies.
ARTIFACT_BYTES = b"OK"
ARTIFACT_DIGEST = "sha256:" + hashlib.sha256(ARTIFACT_BYTES).hexdigest()
ARTIFACT_HEX = ARTIFACT_DIGEST.removeprefix("sha256:")
ARTIFACT_RESULT_REF = f"result_{ARTIFACT_HEX[:32]}"
ARTIFACT_EVIDENCE_REF = f"evidence:hermes_artifact_{ARTIFACT_HEX[:32]}"

CRITERION_ID = "crit_hermes_artifact_ok_0001"
VERIFIER_ID = "ver_hermes_artifact_ok_0001"
VERIFIER_VERSION = "1.0.0"
RESOURCE_REF = "res_hermes_artifact_ok_0001"

#: A first instruction and one clearer retry; both require exactly ``OK``.
ARTIFACT_INSTRUCTION = (
    "You are executing inside a restricted Linux sandbox. The current working "
    "directory is /task. Use the terminal tool to write the two characters OK "
    "(no quotes, no trailing newline, no other characters) into the relative "
    "path out/result.txt. For example run: printf 'OK' > out/result.txt. Do "
    "not create, modify or delete any other file. When the file exists, finish "
    "without further tool calls."
)
ARTIFACT_INSTRUCTION_RETRY = (
    "Use the terminal tool. Run exactly this one shell command and nothing "
    "else: printf 'OK' > out/result.txt . The file out/result.txt must then "
    "contain exactly the two bytes O and K, with no newline. Do not run any "
    "other command and do not print the result. Finish immediately after the "
    "command succeeds."
)

TOKEN_PROMPT_CANARY = "PROMPT-CANARY-hermes-token-boundary"
TOKEN_PROBE_PREFIX = "TOKEN-PROBE-hermes-live-"
TIMEOUT_PARTIAL_ARTIFACT = b"partial"

LIVE_TIMEOUT_SECONDS = 300
UNIT_START_TIMEOUT_SECONDS = 90
UNIT_STOP_TIMEOUT_SECONDS = 90
STATUS_TIMEOUT_SECONDS = 420
CAPTURE_TIMEOUT_SECONDS = 180
CANCEL_REQUEST_TIMEOUT_SECONDS = 180
BROKER_PROBE_TIMEOUT_SECONDS = 30
REAL_TASK_MAX_RUNTIME_SECONDS = 300
TIMEOUT_TASK_MAX_RUNTIME_SECONDS = 120
TIMEOUT_UNIT_RUNTIME_SEC = 20
STUB_UNIT_RUNTIME_SEC = 120

#: Files a finished managed run must leave before key absence can be proven.
FINISHED_TASK_FILES: tuple[str, ...] = (
    "manifest.json",
    "prompt.txt",
    "egress.token",
    "stdout.txt",
    "stderr.txt",
    "status/exit.json",
)

SUDO = shutil.which("sudo") or "/usr/bin/sudo"
SYSTEMCTL = shutil.which("systemctl") or "/usr/bin/systemctl"
JOURNALCTL = shutil.which("journalctl") or "/usr/bin/journalctl"
CAT = shutil.which("cat") or "/usr/bin/cat"
FIND = shutil.which("find") or "/usr/bin/find"
RM = shutil.which("rm") or "/usr/bin/rm"

live_only = pytest.mark.skipif(not LIVE_ENABLED, reason=LIVE_SKIP_REASON)


# ---------------------------------------------------------------------------
# production helper module (masked/private-tmp source constants only)
# ---------------------------------------------------------------------------


def _load_helper() -> Any:
    spec = importlib.util.spec_from_file_location(
        "agentmgr_hermes_exec_live_e2e", OPS_HELPER
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helper = _load_helper()
# A live run must never enable the unprivileged unit-test seam.
assert helper.ALLOW_PRIVATE_TMP_SOURCES is False


# ---------------------------------------------------------------------------
# environment contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiveEnvironment:
    """Resolved live-test configuration; every value is optional until checked."""

    group: str
    runtime_root: Path | None
    task_root: Path | None
    egress_socket: Path | None
    control_dir: Path | None
    helper: Path | None
    attestation_key: Path | None
    egress_token_key: Path | None
    runtime_root_hash: str | None
    evidence_ref: str | None
    runtime_name: str
    runtime_version: str | None
    model: str | None
    provider_host: str | None
    runtime_max_sec: int
    provider_key_file: Path | None
    provider_base_url: str | None

    # -- readiness -----------------------------------------------------------

    def managed_skip_reason(self) -> str | None:
        """Return why no managed live body can run, or ``None`` when it can."""

        return _skip_reason(
            "the managed Hermes live bodies", self._managed_problems()
        )

    def real_model_skip_reason(self) -> str | None:
        """Return why the real-provider body cannot run, or ``None``."""

        problems = self._provider_problems()
        if problems:
            return _skip_reason("the real-model artifact body", problems)
        return None

    def _managed_problems(self) -> list[str | None]:
        problems: list[str | None] = [
            _directory_problem(self.runtime_root, "AGENTMGR_HERMES_RUNTIME_ROOT"),
            _directory_problem(self.task_root, "AGENTMGR_HERMES_TASK_ROOT"),
            self._socket_problem(),
            _executable_problem(self.helper, "AGENTMGR_HERMES_HELPER"),
            _readable_problem(
                self.attestation_key, "AGENTMGR_HERMES_ATTESTATION_KEY"
            ),
            _readable_problem(
                self.egress_token_key, "AGENTMGR_HERMES_EGRESS_TOKEN_KEY"
            ),
            _hash_problem(self.runtime_root_hash),
            _text_problem(self.evidence_ref, "AGENTMGR_HERMES_EVIDENCE_REF"),
            _text_problem(
                self.runtime_version, "AGENTMGR_HERMES_RUNTIME_VERSION"
            ),
            self._group_problem(),
            self._control_problem(),
        ]
        for path, name, minimum in (
            (self.attestation_key, "AGENTMGR_HERMES_ATTESTATION_KEY", 32),
            (self.egress_token_key, "AGENTMGR_HERMES_EGRESS_TOKEN_KEY", 32),
        ):
            if path is not None and path.is_file():
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if size < minimum:
                    problems.append(
                        f"{name} must be at least {minimum} bytes "
                        "(the helper/broker key contract)"
                    )
        model = self.model or STUB_MODEL_FALLBACK
        if not MODEL_NAME_PATTERN.fullmatch(model):
            problems.append(
                "AGENTMGR_HERMES_MODEL (or the stub-model fallback) is not a "
                "valid model name"
            )
        override = self.provider_host
        if override is not None and not _HOST_PATTERN.fullmatch(override):
            problems.append(
                "AGENTMGR_HERMES_PROVIDER_HOST must be a DNS name or IPv4"
            )
        return problems

    def _provider_problems(self) -> list[str | None]:
        problems: list[str | None] = [
            _readable_problem(
                self.provider_key_file, "AGENTMGR_HERMES_PROVIDER_KEY_FILE"
            ),
            _text_problem(
                self.provider_base_url, "AGENTMGR_HERMES_PROVIDER_BASE_URL"
            ),
            _text_problem(self.model, "AGENTMGR_HERMES_MODEL"),
        ]
        if not self.model:
            problems.append(
                "AGENTMGR_HERMES_MODEL is required for the real provider run"
            )
        key_file = self.provider_key_file
        if key_file is not None and key_file.is_file():
            try:
                key = key_file.read_bytes().strip()
            except OSError:
                key = b""
            if len(key) < MIN_PROVIDER_KEY_BYTES:
                problems.append(
                    "AGENTMGR_HERMES_PROVIDER_KEY_FILE must contain at least "
                    f"{MIN_PROVIDER_KEY_BYTES} non-whitespace bytes"
                )
            elif not key.isascii():
                problems.append(
                    "AGENTMGR_HERMES_PROVIDER_KEY_FILE must be ASCII so the "
                    "leakage scan is exact"
                )
        base_url = self.provider_base_url
        if base_url:
            problems.append(_upstream_base_problem(base_url))
        override = self.provider_host
        if override is not None and not _HOST_PATTERN.fullmatch(override):
            problems.append(
                "AGENTMGR_HERMES_PROVIDER_HOST must be a DNS name or IPv4"
            )
        return problems

    # -- managed configuration ------------------------------------------------

    def managed_config(
        self,
        *,
        broker_socket: Path,
        revocation_dir: Path,
        model: str,
        provider_host: str,
        runtime_max_sec: int,
    ) -> HermesManagedConfig:
        """Build the trusted adapter configuration from the live environment."""

        assert self.helper is not None
        assert self.runtime_root is not None
        assert self.runtime_root_hash is not None
        assert self.task_root is not None
        assert self.attestation_key is not None
        assert self.egress_token_key is not None
        assert self.evidence_ref is not None
        assert self.runtime_version is not None
        return HermesManagedConfig(
            helper_path=self.helper,
            runtime_root=self.runtime_root,
            runtime_root_hash=self.runtime_root_hash,
            task_root=self.task_root,
            broker_socket=broker_socket,
            attestation_key_path=self.attestation_key,
            egress_token_key_path=self.egress_token_key,
            runtime_attestation_path=(broker_socket.parent / "runtime-attestation.json"),
            evidence_ref=self.evidence_ref,
            runtime_name=self.runtime_name,
            runtime_version=self.runtime_version,
            model_name=model,
            egress_provider_host=provider_host,
            worker_id=WORKER_ID,
            runtime_max_sec=runtime_max_sec,
            revocation_dir=revocation_dir,
        )

    # -- internal checks -----------------------------------------------------

    def _group_problem(self) -> str | None:
        try:
            entry = grp.getgrnam(self.group)
        except KeyError:
            return f"the sandbox group {self.group} does not exist"
        if os.geteuid() != 0 and entry.gr_gid not in {
            *os.getgroups(),
            os.getgid(),
        }:
            return f"the test user is not a member of the sandbox group {self.group}"
        return None

    def _socket_problem(self) -> str | None:
        path = self.egress_socket
        if path is None:
            return "AGENTMGR_HERMES_EGRESS_SOCKET is not set"
        if not path.is_absolute():
            return "AGENTMGR_HERMES_EGRESS_SOCKET must be an absolute path"
        if path.parent == Path("/"):
            return "AGENTMGR_HERMES_EGRESS_SOCKET needs a control directory"
        if not path.parent.is_dir():
            return "the parent of AGENTMGR_HERMES_EGRESS_SOCKET does not exist"
        return None

    def _control_problem(self) -> str | None:
        directory = self.control_dir
        if directory is None:
            return "AGENTMGR_HERMES_EGRESS_SOCKET is not set"
        if not directory.is_absolute():
            return "AGENTMGR_HERMES_EGRESS_SOCKET must be an absolute path"
        if directory == Path("/"):
            return "AGENTMGR_HERMES_EGRESS_SOCKET needs a control directory"
        if not os.access(directory, os.W_OK | os.X_OK):
            return (
                "the AGENTMGR_HERMES_EGRESS_SOCKET control directory is not "
                "writable by the test user"
            )
        return _source_problem(directory, "the egress socket control directory")


def _text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _path(name: str) -> Path | None:
    value = _text(name)
    return Path(value) if value is not None else None


def read_environment() -> LiveEnvironment:
    """Read the live-test environment; nothing is validated here."""

    egress_socket = _path("AGENTMGR_HERMES_EGRESS_SOCKET")
    return LiveEnvironment(
        group=_text("AGENTMGR_HERMES_GROUP") or DEFAULT_GROUP,
        runtime_root=_path("AGENTMGR_HERMES_RUNTIME_ROOT"),
        task_root=_path("AGENTMGR_HERMES_TASK_ROOT"),
        egress_socket=egress_socket,
        control_dir=(
            egress_socket.parent
            if egress_socket is not None and egress_socket.parent != Path("/")
            else None
        ),
        helper=_path("AGENTMGR_HERMES_HELPER"),
        attestation_key=_path("AGENTMGR_HERMES_ATTESTATION_KEY"),
        egress_token_key=_path("AGENTMGR_HERMES_EGRESS_TOKEN_KEY"),
        runtime_root_hash=_text("AGENTMGR_HERMES_RUNTIME_ROOT_HASH"),
        evidence_ref=_text("AGENTMGR_HERMES_EVIDENCE_REF"),
        runtime_name=_text("AGENTMGR_HERMES_RUNTIME_NAME") or DEFAULT_RUNTIME_NAME,
        runtime_version=_text("AGENTMGR_HERMES_RUNTIME_VERSION"),
        model=_text("AGENTMGR_HERMES_MODEL"),
        provider_host=_text("AGENTMGR_HERMES_PROVIDER_HOST"),
        runtime_max_sec=_runtime_max_sec(),
        provider_key_file=_path("AGENTMGR_HERMES_PROVIDER_KEY_FILE"),
        provider_base_url=_text("AGENTMGR_HERMES_PROVIDER_BASE_URL"),
    )


def _runtime_max_sec() -> int:
    raw = _text("AGENTMGR_HERMES_RUNTIME_MAX_SEC")
    if raw is None:
        return DEFAULT_RUNTIME_MAX_SEC
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_RUNTIME_MAX_SEC
    return value if 9 <= value <= 86_400 else DEFAULT_RUNTIME_MAX_SEC


def _skip_reason(body: str, problems: list[str | None]) -> str | None:
    present = [problem for problem in problems if problem]
    unique = list(dict.fromkeys(present))
    if not unique:
        return None
    return f"{body} requires: " + "; ".join(unique)


def _directory_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not path.is_dir():
        return f"{name} is not an existing directory"
    return _source_problem(path, name)


def _executable_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not os.access(path, os.X_OK):
        return f"{name} is not an executable file"
    return None


def _readable_problem(path: Path | None, name: str) -> str | None:
    if path is None:
        return f"{name} is not set"
    if not path.is_absolute():
        return f"{name} must be an absolute path"
    if not os.access(path, os.R_OK):
        return f"{name} is not readable by the test user"
    return None


def _hash_problem(value: str | None) -> str | None:
    if value is None:
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH is not set"
    if not value.startswith("sha256:") or len(value) != 71:
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH must be 'sha256:' + 64 lowercase hex"
    if any(character not in "0123456789abcdef" for character in value[7:]):
        return "AGENTMGR_HERMES_RUNTIME_ROOT_HASH must be 'sha256:' + 64 lowercase hex"
    return None


def _text_problem(value: str | None, name: str) -> str | None:
    if not value:
        return f"{name} is not set"
    if any(character.isspace() for character in value):
        return f"{name} must not contain whitespace"
    return None


def _source_problem(path: Path, label: str) -> str | None:
    """Reject a path the sandbox unit cannot see as a bind source."""

    prefixes = (*helper.MASKED_SOURCE_PREFIXES, *helper.PRIVATE_TMP_PREFIXES)
    for prefix in prefixes:
        if path == prefix or prefix in path.parents:
            return (
                f"{label} ({path}) must not be under {prefix}: the sandbox unit "
                "has PrivateTmp=yes and masks that prefix"
            )
    return None


def _normalize_upstream_base(value: str) -> str:
    """Strip a trailing ``/v1`` so the broker can append its own route."""

    base = value.strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")].rstrip("/")
    return base


def _upstream_base_problem(value: str) -> str | None:
    base = _normalize_upstream_base(value)
    if not base:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL is empty"
    try:
        parsed = urlsplit(base)
    except ValueError:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL is not a valid URL"
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL must use https (or loopback http)"
    if parsed.username is not None or parsed.password is not None:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL must not carry credentials"
    if parsed.query or parsed.fragment:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL must not carry a query or fragment"
    host = parsed.hostname
    if not host:
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL has no host"
    if scheme == "http" and host not in {"127.0.0.1", "localhost"}:
        # The broker's upstream parser accepts a DNS name or IPv4 only; IPv6
        # loopback (``::1``) cannot be an embedded test upstream.
        return (
            "AGENTMGR_HERMES_PROVIDER_BASE_URL may use plain http only for a "
            "loopback IPv4/localhost upstream"
        )
    if not _HOST_PATTERN.fullmatch(host):
        return "AGENTMGR_HERMES_PROVIDER_BASE_URL host is not a DNS name or IPv4"
    return None


_HOST_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")


def _provider_host_for(base_url: str, override: str | None) -> str:
    if override:
        return override
    host = urlsplit(_normalize_upstream_base(base_url)).hostname
    assert host is not None
    return host


def _read_provider_key(path: Path) -> bytes:
    """Read the dedicated key without ever echoing it."""

    key = path.read_bytes().strip()
    assert len(key) >= MIN_PROVIDER_KEY_BYTES, (
        "the dedicated provider key is shorter than the scan minimum"
    )
    assert key.isascii(), "the dedicated provider key must be ASCII"
    return key


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def require_sudo() -> bool:
    if not LIVE_ENABLED:
        pytest.skip(LIVE_SKIP_REASON)
    completed = _run([SUDO, "-n", "true"], timeout=30)
    if completed.returncode != 0:
        pytest.skip(
            "the live tests require passwordless sudo ('sudo -n true' failed)"
        )
    return True


@pytest.fixture(scope="module")
def live_environment(require_sudo: bool) -> LiveEnvironment:
    del require_sudo
    environment = read_environment()
    reason = environment.managed_skip_reason()
    if reason:
        pytest.skip(reason)
    return environment


# ---------------------------------------------------------------------------
# command helpers
# ---------------------------------------------------------------------------


def _run(
    argv: list[str],
    *,
    timeout: float = LIVE_TIMEOUT_SECONDS,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one command without a shell; no prompt, token or key is in argv."""

    try:
        return subprocess.run(
            list(argv),
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            input=stdin_text,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"command timed out after {timeout}s: {argv[0]}")


def _sudo(
    argv: list[str],
    *,
    timeout: float = LIVE_TIMEOUT_SECONDS,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return _run([SUDO, "-n", *argv], timeout=timeout, stdin_text=stdin_text)


def _property_values(dump: str, name: str) -> list[str]:
    prefix = f"{name}="
    return [
        line[len(prefix) :].strip()
        for line in dump.splitlines()
        if line.startswith(prefix)
    ]


def _property(dump: str, name: str) -> str:
    values = _property_values(dump, name)
    return values[-1] if values else ""


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        completed = _sudo([CAT, str(path)])
        return completed.stdout if completed.returncode == 0 else None


def _read_json(path: Path) -> dict[str, object] | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    return document if isinstance(document, dict) else None


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        try:
            completed = subprocess.run(
                [SUDO, "-n", CAT, str(path)],
                shell=False,
                capture_output=True,
                timeout=LIVE_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return completed.stdout if completed.returncode == 0 else None


def _remove_tree(path: Path, parent: Path) -> None:
    """Remove one derived task directory; never a caller-supplied path."""

    if path.parent != parent or not path.name.startswith("agentmgr-hermes-"):
        pytest.fail(f"refusing to remove a path that is not derived: {path.name}")
    _sudo([RM, "-rf", "--", str(path)])


def _wait_for_started_marker(task_dir: Path, *, timeout: float) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        marker = _read_json(task_dir / "status" / "started.json")
        if marker is not None:
            return marker
        time.sleep(0.2)
    pytest.fail("the managed unit never wrote status/started.json")


def _unit_dump_is_gone(dump: str) -> bool:
    """Return whether a ``systemctl show`` dump proves the cgroup is gone.

    A killed one-shot usually ends ``failed`` with ``MainPID=0`` and no
    control group; that is gone just like ``inactive``.  A non-empty
    ``ControlGroup`` is never accepted as gone here (the helper's own
    ``stop_confirmed`` is separately asserted by the adapter status).
    """

    if _property(dump, "LoadState") == "not-found":
        return True
    return (
        _property(dump, "ActiveState") in {"inactive", "failed"}
        and _property(dump, "MainPID") in {"", "0"}
        and _property(dump, "ControlGroup") == ""
    )


def _unit_is_gone(unit: str) -> bool:
    completed = _sudo(
        [
            SYSTEMCTL,
            "show",
            "--no-pager",
            "--property=LoadState,ActiveState,MainPID,ControlGroup",
            unit,
        ]
    )
    if completed.returncode != 0:
        pytest.fail("systemctl show failed; the unit state cannot be proven")
    return _unit_dump_is_gone(completed.stdout)


def _wait_for_unit_gone(unit: str, *, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _unit_is_gone(unit):
            return
        time.sleep(0.5)
    pytest.fail(f"unit {unit} still holds a process after {timeout}s")


def _journal(unit: str) -> tuple[str, bool]:
    completed = _sudo([JOURNALCTL, "--no-pager", "-o", "cat", "-u", unit])
    if completed.returncode != 0:
        return "", False
    return completed.stdout, True


def _task_files(task_dir: Path) -> dict[str, str]:
    """Read **every** regular file in a finished task directory, or fail.

    A file listed by ``find`` that cannot be read directly or through
    ``sudo -n cat`` is a hard failure: omitting it would let the key-isolation
    proof pass over an unreadable (for example ``0600``) file.  An empty or
    partial listing is never accepted as evidence; callers must also require
    :data:`FINISHED_TASK_FILES`.
    """

    if not task_dir.is_dir():
        return {}
    completed = _sudo([FIND, str(task_dir), "-type", "f", "-print"])
    if completed.returncode != 0:
        pytest.fail("the task directory file listing failed")
    files: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        path = Path(line)
        try:
            relative = path.relative_to(task_dir).as_posix()
        except ValueError:
            pytest.fail("the task file listing escaped the task directory")
        text = _read_text(path)
        if text is None:
            pytest.fail(
                f"the task file {relative} is unreadable; key isolation "
                "cannot be proven"
            )
        files[relative] = text
    return files


def _require_finished_files(files: Mapping[str, str]) -> None:
    """Fail unless every file a finished managed run must leave was read."""

    missing = [name for name in FINISHED_TASK_FILES if name not in files]
    if missing:
        pytest.fail(
            "the finished task directory is missing required files: "
            + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# secret scanning (never prints the secret)
# ---------------------------------------------------------------------------


def _mentions(text: str, secret: bytes) -> bool:
    """Return whether ``text`` contains ``secret`` without logging either."""

    if not secret:
        raise ValueError("a scan secret is required")
    return secret in text.encode("utf-8", "replace")


# ---------------------------------------------------------------------------
# child-process capture
# ---------------------------------------------------------------------------


@dataclass(repr=False)
class _ChildCapture:
    """Capture the live Hermes child's argv/environment exactly once.

    The capture runs while the unit is active; a missing capture is recorded as
    an error so the caller cannot claim the key's absence from nothing.
    """

    task_dir: Path
    argv: tuple[str, ...] = ()
    environ: str = ""
    error: str | None = None
    _thread: threading.Thread | None = field(default=None, repr=False)
    _stop: threading.Event = field(
        default_factory=threading.Event, repr=False
    )

    @property
    def captured(self) -> bool:
        return bool(self.argv)

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="hermes-child-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=CAPTURE_TIMEOUT_SECONDS)

    def _run(self) -> None:
        deadline = time.monotonic() + CAPTURE_TIMEOUT_SECONDS
        try:
            while not self._stop.is_set() and time.monotonic() < deadline:
                marker = _read_json(self.task_dir / "status" / "started.json")
                if marker is not None:
                    pid = marker.get("child_pid")
                    if type(pid) is int and pid > 0:
                        cmdline = _read_text(Path(f"/proc/{pid}/cmdline"))
                        if cmdline:
                            self.argv = tuple(
                                item for item in cmdline.split("\x00") if item
                            )
                            environ = _read_text(Path(f"/proc/{pid}/environ"))
                            self.environ = environ or ""
                            if not self.argv:
                                self.error = "the child argv is empty"
                            return
                time.sleep(0.1)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.error = f"{type(exc).__name__}"
            return
        if not self._stop.is_set():
            self.error = "the Hermes child never exposed a readable argv"


# ---------------------------------------------------------------------------
# stub upstream (fixed text; holds the first chat so the unit stays live)
# ---------------------------------------------------------------------------


class _StubUpstream:
    """Loopback OpenAI-compatible upstream with no real credential.

    The first chat completion is held open until :meth:`stop`/``release`` so a
    managed unit stays active while the test inspects the live unit; later
    requests (the token-boundary probes) answer immediately.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.release = threading.Event()
        self.requests: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._held = False
        self._server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._handler()
        )
        self._thread: threading.Thread | None = None
        self._stopped = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def held(self) -> bool:
        with self._lock:
            return self._held

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="stub-upstream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.release.set()
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def chat_requests(self) -> list[dict[str, object]]:
        with self._lock:
            records = list(self.requests)
        return [
            record
            for record in records
            if record.get("method") == "POST"
            and str(record.get("path", "")).split("?", 1)[0]
            == "/v1/chat/completions"
        ]

    def request_with(self, marker: str) -> dict[str, object] | None:
        encoded = marker.encode("utf-8")
        with self._lock:
            for record in self.requests:
                body = record.get("body")
                if isinstance(body, bytes) and encoded in body:
                    return record
        return None

    def count_with(self, marker: str) -> int:
        encoded = marker.encode("utf-8")
        with self._lock:
            return sum(
                1
                for record in self.requests
                if isinstance(record.get("body"), bytes)
                and encoded in record["body"]  # type: ignore[operator]
            )

    def wait_for_first_chat(
        self, *, timeout: float = STUB_HOLD_TIMEOUT_SECONDS
    ) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            records = self.chat_requests()
            if records and self.held:
                return records[0]
            time.sleep(0.2)
        pytest.fail("Hermes never sent a chat completion through the broker")

    def _handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        state = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - handler contract
                self._handle()

            def do_GET(self) -> None:  # noqa: N802 - handler contract
                self._handle()

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                with state._lock:
                    state.requests.append(
                        {
                            "method": self.command,
                            "path": self.path,
                            "headers": dict(self.headers.items()),
                            "body": body,
                        }
                    )
                if self.command == "POST" and self.path.startswith(
                    "/v1/chat/completions"
                ):
                    hold = False
                    with state._lock:
                        if not state._held:
                            state._held = True
                            hold = True
                    if hold:
                        state.release.wait(timeout=STUB_HOLD_TIMEOUT_SECONDS)
                    if _request_streams(body):
                        self._send_stream()
                    else:
                        self._send_json(
                            200,
                            json.dumps(state._completion(), separators=(",", ":")),
                        )
                    return
                if self.command == "GET" and self.path.startswith("/v1/models"):
                    self._send_json(
                        200,
                        json.dumps(
                            {
                                "object": "list",
                                "data": [{"id": state.model, "object": "model"}],
                            },
                            separators=(",", ":"),
                        ),
                    )
                    return
                self._send_json(404, '{"error":"unsupported"}')

            def _send_json(self, status: int, payload: str) -> None:
                data = payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_stream(self) -> None:
                self.close_connection = True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                chunks = (
                    {
                        "id": "chatcmpl-stub-live",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": state.model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": STUB_ASSISTANT_TEXT,
                                },
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-stub-live",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": state.model,
                        "choices": [
                            {"index": 0, "delta": {}, "finish_reason": "stop"}
                        ],
                    },
                )
                for chunk in chunks:
                    self.wfile.write(
                        f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n".encode()
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()

        return Handler

    def _completion(self) -> dict[str, object]:
        return {
            "id": "chatcmpl-stub-live",
            "object": "chat.completion",
            "created": 0,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": STUB_ASSISTANT_TEXT},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
            },
        }


def _request_streams(body: bytes) -> bool:
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(document, dict) and document.get("stream") is True


# ---------------------------------------------------------------------------
# broker / backend harness
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Backend:
    environment: LiveEnvironment
    stub: _StubUpstream | None
    broker: EgressBroker
    config: HermesManagedConfig
    supervisor: HelperSandboxSupervisor
    adapter: HermesManagedAdapter
    socket_path: Path
    revocation_dir: Path
    provider_key: bytes = field(repr=False)
    model: str
    provider_host: str


@contextlib.contextmanager
def _backend(
    environment: LiveEnvironment,
    *,
    label: str,
    provider_key: bytes,
    upstream_base_url: str,
    provider_host: str,
    allow_insecure_upstream: bool,
    model: str,
    stub: _StubUpstream | None,
    runtime_max_sec: int,
) -> Iterator[_Backend]:
    """Start one in-process broker on a dedicated socket; always clean up."""

    control = environment.control_dir
    task_root = environment.task_root
    socket_path = environment.egress_socket
    token_key_path = environment.egress_token_key
    assert control is not None
    assert task_root is not None
    assert socket_path is not None
    assert token_key_path is not None

    revocation_dir = control / f"agentmgr-hermes-revocations-{label}-{secrets.token_hex(4)}"
    if socket_path.exists():
        pytest.fail(
            f"the test-dedicated broker socket already exists; this {label} "
            "body must own it and never replaces a live socket"
        )
    if revocation_dir.exists():
        pytest.fail(f"the dedicated {label} revocation directory already exists")

    token_key = token_key_path.read_bytes()
    assert len(token_key) >= MIN_TOKEN_KEY_BYTES, (
        "the egress token key is shorter than the broker contract"
    )

    broker: EgressBroker | None = None
    try:
        revocation_dir.mkdir(mode=0o700)
        config = BrokerConfig(
            socket_path=socket_path,
            upstream_base_url=upstream_base_url,
            token_key=token_key,
            provider_key=provider_key,
            revocation_dir=revocation_dir,
            task_root=task_root,
            provider_host=provider_host,
            allow_insecure_upstream=allow_insecure_upstream,
            socket_group=environment.group,
        )
        broker = EgressBroker(config)
        broker.start()
        managed = environment.managed_config(
            broker_socket=socket_path,
            revocation_dir=revocation_dir,
            model=model,
            provider_host=provider_host,
            runtime_max_sec=runtime_max_sec,
        )
        supervisor = HelperSandboxSupervisor(managed)
        adapter = HermesManagedAdapter(managed, supervisor)
        yield _Backend(
            environment=environment,
            stub=stub,
            broker=broker,
            config=managed,
            supervisor=supervisor,
            adapter=adapter,
            socket_path=socket_path,
            revocation_dir=revocation_dir,
            provider_key=provider_key,
            model=model,
            provider_host=provider_host,
        )
    finally:
        if stub is not None:
            stub.stop()
        if broker is not None:
            with contextlib.suppress(Exception):
                broker.stop()
        shutil.rmtree(revocation_dir, ignore_errors=True)


@contextlib.contextmanager
def _stub_backend(
    environment: LiveEnvironment,
    *,
    label: str,
    runtime_max_sec: int = STUB_UNIT_RUNTIME_SEC,
) -> Iterator[_Backend]:
    """Broker with a loopback stub upstream; no real provider key is used."""

    model = environment.model or STUB_MODEL_FALLBACK
    stub = _StubUpstream(model)
    stub.start()
    try:
        with _backend(
            environment,
            label=label,
            provider_key=STUB_PROVIDER_KEY,
            upstream_base_url=f"http://127.0.0.1:{stub.port}",
            provider_host=environment.provider_host or STUB_PROVIDER_HOST,
            allow_insecure_upstream=True,
            model=model,
            stub=stub,
            runtime_max_sec=runtime_max_sec,
        ) as backend:
            yield backend
    finally:
        stub.stop()


@contextlib.contextmanager
def _real_backend(
    environment: LiveEnvironment, *, label: str
) -> Iterator[_Backend]:
    """Broker with the dedicated provider key and the configured upstream."""

    key_file = environment.provider_key_file
    base_url = environment.provider_base_url
    assert key_file is not None
    assert base_url is not None
    assert environment.model is not None
    key = _read_provider_key(key_file)
    base = _normalize_upstream_base(base_url)
    host = _provider_host_for(base, environment.provider_host)
    with _backend(
        environment,
        label=label,
        provider_key=key,
        upstream_base_url=base,
        provider_host=host,
        allow_insecure_upstream=urlsplit(base).scheme.lower() == "http",
        model=environment.model,
        stub=None,
        runtime_max_sec=environment.runtime_max_sec,
    ) as backend:
        yield backend


def _require_verified_runtime(backend: _Backend) -> None:
    """Fail (never skip) when the live runtime cannot prove managed capability."""

    health = backend.adapter.health()
    if health.status is not HealthStatus.HEALTHY:
        pytest.fail(
            "the live adapter health is not healthy: "
            f"{health.status.value}; details={health.details}"
        )
    capabilities = {
        capability.name: capability.status
        for capability in backend.adapter.capabilities().capabilities
    }
    managed = capabilities.get(RuntimeCapability.RUNTIME_MANAGED_EXECUTION.value)
    if managed is not CapabilityStatus.VERIFIED:
        pytest.fail(
            "the live runtime attestation does not verify managed execution: "
            f"{managed!r}"
        )


# ---------------------------------------------------------------------------
# broker probes over the dedicated Unix socket
# ---------------------------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, *, timeout: float = BROKER_PROBE_TIMEOUT_SECONDS):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(self._path)
        except BaseException:
            connection.close()
            raise
        self.sock = connection


def _broker_probe(
    socket_path: Path,
    token: str,
    *,
    model: str,
    marker: str,
) -> tuple[int, str]:
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": marker}],
            "stream": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    connection = _UnixConnection(str(socket_path))
    try:
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=body,
            headers={
                EGRESS_TOKEN_HEADER: token,
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", "replace")
    finally:
        connection.close()


def _broker_error(payload: str) -> str | None:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    error = document.get("error")
    return error if isinstance(error, str) else None


# ---------------------------------------------------------------------------
# database / dispatcher helpers
# ---------------------------------------------------------------------------


def _build_world(session: Session) -> TaskWorld:
    """Build the same real ``TaskWorld`` the task conftest fixture builds."""

    org = make_org(session)
    policy = publish_policy(session, org)
    actors = TaskActors(
        requester=resolve_employee_principal(session, employee_id=org.employee.id),
        owner=resolve_employee_principal(session, employee_id=org.manager.id),
        executor=resolve_employee_principal(session, employee_id=org.employee.id),
        approver=resolve_employee_principal(session, employee_id=org.finance.id),
        outsider=resolve_employee_principal(session, employee_id=org.auditor.id),
    )
    session.commit()
    return TaskWorld(org=org, policy=policy, actors=actors)


def _service_principal(
    session: Session, world: TaskWorld
) -> GovernancePrincipal:
    identity = make_service_identity(session, owner=world.org.admin)
    session.flush()
    principal = resolve_service_principal(session, identity.id)
    session.commit()
    return principal


def _dispatcher(
    session: Session, world: TaskWorld, adapter: HermesManagedAdapter
) -> RuntimeDispatcher:
    return RuntimeDispatcher(
        adapter,
        DispatcherConfig(
            worker_id=WORKER_ID,
            poll_interval=0.5,
            max_status_observations=None,
        ),
        service_principal=_service_principal(session, world),
        session=session,
    )


def _persisted_grant(session: Session, task: Task) -> ExecutionGrant:
    grant = session.scalar(
        select(ExecutionGrant).where(ExecutionGrant.grant_id == task.grant_id)
    )
    assert grant is not None
    return grant


def _unit_for_task(session: Session, task: Task) -> str:
    """Derive the unit exactly as the dispatcher will derive it."""

    key = build_runtime_idempotency_key(task, _persisted_grant(session, task), 1)
    return unit_name_for(task.task_id, key)


def _artifact_goal(instruction: str) -> dict[str, object]:
    """An ``artifact_hash`` goal for exactly ``b"OK"`` plus its instruction."""

    return {
        "summary": "Write exactly OK to out/result.txt in the sandbox",
        "instructions": instruction,
        "completion_criteria": [
            {
                "criterion_id": CRITERION_ID,
                "type": "artifact_hash",
                "verifier": {
                    "verifier_id": VERIFIER_ID,
                    "version": VERIFIER_VERSION,
                },
                "resource_ref": RESOURCE_REF,
                "expected": ARTIFACT_DIGEST,
                "evidence_ref": ARTIFACT_EVIDENCE_REF,
                "evidence_hash": ARTIFACT_DIGEST,
            }
        ],
        "required_inputs": [],
        "out_of_scope": [
            "No host access.",
            "No newline or extra bytes in out/result.txt.",
        ],
    }


def _managed_grant(
    task_id: str, grant_id: str, **overrides: object
) -> RuntimeGrant:
    values: dict[str, object] = {
        "grant_id": grant_id,
        "task_id": task_id,
        "task_revision": 1,
        "execution_mode": "managed_execution",
        "capabilities": ("task.run@1.0.0",),
        "resource_scope": {"scope_type": "hermes_live_e2e"},
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    values.update(overrides)
    return RuntimeGrant(**values)  # type: ignore[arg-type]


def _artifact_payload(task_dir: Path) -> tuple[bytes | None, str | None]:
    path = task_dir / "out" / "result.txt"
    try:
        info = path.lstat()
    except OSError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        return None, None
    data = _read_bytes(path)
    if data is None:
        return None, None
    return data, "sha256:" + hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# real-model artifact body
# ---------------------------------------------------------------------------


@dataclass(frozen=True, repr=False)
class _Attempt:
    task_id: str
    unit: str
    task_dir: Path
    handle: str | None
    dispatcher_outcome: str
    dispatcher_detail: str | None
    task_state: str
    artifact_bytes: bytes | None
    artifact_digest: str | None
    result_document: Mapping[str, Any]
    files: Mapping[str, str]
    journal: str
    journal_ok: bool
    child_argv: tuple[str, ...]
    child_environ: str
    capture_error: str | None
    launched: bool
    captured: bool


@dataclass(frozen=True, repr=False)
class _RealArtifactRun:
    environment: LiveEnvironment
    backend: _Backend
    provider_key: bytes = field(repr=False)
    task_id: str
    unit: str
    task_dir: Path
    handle: str
    task_state: str
    result_document: Mapping[str, Any]
    artifact_digest: str
    artifact_bytes: bytes
    owner_employee_id: int
    attempts: tuple[_Attempt, ...]
    final_state: str = ""
    final_outcome: str | None = None
    all_criteria_passed: bool = False


def _snapshot_attempt(
    *,
    task: Task,
    unit: str,
    task_dir: Path,
    result: Any,
    capture: _ChildCapture,
) -> _Attempt:
    files = _task_files(task_dir)
    journal, journal_ok = _journal(unit)
    data, digest = _artifact_payload(task_dir)
    task_dir_present = task_dir.exists()
    started = (task_dir / "status" / "started.json").exists()
    launched = task_dir_present or started or bool(files) or capture.captured
    if launched:
        # A partial/unreadable finished-run set cannot prove the key's absence.
        # ``status/exit.json`` is the workload's own end-of-run marker; a
        # launched unit without it has not finished (fail closed, no retry).
        _require_finished_files(files)
    handle = task.runtime_task_id
    return _Attempt(
        task_id=task.task_id,
        unit=unit,
        task_dir=task_dir,
        handle=handle,
        dispatcher_outcome=result.outcome,
        dispatcher_detail=result.detail,
        task_state=task.state,
        artifact_bytes=data,
        artifact_digest=digest,
        result_document=dict(task.result or {}),
        files=files,
        journal=journal,
        journal_ok=journal_ok,
        child_argv=capture.argv,
        child_environ=capture.environ,
        capture_error=capture.error,
        launched=launched,
        captured=capture.captured,
    )


def _false_success(attempt: _Attempt) -> str | None:
    """Return why an attempt already looks successful, or ``None``.

    The dispatcher may only reach ``verifying``; a task/runtime that already
    reports ``succeeded`` or a passed result before the independent
    ``complete_task`` is a false success and must fail immediately (never be
    retried).
    """

    if attempt.task_state == "succeeded":
        return "task_state=succeeded"
    if attempt.dispatcher_outcome == "succeeded":
        return "dispatcher_outcome=succeeded"
    if attempt.result_document.get("outcome") == "passed":
        return "result_outcome=passed"
    return None


def _cleanup_unit(
    backend: _Backend,
    *,
    unit: str,
    task_dir: Path,
    task_root: Path,
) -> None:
    """Stop the unit and remove its derived directory; never raise."""

    with contextlib.suppress(Exception):
        backend.supervisor.stop(unit, reason="live_test_cleanup")
    removed = False
    with contextlib.suppress(Exception):
        backend.supervisor.cleanup(unit, result_read=True)
        removed = not task_dir.exists()
    if not removed and task_dir.exists():
        with contextlib.suppress(Exception):
            _remove_tree(task_dir, task_root)


def _complete_artifact_run(
    engine: Engine, run: _RealArtifactRun
) -> _RealArtifactRun:
    """Independently verify the captured artifact and record completion.

    Runs inside the module-scoped fixture so the ``succeeded`` snapshot is
    available without depending on test ordering.  The evidence record hashes
    the **actual captured bytes**, not a constant.
    """

    assert run.artifact_bytes == ARTIFACT_BYTES
    actual_hex = hashlib.sha256(run.artifact_bytes).hexdigest()
    actual_digest = f"sha256:{actual_hex}"
    assert actual_digest == ARTIFACT_DIGEST
    actual_result_ref = f"result_{actual_hex[:32]}"
    actual_evidence_ref = f"evidence:hermes_artifact_{actual_hex[:32]}"
    assert run.result_document.get("result_ref") == actual_result_ref
    assert list(run.result_document.get("evidence_refs") or []) == [
        actual_evidence_ref
    ]
    with Session(engine) as session:
        task = session.scalar(
            select(Task).where(Task.task_id == run.task_id)
        )
        assert task is not None
        assert task.state == "verifying", (
            "the independent verifier must only run on a verifying task"
        )
        actor = resolve_employee_principal(
            session, employee_id=run.owner_employee_id
        )
        task_service.complete_task(
            session,
            task_id=run.task_id,
            actor=actor,
            result_ref=actual_result_ref,
            evidence_refs=[actual_evidence_ref],
            evidence=[
                EvidenceRecord(
                    criterion_id=CRITERION_ID,
                    verifier_id=VERIFIER_ID,
                    verifier_version=VERIFIER_VERSION,
                    evidence_ref=actual_evidence_ref,
                    observed=actual_digest,
                    evidence_hash=actual_digest,
                    source="artifact_store",
                )
            ],
        )
        session.commit()
        session.refresh(task)
        final_state = task.state
        document = dict(task.result or {})
        outcome = document.get("outcome")
        final_outcome = str(outcome) if outcome is not None else None
        all_criteria_passed = document.get("all_criteria_passed") is True

    return dataclasses.replace(
        run,
        artifact_digest=actual_digest,
        final_state=final_state,
        final_outcome=final_outcome,
        all_criteria_passed=all_criteria_passed,
    )


@pytest.fixture(scope="module")
def real_artifact_run(
    live_environment: LiveEnvironment,
    require_sudo: bool,
    test_db_url: str,
    _schema: None,
) -> Iterator[_RealArtifactRun]:
    """Drive one real-key managed task; retry once with a clearer prompt.

    The run stops at ``verifying``.  A missing or incorrect artifact is never
    accepted: the fixture retries exactly once with a clearer instruction and
    then fails the whole live body.
    """

    del require_sudo
    environment = live_environment
    reason = environment.real_model_skip_reason()
    if reason:
        pytest.skip(reason)

    assert environment.task_root is not None
    engine = create_engine(test_db_url)
    session = Session(engine)
    reset_task_state(engine)
    # The hand-built world must not depend on whatever fixture residue the last
    # test left: seed the catalog explicitly after the truncate.
    seed_permission_catalog(session)
    session.commit()
    world = _build_world(session)
    service = _service_principal(session, world)

    attempts: list[_Attempt] = []
    tracked: list[tuple[str, Path]] = []
    run: _RealArtifactRun | None = None
    try:
        with _real_backend(environment, label="artifact") as backend:
            try:
                _require_verified_runtime(backend)
                dispatcher = RuntimeDispatcher(
                    backend.adapter,
                    DispatcherConfig(
                        worker_id=WORKER_ID,
                        poll_interval=0.5,
                        max_status_observations=None,
                    ),
                    service_principal=service,
                    session=session,
                )
                suffix = secrets.token_hex(4)
                for index, instruction in enumerate(
                    (ARTIFACT_INSTRUCTION, ARTIFACT_INSTRUCTION_RETRY), start=1
                ):
                    task = submit_managed(
                        session,
                        world,
                        key=f"idem_hermes_live_artifact_{index}_{suffix}",
                        goal=_artifact_goal(instruction),
                        max_runtime_seconds=REAL_TASK_MAX_RUNTIME_SECONDS,
                    )
                    session.commit()
                    unit = _unit_for_task(session, task)
                    task_dir = environment.task_root / unit
                    tracked.append((unit, task_dir))

                    capture = _ChildCapture(task_dir)
                    capture.start()
                    result = dispatcher.run_once()
                    session.refresh(task)
                    capture.stop()

                    attempt = _snapshot_attempt(
                        task=task,
                        unit=unit,
                        task_dir=task_dir,
                        result=result,
                        capture=capture,
                    )
                    attempts.append(attempt)
                    false_success = _false_success(attempt)
                    if false_success is not None:
                        pytest.fail(
                            "an attempt reported success before independent "
                            f"verification ({false_success}); refusing to retry"
                        )
                    if (
                        result.outcome == "verifying"
                        and task.state == "verifying"
                        and attempt.artifact_digest == ARTIFACT_DIGEST
                        and attempt.artifact_bytes == ARTIFACT_BYTES
                        and task.runtime_task_id
                    ):
                        run = _RealArtifactRun(
                            environment=environment,
                            backend=backend,
                            provider_key=backend.provider_key,
                            task_id=task.task_id,
                            unit=unit,
                            task_dir=task_dir,
                            handle=task.runtime_task_id,
                            task_state=task.state,
                            result_document=dict(task.result or {}),
                            artifact_digest=attempt.artifact_digest,
                            artifact_bytes=attempt.artifact_bytes,
                            owner_employee_id=world.org.manager.id,
                            attempts=tuple(attempts),
                        )
                        break
                    artifact_miss = not (
                        attempt.artifact_bytes == ARTIFACT_BYTES
                        and attempt.artifact_digest == ARTIFACT_DIGEST
                    )
                    if not artifact_miss:
                        pytest.fail(
                            "the artifact is exactly OK but the managed attempt "
                            "did not reach verifying; refusing to retry "
                            f"({result.outcome}/{task.state})"
                        )
                    _cleanup_unit(
                        backend,
                        unit=unit,
                        task_dir=task_dir,
                        task_root=environment.task_root,
                    )
                if run is None:
                    pytest.fail(
                        "the real model did not produce exactly OK in "
                        "out/result.txt after two attempts; no success was "
                        "recorded (check the dispatcher outcome and artifact "
                        "digest in the attempt snapshots)"
                    )
                # Independent verification runs here, before yield, so the
                # completed snapshot is available regardless of test order.
                run = _complete_artifact_run(engine, run)
            finally:
                for unit, task_dir in tracked:
                    _cleanup_unit(
                        backend,
                        unit=unit,
                        task_dir=task_dir,
                        task_root=environment.task_root,
                    )
                assert not any(
                    task_dir.exists() for _unit, task_dir in tracked
                ), "a managed task directory survived the live cleanup"
    except BaseException:
        session.close()
        engine.dispose()
        raise

    session.close()
    engine.dispose()
    assert run is not None
    yield run


@live_only
class TestRealModelArtifact:
    def test_real_model_writes_artifact_and_dispatcher_reaches_verifying(
        self, real_artifact_run: _RealArtifactRun
    ) -> None:
        run = real_artifact_run

        # The dispatcher must never certify completion by itself.
        assert run.task_state == "verifying"
        assert run.task_state != "succeeded"
        assert run.handle.startswith("rtask_hermes_")

        document = dict(run.result_document)
        assert document["outcome"] == "completed"
        actual_hex = hashlib.sha256(run.artifact_bytes).hexdigest()
        actual_digest = f"sha256:{actual_hex}"
        assert actual_digest == ARTIFACT_DIGEST
        assert document["result_ref"] == f"result_{actual_hex[:32]}"
        assert list(document.get("evidence_refs") or []) == [
            f"evidence:hermes_artifact_{actual_hex[:32]}"
        ]
        assert (document.get("verification") or {}).get("result") == "pending"

        # The artifact really is the exact two-byte file the goal names.
        # The fixture captured it before cleaning the task directory.
        assert run.artifact_bytes == ARTIFACT_BYTES
        assert run.artifact_digest == actual_digest

        # The module fixture already ran the independent verifier before yield;
        # assert that snapshot instead of completing here (order-independent).
        assert run.final_state == "succeeded"
        assert run.final_outcome == "passed"
        assert run.all_criteria_passed is True

    def test_real_key_never_enters_unit_or_task_dir(
        self, real_artifact_run: _RealArtifactRun
    ) -> None:
        run = real_artifact_run
        key = run.provider_key

        # The broker is the only component configured with the real key.
        broker_has_key = hmac.compare_digest(
            run.backend.broker.config.provider_key, key
        )
        assert broker_has_key, "the broker is not configured with the real key"
        config_has_key = _mentions(repr(run.backend.config), key)
        assert not config_has_key, "the adapter configuration carries the key"

        # The key file itself must not be world-readable or inside the task
        # root (the sandbox's only writable/visible area).
        key_path = run.environment.provider_key_file
        assert key_path is not None
        key_info = key_path.lstat()
        assert stat.S_ISREG(key_info.st_mode), (
            "the dedicated provider key file is not a regular file"
        )
        key_mode = stat.S_IMODE(key_info.st_mode)
        world_readable = bool(key_mode & 0o007)
        assert not world_readable, (
            "the dedicated provider key file must not be world-readable"
        )
        task_root = run.environment.task_root
        assert task_root is not None
        inside_task_root = key_path == task_root or task_root in key_path.parents
        assert not inside_task_root, (
            "the dedicated provider key file must live outside the task root"
        )

        assert run.attempts, "the real run captured no attempts"
        for attempt in run.attempts:
            false_success = _false_success(attempt)
            assert false_success is None, (
                f"an attempt reported a false success ({false_success})"
            )
            for relative, text in attempt.files.items():
                leaked = _mentions(text, key)
                assert not leaked, (
                    f"the provider key leaked into the task file {relative}"
                )
            result_leaked = _mentions(json.dumps(attempt.result_document), key)
            assert not result_leaked, (
                "the provider key leaked into the persisted task result"
            )

            if not attempt.launched:
                continue
            assert attempt.files, "a launched attempt left no task files"
            _require_finished_files(attempt.files)
            assert attempt.journal_ok, "the unit journal is unreadable"
            assert attempt.journal.strip(), "an empty journal cannot prove absence"
            journal_leaked = _mentions(attempt.journal, key)
            assert not journal_leaked, (
                "the provider key leaked into the unit journal"
            )
            assert attempt.captured, (
                "the Hermes child process was never captured; the key's "
                f"absence from argv/environment cannot be proven "
                f"(capture error: {attempt.capture_error})"
            )
            assert attempt.child_argv, "the captured child argv is empty"
            for argument in attempt.child_argv:
                argv_leaked = _mentions(argument, key)
                assert not argv_leaked, (
                    "the provider key leaked into the Hermes child argv"
                )
            environ_leaked = _mentions(attempt.child_environ, key)
            assert not environ_leaked, (
                "the provider key leaked into the Hermes child environment"
            )

            # Positive control: this is the sandbox child, and it carries only
            # the short-lived task token -- not the provider key.
            assert "HERMES_HOME=/task/profile" in attempt.child_environ
            token = attempt.files.get("egress.token", "").strip()
            token_present = token.startswith("egt.")
            assert token_present, "the task egress token is missing"
            token_reached_child = _mentions(attempt.child_environ, token.encode())
            assert token_reached_child, (
                "the task token never reached the child environment"
            )


# ---------------------------------------------------------------------------
# deterministic stub bodies
# ---------------------------------------------------------------------------


class _CancelOnRunning:
    """Request a real core cancel from a second connection once running."""

    def __init__(
        self,
        engine: Engine,
        *,
        task_id: str,
        actor: GovernancePrincipal,
        timeout: float = CANCEL_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self._engine = engine
        self._task_id = task_id
        self._actor = actor
        self._timeout = timeout
        self.error: BaseException | None = None
        self.requested = False
        self._thread = threading.Thread(
            target=self._run, name="hermes-live-cancel", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=self._timeout + 10)
        if self._thread.is_alive():
            self.error = TimeoutError("the cancel request thread did not finish")

    def _run(self) -> None:
        deadline = time.monotonic() + self._timeout
        try:
            while time.monotonic() < deadline:
                with Session(self._engine) as session:
                    task = session.scalar(
                        select(Task).where(Task.task_id == self._task_id)
                    )
                    state = task.state if task is not None else None
                if state == "running":
                    with Session(self._engine) as session:
                        task_service.request_cancel(
                            session, task_id=self._task_id, actor=self._actor
                        )
                        session.commit()
                    self.requested = True
                    return
                if state in {
                    "needs_human",
                    "unknown",
                    "verifying",
                    "succeeded",
                    "failed",
                    "timed_out",
                    "cancelled",
                }:
                    self.error = AssertionError(
                        f"the task reached {state} before the cancel request"
                    )
                    return
                time.sleep(0.05)
            self.error = TimeoutError(
                "the managed task was never observed running for a cancel"
            )
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.error = exc


class _WriteArtifactAfterStart:
    """Write a partial artifact once the managed unit has started."""

    def __init__(
        self,
        task_dir: Path,
        data: bytes,
        *,
        timeout: float = UNIT_START_TIMEOUT_SECONDS,
    ) -> None:
        self._task_dir = task_dir
        self._data = data
        self._timeout = timeout
        self.error: BaseException | None = None
        self.written = False
        self._thread = threading.Thread(
            target=self._run, name="hermes-live-artifact", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def join(self) -> None:
        self._thread.join(timeout=self._timeout + 10)
        if self._thread.is_alive():
            self.error = TimeoutError("the artifact writer did not finish")

    def _run(self) -> None:
        deadline = time.monotonic() + self._timeout
        try:
            while time.monotonic() < deadline:
                marker = _read_json(
                    self._task_dir / "status" / "started.json"
                )
                if marker is not None:
                    path = self._task_dir / "out" / "result.txt"
                    path.write_bytes(self._data)
                    self.written = True
                    return
                time.sleep(0.1)
            self.error = TimeoutError("the unit never started for the artifact")
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.error = exc


@live_only
class TestEgressTokenBoundaries:
    def test_expired_revoked_and_cross_task_tokens_are_rejected(
        self, live_environment: LiveEnvironment
    ) -> None:
        environment = live_environment
        assert environment.task_root is not None
        assert environment.egress_token_key is not None

        suffix = secrets.token_hex(4)
        task_id = f"task_hermes_token_{suffix}"
        grant_id = f"grnt_hermes_token_{suffix}"
        idempotency_key = f"idem_hermes_token_{suffix}"
        unit = unit_name_for(task_id, idempotency_key)
        task_dir = environment.task_root / unit

        with _stub_backend(environment, label="token") as backend:
            _require_verified_runtime(backend)
            token_key = environment.egress_token_key.read_bytes()
            try:
                submitted = backend.adapter.submit_task(
                    task_id,
                    TOKEN_PROMPT_CANARY,
                    idempotency_key,
                    _managed_grant(task_id, grant_id),
                )
                assert submitted.outcome is SubmitOutcome.ACCEPTED, submitted.error
                handle = submitted.runtime_task_id
                assert handle is not None and handle.startswith("rtask_hermes_")

                _wait_for_started_marker(
                    task_dir, timeout=UNIT_START_TIMEOUT_SECONDS
                )
                first_chat = backend.stub.wait_for_first_chat(
                    timeout=UNIT_START_TIMEOUT_SECONDS
                )
                first_body = first_chat.get("body")
                assert isinstance(first_body, bytes)
                assert TOKEN_PROMPT_CANARY in first_body.decode(
                    "utf-8", "replace"
                ), "the unit never reached the broker with its own prompt"

                token = _read_text(task_dir / "egress.token")
                assert token is not None, "the task egress token is unreadable"
                token = token.strip()
                token_present = token.startswith("egt.")
                assert token_present
                claims = verify_egress_token(token, key=token_key)
                assert claims.task_id == task_id
                assert claims.grant_id == grant_id
                assert claims.unit == unit

                # -- the real token is accepted -------------------------------
                valid_marker = TOKEN_PROBE_PREFIX + secrets.token_hex(4)
                status, payload = _broker_probe(
                    backend.socket_path,
                    token,
                    model=claims.model,
                    marker=valid_marker,
                )
                assert status == 200, status
                assert STUB_ASSISTANT_TEXT in payload
                forwarded = backend.stub.request_with(valid_marker)
                assert forwarded is not None, "the valid token did not reach upstream"
                headers = forwarded.get("headers")
                assert isinstance(headers, dict)
                lowered = {
                    str(name).lower(): str(value)
                    for name, value in headers.items()
                }
                authorization = f"Bearer {STUB_PROVIDER_KEY.decode()}"
                assert lowered.get("authorization") == authorization
                assert EGRESS_TOKEN_HEADER.lower() not in lowered

                # -- expired token --------------------------------------------
                past = datetime.now(UTC) - timedelta(seconds=120)
                expired_claims = dataclasses.replace(
                    claims,
                    issued_at=past,
                    expires_at=past + timedelta(seconds=60),
                    nonce=secrets.token_hex(16),
                )
                expired_token = mint_egress_token(
                    expired_claims, key=token_key, now=past
                )
                expired_marker = TOKEN_PROBE_PREFIX + secrets.token_hex(4)
                status, payload = _broker_probe(
                    backend.socket_path,
                    expired_token,
                    model=claims.model,
                    marker=expired_marker,
                )
                assert status == 401, status
                assert _broker_error(payload) == "token_expired"
                assert backend.stub.count_with(expired_marker) == 0

                # -- a token bound to another unit/task -----------------------
                other_unit = "agentmgr-hermes-" + "0" * 16
                other_unit_claims = dataclasses.replace(
                    claims, unit=other_unit, nonce=secrets.token_hex(16)
                )
                other_unit_token = mint_egress_token(
                    other_unit_claims, key=token_key, now=datetime.now(UTC)
                )
                other_unit_marker = TOKEN_PROBE_PREFIX + secrets.token_hex(4)
                status, payload = _broker_probe(
                    backend.socket_path,
                    other_unit_token,
                    model=claims.model,
                    marker=other_unit_marker,
                )
                assert status == 403, status
                assert _broker_error(payload) == "token_binding_mismatch"
                assert backend.stub.count_with(other_unit_marker) == 0

                other_task_claims = dataclasses.replace(
                    claims,
                    task_id=f"task_hermes_other_{suffix}",
                    grant_id=f"grnt_hermes_other_{suffix}",
                    nonce=secrets.token_hex(16),
                )
                other_task_token = mint_egress_token(
                    other_task_claims, key=token_key, now=datetime.now(UTC)
                )
                other_task_marker = TOKEN_PROBE_PREFIX + secrets.token_hex(4)
                status, payload = _broker_probe(
                    backend.socket_path,
                    other_task_token,
                    model=claims.model,
                    marker=other_task_marker,
                )
                assert status == 403, status
                assert _broker_error(payload) == "token_binding_mismatch"
                assert backend.stub.count_with(other_task_marker) == 0

                # -- revoked token --------------------------------------------
                write_revocation_markers(
                    backend.revocation_dir, claims, egress_token_hash(token)
                )
                revoked_marker = TOKEN_PROBE_PREFIX + secrets.token_hex(4)
                status, payload = _broker_probe(
                    backend.socket_path,
                    token,
                    model=claims.model,
                    marker=revoked_marker,
                )
                assert status == 401, status
                assert _broker_error(payload) == "token_revoked"
                assert backend.stub.count_with(revoked_marker) == 0

                status = backend.adapter.get_status(handle, task_id=task_id)
                assert status.state is not RuntimeState.COMPLETED
            finally:
                _cleanup_unit(
                    backend,
                    unit=unit,
                    task_dir=task_dir,
                    task_root=environment.task_root,
                )
                assert not task_dir.exists()


@live_only
class TestCancelConfirmed:
    def test_cancel_confirmed_routes_to_needs_human(
        self,
        session: Session,
        world: TaskWorld,
        engine: Engine,
        live_environment: LiveEnvironment,
    ) -> None:
        environment = live_environment
        assert environment.task_root is not None

        task = submit_managed(
            session,
            world,
            key=f"idem_hermes_cancel_{secrets.token_hex(4)}",
            max_runtime_seconds=TIMEOUT_TASK_MAX_RUNTIME_SECONDS,
        )
        session.commit()
        unit = _unit_for_task(session, task)
        task_dir = environment.task_root / unit

        with _stub_backend(environment, label="cancel") as backend:
            _require_verified_runtime(backend)
            try:
                cancel = _CancelOnRunning(
                    engine, task_id=task.task_id, actor=world.actors.requester
                )
                cancel.start()
                result = _dispatcher(session, world, backend.adapter).run_once()
                cancel.join()
                session.refresh(task)

                assert cancel.error is None, f"the cancel request failed: {cancel.error}"
                assert cancel.requested is True
                assert result.outcome == "needs_human", result.detail
                assert result.detail == RUNTIME_STOP_CONFIRMED
                assert task.state == "needs_human"
                assert task.state != "succeeded"
                assert task.needs_human_origin == "cancel_requested"
                # A confirmed runtime stop is evidence, never reconciliation.
                assert task.stop_confirmed is False
                document = dict(task.result or {})
                assert document.get("side_effects_reconciled") is not True
                assert document.get("outcome") != "cancelled"

                handle = task.runtime_task_id
                assert handle is not None
                status = backend.adapter.get_status(
                    handle, task_id=task.task_id
                )
                assert status.stop_confirmed is True
                assert status.state is RuntimeState.STOPPED
                _wait_for_unit_gone(unit, timeout=UNIT_STOP_TIMEOUT_SECONDS)

                cancelled_result = backend.adapter.get_result(
                    handle, task_id=task.task_id
                )
                assert cancelled_result.outcome in {
                    ResultOutcome.UNKNOWN,
                    ResultOutcome.NEEDS_HUMAN,
                }
                assert cancelled_result.result_ref is None
            finally:
                _cleanup_unit(
                    backend,
                    unit=unit,
                    task_dir=task_dir,
                    task_root=environment.task_root,
                )
                assert not task_dir.exists()


@live_only
class TestTimeout:
    def test_timeout_stops_unit_and_never_succeeds(
        self,
        session: Session,
        world: TaskWorld,
        live_environment: LiveEnvironment,
    ) -> None:
        environment = live_environment
        assert environment.task_root is not None

        task = submit_managed(
            session,
            world,
            key=f"idem_hermes_timeout_{secrets.token_hex(4)}",
            max_runtime_seconds=TIMEOUT_TASK_MAX_RUNTIME_SECONDS,
        )
        session.commit()
        unit = _unit_for_task(session, task)
        task_dir = environment.task_root / unit
        partial_digest = (
            "sha256:" + hashlib.sha256(TIMEOUT_PARTIAL_ARTIFACT).hexdigest()
        )
        partial_hex = partial_digest.removeprefix("sha256:")
        partial_result_ref = f"result_{partial_hex[:32]}"

        with _stub_backend(
            environment,
            label="timeout",
            runtime_max_sec=TIMEOUT_UNIT_RUNTIME_SEC,
        ) as backend:
            _require_verified_runtime(backend)
            writer = _WriteArtifactAfterStart(task_dir, TIMEOUT_PARTIAL_ARTIFACT)
            writer.start()
            try:
                result = _dispatcher(session, world, backend.adapter).run_once()
                writer.join()
                session.refresh(task)

                assert writer.error is None, f"the artifact writer failed: {writer.error}"
                assert writer.written is True
                assert result.outcome == "timed_out", result.detail
                assert task.state == "timed_out"
                assert task.state != "succeeded"
                assert task.state != "verifying"
                document = dict(task.result or {})
                assert document.get("outcome") == "timed_out"
                assert document.get("stop_confirmed") is True
                assert document.get("result_ref") == partial_result_ref
                assert document.get("result_ref") != ARTIFACT_RESULT_REF

                handle = task.runtime_task_id
                assert handle is not None
                status = backend.adapter.get_status(handle, task_id=task.task_id)
                assert status.state is RuntimeState.TIMED_OUT
                assert status.stop_confirmed is True
                _wait_for_unit_gone(unit, timeout=UNIT_STOP_TIMEOUT_SECONDS)

                data, digest = _artifact_payload(task_dir)
                assert data == TIMEOUT_PARTIAL_ARTIFACT
                assert digest == partial_digest
                assert digest != ARTIFACT_DIGEST
            finally:
                _cleanup_unit(
                    backend,
                    unit=unit,
                    task_dir=task_dir,
                    task_root=environment.task_root,
                )
                assert not task_dir.exists()


# ---------------------------------------------------------------------------
# DB-free tests: they keep the module meaningful when every live body skips
# ---------------------------------------------------------------------------


def test_live_gate_names_both_required_variables() -> None:
    assert LIVE_ENABLED == all(
        os.environ.get(name) == "1" for name in LIVE_ENV_VARS
    )
    for name in LIVE_ENV_VARS:
        assert name in LIVE_SKIP_REASON


def test_managed_skip_reason_names_the_required_inputs(monkeypatch) -> None:
    for name in (*MANAGED_ENV_VARS, *PROVIDER_ENV_VARS):
        monkeypatch.delenv(name, raising=False)
    environment = read_environment()
    reason = environment.managed_skip_reason()
    assert reason is not None
    for name in MANAGED_ENV_VARS:
        assert name in reason


def test_real_model_skip_reason_names_the_provider_inputs(monkeypatch) -> None:
    for name in PROVIDER_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    environment = read_environment()
    reason = environment.real_model_skip_reason()
    assert reason is not None
    for name in PROVIDER_ENV_VARS:
        assert name in reason


def test_artifact_goal_names_the_exact_ok_artifact() -> None:
    assert ARTIFACT_BYTES == b"OK"


def test_unit_dump_gone_accepts_failed_empty_cgroup() -> None:
    assert _unit_dump_is_gone("LoadState=not-found\n")
    assert _unit_dump_is_gone(
        "LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup=\n"
    )
    # A killed one-shot ends failed; the empty cgroup is the stop proof.
    assert _unit_dump_is_gone(
        "LoadState=loaded\nActiveState=failed\nMainPID=0\nControlGroup=\n"
    )
    assert not _unit_dump_is_gone(
        "LoadState=loaded\nActiveState=failed\nMainPID=0\n"
        "ControlGroup=/system.slice/agentmgr-hermes-x.service\n"
    )
    assert not _unit_dump_is_gone(
        "LoadState=loaded\nActiveState=active\nMainPID=123\nControlGroup=\n"
    )
    assert ARTIFACT_DIGEST == "sha256:" + hashlib.sha256(b"OK").hexdigest()
    assert ARTIFACT_RESULT_REF == f"result_{ARTIFACT_HEX[:32]}"
    assert ARTIFACT_EVIDENCE_REF == f"evidence:hermes_artifact_{ARTIFACT_HEX[:32]}"
    goal = _artifact_goal(ARTIFACT_INSTRUCTION)
    normalized = validate_goal(goal)
    criterion = normalized["completion_criteria"][0]
    assert criterion["type"] == "artifact_hash"
    assert criterion["expected"] == ARTIFACT_DIGEST
    assert criterion["evidence_ref"] == ARTIFACT_EVIDENCE_REF
    assert criterion["evidence_hash"] == ARTIFACT_DIGEST
    assert normalized["instructions"] == ARTIFACT_INSTRUCTION


def test_upstream_base_normalization_strips_one_v1_suffix() -> None:
    assert (
        _normalize_upstream_base("https://provider.example/v1/")
        == "https://provider.example"
    )
    assert (
        _normalize_upstream_base("https://provider.example/prefix")
        == "https://provider.example/prefix"
    )
    assert (
        _normalize_upstream_base("http://127.0.0.1:8080/v1")
        == "http://127.0.0.1:8080"
    )


def test_secret_scan_matches_without_printing_the_secret() -> None:
    secret = b"canary-provider-key-value"
    assert _mentions("prefix canary-provider-key-value suffix", secret)
    assert not _mentions("clean text", secret)
    with pytest.raises(ValueError):
        _mentions("text", b"")
