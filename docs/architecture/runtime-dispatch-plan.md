# Managed Runtime Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production Managed Execution dispatcher that safely binds the existing task/ExecutionGrant core to any `RuntimeAdapter`, verified with a scripted Fake Adapter; no Hermes, no public API, no sandbox work.

**Architecture:** A one-task-at-a-time `RuntimeDispatcher` claims only managed tasks, projects the active server-side Grant into `RuntimeGrant`, calls an injected adapter, and writes only through existing `worker`/`task_service` primitives. Submit ambiguity is reconciled before any retry; completed runs stop at `verifying`; cancel/stop evidence and lease/fencing stay distinct.

**Tech Stack:** Python 3.13, SQLAlchemy/PostgreSQL, pytest, existing `app.runtime.contract` Protocol types, existing `app.tasks.service`/`app.tasks.worker`.

**Spec:** `docs/architecture/runtime-dispatch.md`

## Global Constraints

- Only `managed_execution` tasks are claimable/executable by this dispatcher. No advisory/governed fallback.
- Fake Adapter evidence is test-only and never upgrades real runtime capability labels.
- No new dependency, DB migration, public MCP/REST route, `app/api/**`, `app/security/**`, or `ops/security/**` change.
- No Hermes integration, model egress, browser/terminal governance, or task-scoped model credentials.
- Use existing lease/fencing for every managed state write; never write execution facts without the current worker/fencing pair.
- Never let runtime self-report directly produce `succeeded`; `completed` only reaches `verifying`.
- No token, API key, host path, raw payload, or private runtime URL in logs/audit/errors/test evidence.
- Use the shared test-host venv with `uv run --no-sync`; no worktree `.venv`.
- TDD: every task starts with failing tests, then minimal implementation, then a focused test run and commit.

## Review Focus

- A managed task is handed an adapter whose `task.submit`/`task.status` is not `verified`; it must not submit or pretend support.
- The adapter accepts a run but the core cannot bind `rtask_*`; it must not become retryable or create a second run.
- The runtime reports `completed` with no independent `result_*`/`evidence:*`; it must never become `succeeded`.
- A cancel is requested but the runtime only returns `requested`; the task must not become `cancelled`.
- A worker lease expires while the runtime may still be running; recovery must not replay or resume execution.

## File Structure

- Create `app/runtime/projection.py` — structural `ExecutionGrant` → `RuntimeGrant` projection; no task-service writes.
- Create `app/tasks/execution.py` — dispatcher, config/result, recovery checker, CLI.
- Modify `app/tasks/worker.py` — optional `execution_mode` claim filter.
- Create `app/tests/runtime/test_projection.py`.
- Create `app/tests/tasks/fake_runtime.py` — scripted Fake Adapter.
- Create `app/tests/tasks/test_execution.py`.
- Modify `app/tests/tasks/helpers.py` — `submit_managed` fixture.
- Create `docs/operations/runtime-dispatch.md`.
- No changes to `app/api/**`, `app/security/**`, migrations, dependencies.

---

### Task 1: RuntimeGrant Projection

**Files:**
- Create: `app/runtime/projection.py`
- Create: `app/tests/runtime/test_projection.py`

**Interfaces:**
- Produces: `project_runtime_grant(task, grant, *, now) -> RuntimeGrant`
- Produces: `GrantProjectionError(reason_code, detail)`
- Consumes: `app.runtime.contract.RuntimeGrant`; structural attributes only (no import of `app.tasks.models`).

- [ ] **Step 1: Write failing projection tests**

Add tests using small structural stubs:

```python
def test_projection_accepts_active_managed_grant():
    task, grant = _task_and_grant()
    projected = project_runtime_grant(task, grant, now=NOW)
    assert projected.grant_id == grant.grant_id
    assert projected.execution_mode == "managed_execution"
    assert projected.capabilities == ("expense.read@1.0.0",)

def test_projection_rejects_revoked_grant():
    ...
    with pytest.raises(GrantProjectionError) as exc:
        project_runtime_grant(...)
    assert exc.value.reason_code == "grant_not_active"
```

