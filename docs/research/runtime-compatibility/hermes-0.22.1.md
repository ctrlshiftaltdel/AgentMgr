# Hermes 0.22.1 runtime compatibility spike

- Issue: #81 (Epic #74)
- Runtime: Hermes `0.22.1+72.g450ad50` (2026.9.24)
- Environment: approved isolated **test host** (`<test-host>`)
- API surface: Hermes API server `/health` and `/v1/*`
- Implementation under test: `app/runtime/hermes.py`
- Adapter contract: [Runtime Adapter 1.0.0](../../contracts/runtime-adapter.md)
- Integration guide: [Hermes adapter](../../integrations/runtimes/hermes.md)
- Mock contract tests: `app/tests/runtime/test_hermes_adapter.py`
- Env-gated real test: `app/tests/runtime/test_hermes_integration.py`

> **Evidence scope.** This document records the real-runtime observations that
> support the `partially_verified` labels for the adapter. A committed
> real-runtime contract suite run is now recorded in §3.6 (command, exit code,
> runtime version, commit SHA). It covers health/auth and the positive advisory
> lifecycle only; redirects, 408/504, malformed 2xx, keepalive-only streams and
> cross-task rejection remain mock-only, so no capability is upgraded from
> `partially_verified` to `verified`. It does **not** claim OS isolation,
> task-scoped credentials, per-task tool permissions, managed-execution
> readiness, or a stable business result schema. Mock tests are labelled
> separately and are not runtime compatibility evidence.
>
> **Placeholders.** Host, port, run ids and the API key are redacted as
> `<test-host>`, `<api-port>`, `<run-id>` and `<api-key>`. The temporary test
> key and API-server configuration were removed after the run; the original
> gateway/`.env` state was restored. No credential, hostname, IP address or
> profile path is committed.

## 1. Environment and setup

The spike used the test host's default profile **temporarily** with a dedicated
test API key. The API server required `API_SERVER_KEY` to be at least 16
characters. After the observations below, the temporary API server/key were
removed and the original gateway/`.env` configuration was restored. No
production or personal profile was touched.

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'   # >= 16 characters, test-only
```

The API key was passed only as a bearer header. It is not reproduced here and
was never written to the repository, a prompt, an event payload or a log.

## 2. Observed API surface

| Operation | Endpoint | Observed result |
|---|---|---|
| Liveness | `GET /health` | 200, no auth required |
| Capabilities | `GET /v1/capabilities` | 200 with Bearer; `runtime.mode=server_agent`, `tool_execution=server`, `split_runtime=false` |
| Submit | `POST /v1/runs` | 202 `{run_id,status:"started",replayed:false}` |
| Status | `GET /v1/runs/{id}` | `output`, `usage`, `runtime`, `session_id`, `pending_controls`, `admission_id`, `execution_generation` |
| Events | `GET /v1/runs/{id}/events` | `text/event-stream` with `id:` sequence, keepalive comments, 5-minute retention |
| Stop | `POST /v1/runs/{id}/stop` | 200 `{run_id,status:"stopping"}` |
| Approval | `POST /v1/runs/{id}/approval` | endpoint exists; not E2E-triggered in this spike |
| Unknown resolution | `POST /v1/runs/{id}/resolve-unknown` | endpoint exists; not E2E-triggered in this spike |

Authentication is `Authorization: Bearer <api-key>`. The adapter does not read a
profile, session database or profile directory.

## 3. Commands and recorded results

### 3.1 Health and capabilities

```bash
curl -sS "http://<test-host>:<api-port>/health"
# {"status":"ok"}

curl -sS -H "Authorization: Bearer <api-key>" \
  "http://<test-host>:<api-port>/v1/capabilities"
# {"runtime":{"name":"hermes","version":"0.22.1+72.g450ad50"},
#  "runtime_mode":"server_agent","tool_execution":"server",
#  "split_runtime":false, ...}
```

The capability payload also listed the configured toolset, including terminal
access. It did not provide per-run tool permissions or credential injection.

### 3.2 Real short run

```bash
curl -sS -X POST \
  -H "Authorization: Bearer <api-key>" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: <idempotency-key>" \
  -d '{"input":"Reply with exactly OK and nothing else.","session_id":"<task-id>"}' \
  "http://<test-host>:<api-port>/v1/runs"
# 202 {"run_id":"<run-id>","status":"started","replayed":false}

curl -sS -H "Authorization: Bearer <api-key>" \
  "http://<test-host>:<api-port>/v1/runs/<run-id>"
