# Hermes-in-sandbox Managed Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect #96's Managed Execution dispatcher to a real Hermes runtime that runs one task per ephemeral systemd sandbox, with terminal-only tools, task-scoped model egress, host-verified artifact evidence, and fail-closed isolation claims.

**Architecture:** A host `HermesManagedAdapter` calls a root-owned narrow helper to launch a transient `agentmgr-hermes-<id>.service` with `DynamicUser`, private `/run`/`/tmp`, isolated loopback only, and a read-only Hermes runtime. The unit runs a stdlib workload that starts a local model proxy, invokes `hermes chat --query-file ... --oneshot -t terminal --safe-mode`, and writes heartbeat/status/artifact files. A host egress broker injects the real model key after validating a short-lived task token. The adapter returns `result_ref` + `evidence:*` from a host-computed artifact hash; the dispatcher stops at `verifying`.

**Tech Stack:** Python 3.13, SQLAlchemy/PostgreSQL, pytest, systemd transient units, stdlib `http.server`/`http.client`/`ssl`/`hmac`, existing `app.runtime.contract`, #96 `RuntimeDispatcher`, #82 systemd patterns.

**Spec:** `docs/security/hermes-managed-execution-design.md`

## Global Constraints

- Only `managed_execution` tasks are handled; no advisory/governed fallback.
- One task per transient systemd unit; `DynamicUser=yes`; no per-task API server; no shared Hermes gateway.
- Never start, stop, replace or mount the personal/default Hermes gateway, Profile, auth, `.env` or sessions.
- Hermes toolset is exactly `terminal`; browser/web/file/code-execution/MCP/plugins/memory/connectors are disabled.
- Prompt is passed only via `--query-file`; the task egress token only via child environment. Neither appears in argv, host `ps`, logs, audit or errors.
- Real provider key lives only in the host egress broker; the unit/task dir/logs never contain it.
- `stop_confirmed=true` only when the unit/cgroup has no processes. Runtime stop is evidence, not business reconciliation; confirmed stops route to `needs_human`, never auto-`cancelled` or `side_effects_reconciled=true`.
- `completed` only reaches `verifying`; the dispatcher never calls `complete_task` and never writes `succeeded`.
- Result evidence is a host-computed SHA-256 of a regular `out/result.txt`; no model/runtime self-report.
- A valid runtime attestation + task attestation is mandatory; absent/expired/mismatched attestation means `unverified`/no launch.
- No new Python dependency, DB migration, public MCP/REST route, or change to `app/api/**`.
- No change to #96 state-machine/Grant/lease semantics; reuse `RuntimeDispatcher`.
- DB tests are Master-run on the approved test host; implementers must run DB-free tests locally and must not claim DB/live passes.
- Live tests are env-gated and Master-run on the approved test host; evidence records command, exit code, runtime version, root hash and SHA.
- Use `uv run --no-sync` with the shared environment; do not create a worktree `.venv`.
- TDD for every task: failing test, RED, minimal implementation, GREEN, commit.

## Review Focus

1. **Prompt/token exposure** — a task prompt or egress token must not appear in host `ps`, process argv, error text, logs, audit, or the result document. Tests must scan the actual argv/env and serialized errors.
2. **Cross-task access** — a task must not read another task's directory, token, unit status, or result; a handle must not bind to a different task. Tests must create two tasks/two units and assert denial.
3. **Ambiguous launch and replay** — accepted only after `started`; missing marker/launch timeout is `unknown` with no second unit; duplicate same key/payload reuses the same unit; different payload is conflict. Tests must assert launch/unit counts.
4. **Artifact trust** — missing, empty, oversized, FIFO, symlinked-final, symlinked-parent, or replaced artifact must fail closed without blocking; the host must hash the file itself. Tests must not let a model-produced string satisfy evidence.
5. **Stop/cancel truth** — `requested` is not stopped; only cgroup-gone is `stop_confirmed`; a completed artifact wins a cancel race; confirmed stop becomes `needs_human`, never auto-`cancelled`.

---

## File Structure

- Create `app/runtime/hermes_egress.py` — egress token claims/mint/verify/hash and host broker server.
- Create `app/runtime/hermes_managed.py` — config, attestations, sandbox spec/status, artifact reader, capabilities, supervisor protocol/client, managed adapter, fixed prompt wrapper.
- Create `app/runtime/hermes_managed_cli.py` — trusted config loader + #96 dispatcher entry point.
- Create `ops/security/agentmgr_hermes_exec.py` — stdlib root helper (prepare/launch/status/stop/cleanup/attest).
- Create `ops/security/agentmgr-hermes-exec` — fixed sudo wrapper.
- Create `ops/security/agentmgr-hermes.sudoers` — single fixed command.
- Create `ops/security/agentmgr_hermes_workload.py` — sandbox workload + local model proxy.
- Create `ops/security/agentmgr-hermes-egress.service` — broker unit.
- Create `ops/security/provision_hermes_runtime.sh` — provisioning checks/install.
- Create `ops/security/verify_hermes_isolation.sh` / `.py` — live preflight/negative checks.
- Create `app/tests/runtime/test_hermes_egress.py` — token/broker tests.
- Create `app/tests/runtime/test_hermes_managed_contract.py` — config/attestation/artifact/capability tests.
- Create `app/tests/runtime/test_hermes_managed_adapter.py` — adapter with fake supervisor.
- Create `app/tests/runtime/test_hermes_managed_live.py` — real broker/adapter/dispatcher, env-gated.
- Create `app/tests/tasks/test_hermes_managed_dispatch.py` — real PostgreSQL dispatcher E2E with fake supervisor.
- Create `app/tests/security/test_hermes_managed_unit.py` — helper argv/spec/attestation unit tests.
- Create `app/tests/security/test_hermes_workload_unit.py` — workload/proxy tests.
- Create `app/tests/security/hermes_probe_workload.py` — fixed test-only workload for the sandbox negative matrix.
- Create `app/tests/security/test_hermes_sandbox_live.py` — test-only probe unit + negative matrix.
- Create `docs/security/hermes-managed-execution-boundary.md` — evidence/limitations.
- Create `docs/operations/hermes-managed-execution.md` — provisioning/run/rollback.
- Modify `docs/integrations/runtimes/hermes.md` — managed-vs-advisory section.
- Modify `ops/security/README.md` — install/rollback notes.

---

### Task 1: Pure Contracts — Egress Tokens, Attestations, Artifact, Capabilities

**Files:**
- Create: `app/runtime/hermes_egress.py`
- Create: `app/runtime/hermes_managed.py`
- Test: `app/tests/runtime/test_hermes_egress.py`
- Test: `app/tests/runtime/test_hermes_managed_contract.py`