Cover: `grant_missing`, `grant_not_active`, `grant_expired`, `grant_task_mismatch`, `grant_revision_mismatch`, `mode_mismatch`, empty capabilities, non-mapping scope.

- [ ] **Step 2: Run to verify RED**

Run: `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/runtime/test_projection.py -q -o addopts='' --noconftest`
Expected: FAIL with `ModuleNotFoundError` or missing `project_runtime_grant`.

- [ ] **Step 3: Implement projection**

Create `GrantProjectionError` with `reason_code` and a short non-sensitive detail. Implement `project_runtime_grant` using attribute reads only; verify task/grant binding, active/usable/unexpired, managed mode, positive revision, non-empty capabilities, mapping scope; return a frozen `RuntimeGrant`. Do not log or include secret values.

- [ ] **Step 4: Run to verify GREEN**

Run: the same command. Expected: all projection tests pass.

- [ ] **Step 5: Commit**

```bash
git add app/runtime/projection.py app/tests/runtime/test_projection.py
git commit -m "feat(runtime): add managed execution grant projection"
```

---

### Task 2: Managed-Only Claim Filter

**Files:**
- Modify: `app/tasks/worker.py` (claim query)
- Modify: `app/tests/tasks/helpers.py` (add `submit_managed`)
- Modify: `app/tests/tasks/test_worker.py`

**Interfaces:**
- Produces: `claim_task(..., execution_mode: str | None = None) -> LeaseClaim | None`
- Produces test helper `submit_managed(session, world, *, key="idem_task_managed_0001", **overrides) -> Task`
- `execution_mode=None` preserves today's behavior.

- [ ] **Step 1: Write failing tests**

Add to `test_worker.py`:

```python
def test_claim_filter_only_returns_managed_tasks(session, world):
    submit_read(session, world, key="idem_advisory_filter_0001")
    managed = submit_managed(session, world, key="idem_managed_filter_0001")
    session.commit()

    claimed = worker.claim_task(
        session, worker_id="worker-managed", actor=world.actors.executor,
        execution_mode="managed_execution",
    )
    assert claimed is not None
    assert claimed.task.task_id == managed.task_id

def test_claim_filter_none_keeps_existing_behavior(session, world):
    advisory = submit_read(session, world, key="idem_advisory_none_0001")
    session.commit()
    claimed = worker.claim_task(session, worker_id="worker-any", actor=world.actors.executor)
    assert claimed is not None and claimed.task.task_id == advisory.task_id
```

Add `submit_managed` to helpers using `submit_task(..., execution_mode="managed_execution", runtime_capabilities_verified=True)` with the same goal/scope/capability defaults.

- [ ] **Step 2: Run RED**

Run: `... pytest app/tests/tasks/test_worker.py -q -o addopts=''`
Expected: the filter test fails because `execution_mode` is not accepted/ignored.

- [ ] **Step 3: Implement**

Add an optional `execution_mode` parameter to `claim_task`; add `Task.execution_mode == execution_mode` to the candidate query only when non-`None`; leave all lease/fencing logic unchanged.

- [ ] **Step 4: Run GREEN**

Run: the same command; then `pytest app/tests/tasks -q -o addopts=''`.

- [ ] **Step 5: Commit**

```bash
git add app/tasks/worker.py app/tests/tasks/helpers.py app/tests/tasks/test_worker.py
git commit -m "feat(tasks): filter worker claims by execution mode"
```

---

### Task 3: Dispatcher Skeleton and Preflight

**Files:**
- Create: `app/tasks/execution.py`
- Create: `app/tests/tasks/fake_runtime.py`
- Create: `app/tests/tasks/test_execution.py`