# queued -> running -> completed, output "OK"
```

The adapter's real integration test asserts `completed`, a non-empty text
output containing `OK`, `structured_result=false`, and `partial_success` (no
independent evidence refs).

### 3.3 Idempotency replay

Repeating the **same** POST with the **same** `Idempotency-Key` and payload
returned the original `run_id`:

```json
{"run_id": "<run-id>", "status": "started", "replayed": true}
```

A different payload with the same key returned HTTP 409. This is the behavior the
adapter relies on when a submit response is lost: it retries the same key/body
and never generates a new key. Hermes has no separate
`find_by_idempotency_key` endpoint, so the adapter keeps a **process-local**
submission map and implements `find_by_idempotency_key` by replaying the exact
stored POST (same key, same body). The map is lost on restart; `task.lookup`
therefore stays `unverified` (deviation from contract §3.2).

### 3.4 Stop

```bash
curl -sS -X POST -H "Authorization: Bearer <api-key>" \
  "http://<test-host>:<api-port>/v1/runs/<run-id>/stop"
# 200 {"run_id":"<run-id>","status":"stopping"}

curl -sS -H "Authorization: Bearer <api-key>" \
  "http://<test-host>:<api-port>/v1/runs/<run-id>"
# {"status":"cancelled", ...}
```

Stopping is asynchronous. `stopping` is not a stop; only the later `cancelled`
status supports `stop_confirmed=true`. The adapter polls until terminal or the
bounded poll deadline and otherwise returns `requested` with
`stop_confirmed=false`.

### 3.5 SSE events

Observed frames:

```text
: keepalive

id: 1
event: message.delta
data: {"type":"message.delta","text":"O"}