**Interfaces:**
- Produces: `EgressClaims` (frozen dataclass: `version`, `task_id`, `grant_id`, `unit`, `model`, `provider_host`, `path_prefix`, `issued_at`, `expires_at`, `nonce`)
- Produces: `mint_egress_token(claims, *, key: bytes, now: datetime | None = None) -> str`
- Produces: `verify_egress_token(token, *, key: bytes, now: datetime | None = None) -> EgressClaims`
- Produces: `egress_token_hash(token: str) -> str`
- Produces: `MAX_EGRESS_TTL_SECONDS = 300`
- Produces: `HermesManagedConfig` (frozen dataclass: helper path, runtime root, **trusted `runtime_root_hash: str`**, task root, broker socket, attestation key path, egress token key path, runtime attestation path, evidence ref, runtime name/version, model name, local proxy base URL template, egress provider host/path prefix, runtime/stop/prompt/result limits, worker_id, lease_seconds, poll_interval, health_backoff_seconds)
- Produces: `SandboxSpec` (frozen dataclass: `task_id`, `grant_id`, `unit`, `runtime_handle`, `task_dir: Path`, `prompt_size: int`, `prompt_sha256: str`, `profile_template_sha256: str`, `egress_token_sha256: str`, `ttl_seconds: int`, `runtime_max_sec: int`, `memory_max_mb: int`, `cpu_quota_percent: int`, `tasks_max: int`; **no raw prompt/token content**)
- Produces: `build_sandbox_spec(config, *, task_id, grant_id, idempotency_key, prompt, profile_template, egress_token) -> SandboxSpec`
- Produces: `TaskAttestation` (`schema`, `unit`, `task_id`, `grant_id`, `runtime_handle`, `uid`, `runtime_root_hash`, `profile_template_sha256`, `prompt_sha256`, `sandbox_facts`, `issued_at`, `expires_at`, `nonce`, `hmac`) and `RuntimeAttestation` (`schema`, `helper_version`, `runtime_name`, `runtime_version`, `runtime_root_hash`, `sandbox_facts`, `broker_socket_ok`, `evidence_ref`, `issued_at`, `expires_at`, `nonce`, `hmac`), with `to_document`/`from_document`.
- Produces: `unit_name_for(task_id: str, idempotency_key: str) -> str`
- Produces: `runtime_handle_for(unit: str) -> str`
- Produces: `VerifiedRuntimeAttestation` (opaque proof-carrying marker returned only by successful HMAC verification)
- Produces: `verify_task_attestation(attestation, *, key: bytes, spec: SandboxSpec, expected_runtime_root_hash: str, now: datetime) -> None`
- Produces: `verify_runtime_attestation(attestation, *, key: bytes, config: HermesManagedConfig, now: datetime) -> VerifiedRuntimeAttestation`
- Produces: `ArtifactResult` (`sha256: str`, `size: int`, `relative_path: str == "out/result.txt"`) and `read_artifact(task_dir: Path, *, max_bytes: int) -> ArtifactResult` — opens `out` then `result.txt` with `dir_fd`, `O_NOFOLLOW|O_DIRECTORY`, `O_NONBLOCK`; rejects FIFO/symlink-parent/non-regular/oversize.
- Produces: `managed_capabilities(config, verified_runtime_attestation: VerifiedRuntimeAttestation | None) -> RuntimeCapabilities` (a raw/unverified attestation is a type error or unverified, never `VERIFIED`).
- Produces: `build_managed_prompt(prompt: str) -> str`
- Produces: `SandboxStatus` (`unit`, `task_id`, `runtime_handle`, `active_state`, `sub_state`, `main_pid`, `result`, `exit_status`, `started`, `heartbeat_at`, `exit_code`, `timed_out`, `cancelled`, `process_gone`, `observed_at`) and `SandboxStop` (`unit`, `active_state`, `result`, `exit_status`, `main_pid`, `process_gone`, `stop_confirmed`, `observed_at`) frozen dataclasses used by Task 5.
- Consumes: `app.runtime.contract` enums/types; stdlib only.

- [ ] **Step 1: Write failing token tests**

In `app/tests/runtime/test_hermes_egress.py`, add tests with these assertions:

```python
def test_egress_token_roundtrip_preserves_task_binding():
    claims = EgressClaims(version=1, task_id="task_1", grant_id="grnt_1",
        unit="agentmgr-hermes-abc", model="m", provider_host="provider.example",
        path_prefix="/v1", issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        nonce="n" * 32)
    token = mint_egress_token(claims, key=KEY, now=NOW)
    assert verify_egress_token(token, key=KEY, now=NOW) == claims

def test_egress_token_rejects_tamper_expiry_and_cross_key():
    ...
    with pytest.raises(EgressTokenError):
        verify_egress_token(token[:-1] + "A", key=KEY, now=NOW)
    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=KEY, now=NOW + timedelta(seconds=61))
    with pytest.raises(EgressTokenError):
        verify_egress_token(token, key=OTHER_KEY, now=NOW)
```

Also test TTL > 300, malformed tokens, empty/oversized claims, constant-time comparison path, and `egress_token_hash` stability/no raw token in `repr`/errors.

- [ ] **Step 2: Run token tests to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_egress.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module/names.

- [ ] **Step 3: Implement `app/runtime/hermes_egress.py` token part**

Implement `EgressClaims`, `EgressTokenError`, `mint_egress_token`, `verify_egress_token`, `egress_token_hash` with stdlib `hmac`, `hashlib`, `base64.urlsafe_b64encode/decode`, `hmac.compare_digest`, JSON canonicalization (sorted keys, compact separators). Validate exact claim keys, non-empty IDs, TTL 1..300, `expires_at > issued_at`, provider host/path shape. Never include the raw token in `repr`/error text.

- [ ] **Step 4: Write failing managed-contract tests**

In `app/tests/runtime/test_hermes_managed_contract.py`, add:

