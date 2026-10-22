# Hermes 0.22.1 runtime compatibility spike

- Issue: #81 (Epic #74)
- Runtime: Hermes `0.22.1+72.g450ad50`
- Environment: approved isolated **test host** (`<test-host>`)
- API surface: Hermes API server `/health` and `/v1/*`
- Implementation under test: `app/runtime/hermes.py`
- Adapter contract: [Runtime Adapter 1.0.0](../../contracts/runtime-adapter.md)
- Integration guide: [Hermes adapter](../../integrations/runtimes/hermes.md)
- Mock contract tests: `app/tests/runtime/test_hermes_adapter.py`
- Env-gated real test: `app/tests/runtime/test_hermes_integration.py`

> **Evidence scope.** This document records the real-runtime observations that
> support the `partially_verified` labels for the adapter. It is **partial
> evidence**: a committed real-runtime contract suite run (command, exit code,
> runtime version, commit SHA) has **not** been recorded, so no capability is
> labelled `verified`. It does **not** claim OS isolation, task-scoped
> credentials, per-task tool permissions, managed-execution readiness, or a
> stable business result schema. Mock tests are labelled separately and are not
> runtime compatibility evidence.
>
> **Placeholders.** Host, port, run ids and the API key are redacted as
> `<test-host>`, `<api-port>`, `<run-id>` and `<api-key>`. The temporary test
> key and API-server configuration were removed after the spike; the original
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

## 4. Capability summary

| Capability | Label | Evidence / limitation |
|---|---|---|
| `task.submit` | `partially_verified` | 202 + opaque `run_id`; replay and 409 conflict observed. No committed real contract suite recorded. |
| `task.status` | `partially_verified` | Status transitions observed: queued/running/completed and stopping/cancelled. No committed real contract suite recorded. |
| `task.events.poll` | `partially_verified` | SSE `id:`, keepalive, `message.delta`, `run.completed`; client cursor implemented and tested. Last-Event-ID support not confirmed. |
| `task.cancel` | `partially_verified` | Requested (`stopping`) vs confirmed (`cancelled`) observed. No committed real contract suite recorded. |
| `task.result` | `partially_verified` | Text output + usage + runtime; no evidence refs, so completion is `partial_success`. |
| `runtime.health` | `partially_verified` | `/health` + authenticated `/v1/capabilities`; manual spike only. |
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

- No committed real-runtime contract suite run with command, exit code, runtime
  version and commit SHA was recorded. The manual observation above is partial
  evidence only, so no capability is `verified`.
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
export HERMES_API_KEY='<api-key>'
export HERMES_INTEGRATION_STOP=0   # set 1 only for the optional stop scenario
# Test-only: managed_execution is refused by default until Issue #82.
export HERMES_ALLOW_UNVERIFIED_MANAGED_EXECUTION=1
uv run pytest app/tests/runtime/test_hermes_integration.py -q
```

The test asserts `healthy` for the health probe, proves the default refusal when
the opt-in is absent, and then exercises the lifecycle under the explicit
test-only opt-in. It reads credentials from the environment, never prints or
stores them, and is skipped by default. Mock contract tests need no runtime or
database credentials:

```bash
uv run pytest app/tests/runtime/test_hermes_adapter.py -q
```
