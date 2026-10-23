# Managed Execution 隔离基座 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the #82 controlled-execution substrate: per-task systemd sandbox, task-scoped short-lived credentials, Unix-socket PEP gateway, supervisor stop evidence, and reproducible negative tests with a synthetic Agent workload.

**Architecture:** A trusted controller validates a server-resolved Grant, issues an `agx1` credential, and starts a transient systemd unit with `DynamicUser` and a hardened sandbox. The untrusted workload talks only to a Unix-socket PEP gateway; the gateway revalidates the credential, revocation state, live Grant state, capability, and resource scope on every call. The supervisor stops and verifies the unit when the Grant expires or is revoked.

**Tech Stack:** Python 3.13 repo code, stdlib only (`hmac`, `hashlib`, `json`, `socket`, `socketserver`, `subprocess`, `dataclasses`), pytest, systemd 257 on the approved test host, cgroup v2.

**Spec:** `docs/security/managed-execution-isolation-design.md` (PR #94; this plan's commit includes the `GrantStatus.subject_id` interface clarification). The plan implements that approved design; do not broaden it.

## Global Constraints

- Scope is the **isolation substrate + synthetic Agent workload only**. Hermes-in-sandbox, model-provider egress, browser/terminal governance, and end-to-end managed execution are **#83**.
- No new Python dependency, no database migration, no changes to `app/governance/**`, `app/tasks/**`, `app/runtime/**`, `app/api/**`, `migrations/**`, `pyproject.toml`, or `uv.lock`.
- Only these paths may change: `app/security/**`, `app/tests/security/**`, `ops/security/**`, `docs/security/**`.
- TTL and credential lifetime are both capped at **300 seconds**. Longer tasks/credential refresh are #83.
- Sandbox baseline: systemd **>= 249**; test host is systemd 257 / cgroup v2. If a required property is unsupported, fail closed; do not silently drop a property.
- Per-task unit name is exactly `agentmgr-task-<12 lowercase hex>` derived from `sha256("agentmgr-task:v1:" + task_id)`. No user-controlled unit names.
- `StateDirectory=agentmgr-exec/<unit>` uses the explicit validated unit name for non-template transient units; `systemd-run -p` does not expand `%N`/`%i`.
- Credentials are `agx1` HMAC-SHA256 tokens, JCS-canonicalized, injected only via `LoadCredential`; never in argv, environment, prompts, events, audit, or logs.
- `process_gone`, `stop_confirmed`, and `never_started` are distinct facts. `stop_confirmed=true` only for a requested stop/cancel or systemd timeout with verified evidence. Natural `completed`/`failed` are not stop confirmations.
- Test host changes are temporary and reversible: `sudo -n` transient units, `/run/agentmgr-exec`, `/var/lib/private/agentmgr-exec`; no persistent users, packages, firewall/nftables changes, or writes to existing Hermes profiles. Clean up all units/dirs after tests.
- Repo/public text uses `<test-host>`, `<test-host-home>`, `<metadata-endpoint>`, `203.0.113.10`, `127.0.0.1` (test only); no real hostnames, usernames, keys, profiles, or absolute home paths.
- All live evidence is `planned / not run` until Task 8. Do not claim Hermes or any real business runtime is managed-execution verified.
- Every task ends with a commit. Run `python3 scripts/check_privacy.py --ci` before the final push.

## Review Focus

These are the failure modes the spec implies but a task-local happy-path test may miss; each is pinned by a test in the owning task.

1. **`StateDirectory` specifier expansion in a transient unit** — `systemd-run -p` does not expand `%N`/`%i`; a literal `%N` creates a shared literal directory and breaks per-task isolation. The builder must emit `agentmgr-exec/<unit>`. Task 4 asserts the exact property; Task 6's live test asserts two units have distinct `$STATE_DIRECTORY`.
2. **`PrivateNetwork=yes` plus a bind-mounted Unix socket** — if the socket cannot be reached, the fallback must still permit **no IP** and only `AF_UNIX`. Task 6 probes the allowed gateway call and denied `AF_INET`; the evidence records `private_netns` or `af_unix_only_fallback`.
3. **Natural completion misreported as a confirmed stop** — `completed`/`failed` must be `process_gone=true, stop_confirmed=false`; `not_started` must be `never_started=true`. Task 5 tests the full table; Task 6 races completion vs stop.
4. **Stale Grant provider** — a revoked/expired Grant must be denied on the next gateway call even while the unit is still stopping. Task 3 mutates the provider between calls; Task 5 stops the unit; Task 6 exercises both.
5. **Credential/run-directory ownership and cleanup** — the controller must create the socket and revocation records, the helper must write root-only credential/spec files, and the task must see only `$CREDENTIALS_DIRECTORY` plus the bound socket. Task 2/5 assert no token in argv/env/log; Task 6 checks owners/modes and source-file removal.

---

## File Structure

| File | Responsibility |
|---|---|
| `app/security/__init__.py` | Public exports only. |
| `app/security/errors.py` | Stable `SecurityError` codes and safe messages. |
| `app/security/boundary.py` | `ManagedTaskSpec`, `ResourceScope`, `GrantStatus`, `BoundaryClaim`, unit-name derivation, `RuntimeObservation`, `LaunchHandle`, `StopResult`, `CleanupResult`. |
| `app/security/credentials.py` | RFC 8785 subset canonicalization, `agx1` issue/validate, keyring, revocation stores. |
| `app/security/gateway.py` | NDJSON Unix-socket server, admission/rate limits, validation chain, `FileResourceBackend`, audit sink. |
| `app/security/systemd.py` | `systemd-run`/`systemctl` argv builders, `systemctl show` parsing, `SystemdOps` and `SudoSystemdOps`. |
| `app/security/supervisor.py` | launch/poll/stop/cleanup state machine and stop evidence. |
| `ops/security/smoke_workload.py` | Synthetic untrusted workload and probes; stdlib only. |
| `ops/security/verify_isolation.py` | In-process gateway + live systemd harness; writes evidence JSON. |
| `ops/security/verify_isolation.sh` | Environment checks and harness invocation. |
| `ops/security/agentmgr_exec.py` | Production root helper implementation (installed by ops, not run by #82 tests). |
| `ops/security/agentmgr-exec` | Thin executable wrapper for the helper. |
| `ops/security/agentmgr-exec.sudoers` | Fixed-executable sudoers template. |
| `ops/security/agentmgr-pep-gateway.service` | Production gateway unit template (#83 adapter required). |
| `ops/security/agentmgr-task@.service` | Documentation/fallback template; default is transient. |
| `ops/security/README.md` | Install, verify, rollback, limitations. |
| `app/tests/security/test_boundary.py` | Strict spec/model tests. |
| `app/tests/security/test_credentials.py` | JCS, token, revocation tests. |
| `app/tests/security/test_gateway.py` | Validation chain, backend, limits, audit tests. |
| `app/tests/security/test_systemd_unit.py` | argv and observation mapping tests. |
| `app/tests/security/test_supervisor.py` | Lifecycle/race tests with fake ops/provider. |
| `app/tests/security/test_isolation_live.py` | Env-gated real isolation tests. |
| `docs/security/managed-execution-boundary.md` | Filled after Task 8 with actual commands/results and honest capability labels. |

---

### Task 1: Boundary, Spec, and Error Model

**Files:**
- Create: `app/security/__init__.py`
- Create: `app/security/errors.py`
- Create: `app/security/boundary.py`
- Create: `app/tests/security/__init__.py`
- Test: `app/tests/security/test_boundary.py`

**Interfaces:**
- Produces: `SecurityError(code: str, detail: str | None = None)` with `.code`/`.detail`.
- Produces: `ResourceScope(scope_type: str, resource_ids: tuple[str, ...], connector_ids: tuple[str, ...], data_classifications: tuple[str, ...], exclude_resource_ids: tuple[str, ...])`; `.from_mapping(value) -> ResourceScope`, `.as_dict() -> dict`, `.is_subset_of(other: ResourceScope) -> bool`.
- Produces: `ManagedTaskSpec(task_id, grant_id, execution_mode, ttl_seconds, cpu_quota_percent, memory_max_mb, tasks_max, capabilities, resource_scope, backend)`; `.from_mapping(value) -> ManagedTaskSpec`; `.as_dict() -> dict`; `.validate(now: datetime) -> None`.
- Produces: `GrantStatus(grant_id, task_id, subject_id, execution_mode, status, usable, expires_at, revocation_reason, capabilities, resource_scope)`; `GrantStatusProvider.status(grant_id: str, *, task_id: str) -> GrantStatus | None` Protocol. `subject_id` is the server-resolved `prin_`/`svc_`/`rt_` subject used in the credential.
- Produces: `BoundaryClaim(contract_version, substrate, network_profile, product_claim, enforced, bypassable, deferred)`; `.as_dict() -> dict`.
- Produces: `unit_name_for_task(task_id: str) -> str`.
- Produces: `RuntimeObservation(...)` with `state: str`, `process_gone: bool`, `stop_confirmed: bool`, `never_started: bool`, `main_pid: int`, `result: str`, `exec_main_status: int | None`.
- Produces: `LaunchHandle(unit, task_id, grant_id, jti, expires_at, network_profile, boundary_claim, launched_at)`; `StopResult(stop_confirmed, process_gone, result, exec_main_status)`; `CleanupResult(units_remaining, state_dir_removed, credential_removed)`.

- [ ] **Step 1: Write the failing boundary tests**

Create `app/tests/security/test_boundary.py` with:
- `test_managed_task_spec_accepts_exact_fields` using a valid spec mapping and asserting normalized `resource_scope.as_dict()`.
- `test_managed_task_spec_round_trips_via_as_dict`; `ManagedTaskSpec.from_mapping(spec.as_dict()) == spec`.
- `test_managed_task_spec_rejects_unknown_fields` for `grant_expires_at`, `workspace`, `state`, and a nested unknown key.
- `test_managed_task_spec_rejects_control_chars_and_paths` for NUL, `../`, absolute workspace-like values (unknown fields must still be rejected).
- `test_ttl_and_limits_boundaries` with `ttl_seconds` 0/1/300/301, memory 31/32/1024/1025, CPU 0/1/100/101, tasks 0/1/64/65.
- `test_resource_scope_subset_rejects_expansion` and `test_resource_scope_exclude_is_subset`.
- `test_unit_name_is_deterministic_and_strict`; assert `unit_name_for_task("task_synthetic_0001") == "agentmgr-task-" + sha256("agentmgr-task:v1:task_synthetic_0001").hexdigest()[:12]`, regex `^agentmgr-task-[0-9a-f]{12}$`, and a different task changes it.
- `test_boundary_claim_contains_all_three_sections`; assert non-empty `enforced`, `bypassable`, `deferred`, and `product_claim == "synthetic_substrate_only"`.
- `test_grant_status_requires_subject_id`; construct `GrantStatus` and assert `subject_id` is present and matches `^(prin|svc|rt)_...`; missing/non-string is rejected.
- `test_errors_do_not_include_context_paths`; construct `SecurityError("invalid_spec", "bad")` and assert `str`/`repr` do not contain `/home` or a token-like string.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_boundary.py -q -o addopts='' --noconftest`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.security.boundary'`.

- [ ] **Step 3: Implement `app/security/errors.py` and `app/security/boundary.py`**

Define the dataclasses/protocols with `frozen=True, slots=True`; `from_mapping` must reject unknown keys, duplicate keys are caught by a strict JSON object hook in the caller but `from_mapping` itself must reject non-Mapping and non-exact keys. Use the exact field names above. `ManagedTaskSpec.validate` must enforce:
`execution_mode == "managed_execution"`; TTL `1..300`; CPU `1..100`; memory `32..1024`; tasks `1..64`; capabilities non-empty, unique, matching `^[a-z][a-z0-9_.-]*@\d+\.\d+\.\d+$`; backend `file`; resource scope only `self`, no connectors, classifications subset `{"internal"}`.
`BoundaryClaim` constants live in `boundary.py`; include the enforced/bypassable/deferred items from spec §15.1–§15.3, no paths.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_boundary.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/security/__init__.py app/security/errors.py app/security/boundary.py app/tests/security/__init__.py app/tests/security/test_boundary.py
git commit -m "feat(security): add managed execution boundary model"
```

---

### Task 2: Credentials, JCS, and Revocation

**Files:**
- Create: `app/security/credentials.py`
- Test: `app/tests/security/test_credentials.py`

**Interfaces:**
- Consumes: `ResourceScope`, `GrantStatus`, `SecurityError` from Task 1.
- Produces: `canonicalize_claims(value: Mapping[str, object]) -> bytes` (RFC 8785 subset; sorted keys, compact separators, UTF-8, integers only; reject floats/NaN).
- Produces: `CredentialClaims` frozen dataclass with exact fields `v, iss, aud, kid, jti, task_id, grant_id, subject_id, capabilities, resource_scope, iat, nbf, exp, nonce`; `.as_dict()`.
- Produces: `IssuedCredential(token: str, claims: CredentialClaims)`.
- Produces: `CredentialIssuer(keyring: Mapping[str, bytes], active_kid: str, clock: Callable[[], datetime])`; `.issue(*, task_id, grant_id, subject_id, capabilities, resource_scope: ResourceScope, ttl_seconds: int) -> IssuedCredential`.
- Produces: `CredentialValidator(keyring: Mapping[str, bytes], clock: Callable[[], datetime])`; `.validate(token: str, *, expected_task_id: str | None = None, expected_grant_id: str | None = None) -> CredentialClaims`; `.validate_call(claims, *, grant_status: GrantStatus, capability: str, resource: str) -> None`.
- Produces: `RevocationStore` Protocol; `InMemoryRevocationStore`; `FileRevocationStore(root: Path, clock)` with `.revoke(jti, *, reason, revoked_at)`, `.lookup(jti)`, `.prune(now)`.
- Produces: `load_keyring_from_env(env: Mapping[str, str]) -> tuple[Mapping[str, bytes], str]`; fail closed on missing/short/invalid keys.

- [ ] **Step 1: Write failing credential tests**

`app/tests/security/test_credentials.py`:
- `test_jcs_utf8_key_order_and_integers`: `{"z":1,"ä":"café","a":[1,2]}` canonicalizes to expected UTF-8 bytes; `-0` normalized to `0`; float `1.5` raises `SecurityError("invalid_claim")`.
- `test_issue_and_validate_round_trip`: token starts `agx1.`, claims match, `validate` returns same claims; `exp - iat <= 300`.
- `test_token_tamper_wrong_kid_and_short_key_fail_closed`: flip a payload byte → `token_invalid`; unknown kid → `token_invalid`; 16-byte key → `SecurityError("credential_key_invalid")`.
- `test_expiry_and_not_before`: clock after `exp` → `credential_expired`; before `nbf` → `token_not_yet_valid`.
- `test_claim_key_set_is_exact`: add/remove a claim → `token_invalid`.
- `test_validate_call_rejects_cross_task_scope_and_capability`: wrong task/grant → `cross_task_denied`; resource not in scope → `resource_scope_denied`; capability missing → `capability_not_granted`; revoked Grant → `grant_revoked`; non-managed mode → `execution_mode_mismatch`.
- `test_revocation_store_memory_and_file`: revoke/lookup/prune; file store writes `0600` under a temp root, survives a new instance, ignores `*.tmp`.
- `test_token_never_repr_or_error_leaks_secret`: `repr(claims)`/exception text do not contain the token.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_credentials.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module/names.

- [ ] **Step 3: Implement `app/security/credentials.py`**

Use `hmac.compare_digest`; base64url without padding; `jti = "cred_" + secrets.token_hex(16)`; `nonce = secrets.token_hex(16)`; token max 4096 bytes; `exp = min(grant_status.expires_at, now + ttl)` is computed by the caller, but `issue` must assert `ttl_seconds in 1..300` and `exp - iat <= 300`. `validate_call` must check exact task/grant/subject, `grant_status.status == "active"`, `usable`, `execution_mode == "managed_execution"`, unexpired, capability in both token and Grant, and resource in scope excluding `exclude_resource_ids`. `FileRevocationStore` uses atomic temp+`os.replace`+fsync, `0600`, and a per-process `threading.Lock`. No token in `__repr__`/errors.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_credentials.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/security/credentials.py app/tests/security/test_credentials.py
git commit -m "feat(security): add task-scoped credentials and revocation"
```

---

### Task 3: PEP Gateway and File Backend

**Files:**
- Create: `app/security/gateway.py`
- Test: `app/tests/security/test_gateway.py`

**Interfaces:**
- Consumes: `CredentialValidator`, `CredentialClaims`, `RevocationStore`, `GrantStatusProvider` from Tasks 1–2.
- Produces: `GatewayConfig(socket_path: Path, resources_root: Path, validator, revocation_store, grant_provider, audit_sink, limits: GatewayLimits, clock)`.
- Produces: `GatewayLimits(max_connections_per_task=8, max_connections_global=64, max_rps_per_task=16, max_rps_global=200, max_request_bytes=65536, timeout_seconds=5)`.
- Produces: `GatewayResponse(ok: bool, result: dict | None = None, reason: str | None = None)`.
- Produces: `GatewayServer(config)`; `.handle_request(request: Mapping[str, object], *, peer_uid: int | None = None) -> GatewayResponse`; `.start() -> None`; `.stop() -> None`.
- Produces: `FileResourceBackend(root: Path)`; `.read(resource_id: str, *, scope: ResourceScope, capability: str) -> dict`.
- Produces: `AuditSink` Protocol with `.record(event: Mapping[str, object]) -> None`.
- Produces: `RateLimiter` with `.allow_connection(peer_uid)`, `.release_connection(peer_uid)`, `.allow_request(peer_uid)`.

- [ ] **Step 1: Write failing gateway tests**

`app/tests/security/test_gateway.py`:
- `test_allowed_read_returns_scoped_resource`: temp resources root with `res_synthetic_0001.json`; valid token; response `ok=True`, body has only that resource.
- `test_unknown_fields_bad_json_and_oversize`: `{"extra":1}` → `bad_request`; invalid JSON → `bad_request`; >64 KiB → `payload_too_large`.
- `test_token_expired_revoked_and_wrong_audience`: expired → `credential_expired`; revoked → `credential_revoked`; wrong aud → `token_invalid`.
- `test_grant_revoked_and_mode_mismatch`: mutate live provider to revoked → `grant_revoked`; non-managed → `execution_mode_mismatch`; provider returns None → `grant_missing`.
- `test_cross_task_capability_and_scope_denied`: wrong task/grant → `cross_task_denied`; missing capability → `capability_not_granted`; out-of-scope resource → `resource_scope_denied`.
- `test_backend_rejects_traversal_symlink_and_non_regular`: `../x`, absolute, symlink, directory → `resource_denied`.
- `test_rate_limiter_busy_and_rate_limited`: fake clock; 9th connection → `busy`; 17th request in 1s → `rate_limited`; one task cannot consume global.
- `test_audit_redacts_token`: audit sink captures allow/deny; serialized event does not contain token or payload; includes `peer_uid`, `task_id`, `reason`.
- `test_audit_write_failure_denies`: audit sink raises; the request is denied (`backend_error`/`unknown`), no resource body is returned.
- `test_ndjson_socket_round_trip`: start server on a temp Unix socket; send one NDJSON request with a valid token; read one NDJSON response; server closes; no second request accepted.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_gateway.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement `app/security/gateway.py`**

Implement `handle_request` as the pure validation chain from spec §10.4: length/JSON → version → MAC/claims → nbf/exp → revocation → live Grant → task/grant/subject consistency → capability → resource scope/backend. Admission/rate limits happen in the socket handler before reading; `handle_request` may be called directly by tests with `peer_uid`. Use `socketserver.ThreadingUnixStreamServer`; set `daemon_threads=True`; one request per connection; `socket.settimeout(5)`; always close and release counters in `finally`. `FileResourceBackend` uses `os.open(O_RDONLY|O_NOFOLLOW)`, `fstat` regular file, `realpath` under root, 1 MiB max. Audit write failure → deny and `backend_error`/`unknown`; never log token.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_gateway.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/security/gateway.py app/tests/security/test_gateway.py
git commit -m "feat(security): add Unix-socket PEP gateway"
```

---

### Task 4: systemd Argv, Show Parsing, and Ops Adapter

**Files:**
- Create: `app/security/systemd.py`
- Test: `app/tests/security/test_systemd_unit.py`

**Interfaces:**
- Consumes: `ManagedTaskSpec`, `RuntimeObservation`, `SecurityError`.
- Produces: `SystemdUnitConfig(unit, spec, credential_source: Path, workload_source: Path, gateway_socket_source: Path)`.
- Produces: `build_systemd_run_argv(config) -> list[str]`; exact properties from spec §8.1, including `StateDirectory=agentmgr-exec/<unit>`, `LoadCredential=task-credential:<credential_source>`, `BindReadOnlyPaths=<gateway_socket_source>:/run/agentmgr/gateway.sock`, `BindReadOnlyPaths=<workload_source>:/workload.py`, `ExecStart=/usr/bin/python3 /workload.py`.
- Produces: `build_systemctl_show_argv(unit) -> list[str]`.
- Produces: `parse_systemctl_show(text: str) -> dict[str, str]`.
- Produces: `parse_systemd_timestamp(value: str) -> datetime | None`.
- Produces: `observation_from_show(show: Mapping[str, str], *, stop_requested_at: datetime | None = None, had_start_evidence: bool = False) -> RuntimeObservation`.
- Produces: `SystemdOps` Protocol: `launch(config, *, spec_text: str, credential_text: str)`, `stop(unit)`, `terminate(unit)`, `kill(unit)`, `show(unit) -> Mapping[str, str]`, `reset_failed(unit)`, `remove_sources(unit)`, `cleanup(unit)`.
- Produces: `SudoSystemdOps(runner: Callable[[list[str], str | None], subprocess.CompletedProcess] = run_checked)` for the approved test host; it writes `specs/`/`credentials/` with `sudo -n tee` + `sudo -n chmod 600` (token only via stdin), launches with fixed `systemd-run` argv, and cleans up only validated paths. `HelperSystemdOps(helper_path: Path)` for production; it sends the same spec/credential JSON over stdin.
- Produces: `run_checked(argv: list[str], input_text: str | None = None) -> subprocess.CompletedProcess` (no shell; timeout 15s).

- [ ] **Step 1: Write failing systemd tests**

`app/tests/security/test_systemd_unit.py`:
- `test_systemd_argv_exact_properties`: assert `DynamicUser=yes`, `StateDirectory=agentmgr-exec/<unit>`, `StateDirectoryMode=0700`, `RemainAfterExit=yes`, `CollectMode=inactive`, `RuntimeMaxSec=60s`, `ProtectSystem=strict`, `ProtectHome=yes`, `InaccessiblePaths` entries, `PrivateTmp=yes`, `PrivateDevices=yes`, `PrivateNetwork=yes`, `RestrictAddressFamilies=AF_UNIX`, `IPAddressDeny=any`, `NoNewPrivileges=yes`, `CapabilityBoundingSet=` and `AmbientCapabilities=` empty, `SystemCallFilter=@system-service`, `SystemCallArchitectures=native`, `RestrictNamespaces=yes`, `LockPersonality=yes`, `MemoryDenyWriteExecute=yes`, `RestrictRealtime=yes`, `RestrictSUIDSGID=yes`, `RemoveIPC=yes`, `ProtectClock/KernelTunables/KernelModules/KernelLogs/ControlGroups/Hostname=yes`, `ProtectProc=invisible`, `ProcSubset=pid`, `UMask=0077`, `MemoryMax=256M`, `CPUQuota=50%`, `TasksMax=32`, `LimitNOFILE=256`, `LoadCredential=...`, both `BindReadOnlyPaths`, `ExecStart`.
- `test_argv_contains_no_user_string_command_or_shell`: no `sh`, `-c`, `bash`, `Environment`, `User=`, `ExecStart` from spec; unit must match regex.
- `test_show_parser_and_state_table`: table-driven rows for activating/active/running/active-exited/deactivating/completed/timed_out/cancelled/failed/not-found/unknown; assert `process_gone`, `stop_confirmed`, `never_started`.
- `test_natural_completion_is_not_stop_confirmed`: `Result=success`, `ExecMainStatus=0`, exit timestamp before stop request → `stop_confirmed=False`.
- `test_stop_request_before_exit_is_cancelled`: stop request earlier than exit → `cancelled`, `stop_confirmed=True`.
- `test_helper_ops_sends_stdin_and_never_token_argv`: fake runner captures argv/input; token is in stdin JSON, not argv; unit/path validation rejects bad names.
- `test_sudo_ops_cleanup_only_removes_validated_unit_paths`: fake runner; `cleanup("agentmgr-task-<hex>")` uses exactly the state/source paths for that unit; a bad unit is rejected before any `rm`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_systemd_unit.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement `app/security/systemd.py`**

Build argv as a list of `--property=<key>=<value>` entries; validate unit with `^agentmgr-task-[0-9a-f]{12}$`; render integer properties only after Task 1 validation; do not use `shell=True`. `observation_from_show` must parse `ActiveState`, `SubState`, `MainPID`, `Result`, `ExecMainStatus`, `LoadState`, `ActiveEnterTimestamp`, `ExecMainStartTimestamp`, `ExecMainExitTimestamp`; treat missing/unparseable values as `unknown`/false. `SudoSystemdOps` uses `["sudo","-n","systemd-run",...]`, `["sudo","-n","systemctl","stop",unit]`, etc. `HelperSystemdOps` sends `{"command":"launch","spec":...,"credential":...}` over stdin; never put token in argv.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_systemd_unit.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/security/systemd.py app/tests/security/test_systemd_unit.py
git commit -m "feat(security): add systemd sandbox argv and observation mapping"
```

---

### Task 5: Supervisor Lifecycle

**Files:**
- Create: `app/security/supervisor.py`
- Test: `app/tests/security/test_supervisor.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces: `SupervisorConfig(workload_source, gateway_socket_source, network_profile, clock)`.
- Produces: `ManagedExecutionSupervisor(config, ops: SystemdOps, provider: GrantStatusProvider, issuer: CredentialIssuer, revocation_store: RevocationStore, audit_sink: AuditSink)`.
- Produces: `.launch(spec: ManagedTaskSpec) -> LaunchHandle`.
- Produces: `.poll(handle: LaunchHandle) -> RuntimeObservation`.
- Produces: `.stop(handle: LaunchHandle, reason: str) -> StopResult`.
- Produces: `.cleanup(handle: LaunchHandle) -> CleanupResult`.
- Produces: `.check_and_stop(handle: LaunchHandle) -> RuntimeObservation` (poll; if Grant revoked/expired, stop; return observation).

- [ ] **Step 1: Write failing supervisor tests**

`app/tests/security/test_supervisor.py` with fake `SystemdOps`, fake live provider, fake clock, in-memory revocation, and a recording audit sink:
- `test_launch_rejects_non_managed_expired_revoked_and_task_mismatch`: each raises the exact code, no `ops.launch` call.
- `test_launch_issues_credential_and_removes_sources`: credential passed to ops; after active, `remove_sources` called; audit includes unit/jti/grant but no token.
- `test_launch_start_timeout_with_unit_present_stops_and_returns_unknown`: fake ops says active never; show says active after first poll → calls stop, returns `unknown`, no false "isolated" claim.
- `test_poll_maps_all_observation_states`: table from Task 4; `completed` → process_gone true/stop_confirmed false; `not_started` → never_started true.
- `test_grant_revoke_stops_and_confirms`: provider mutated to revoked; `check_and_stop` revokes jti, calls stop, show inactive/MainPID=0, returns stop_confirmed true.
- `test_natural_completion_race_keeps_completed`: exit timestamp before stop request; stop is idempotent; result completed, stop_confirmed false.
- `test_stop_timeout_unconfirmed_does_not_cleanup`: show deactivating/MainPID 0; stop returns `unit_stop_unconfirmed`, cleanup raises, no state deletion.
- `test_cleanup_requires_process_gone_or_never_started`: process_gone false/never_started false → `unit_stop_unconfirmed`; true → reset-failed, remove sources/state, result.
- `test_token_absent_from_audit_and_exception`: no token in audit JSON or error text.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_supervisor.py -q -o addopts='' --noconftest`
Expected: FAIL with missing module.

- [ ] **Step 3: Implement `app/security/supervisor.py`**

`launch`: validate spec; live `provider.status`; verify `provider.subject_id` matches `^(prin|svc|rt)_` and use it in the credential; compute TTL `min(spec.ttl_seconds, (expires_at-now).total_seconds(), 300)`, reject <1; unit name; issue credential; call `ops.launch(config, spec_text=json.dumps(spec.as_dict()), credential_text=token)`; poll active up to 10s; on timeout inspect and stop if present; on active call `remove_sources`; return handle + claim. `stop`: revoke jti first; `ops.stop`; poll up to 10s; `ops.kill`; poll 3s; if still not process_gone → `unit_stop_unconfirmed`, no cleanup; audit. `cleanup`: only process_gone or never_started; `ops.cleanup(unit)` (reset-failed, removes state/sources); prune revocation store; return result. Never write task state machine.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_supervisor.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/security/supervisor.py app/tests/security/test_supervisor.py
git commit -m "feat(security): add managed execution supervisor"
```

---

### Task 6: Synthetic Workload, In-Process Harness, and Live Tests

**Files:**
- Create: `ops/security/smoke_workload.py`
- Create: `ops/security/verify_isolation.py`
- Create: `ops/security/verify_isolation.sh`
- Create: `app/tests/security/test_isolation_live.py`
- Test: `app/tests/security/test_isolation_live.py` (live, env-gated) and a small unit test for the harness path/limits in the same file or `test_supervisor.py`.

**Interfaces:**
- `smoke_workload.py`: reads `$CREDENTIALS_DIRECTORY/task-credential`; `gateway_call(capability, resource, payload={}) -> dict` over `/run/agentmgr/gateway.sock`; `probe_filesystem(paths)`, `probe_network(targets)`, `probe_privileges()`, `write_result(state_dir, result)`; exits 0 only if all expected denials/success match.
- `verify_isolation.py`: `HarnessConfig(repo_root, run_root, resources_root, evidence_path, gateway_limits)`; `run_harness(config) -> dict`; starts `GatewayServer` in a thread with an in-memory live provider; uses `SudoSystemdOps`; creates two units with distinct task IDs; runs the 26 probes; cleans up; writes JSON to `$AGENTMGR_ISOLATION_EVIDENCE`.
- `verify_isolation.sh`: checks `AGENTMGR_ISOLATION_TEST=1`, `AGENTMGR_ISOLATION_SUDO=1`, systemd >=249, `systemd-run`/`systemctl`, then `PYTHONPATH=<repo-root> python3 ops/security/verify_isolation.py`.

- [ ] **Step 1: Write failing harness/live tests**

`app/tests/security/test_isolation_live.py`:
- Module-level skip unless `AGENTMGR_ISOLATION_TEST=1` and `AGENTMGR_ISOLATION_SUDO=1`; reason says approved test host required.
- `test_live_filesystem_and_privilege_boundaries`: read `/etc/shadow`, `<test-host-home>/.hermes` (constructed at runtime, not committed), `/var/lib/postgresql`, other unit `$STATE_DIRECTORY`; all denied; `CapEff=0`, `NoNewPrivs=1`, `unshare` fails, `ps` only self.
- `test_live_network_is_af_unix_only`: `socket(AF_INET)` fails; connect `127.0.0.1:5432`, `203.0.113.10:443`, `<metadata-endpoint>` fail; allowed gateway call succeeds.
- `test_live_credential_expiry_revocation_scope_and_cross_task`: valid read succeeds; expired/revoked/cross-task/out-of-scope/ungranted capability denied; Grant revoked while unit running → next call `grant_revoked`, then stop verified.
- `test_live_ttl_and_stop_confirmed`: `sleep 30` with TTL 2s ends ≤8s with `Result=timeout`, `process_gone=true`, `stop_confirmed=true`; stop on revoke ends ≤5s with retained `active/exited` exit evidence or `MainPID=0`.
- `test_live_gateway_rate_limits`: 17 requests in 1s → 17th `rate_limited`; 9 idle connections → 9th `busy`; cleanup after.
- `test_live_run_directory_ownership_and_source_removal`: `/run/agentmgr-exec` owner/mode is the controller/gateway user `0700`; `gateway.sock` owner matches; after the unit is active, `credentials/<unit>.json` is absent; the unit cannot traverse `/run/agentmgr-exec`.
- `test_live_cleanup_leaves_no_units_or_dirs`: no `agentmgr-task-*`, no `/run/agentmgr-exec`, no `/var/lib/private/agentmgr-exec` after harness.

- [ ] **Step 2: Run tests to verify they fail/skip**

Run: `uv run --no-sync pytest app/tests/security/test_isolation_live.py -q -o addopts='' --noconftest`
Expected: SKIPPED (env vars absent). Then on the approved test host with both env vars, expect FAIL because the modules/scripts do not exist yet.

- [ ] **Step 3: Implement workload, harness, and shell entrypoint**

`smoke_workload.py` must be stdlib-only and must not print the token. It writes a result JSON with `probes[]` (`id`, `expected`, `observed`, `passed`, `errno`/`reason`), `process.cap_eff`, `process.no_new_privs`, `uid`, `state_dir_writable`, and `gateway_allowed`. The harness starts the gateway thread before launching units, creates two distinct tasks/credentials, deploys `workload.py` and the resource files via `sudo -n install`, and cleans up in `finally`. Use the existing `SudoSystemdOps`; no persistent helper install. Create `/run/agentmgr-exec` with `sudo -n install -d -o <controller-user> -g <controller-group> -m 0700` (test host: `agents`), and create `credentials/`/`specs/` root-only. Probe `private_netns` first with a minimal gateway connect; if it fails, record `af_unix_only_fallback`, relaunch with the fallback profile, and re-probe; both profiles must keep `AF_UNIX`/no-IP. Record the actual profile in the evidence.

- [ ] **Step 4: Run the real tests on the approved test host**

Run on the test host from the branch worktree:
```bash
AGENTMGR_ISOLATION_TEST=1 AGENTMGR_ISOLATION_SUDO=1 \
AGENTMGR_ISOLATION_EVIDENCE=/tmp/agentmgr-isolation-evidence.json \
uv run --no-sync pytest app/tests/security/test_isolation_live.py -q -o addopts='' --noconftest
```
Expected: PASS with real command/exit codes; evidence file written outside the repo. If a probe fails, fix the code, not the assertion; do not mark the test skipped to get green.
**Execution note:** the Master runs this step on the approved test host after the implementation head is pushed. The implementation agent must not claim this step if it cannot access the host; a skipped live test is not acceptance evidence.

- [ ] **Step 5: Commit**

```bash
git add ops/security/smoke_workload.py ops/security/verify_isolation.py ops/security/verify_isolation.sh app/tests/security/test_isolation_live.py
git commit -m "test(security): add live managed execution isolation harness"
```

---

### Task 7: Production Helper, Templates, and Operator README

**Files:**
- Create: `ops/security/agentmgr_exec.py`
- Create: `ops/security/agentmgr-exec` (thin executable wrapper)
- Create: `ops/security/agentmgr-exec.sudoers`
- Create: `ops/security/agentmgr-pep-gateway.service`
- Create: `ops/security/agentmgr-task@.service`
- Create: `ops/security/README.md`
- Test: `app/tests/security/test_helper.py`

**Interfaces:**
- `agentmgr_exec.py`: `validate_unit(unit) -> None`; `validate_spec_payload(payload) -> None`; `launch_from_stdin(stdin_text, *, runner=run_checked) -> dict`; `stop(unit)`, `inspect(unit)`, `cleanup(unit)`.
- `HelperSystemdOps` in `app/security/systemd.py` already sends `{"command": ...}` via stdin; the helper accepts `launch`/`stop`/`terminate`/`kill`/`inspect`/`reset-failed`/`cleanup`/`remove-sources`.
- `agentmgr-exec` wrapper: `#!/bin/sh` + `exec /usr/bin/python3 /usr/local/libexec/agentmgr_exec.py "$@"` (production install layout; repo wrapper is a template).
- sudoers template: `# <controller-user> ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-exec`; note that the helper, not sudoers, validates arguments.
- systemd templates: gateway service runs as a dedicated `<controller-user>` with `RuntimeDirectory=agentmgr-exec`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=yes`, and a `--socket /run/agentmgr-exec/gateway.sock`; task template documents the same sandbox and says transient is the default.
- README: install, ownership, test, rollback, limitations, and the #83 boundary.

- [ ] **Step 1: Write failing helper/template tests**

`app/tests/security/test_helper.py`:
- `test_validate_unit_rejects_anything_but_agentmgr_task_hex`.
- `test_launch_payload_rejects_unknown_keys_and_token_in_argv`: token only in stdin; `ExecStart`/`User`/`Environment`/`BindPaths` rejected.
- `test_launch_writes_spec_and_credential_root_only` (monkeypatch runner and temp paths; assert `0600`, `O_EXCL`, no shell).
- `test_stop_inspect_cleanup_reject_bad_units`.
- `test_sudoers_template_is_exact_fixed_executable` (read file; no `systemctl *`/`systemd-run *`).
- `test_service_templates_use_hardening_and_unix_socket` (assert key directives).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --no-sync pytest app/tests/security/test_helper.py -q -o addopts='' --noconftest`
Expected: FAIL with missing helper/templates.

- [ ] **Step 3: Implement helper/templates/README**

The helper is Python 3 stdlib; it must run with `env -i`-like clean environment, validate the unit regex, read a JSON object from stdin, and only use fixed `systemd-run` properties/`ExecStart=/usr/bin/python3 /workload.py`. It must never accept arbitrary commands. The README must state this is production template only; #82 live tests use `SudoSystemdOps` and do not install the helper on the test host.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --no-sync pytest app/tests/security/test_helper.py -q -o addopts='' --noconftest`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add ops/security/agentmgr_exec.py ops/security/agentmgr-exec ops/security/agentmgr-exec.sudoers ops/security/agentmgr-pep-gateway.service ops/security/agentmgr-task@.service ops/security/README.md app/tests/security/test_helper.py
git commit -m "docs(security): add managed execution deployment templates"
```

---

### Task 8: Real Evidence, Boundary Record, and Final Verification

**Files:**
- Create: `docs/security/managed-execution-boundary.md`
- Modify: `docs/security/threat-model.md` (status notes for TH-16/17/18/19/24 only; no weakening)
- Test: no new test file; run the full security suite and the live harness.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: an evidence record with `commit_sha`, command, exit code, systemd/kernel version, `network_profile`, unit names/UIDs, `CapEff`, probe results, cleanup result; no host/key/home path.

- [ ] **Step 1: Run all DB-free security tests**

Run: `uv run --no-sync pytest app/tests/security -q -o addopts='' --noconftest`
Expected: PASS; live tests skipped without env.

- [ ] **Step 2: Run the real harness on the approved test host and record evidence**

Run the Task 6 command; save evidence outside the repo. Record the exact command, exit code, `commit_sha`, systemd/kernel version, profile, and a short probe summary in `docs/security/managed-execution-boundary.md`. Mark only what actually passed; keep `partially_verified`/`unverified` labels for anything not covered. Explicitly state Hermes Managed Execution is not verified.

- [ ] **Step 3: Update threat-model status notes**

For TH-16/17/18/19/24, add a short note that the synthetic substrate has real negative-test evidence at the recorded SHA, while Hermes/runtime and production claims remain unverified. Do not change unrelated threat statuses or weaken any assertion.

- [ ] **Step 4: Run full checks**

Run:
```bash
python3 scripts/check_privacy.py --ci
python3 scripts/validate_kit.py --project-ready
git diff --check
uv run --no-sync pytest app/tests/security -q -o addopts='' --noconftest
```
Expected: all exit 0. On the test host, run the full app suite with the project's `flock` command and record the result.

- [ ] **Step 5: Commit**

```bash
git add docs/security/managed-execution-boundary.md docs/security/threat-model.md
git commit -m "docs(security): record managed execution isolation evidence"
```

---

## Self-Review

- **Spec coverage:** Tasks 1–5 cover the model, credentials, gateway, systemd sandbox, and supervisor; Task 6 covers workload/harness/live negative tests; Task 7 covers production helper/templates; Task 8 covers evidence, boundary record, and verification. All #82 acceptance bullets map to Task 6/8 evidence.
- **No new dependency:** stdlib only; no DB; no migrations; no governance/task/runtime changes.
- **Type consistency:** `ManagedTaskSpec`, `ResourceScope`, `GrantStatus`, `RuntimeObservation`, `LaunchHandle`, `StopResult`, `CleanupResult`, `CredentialClaims`, `GatewayResponse`, `SystemdUnitConfig` are defined in Tasks 1/2/3/4 and consumed by name in Tasks 5–8.
- **Review Focus:** each of the five risks has a task test: explicit `StateDirectory`/no specifier expansion (Task 4/6), Unix socket/no IP (Task 6), stop semantics (Task 5/6), live Grant provider (Task 3/5/6), credential/run-dir ownership and cleanup (Task 2/5/6).
- **Proportion:** each task is independently testable and reviewable; no task requires an unstated interface.