```python
def test_unit_and_handle_are_derived_and_pattern_safe():
    unit = unit_name_for("task_abc", "idem_abc")
    assert re.fullmatch(r"agentmgr-hermes-[0-9a-f]{16}", unit)
    assert unit_name_for("task_abc", "idem_abc") == unit
    assert unit_name_for("task_abc", "idem_other") != unit
    handle = runtime_handle_for(unit)
    assert re.fullmatch(r"rtask_hermes_[A-Za-z0-9_-]{4,63}", handle)

def test_sandbox_spec_is_deterministic_and_bounded():
    spec = build_sandbox_spec(config, task_id="task_abc", grant_id="grnt_abc",
        idempotency_key="idem_abc", prompt="PROMPT-CANARY",
        profile_template="PROFILE-CANARY", egress_token="egt.canary")
    assert spec.unit == unit_name_for("task_abc", "idem_abc")
    assert spec.task_dir == config.task_root / spec.unit
    assert spec.prompt_sha256 == hashlib.sha256(b"PROMPT-CANARY").hexdigest()
    assert "PROMPT-CANARY" not in repr(spec)
    assert "egt.canary" not in repr(spec)

def test_artifact_reader_rejects_fifo_symlink_parent_and_oversize(tmp_path):
    task = tmp_path / "task"
    out = task / "out"
    out.mkdir(parents=True)
    os.mkfifo(out / "result.txt")
    with _alarm(5), pytest.raises(ArtifactError):
        read_artifact(task, max_bytes=1024)  # must not block on a FIFO
    (out / "result.txt").unlink()
    out.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "result.txt").write_text("x")
    out.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactError):
        read_artifact(task, max_bytes=1024)
    out.unlink()
    out.mkdir()
    (out / "result.txt").write_text("x" * 100)
    with pytest.raises(ArtifactError):
        read_artifact(task, max_bytes=10)

def test_unit_name_rejects_control_characters():
    with pytest.raises(ValueError):
        unit_name_for("task_a\nidem_b", "c")
    with pytest.raises(ValueError):
        unit_name_for("task_a", "idem_b\nc")

def test_capabilities_are_unverified_without_verified_runtime_attestation():
    caps = managed_capabilities(config, None)
    for name in ("task.submit", "runtime.isolation", "runtime.managed_execution"):
        assert caps.capability(name).status is CapabilityStatus.UNVERIFIED
    with pytest.raises((TypeError, ValueError)):
        managed_capabilities(config, forged_structural_attestation())

def test_capabilities_with_verified_attestation_and_evidence_are_verified():
    verified = verify_runtime_attestation(valid_runtime_attestation(), key=KEY, config=config, now=NOW)
    caps = managed_capabilities(config, verified)
    assert caps.capability("task.submit").status is CapabilityStatus.VERIFIED
    assert caps.capability("runtime.isolation").limitations
    assert caps.capability("task.events.poll").status is CapabilityStatus.UNVERIFIED

def test_runtime_root_hash_mismatch_is_rejected():
    attestation = valid_runtime_attestation(runtime_root_hash="sha256:" + "0" * 64)
    with pytest.raises(AttestationError):
        verify_runtime_attestation(attestation, key=KEY, config=config, now=NOW)
    with pytest.raises(AttestationError):
        verify_task_attestation(valid_task_attestation(), key=KEY, spec=spec,
            expected_runtime_root_hash=config.runtime_root_hash, now=NOW)
```

Add task/runtime attestation HMAC/tamper/expiry/task-binding tests; prompt wrapper includes the fixed “write out/result.txt” instruction and enforces the size bound.

- [ ] **Step 5: Run managed-contract tests to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_contract.py -q -o addopts='' --noconftest`
Expected: FAIL with missing names.

- [ ] **Step 6: Implement the pure `app/runtime/hermes_managed.py` surface**

Implement the dataclasses/signatures above. `unit_name_for` hashes `agentmgr-hermes:v1:<task_id>\n<idempotency_key>` and returns the fixed prefix + first 16 hex; reject control characters. `runtime_handle_for` returns `rtask_hermes_` + first 32 hex of a domain-separated hash. `read_artifact` takes a `task_dir`, opens only `out/result.txt` with `dir_fd`, `O_NOFOLLOW|O_DIRECTORY` for `out`, `O_NOFOLLOW|O_NONBLOCK` for the file, rejects non-regular/empty/oversize, and streams SHA-256. `managed_capabilities` accepts only a `VerifiedRuntimeAttestation`; without one all managed entries are `unverified`; with it, the spec’s target labels/limitations. `build_managed_prompt` prepends a fixed instruction (work only in CWD, no network, write the required artifact to `out/result.txt`) and rejects prompts over `config.prompt_max_bytes`.

- [ ] **Step 7: Run both test files to verify GREEN**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_egress.py app/tests/runtime/test_hermes_managed_contract.py -q -o addopts='' --noconftest`
Expected: all pass, no warnings.

- [ ] **Step 8: Commit**

```bash
git add app/runtime/hermes_egress.py app/runtime/hermes_managed.py app/tests/runtime/test_hermes_egress.py app/tests/runtime/test_hermes_managed_contract.py
git commit -m "feat(runtime): add hermes managed contracts and egress tokens"
```

---

### Task 2: Root Helper, Wrapper and Sudoers

**Files:**
- Create: `ops/security/agentmgr_hermes_exec.py`
- Create: `ops/security/agentmgr-hermes-exec`
- Create: `ops/security/agentmgr-hermes.sudoers`
- Test: `app/tests/security/test_hermes_managed_unit.py`
- Modify: `app/tests/security/test_helper.py` only if a pinned-constant parity test is needed; otherwise no change.

**Interfaces:**
- Produces: `SandboxRequest` (frozen dataclass: operation, task_id, grant_id, unit, runtime_handle, idempotency_key, prompt_sha256, prompt_bytes, profile_template_sha256, egress_token_sha256, ttl_seconds, runtime_max_sec, memory_max_mb, cpu_quota_percent, tasks_max, `result_read: bool = False` accepted only for `cleanup`)
- Produces: `InstallConfig` (fixed task root, runtime root, trusted runtime root hash, broker socket, workload path, adapter user, **adapter private group**; sandbox group is the fixed `agentmgr-hermes`)
- Produces: `load_install_config(path: Path) -> InstallConfig`
- Produces: `validate_request(raw: Mapping[str, object]) -> SandboxRequest`
- Produces: `build_systemd_run_argv(request: SandboxRequest, *, task_dir: Path, runtime_root: Path, broker_socket: Path, workload: Path) -> list[str]`
- Produces: `parse_systemctl_show(text: str) -> UnitState`
- Produces: `sign_attestation(payload: Mapping[str, object], key: bytes) -> dict[str, object]`
- Produces: `unit_for_task(task_id: str, idempotency_key: str) -> str` (must equal Task 1 `unit_name_for`; pin with a shared test vector)
- Consumes: stdlib only; no `app.*` import.

- [ ] **Step 1: Write failing helper tests**

In `app/tests/security/test_hermes_managed_unit.py`, load the helper with `importlib.util.spec_from_file_location`, then test:

```python
def test_request_rejects_unknown_or_forbidden_fields():
    for field in ("ExecStart", "Environment", "BindPaths", "User", "PrivateNetwork"):
        with pytest.raises(HelperError):
            validate_request({**base_request(), field: "x"})
    with pytest.raises(HelperError):
        validate_request({**base_request(), "extra": 1})

def test_systemd_argv_has_fixed_sandbox_properties():
    argv = build_systemd_run_argv(valid_request(), task_dir=TASK_DIR,
        runtime_root=RUNTIME_ROOT, broker_socket=BROKER_SOCKET, workload=WORKLOAD)
    assert argv[0] == "systemd-run"
    assert "--property=DynamicUser=yes" in argv
    assert "--property=Group=agentmgr-hermes" in argv
    assert f"--property=InaccessiblePaths=-{TASK_DIR.parent}" in argv
    assert "--property=PrivateDevices=yes" in argv
    assert "--property=PrivateNetwork=yes" in argv
    assert "--property=ProtectHome=yes" in argv
    assert f"--property=BindPaths={TASK_DIR}:/task" in argv
    assert f"--property=BindReadOnlyPaths={RUNTIME_ROOT}:/runtime" in argv
    assert f"--property=BindReadOnlyPaths={BROKER_SOCKET}:/task/egress.sock" in argv
    assert "--property=KillMode=control-group" in argv
    assert "--property=ExitType=cgroup" in argv
    assert not any("ExecStart=" in item for item in argv)
    assert argv[-len(WORKLOAD_ARGV):] == WORKLOAD_ARGV
```