**Interfaces:**
- Produces: `DispatcherConfig(worker_id, lease_seconds=300, poll_interval=0.5, health_backoff_seconds=5.0)`
- Produces: `DispatcherResult(outcome, task_id=None, runtime_task_id=None, detail=None)`
- Produces: `RuntimeDispatcher(adapter, config, *, service_principal, session)`
- Produces: `run_once(now=None) -> DispatcherResult` (this task stops after preflight/acceptance; Tasks 4–6 extend)
- Produces: `run_forever(stop_event, *, session_factory)` (full loop behavior completed in Task 7/8)
- Produces: `FakeRuntimeAdapter` test helper with recorded calls and scripted outcomes.
- `run_once` commits success/domain rejection on the injected session; unexpected exceptions roll back. The caller owns session close; `run_forever` creates a fresh session per iteration.

- [ ] **Step 1: Write failing preflight tests**

```python
def test_run_once_idle_without_managed_task(session, world):
    dispatcher = _dispatcher(session, world)
    assert dispatcher.run_once().outcome == "idle"

def test_run_once_escalates_unverified_capability(session, world):
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(capabilities_verified=False)
    result = _dispatcher(session, world, adapter=adapter).run_once()
    assert result.outcome == "unsupported"
    assert task.state == "needs_human"
    assert adapter.submit_calls == []

def test_unhealthy_runtime_releases_lease_and_keeps_queued(session, world):
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(health=HealthStatus.UNAVAILABLE)
    result = _dispatcher(session, world, adapter=adapter).run_once()
    assert result.outcome == "adapter_unavailable"
    assert task.state == "queued"
    assert adapter.submit_calls == []
```

Also test: invalid/revoked Grant does not call adapter and closes/escalates safely; service principal must be `service`/`runtime` and active.

- [ ] **Step 2: Run RED**

Run: `... pytest app/tests/tasks/test_execution.py -q -o addopts=''`
Expected: missing execution module/classes.

- [ ] **Step 3: Implement skeleton**

Create the dataclasses and dispatcher constructor. `run_once`:
1. validate service principal;
2. claim with `execution_mode="managed_execution"`;
3. return `idle` if none;
4. load task/grant; project Grant;
5. check adapter capabilities (`task.submit`/`task.status` verified) and health;
6. on invalid grant/capability/health, call the existing task-service path and release lease as specified;
7. return a `DispatcherResult`; do not submit yet if the path is not implemented in this task.

Create `FakeRuntimeAdapter` implementing every `RuntimeAdapter` method; all methods return scripted values; record `submit_calls` as `(task_id, key, grant_id, prompt)`.

- [ ] **Step 4: Run GREEN**

Run: `... pytest app/tests/tasks/test_execution.py -q -o addopts=''` and `... pytest app/tests/tasks -q -o addopts=''`.

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py app/tests/tasks/fake_runtime.py app/tests/tasks/test_execution.py
git commit -m "feat(tasks): add managed dispatcher preflight"
```

---

### Task 4: Submit and Bind

**Files:**
- Modify: `app/tasks/execution.py`
- Modify: `app/tests/tasks/test_execution.py`

**Interfaces:**
- Produces: `build_runtime_idempotency_key(task, grant, attempt_number) -> str` (`idem_...`, stable per attempt/grant).
- Produces: `build_runtime_prompt(task) -> str` (canonical UTF-8 JSON from goal/payload; bounded; no credentials).
- `run_once` after accepted calls `task_service.start_attempt(... runtime_ref=rtask, idempotency_key=key, worker/fencing)`.
- Pre-submit decision binding: load the persisted `GovernanceDecisionRecord` by `task.decision_id`; require `record.task_id == task.task_id`, `grant.decision_id == record.decision_id`, `grant.decision_hash == record.decision_hash`, and `grant.task_revision == record.task_revision`. A mismatch must not call the adapter and must record a safe queued dispatch failure.

- [ ] **Step 1: Write failing tests**

```python
def test_accepted_submit_binds_runtime_handle(session, world):
    task = submit_managed(session, world)
    session.commit()
    adapter = FakeRuntimeAdapter(submit=SubmitResult(outcome=SubmitOutcome.ACCEPTED, runtime_task_id="rtask_fake_0001"))
    result = _dispatcher(session, world, adapter=adapter).run_once()
    assert result.outcome == "accepted"
    assert task.state == "dispatched"
    assert task.runtime_task_id == "rtask_fake_0001"
    assert task.attempt == 1
    assert len(adapter.submit_calls) == 1