id: 2
event: run.completed
data: {"type":"run.completed","output":"OK","usage":{"input_tokens":3,...}}
```

`id:` is a monotonically increasing sequence usable as a cursor. Keepalive
comments are not events. The runtime retains event buffers for about five
minutes; reconnecting after expiry can lose events, so status/result calls must
cross-check. The adapter filters client-side by sequence id and redacts obvious
credential fields before returning payloads.

### 3.6 Committed real integration suite run

The committed env-gated suite was executed on the approved isolated test host
against the Hermes API server in advisory mode. The exact run is recorded here
as real-runtime evidence:

```bash
HERMES_API_URL='http://<test-host>:<api-port>' \
HERMES_API_KEY='<api-key>' \
HERMES_INTEGRATION_STOP=1 \
uv run pytest app/tests/runtime/test_hermes_integration.py -q -o addopts='' --noconftest
# 4 passed in 36.44s
```

- Exit code: `0`
- Result: `4 passed in 36.44s`
- Runtime: Hermes Agent `v0.22.1+72.g450ad50` (2026.9.24), API server
  `/v1/runs`
- Commit under test: `d46c2431aa798e550d1fd00e820b73c7ba78b980`
- Suite: `app/tests/runtime/test_hermes_integration.py`

Scenarios actually exercised by the real suite:

1. `test_health_against_real_runtime` — `health()` returns `healthy` with both
   `api_ok` and `auth_ok` true (`/health` plus authenticated
   `/v1/capabilities`).
2. `test_managed_execution_is_always_unsupported` — a `managed_execution` grant
   is refused with `SubmitOutcome.UNSUPPORTED` and no run id; nothing is sent.
3. `test_short_run_completes_and_replays` — an advisory submit returns an
   accepted `run_id` with `replayed=false`; the same idempotency key/payload
   replays to the same `run_id` with `replayed=true`; the run reaches
   `completed`; the result is `partial_success` with non-empty text containing
   `OK` and `structured_result=false`.
4. `test_stop_is_requested_then_confirmed` — stop is requested (`stopping`) and
   the run reaches `cancelled`; a request is never treated as a confirmed stop.

The run used a temporary test API key and API-server configuration; both were
removed afterwards and the original gateway/`.env` state was restored. The key
and host are not committed.

Why the labels stay `partially_verified`: the real suite covers only the
positive lifecycle and health/auth paths. Redirect refusal, 408/504 handling,
malformed-2xx mapping, keepalive-only streams and cross-task binding rejection
are covered by the committed mock contract suite, not by the real runtime. The
supported capabilities therefore remain `partially_verified`; no label is
`verified`.

## 4. Capability summary

| Capability | Label | Evidence / limitation |
|---|---|---|
| `task.submit` | `partially_verified` | 202 + opaque `run_id`; real suite covers submit + replay. 409 conflict is mock-only. |
| `task.status` | `partially_verified` | queued/running/completed and stopping/cancelled observed; real suite polls to `completed` and `cancelled`. |
| `task.events.poll` | `partially_verified` | SSE `id:`, keepalive, `message.delta`, `run.completed` observed in the manual spike; client cursor implemented and mock-tested, not exercised by the real suite. Last-Event-ID support not confirmed. |
| `task.cancel` | `partially_verified` | Requested (`stopping`) vs confirmed (`cancelled`); real suite covers stop → `cancelled`. |
| `task.result` | `partially_verified` | Text output + usage + runtime; real suite asserts `partial_success` with `structured_result=false`. No evidence refs. |
| `runtime.health` | `partially_verified` | `/health` + authenticated `/v1/capabilities`; real suite asserts `healthy` with `api_ok`/`auth_ok`. |
| `task.approval.resolve` | `unverified` | Endpoint exists; request/response not E2E-triggered. |
| `task.unknown.resolve` | `unverified` | Endpoint exists; request/response not E2E-triggered. |
| `runtime.isolation` | `unverified` | Logical session association only; no OS boundary observed. |
| `task.lookup` | `unverified` | No independent lookup endpoint; idempotent POST replay only, process-local. |
| `task.events.subscribe` | `unverified` | No durable subscription asserted. |
| `task.input.provide` | `unverified` | No generic input contract; unsupported. |
| `runtime.task_scoped_credentials` | `unverified` | No per-run credential injection. |
| `runtime.tool_permissions` | `unverified` | Full configured toolset, including terminal; Hermes-internal calls bypass the AgentMgr PEP. |
| `runtime.managed_execution` | `unverified` | Host toolset, no task credentials, no OS isolation. |
| `runtime.structured_events` | `partially_verified` | SSE framing stable; payload not a business schema. |

## 5. Status and error observations

Observed Hermes statuses: `queued`, `running`, `waiting_for_approval`, `stopping`,
`completed`, `failed`, `cancelled`, `interrupted`, `unknown`.

Observed error behavior:

| Condition | Observed status |
|---|---|
| Missing/wrong bearer key | 401 (and 403 for insufficient access) |
| Unknown run | 404 |
| Idempotency conflict / execution conflict | 409 |
| Max concurrent runs | 429 |
| Drain or storage failure | 503 |

The adapter maps these to `RuntimeAuthError`, `RuntimeNotFound`,
`RuntimeConflict`, `RuntimeRateLimited` and `RuntimeUnavailable`. A 404 on status
becomes `RuntimeState.NOT_FOUND`; the core treats it as unknown and starts
reconciliation.

## 6. What this does not prove

- The committed real suite covers only the positive lifecycle and health/auth
  paths. The negative paths (redirects, 408/504, malformed 2xx, keepalive-only,
  cross-task binding) remain mock-only, so no capability is `verified`.
- No OS-level UID, filesystem, process or network-egress isolation was observed.
- No per-task tool allowlist or credential injection exists in the observed API;
  Hermes-internal tool calls do not pass through the AgentMgr PEP.
- Approval and unknown-resolution endpoints were not exercised end-to-end.
- No business result schema or independent evidence artifact was returned; the
  result is free text plus usage, so completion is reported as
  `partial_success`, not `succeeded`.
- Last-Event-ID support was not confirmed; reconnect relies on client-side
  cursor filtering and can miss events after the 5-minute buffer expires.
- No lookup-by-idempotency-key endpoint exists; the process-local map is lost on
  restart.
- No long-run crash/restart, event-retention boundary, or multi-worker
  concurrency test was performed.
- Mock tests prove the adapter mapping, not runtime compatibility.

These limitations are why `runtime.managed_execution`,
`runtime.task_scoped_credentials`, `runtime.tool_permissions` and
`runtime.isolation` remain `unverified`.

## 7. Reproduction

Against the approved isolated test host only:

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'    # test-only, >= 16 characters
export HERMES_INTEGRATION_STOP=1     # include the stop scenario
uv run pytest app/tests/runtime/test_hermes_integration.py -q -o addopts='' --noconftest
```

The recorded run for commit `d46c2431aa798e550d1fd00e820b73c7ba78b980` against
Hermes Agent `v0.22.1+72.g450ad50` produced `4 passed in 36.44s` with exit code
0. `-o addopts='' --noconftest` bypasses the repository-wide conftest and its
PostgreSQL autouse fixture; these runtime tests do not use the database.

The test asserts `healthy` for the health probe, proves that a
`managed_execution` grant is always `unsupported`, and then exercises the
lifecycle with `advisory` grants. Managed execution has no opt-in and is
rejected unconditionally until Issue #82. The test reads credentials from the
environment, never prints or stores them, and is skipped by default. Mock
contract tests need no runtime or database credentials:

```bash
uv run pytest app/tests/runtime/test_hermes_adapter.py -q
```