Also test unit-name parity with `unit_name_for` (including control-character rejection), task/grant/unit binding, path traversal/symlink/wrong-owner rejection, numeric limits, response file hash validation, `parse_systemctl_show` mapping, HMAC tamper, runtime-root-hash parity, `Group=agentmgr-hermes`/`InaccessiblePaths=-<task-root>`/`PrivateDevices=yes`, `status` not-found nulling, `attest` after token deletion/cleanup returning runtime attestation with `task_attestation=None`, stop negative (active/cgroup non-empty -> `stop_confirmed=False`), attestation key modes/groups (`0440` adapter-private-group accepted, `0440` sandbox-group rejected, `0600` accepted), `/tmp` source rejection, manifest idempotency/payload digest, stable error codes, no secrets in error strings.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_managed_unit.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module/names.

- [ ] **Step 3: Implement the helper**

Implement exact-key validation, fixed path roots, root-owned wrapper contract, `prepare/launch/status/stop/cleanup/attest` dispatch, `systemd-run` argv builder, `systemctl show` parser, atomic JSON response, HMAC attestation. The helper derives `task_dir` as `<fixed task root>/<unit>`, creates it and `out/` mode 0770 with the configured trusted adapter user as owner and `agentmgr-hermes` as group, and refuses symlinks/wrong ownership. The unit uses `Group=agentmgr-hermes` as its static primary group (confirmed with a transient-unit probe) so unit-written files inherit the group and the adapter can read them; `out/` stays group-writable. Add `InaccessiblePaths=-<task_root>` (confirmed bind source still works) so siblings/tokens are not readable, and `PrivateDevices=yes`. `launch` verifies the prompt/profile-template/token file hashes and sizes before starting; it never receives their contents. `stop` waits for `ActiveState=inactive/failed` and `MainPID=0`/empty cgroup before `stop_confirmed=true`; add a negative test where the unit stays active or the cgroup still holds a PID and `stop_confirmed` must be false. `status` must emit `result=None`/`exit_status=None` when `LoadState=not-found` (systemd otherwise defaults to `Result=success`/`ExecMainStatus=0`). `attest` must inspect the unit first and return a runtime attestation with `task_attestation=None` when the unit is stopped or the task dir/token is already cleaned, while still requiring the runtime root/broker/workload. Allow the attestation key to be root:adapter-private-group 0440 (or root-only 0400) so the trusted adapter can verify HMACs; reject group-read with the sandbox `agentmgr-hermes` gid, any other group, and keys under the task root; require `/etc/agentmgr`. The adapter must never receive the key through the sandbox. Reject `/tmp`/`/var/tmp` bind sources (private tmpfs). Add idempotency key + payload digest to the manifest. Mask host `/run` (private tmpfs) and any optional Docker/container socket/state paths; add only explicit syscalls Hermes needs if `@system-service` is insufficient, and record the exception in the evidence doc. No `shell=True`, no argv input, no logging of stdin or secrets.

- [ ] **Step 4: Add wrapper and sudoers**

`ops/security/agentmgr-hermes-exec` is a POSIX shell wrapper that `exec`s `/usr/bin/python3 <fixed helper path>` with no arguments; it must not use `eval`, `sh -c` or caller paths. `ops/security/agentmgr-hermes.sudoers` allows only the trusted adapter user to run that exact wrapper with no arguments. Add a test that reads the wrapper and asserts the fixed command and absence of `eval`/`sh -c`.

- [ ] **Step 5: Run to verify GREEN**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_managed_unit.py -q -o addopts='' --noconftest`
Expected: all pass, no warnings.

- [ ] **Step 6: Commit**

```bash
git add ops/security/agentmgr_hermes_exec.py ops/security/agentmgr-hermes-exec ops/security/agentmgr-hermes.sudoers app/tests/security/test_hermes_managed_unit.py
git commit -m "feat(security): add hermes sandbox helper"
```

---

### Task 3: Sandbox Workload and Local Model Proxy

**Files:**
- Create: `ops/security/agentmgr_hermes_workload.py`
- Test: `app/tests/security/test_hermes_workload_unit.py`

**Interfaces:**
- Produces: `build_child_env(task_dir: Path, proxy_port: int) -> dict[str, str]`
- Produces: `build_hermes_argv(runtime_root: Path, *, model: str, base_url: str, run_budget_sec: int) -> list[str]`
- Produces: `render_profile(template: str, *, proxy_port: int, model: str) -> str` (replaces the `__PORT__` placeholder with the actual loopback port; the rendered profile contains no real key)
- Produces: `class EgressProxy` / `start_egress_proxy(socket_path: Path) -> ProxyHandle` (port, thread, close)
- Produces: `write_status_atomic(path: Path, payload: Mapping[str, object]) -> None`
- Produces: `run_workload(*, task_dir: Path, runtime_root: Path, model: str, run_budget_sec: int, proxy_socket: Path) -> int`
- Consumes: stdlib only; no `app.*` import; no host secrets.

- [ ] **Step 1: Write failing workload tests**

In `app/tests/security/test_hermes_workload_unit.py`, test:

```python
def test_hermes_argv_uses_query_file_and_never_prompt_or_key(tmp_path):
    argv = build_hermes_argv(RUNTIME_ROOT, model="model-x",
        base_url="http://127.0.0.1:1234/v1", run_budget_sec=60)
    assert "chat" in argv
    assert "--query-file" in argv and "/task/prompt.txt" in argv
    assert "--oneshot" in argv and "-Q" in argv
    assert "-t" in argv and "terminal" in argv
    assert "--safe-mode" in argv
    assert "--api-key" not in argv
    assert not any("PROMPT-CANARY" in item or "TOKEN-CANARY" in item for item in argv)

def test_child_env_clears_host_secrets_and_sets_hermes_home(tmp_path):
    env = build_child_env(tmp_path, 1234)
    assert env["HOME"] == str(tmp_path / "home")
    assert env["HERMES_HOME"] == str(tmp_path / "profile")
    assert env["XDG_STATE_HOME"] == str(tmp_path / "state")
    assert env["HERMES_GATEWAY_LOCK_DIR"] == str(tmp_path / "locks")
    assert "AGENTMGR_DB_DSN" not in env
    assert "API_KEY" not in " ".join(env)