def test_runtime_key_is_stable_per_attempt_and_changes_with_grant():
    assert build_runtime_idempotency_key(task, grant, 1) == build_runtime_idempotency_key(task, grant, 1)
    assert build_runtime_idempotency_key(task, grant, 1) != build_runtime_idempotency_key(task, grant, 2)

def test_grant_decision_binding_mismatch_never_submits(session, world):
    # mutate/inject a grant whose decision_id/hash/revision is not the task's
    # persisted decision; assert adapter.submit_calls == [] and safe failure.
    ...

def test_binding_failure_cancels_and_enters_unknown(session, world):
    # accepted, but force start_attempt failure (e.g. stale fencing)
    ...
    assert task.state == "unknown"
    assert adapter.cancel_calls
    assert len(adapter.submit_calls) == 1
```

Also test prompt is canonical, bounded, and does not contain an injected credential key.

- [ ] **Step 2: Run RED; expected missing key/prompt/binding behavior.**

- [ ] **Step 3: Implement submit path**

Before building the key/prompt, load and verify the persisted `GovernanceDecisionRecord` binding described in the Interfaces block; on mismatch, record a safe queued failure and return without touching the adapter. Then build the key and prompt. Call `adapter.submit_task`. On accepted, call `start_attempt` with the current lease/fencing and `runtime_ref`; never pass `execution_ref` for managed. On binding failure, best-effort `adapter.cancel_task`, then `record_dispatch_failure(outcome="unknown")`; no retry.

- [ ] **Step 4: Run GREEN; run focused tests and `app/tests/tasks`.**

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py app/tests/tasks/test_execution.py
git commit -m "feat(tasks): submit managed tasks and bind runtime handles"
```

---

### Task 5: Submit Failure and Reconciliation

**Files:**
- Modify: `app/tasks/execution.py`
- Modify: `app/tests/tasks/test_execution.py`

**Interfaces:**
- Uses: `adapter.find_by_idempotency_key(task_id, grant_id, key)` and `adapter.submit_task`.
- Uses: `task_service.record_dispatch_failure`.

- [ ] **Step 1: Write failing matrix tests**

Add:
- `test_rejected_submit_records_failed_without_retry`
- `test_unsupported_submit_enters_needs_human`
- `test_timeout_lookup_found_adopts_existing_run`
- `test_timeout_lookup_not_found_enters_unknown_without_second_submit`
- `test_timeout_lookup_unknown_enters_unknown`
- `test_conflict_never_creates_second_run`

Assertions must include `len(adapter.submit_calls) == 1` for every ambiguous/conflict path, `task.attempt` unchanged where appropriate, and grant status invalidated for unknown.

- [ ] **Step 2: Run RED.**

- [ ] **Step 3: Implement mapping**

Map `SubmitOutcome` exactly per spec §4.2. On timeout/unknown/conflict, lookup first; adopt only a found handle; if no proof, record `timeout`/`unknown` and never resubmit. Release the lease through the existing worker helper.