```

Also test profile rendering (`__PORT__` replaced, provider custom/base URL/model, no key), proxy forwards method/path/body and strips client Authorization, proxy adds the task token header from `/task/egress.token`, streaming response, status/heartbeat/exit atomic writes, timeout sends SIGTERM/SIGKILL to the child process group, and no prompt/token in status/log files.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_workload_unit.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module/names.

- [ ] **Step 3: Implement the workload**

Implement fixed paths (`/task`, `/task/prompt.txt`, `/task/profile.template.yaml`, `/task/profile/config.yaml`, `/task/egress.token`, `/task/out/result.txt`), environment construction with an allowlist, template rendering, local `ThreadingHTTPServer` proxy on `127.0.0.1:0` that forwards to the Unix socket and streams responses, Hermes argv exactly as in the spec, child process group management, heartbeat every configured interval, atomic status/exit files, bounded stdout/stderr capture, and `--run-budget`. The prompt is read from the file, never argv. The token is read from the file and passed only in the child env / proxy header.

- [ ] **Step 4: Run to verify GREEN**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_workload_unit.py -q -o addopts='' --noconftest`
Expected: all pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add ops/security/agentmgr_hermes_workload.py app/tests/security/test_hermes_workload_unit.py
git commit -m "feat(security): add hermes sandbox workload"
```

---

### Task 4: Host Egress Broker

**Files:**
- Modify: `app/runtime/hermes_egress.py`
- Create: `ops/security/agentmgr-hermes-egress.service`
- Test: `app/tests/runtime/test_hermes_egress_broker.py`

**Interfaces:**
- Produces: `BrokerConfig` (frozen dataclass: socket path, upstream base URL, token HMAC key, provider key, revocation dir, max body bytes, timeout)
- Produces: `BrokerResponse` (status, headers, body)
- Produces: `class EgressBroker` with `handle(method: str, path: str, headers: Mapping[str,str], body: bytes) -> BrokerResponse` and `serve_forever() -> None`
- Produces: `run_broker(config: BrokerConfig) -> int` and `python -m app.runtime.hermes_egress --config <root-only-config>` entrypoint for the service unit.
- Consumes: Task 1 `verify_egress_token`, `egress_token_hash`; stdlib `http.server`, `http.client`, `ssl`, `socketserver`.

- [ ] **Step 1: Write failing broker tests**

In `app/tests/runtime/test_hermes_egress_broker.py`, use a local stdlib upstream HTTP server and a Unix-socket client. Test:

```python
def test_valid_token_injects_real_key_and_forwards_path(broker, upstream):
    token = mint_egress_token(claims(task_id="task_1"), key=KEY, now=NOW)
    response = broker.handle("POST", "/v1/chat/completions",
        {"X-AgentMgr-Egress-Token": token, "Authorization": "Bearer task-token"},
        b'{"model":"m","messages":[]}')
    assert response.status == 200
    assert upstream.last_headers["Authorization"] == f"Bearer {REAL_KEY}"
    assert "X-AgentMgr-Egress-Token" not in upstream.last_headers

def test_invalid_expired_revoked_and_cross_task_tokens_are_rejected(broker):
    ...
    assert broker.handle(...).status in {401, 403}
```

Also test path/method allowlist, 3xx refusal, streaming body, max request/response size, upstream 401/429/503 mapping, no real key/token in logs, and revocation directory behavior.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_egress_broker.py -q -o addopts='' --noconftest`
Expected: FAIL with missing broker.

- [ ] **Step 3: Implement the broker**

Implement a `socketserver.ThreadingUnixStreamServer` + `BaseHTTPRequestHandler` bridge, strict method/path allowlist against `BrokerConfig.upstream_base_url`, token validation, revocation check, header allowlist, body size cap, `http.client.HTTPSConnection` with TLS verification, no redirects, streaming response copy, and stable error responses. The real key is read once from the configured file/credential; it must not appear in errors/logs. The service unit runs the broker as a dedicated user with `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp`, `NoNewPrivileges`, `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX`, and a read-only key credential.

- [ ] **Step 4: Run to verify GREEN**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_egress.py app/tests/runtime/test_hermes_egress_broker.py -q -o addopts='' --noconftest`
Expected: all pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/hermes_egress.py ops/security/agentmgr-hermes-egress.service app/tests/runtime/test_hermes_egress_broker.py
git commit -m "feat(runtime): add hermes egress broker"
```

---

### Task 5: Supervisor Client and Managed Adapter

**Files:**
- Modify: `app/runtime/hermes_managed.py`
- Test: `app/tests/runtime/test_hermes_managed_adapter.py`

**Interfaces:**
- Produces: `SandboxSupervisor` Protocol with `prepare(spec) -> None`, `launch(spec) -> TaskAttestation`, `status(unit) -> SandboxStatus`, `stop(unit, *, reason) -> SandboxStop`, `cleanup(unit, *, result_read: bool = False) -> None`, `runtime_attestation() -> RuntimeAttestation`
- Produces: `HelperSandboxSupervisor(config, *, runner=subprocess.run)` — stdin JSON, no shell, fixed helper path, JSON response parsing, stable error mapping.
- Produces: `HermesManagedAdapter(config, supervisor)` implementing every `RuntimeAdapter` method.
- Consumes: Task 1 dataclasses/`build_sandbox_spec`/artifact/capabilities/prompt; Task 2 helper protocol; Task 4 token minting (using `config.egress_token_key_path`).

- [ ] **Step 1: Write failing adapter tests**

In `app/tests/runtime/test_hermes_managed_adapter.py`, build a `FakeSupervisor` with scripted launch/status/stop and a temp task root. Test:

```python
def test_submit_accepts_only_after_started_and_returns_stable_handle(...):
    result = adapter.submit_task("task_1", "PROMPT-CANARY", "idem_1", managed_grant())
    assert result.outcome is SubmitOutcome.ACCEPTED
    assert result.runtime_task_id.startswith("rtask_hermes_")
    assert supervisor.launch_calls == 1

def test_missing_started_marker_returns_unknown_and_stops(...):
    ...
    assert result.outcome is SubmitOutcome.UNKNOWN
    assert supervisor.stop_calls == 1
    assert supervisor.launch_calls == 1

def test_same_key_same_payload_reuses_unit_and_different_payload_conflicts(...):
    ...
    assert first.runtime_task_id == second.runtime_task_id
    assert supervisor.launch_calls == 1
    assert conflict.outcome is SubmitOutcome.CONFLICT

def test_completed_artifact_becomes_verifying_evidence_not_success(...):
    ...
    status = adapter.get_status(handle, task_id="task_1")
    assert status.state is RuntimeState.COMPLETED
    result = adapter.get_result(handle, task_id="task_1")
    assert result.result_ref.startswith("result_")
    assert result.evidence_refs[0].startswith("evidence:hermes_artifact_")
    assert result.structured_result is True

def test_missing_or_symlinked_artifact_fails_closed(...):
    ...
    assert adapter.get_result(...).outcome in {ResultOutcome.UNKNOWN, ResultOutcome.NEEDS_HUMAN}

def test_cancel_requested_is_not_stopped_and_cgroup_gone_confirms(...):
    ...
    assert cancel_requested.outcome is CancelOutcome.REQUESTED
    assert cancel_requested.stop_confirmed is False
    assert confirmed.outcome is CancelOutcome.STOPPED
    assert confirmed.stop_confirmed is True

def test_capabilities_do_not_claim_managed_without_runtime_attestation(...):
    ...
    assert adapter.capabilities().capability("runtime.managed_execution").status is CapabilityStatus.UNVERIFIED
```

Add cross-task handle rejection, launch failure, status/result unreadable, timeout, completed-vs-cancel, health, no prompt/token in error strings, and no second launch on any ambiguous path.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_adapter.py -q -o addopts='' --noconftest`
Expected: FAIL with missing adapter/supervisor.

- [ ] **Step 3: Implement the supervisor client and adapter**

Implement `HelperSandboxSupervisor` with a fixed helper path, mapping `SandboxSpec` to the helper's exact-key JSON request (hashes/sizes only, no prompt/token content, no caller paths), stdin JSON, `subprocess.run(..., shell=False, timeout=...)`, no argv secrets, strict JSON parsing, and error mapping. Implement `HermesManagedAdapter`:
- `capabilities` calls `verify_runtime_attestation` first and passes only the returned `VerifiedRuntimeAttestation` to `managed_capabilities`; default unverified. A structurally valid forged 64-hex HMAC must not yield `VERIFIED`.
- `submit_task` validates the managed grant shape, calls `verify_task_attestation(..., expected_runtime_root_hash=config.runtime_root_hash)`, uses `build_sandbox_spec` with the dispatcher's idempotency key, creates the task dir via supervisor, writes `prompt.txt`, `profile.template.yaml` (with `__PORT__`), and `egress.token` with the helper's required modes/group (prompt/profile group `agentmgr-hermes`, token exactly `0640`, `out/` `0770`), then calls helper `launch`. The helper returns only after the unit is active and the `started` marker exists; a launch response without that evidence is treated as `unknown` + stop/cleanup. Duplicate same key/payload -> same handle/`replayed=True`; different payload -> conflict.
- `get_status` verifies task binding and maps `SandboxStatus`; `stop_confirmed` only from `process_gone`.
- `get_result` calls `read_artifact(spec.task_dir, max_bytes=...)` (fixed `out/result.txt`, dir_fd/O_NOFOLLOW/O_NONBLOCK); returns result/evidence or unknown/needs_human.
- `cancel_task` maps helper stop; `requested` vs `stopped`/`already_terminal`.
- `poll_events` empty/unsupported; `provide_input` unsupported; `health` helper/broker/runtime root/attestation.
- No DB writes; no `complete_task`.

- [ ] **Step 4: Run to verify GREEN**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_adapter.py app/tests/runtime/test_hermes_managed_contract.py -q -o addopts='' --noconftest`
Expected: all pass, no warnings.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/hermes_managed.py app/tests/runtime/test_hermes_managed_adapter.py
git commit -m "feat(runtime): add hermes managed adapter and supervisor client"
```

---

### Task 6: Dispatcher E2E with Fake Supervisor (Real PostgreSQL)

**Files:**
- Create: `app/tests/tasks/fake_hermes_supervisor.py`
- Create: `app/tests/tasks/test_hermes_managed_dispatch.py`
- Consumes: #96 `RuntimeDispatcher`, `DispatcherConfig`, `submit_managed` test helper, task world fixtures.

**Interfaces:**
- Produces: `FakeHermesSupervisor` (scripted `launch/status/stop/cleanup/runtime_attestation`, call counters; returns a valid runtime attestation/evidence so the adapter's managed capabilities are `verified` in the test).
- Consumes: Task 5 `HermesManagedAdapter`.

- [ ] **Step 1: Write failing dispatcher tests**

In `app/tests/tasks/test_hermes_managed_dispatch.py`, with real PostgreSQL fixtures:

```python
def test_dispatcher_drives_managed_task_to_verifying_and_verifier_completes(session, world):
    task = submit_managed(session, world, key="idem_hermes_dispatch_0001")
    session.commit()
    adapter = HermesManagedAdapter(config, FakeHermesSupervisor(...))
    dispatcher = RuntimeDispatcher(adapter, config, service_principal=service, session=session)
    result = dispatcher.run_once()
    assert result.outcome == "verifying"
    assert task.state == "verifying"
    assert task.state != "succeeded"
    # host-side independent verifier
    complete_task(..., evidence=[EvidenceRecord(source="artifact_store", observed=digest, ...)])
    assert task.state == "succeeded"

def test_dispatcher_cancel_confirmed_routes_to_needs_human(session, world):
    ...
    assert task.state == "needs_human"
    assert (task.result or {}).get("side_effects_reconciled") is not True

def test_dispatcher_ambiguous_launch_does_not_replay(session, world):
    ...
    assert supervisor.launch_calls == 1
    assert task.state == "unknown"

def test_dispatcher_duplicate_submit_creates_one_unit(session, world):
    ...
    assert supervisor.launch_calls == 1
```

Add timeout, grant/lease mismatch, adapter restart/lookup, health degradation, and no `complete_task` tripwire.

- [ ] **Step 2: Run to verify RED / collection**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/tasks/test_hermes_managed_dispatch.py -q -o addopts='' --collect-only`
Expected: collection succeeds after implementation names exist; pre-implementation fails for missing module.

- [ ] **Step 3: Implement the fake supervisor and tests**

Implement the scripted fake and tests using #96 helpers. Do not mock the database, task service, lease/fencing or verification. Use the real `complete_task` with an independent `EvidenceRecord` for the success case.

- [ ] **Step 4: Master runs the real PostgreSQL test**

Master command on the approved host:

```bash
UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/tasks/test_hermes_managed_dispatch.py -q -o addopts=''
```

Implementer must not claim this run.

- [ ] **Step 5: Commit**

```bash
git add app/tests/tasks/fake_hermes_supervisor.py app/tests/tasks/test_hermes_managed_dispatch.py
git commit -m "test(tasks): drive hermes managed adapter through dispatcher"
```

---

### Task 7: Real Sandbox Probe and Hermes Lifecycle (Live)

**Files:**
- Create: `app/tests/security/hermes_probe_workload.py`
- Create: `app/tests/security/test_hermes_sandbox_live.py`
- Consumes: Task 2 helper `build_systemd_run_argv`/`SandboxRequest`; the fixed test-only probe workload; Task 4 broker in stub-upstream mode; Task 3 workload.
- Env gate: `AGENTMGR_HERMES_LIVE=1` and `AGENTMGR_HERMES_SUDO=1`; skip with a reason otherwise.