- [ ] **Step 4: Run GREEN; full task suite.**

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py app/tests/tasks/test_execution.py
git commit -m "feat(tasks): reconcile ambiguous managed submissions"
```

---

### Task 6: Status, Result, and Verification Boundary

**Files:**
- Modify: `app/tasks/execution.py`
- Modify: `app/tests/tasks/test_execution.py`

**Interfaces:**
- Uses: `adapter.get_status(...)`, `adapter.get_result(...)`, `task_service.mark_running`, `record_attempt_result`.
- Produces: `run_once` drives an accepted handle to a terminal/verifying result (or unknown/needs_human).

- [ ] **Step 1: Write failing mapping tests**

```python
def test_running_marks_running(session, world):
    # fake status sequence: RUNNING, then COMPLETED with result
    ...
    assert task.state == "verifying"
    assert "running" in [e.event_type for e in task_events]
    assert task.state != "succeeded"

def test_completed_without_result_evidence_is_unknown(session, world):
    adapter = FakeRuntimeAdapter(result=RuntimeResult(
        outcome=ResultOutcome.SUCCEEDED, result_ref=None, evidence_refs=(),
        summary="done", structured_payload=None, artifacts=(),
        verification_hints=(), stop_confirmed=False, completed_at=NOW,
    ))
    task = submit_managed(session, world)
    session.commit()
    assert _dispatcher(session, world, adapter=adapter).run_once().outcome == "unknown"
    assert task.state == "unknown"

def test_dispatcher_never_completes_success_directly(session, world):
    # A valid result may reach verifying, but no dispatcher path calls complete_task.
    ...
    assert task.state == "verifying"
    assert task.state != "succeeded"
```

Add a separate verification test: submitting evidence with `source="runtime"`/`"agent"` to `complete_task` raises `task_verification_self_report`. The dispatcher itself never calls `complete_task`.

Add failed/timed_out require `stop_confirmed=true`; stopping only from `cancel_requested`; not_found/unknown -> lost/revoked; no auto-success.

- [ ] **Step 2: Run RED.**

- [ ] **Step 3: Implement status mapping**

Use only contract enums and existing task service. `running` -> `mark_running` with a safe `evidence:runtime_start_<hash>`; `completed` -> `get_result`; only if valid `result_*`/`evidence:*` -> `record_attempt_result(completed)` -> verifying; otherwise fail closed. Do not call `complete_task`; no verifier in the dispatcher.

- [ ] **Step 4: Run GREEN; full task suite.**

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py app/tests/tasks/test_execution.py
git commit -m "feat(tasks): map managed runtime status to task facts"
```

---

### Task 7: Cancel and Lease/Recovery

**Files:**
- Modify: `app/tasks/execution.py`
- Modify: `app/tests/tasks/test_execution.py`

**Interfaces:**
- Produces: `RuntimeRecoveryChecker(adapter)` implementing `RecoveryChecker.__call__`.
- `run_forever` calls `worker.recover_expired_leases(checker=...)`; cancel requests use `adapter.cancel_task` and `task_service.confirm_stop`.

- [ ] **Step 1: Write failing tests**

Add:
- `test_cancel_requested_waits_for_requested_then_confirms_stop`
- `test_cancel_requested_only_is_not_cancelled`
- `test_cancel_race_completed_result_wins`
- `test_recovery_checker_found_and_unknown`
- `test_lease_expiry_does_not_replay_running_runtime`
- `test_unknown_with_runtime_handle_best_effort_cancel`

Assertions: `stop_confirmed=false` for `requested`; only `STOPPED`/`TIMED_OUT` with confirmation writes terminal; completed result survives a cancel; recovery never resubmits; unknown task with handle gets cancel attempt.

- [ ] **Step 2: Run RED.**

- [ ] **Step 3: Implement cancel/recovery**

Handle `cancel_requested` before polling; call adapter cancel; `requested` keeps state; `already_terminal` maps to result; `unsupported`/`unknown` do not become cancelled. Implement `RuntimeRecoveryChecker` with lookup/status; return `not_accepted` only on an adapter `not_found` with the contract guarantee, never on missing local state.