**Interfaces:**
- Test-only probe workload path (under `app/tests/security/`) that runs the negative probes inside the same systemd property profile. It is launched by the test with `systemd-run` using the production argv builder; no production helper `backend=probe` surface.
- Production helper live launch uses `agentmgr-hermes-exec` and the Task 3 workload with a **stub broker that returns a fixed assistant text only** (no tool calls). This proves unit/workload/Hermes process lifecycle and prompt handling, not business success and not artifact creation.

- [ ] **Step 1: Write failing/skip-gated live tests**

Add tests that:
- Run a probe unit with `PrivateNetwork=yes`; assert `/etc/shadow`, host home, DB socket, other task dir/prompt/token, `/run/systemd`, `/run/dbus`, Docker socket are denied; external `<external-ipv4>:<port>` gives `ENETUNREACH`; loopback works.
- Launch the production helper for a Hermes task with the stub broker (fixed assistant text, no tool calls); assert unit active, DynamicUser UID, read-only runtime root, task dir writable only, started marker, completed/exit status, stop/cgroup-gone, cleanup, and no artifact -> adapter result is unknown/needs_human (not success).
- Assert the unit does not see the personal gateway lock/socket or profile.
- Assert a malformed/second launch with the same key does not create a second unit.
- Assert prompt/token are absent from `systemctl show`, `/proc/<pid>/cmdline` (where readable), and unit logs; token only in the task dir with mode 0640 and child env.
- Skip by default with a clear reason when env vars are missing.

- [ ] **Step 2: Run collection / RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts='' --collect-only`
Expected: collection succeeds; tests skip locally.

- [ ] **Step 3: Implement the probe/test helpers**

Use the production helper's pure `build_systemd_run_argv` for the test-only probe unit; keep the probe script fixed and read-only. Use `subprocess.run([...], shell=False)` and `sudo -n`. Clean up units/task dirs in `finally`; never touch the personal gateway.

- [ ] **Step 4: Master runs live on the approved host**

Master command (env values provided at test time):

```bash
AGENTMGR_HERMES_LIVE=1 AGENTMGR_HERMES_SUDO=1 \
AGENTMGR_HERMES_RUNTIME_ROOT=<runtime-root> ... \
UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''
```

Record command, exit code, runtime root hash, Hermes version, and SHA in the boundary evidence doc (no host/IP/personal paths). Implementer must not claim the live run.

- [ ] **Step 5: Commit**

```bash
git add app/tests/security/test_hermes_sandbox_live.py
git commit -m "test(security): add hermes sandbox live matrix"
```

---

### Task 8: Real Model Egress, Real Adapter and Dispatcher E2E (Live)

**Files:**
- Create: `app/tests/runtime/test_hermes_managed_live.py`
- Consumes: Task 4 broker, Task 5 adapter/helper, Task 6 DB fixtures, dedicated provider key/base URL/model supplied at test time.
- Env gate: `AGENTMGR_HERMES_LIVE=1`, `AGENTMGR_HERMES_SUDO=1`, `AGENTMGR_HERMES_PROVIDER_KEY_FILE`, `AGENTMGR_HERMES_PROVIDER_BASE_URL`, `AGENTMGR_HERMES_MODEL`, runtime root/task root/helper paths.

**Interfaces:**
- Starts a real broker on a temp Unix socket with the dedicated key for the artifact test, and a local stub upstream for deterministic cancel/timeout/token tests.
- Creates a managed task whose goal requires a deterministic artifact: prompt instructs the model to write exactly `OK` to `out/result.txt`; expected SHA-256 is computed in the test.
- Runs the real `HermesManagedAdapter` + `RuntimeDispatcher` against real PostgreSQL; asserts `verifying`, artifact hash, independent `complete_task` -> `succeeded`.
- Token expiry/revoke/cross-task and cancel/timeout use the local stub upstream; real key absence from unit/task/logs is checked with the real-key run.

- [ ] **Step 1: Write failing/skip-gated live tests**

Add tests with the exact env gates and assertions above. Include:
- `test_real_model_writes_artifact_and_dispatcher_reaches_verifying`
- `test_real_key_never_enters_unit_or_task_dir`
- `test_expired_revoked_and_cross_task_tokens_are_rejected`
- `test_cancel_confirmed_routes_to_needs_human`
- `test_timeout_stops_unit_and_never_succeeds`

Skip clearly if the dedicated key/config is absent.

- [ ] **Step 2: Run collection / RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_live.py -q -o addopts='' --collect-only`
Expected: collection succeeds; tests skip locally.

- [ ] **Step 3: Implement the live harness**

Start/stop the broker and units in fixtures; use temp roots; never read the personal profile; clean up all units/dirs/tokens in `finally`. The test may retry the real model once with a clearer prompt, but must not mark a missing/incorrect artifact as success.

- [ ] **Step 4: Master runs live on the approved host**

Master command:

```bash
AGENTMGR_HERMES_LIVE=1 AGENTMGR_HERMES_SUDO=1 \
AGENTMGR_HERMES_PROVIDER_KEY_FILE=<root-only-file> \
AGENTMGR_HERMES_PROVIDER_BASE_URL=<provider-base-url> \
AGENTMGR_HERMES_MODEL=<model> ... \
UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_live.py -q -o addopts=''
```

Record evidence; implementer must not claim it.

- [ ] **Step 5: Commit**

```bash
git add app/tests/runtime/test_hermes_managed_live.py
git commit -m "test(runtime): add hermes managed live e2e"
```

---

### Task 9: CLI Launcher, Provisioning, Verification and Documentation

**Files:**
- Create: `app/runtime/hermes_managed_cli.py`
- Create: `app/tests/runtime/test_hermes_managed_cli.py`
- Create: `ops/security/provision_hermes_runtime.sh`
- Create: `ops/security/verify_hermes_isolation.sh`
- Create: `ops/security/verify_hermes_isolation.py`
- Modify: `ops/security/README.md`
- Create: `docs/security/hermes-managed-execution-boundary.md`
- Create: `docs/operations/hermes-managed-execution.md`
- Modify: `docs/integrations/runtimes/hermes.md`

**Interfaces:**
- Produces: `load_config(path: Path) -> HermesManagedConfig` (no secrets; validates roots/attestation/evidence/model name/local proxy template/worker settings; the real provider upstream/key belongs to the broker config, not this file).
- Produces: `main(argv=None, *, dispatcher_factory=None) -> int`; `--once`/`--loop`, no default adapter, nonzero with a stable message when trusted config/attestation/service principal is absent. `main` constructs `HermesManagedAdapter`, #96 `DispatcherConfig`/`RuntimeDispatcher`, and owns the session lifecycle.
- Consumes: Task 5 adapter, #96 `RuntimeDispatcher`, `SessionLocal`.

- [ ] **Step 1: Write failing CLI/config tests**

In `app/tests/runtime/test_hermes_managed_cli.py`, test:
- no trusted config -> nonzero, message mentions #97 configuration; no adapter construction.
- unknown/malformed config -> nonzero stable reason; no secrets/paths printed.
- injected dispatcher factory -> `--once` calls `run_once` once; `--loop` stops on event.
- config loader rejects missing/invalid roots, attestation, evidence, service identity, provider/model.
- no default Fake/Advisory/Hermes host fallback.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_cli.py -q -o addopts='' --noconftest`
Expected: FAIL with missing CLI.

- [ ] **Step 3: Implement CLI/config**

Implement the trusted config loader (root-only path passed by the launcher; no secrets), session factory, service-principal resolver (existing `app.governance.principal` service-identity lookup; no new auth path), adapter + `RuntimeDispatcher` construction, `--once`/`--loop`, signal handling and stable exit codes. The CLI never prints the config path, prompt, token or exception message; only stable codes. It must call `run_forever` with a fresh session per iteration.

- [ ] **Step 4: Implement provisioning/verification scripts**

`provision_hermes_runtime.sh` must be idempotent and fail closed: create group/user/dirs, compute and record the trusted runtime root manifest hash, verify runtime root excludes personal profile/auth, install helper/wrapper/workload/broker unit/sudoers (root-owned) at the fixed deployment names used by the helper (`/usr/local/libexec/agentmgr_hermes_exec.py`, `/usr/local/libexec/agentmgr-hermes-exec`, `/etc/sudoers.d/agentmgr-hermes`, `/etc/agentmgr/hermes-exec.json`), write no secrets, print the required root-only inputs. Create the adapter's private group and install the attestation key root:adapter-private-group 0440 (or root-only 0400), never `agentmgr-hermes`-readable; add the adapter user to `agentmgr-hermes` for task-dir access. The broker socket must live outside the unit's private `/run`, `/tmp` and `/var/tmp` (for example under the task root's parent with group `agentmgr-hermes`), because it is bind-mounted into the unit. `verify_hermes_isolation.sh`/`.py` must check helper/wrapper modes, sudoers exactness, unit property builder, runtime root manifest hash equality against the configured `runtime_root_hash`, personal profile not mounted, broker socket/key permissions, and fail on any mismatch. Tests may inspect script text for forbidden `eval`/`sh -c` and fixed paths.

- [ ] **Step 5: Write docs**

- `docs/security/hermes-managed-execution-boundary.md`: evidence scope (live command/exit/version/SHA), what was proven, what remains unverified, residual risks (copy §17), capability labels, no overclaim.
- `docs/operations/hermes-managed-execution.md`: architecture, provisioning, start/stop, `--once`/`--loop`, config, health/backoff, cancel/timeout/recovery, rollback, capacity, no public Managed API, #97/#83 dependency.
- `docs/integrations/runtimes/hermes.md`: split advisory `/v1/runs` (unchanged) from managed one-shot supervisor; labels and limitations.
- `ops/security/README.md`: install/rollback notes.

- [ ] **Step 6: Run unit/CLI tests and full verification**

Run:
- `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_hermes_managed_contract.py app/tests/runtime/test_hermes_egress.py app/tests/runtime/test_hermes_egress_broker.py app/tests/runtime/test_hermes_managed_adapter.py app/tests/runtime/test_hermes_managed_cli.py app/tests/security/test_hermes_managed_unit.py app/tests/security/test_hermes_workload_unit.py -q -o addopts=''`
- `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests -q -o addopts=''` (Master on test host if DB needed)
- `python3 scripts/check_privacy.py --ci`
- `python3 scripts/validate_kit.py --project-ready`
- `git diff --check`

Expected: all pass; live tests skip without env; no warnings.

- [ ] **Step 7: Commit**

```bash
git add app/runtime/hermes_managed_cli.py app/tests/runtime/test_hermes_managed_cli.py \
  ops/security/provision_hermes_runtime.sh ops/security/verify_hermes_isolation.sh \
  ops/security/verify_hermes_isolation.py ops/security/README.md \
  docs/security/hermes-managed-execution-boundary.md docs/operations/hermes-managed-execution.md \
  docs/integrations/runtimes/hermes.md
git commit -m "feat(runtime): add hermes managed cli and operations"
```

---

### Task 10: Final Whole-Branch Verification

**Files:** none (verification/review task; fixes go back into the owning task files).

- [ ] **Step 1: Full test suite on the approved test host**

Run:
- `pytest app/tests/tasks/test_hermes_managed_dispatch.py -q -o addopts=''`
- `pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''` (live env)
- `pytest app/tests/runtime/test_hermes_managed_live.py -q -o addopts=''` (live env)
- `pytest app/tests -q -o addopts=''`

Record exact counts/exit codes. No live claim without the env-gated run.

- [ ] **Step 2: CI/privacy/validate/diff**

Confirm `kit-validate`, `privacy-scan`, `app-tests` on the current head; `git diff --check`; no host/path/secret in docs/evidence.

- [ ] **Step 3: Whole-branch review**

Use the final reviewer with the spec, plan, deferred-minor ledger, diff, test-host evidence and CI. Triage every `minor`/`carry`/`out-of-scope`; fix Critical/Important, then scoped re-review.

- [ ] **Step 4: Merge/post-merge verification**

Only after all gates pass: merge, run Fast + Full on main, record evidence, close #97, release the active lock, clean branches/worktrees/test-host units/task dirs. If any live gate is not met, keep #97 open and report the exact blocker; do not upgrade capability labels.

---

## Plan Self-Review

- **Spec coverage:** Task 1 contracts/tokens/attestation/artifact/capabilities; Task 2 helper/sandbox; Task 3 workload/proxy; Task 4 broker; Task 5 adapter; Task 6 dispatcher DB E2E; Task 7 live sandbox; Task 8 live model/dispatcher; Task 9 CLI/ops/docs/full verification; Task 10 final gates. All spec sections §1–§19 map.
- **Type consistency:** `EgressClaims`, `HermesManagedConfig`, `SandboxSpec`, `TaskAttestation`, `RuntimeAttestation`, `SandboxStatus`, `SandboxStop`, `SandboxSupervisor`, `HermesManagedAdapter`, `unit_name_for`, `runtime_handle_for`, `read_artifact`, `managed_capabilities`, `build_managed_prompt`, `BrokerConfig`, `EgressBroker`, `load_config`, `main` are fixed in Task 1/2/4/5/9 and reused.
- **Review Focus:** prompt/token exposure (Tasks 1/2/3/5), cross-task (Tasks 5/7), ambiguous launch/replay (Tasks 5/6), artifact trust (Tasks 1/5/6), stop/cancel truth (Tasks 5/6/8).
- **Proportion:** no production bodies are transcribed; exact signatures, test names/assertions, commands and values are pinned. Live tests are env-gated and Master-run.

## Execution Handoff

Do not start implementation until the owner reviews this plan and selects an execution method. Recommended: **subagent-driven**, because the helper/workload/broker/adapter interfaces are tightly coupled and a wrong managed-state write is expensive.