- [ ] **Step 4: Run GREEN; full task suite.**

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py app/tests/tasks/test_execution.py
git commit -m "feat(tasks): handle managed cancel and lease recovery"
```

---

### Task 8: CLI, Operations Doc, Full Verification

**Files:**
- Modify: `app/tasks/execution.py` (CLI)
- Create: `docs/operations/runtime-dispatch.md`
- Modify: `app/tests/tasks/test_execution.py` (CLI tests)
- Update: `docs/architecture/runtime-dispatch.md` only if the implemented interface differs from the spec.

**Interfaces:**
- Produces: `main(argv=None, *, dispatcher_factory=None) -> int`
- `--once`, `--loop`, `--adapter <name>`; unknown/missing adapter exits nonzero with a stable message; no default production adapter. `dispatcher_factory` is a trusted launcher/test injection, never a task/client value. Without a trusted factory/config, `main` returns nonzero and mentions `#97 production dispatch configuration not supplied`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_cli_without_trusted_configuration_fails_closed(capsys):
    assert main(["--once", "--adapter", "fake"]) != 0
    assert "#97" in capsys.readouterr().err.lower()

def test_cli_rejects_unknown_adapter(capsys):
    factory = lambda name: None
    assert main(["--once", "--adapter", "unknown"], dispatcher_factory=factory) != 0
    assert "unknown adapter" in capsys.readouterr().err.lower()

def test_cli_once_uses_injected_dispatcher():
    # dispatcher_factory returns a fake dispatcher; run_once called once
    assert main(["--once", "--adapter", "fake"], dispatcher_factory=factory) == 0
```

Add `test_run_forever_stops_on_event` if not already covered.

- [ ] **Step 2: Run RED.**

- [ ] **Step 3: Implement CLI and doc**

Implement a fixed adapter registry; no arbitrary imports; `--adapter` names only; tests inject a factory. Document:
- Fake adapter only test;
- no production adapter until #97;
- command/flags, lease/lifecycle, failure behavior;
- explicit `#96 does not verify Hermes or real execution`;
- capacity: one task per worker; multiple workers process different tasks;
- no public managed API; trusted intake only.

- [ ] **Step 4: Run all verification**

Run:
- `UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest app/tests/tasks/test_execution.py -q -o addopts=''`
- `... pytest app/tests/runtime/test_projection.py app/tests/tasks -q -o addopts=''`
- `... pytest app/tests -q -o addopts=''`
- `python3 -m py_compile app/tasks/execution.py app/runtime/projection.py`
- `python3 scripts/check_privacy.py --ci`
- `git diff --check`

Expected: all pass, no warnings/noise; full suite count recorded.

- [ ] **Step 5: Commit**

```bash
git add app/tasks/execution.py docs/operations/runtime-dispatch.md app/tests/tasks/test_execution.py
git commit -m "feat(tasks): add managed dispatch CLI and operations guide"
```

---

## Plan Self-Review

- **Spec coverage:** projection (Task 1), managed claim fence (Task 2), preflight/health/capability (Task 3), submit/bind (Task 4), ambiguity/reconciliation (Task 5), status/result/no auto-success (Task 6), cancel/recovery (Task 7), CLI/docs/verification (Task 8).
- **Type consistency:** `project_runtime_grant(task, grant, *, now)`, `DispatcherConfig`, `DispatcherResult`, `RuntimeDispatcher`, `RuntimeRecoveryChecker`, `build_runtime_idempotency_key`, `build_runtime_prompt`, `main` are fixed above and reused below.
- **Review Focus pinning:** the five failure modes map to tests in Tasks 3–7.
- **Proportion:** no production code bodies are transcribed; test names/assertions and exact interfaces are specified.

## Execution Handoff

The spec and this plan are design artifacts only. Do not start implementation until the owner reviews the plan and chooses an execution method. The expected recommendation is subagent-driven, because the tasks share strict core interfaces and a single wrong managed-state write is expensive.
