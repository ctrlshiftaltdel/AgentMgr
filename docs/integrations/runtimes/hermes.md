# Hermes Runtime Adapter (B3)

- Issue: #81 (Epic #74)
- Contract: [Runtime Adapter 1.0.0](../../contracts/runtime-adapter.md)
- State machine: [task-state-machine](../../architecture/task-state-machine.md)
- Spike evidence: [Hermes 0.22.1](../../research/runtime-compatibility/hermes-0.22.1.md)
- Implementation: `app/runtime/hermes.py`, `app/runtime/contract.py`,
  `app/runtime/errors.py`, `app/runtime/http.py`
- Contract tests: `app/tests/runtime/test_hermes_adapter.py` (mock server),
  `app/tests/runtime/test_hermes_integration.py` (env-gated real runtime)

> **Scope.** This adapter is the internal B3 execution boundary for Managed
> Execution. It maps the Hermes `/v1/runs` API onto the neutral Runtime Adapter
> contract. It does not write task state, issue or accept Grants, expose Hermes
> profile/database/session internals, or claim OS-level isolation.
>
> **Evidence split.** Mock tests prove the adapter/contract mapping. Only the
> real-runtime spike in the evidence document can support a compatibility label;
> a mock-only run must never be presented as runtime compatibility.

## 1. Setup

The adapter talks to the Hermes **API server**, not to the CLI, profile files or
database.

1. Enable the API server on the approved host. The spike used the unauthenticated
   `GET /health` plus a bearer-protected `/v1/*` surface.
2. Configure `API_SERVER_KEY` with **at least 16 characters**. Shorter values are
   rejected by both Hermes and `HermesConfig` (fail closed).
3. Restart the Hermes gateway/API server so the key and listen address take
   effect. Verify `GET /health` before submitting anything.
4. Export the two adapter variables in the Worker process only:

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'
```

`HERMES_API_URL` must be an absolute `http`/`https` URL and must not embed
credentials. The adapter never reads a Hermes profile, `.env`, session file or
`~/.hermes`; it only uses the two variables above. Missing or invalid values
raise `RuntimeUnavailable` at construction time.

For TLS deployments, terminate TLS at the API server or a trusted local proxy
and keep the key out of command history, repository files and logs.

## 2. Endpoints and authentication

| Purpose | Method and path | Auth | Adapter method |
|---|---|---|---|
| Liveness | `GET /health` | none | `health()` |
| Capabilities/version | `GET /v1/capabilities` | Bearer | `capabilities(probe=True)`, `health()` |
| Submit | `POST /v1/runs` | Bearer + `Idempotency-Key` | `submit_task()` |
| Status | `GET /v1/runs/{id}` | Bearer | `get_status()` |
| Events | `GET /v1/runs/{id}/events` (SSE) | Bearer | `poll_events()` |
| Stop | `POST /v1/runs/{id}/stop` | Bearer | `cancel_task()` |
| Approval (partial) | `POST /v1/runs/{id}/approval` | Bearer | `resolve_approval()` |
| Unknown resolution (partial) | `POST /v1/runs/{id}/resolve-unknown` | Bearer | `resolve_unknown()` |

Authentication is `Authorization: Bearer <API_SERVER_KEY>`. The adapter sends
that header only to the configured API URL. It never logs headers, request
bodies or the key.

HTTP errors map to stable adapter errors:

| HTTP | Adapter error | Meaning |
|---|---|---|
| 401 | `RuntimeAuthError` (`unauthorized`) | missing/rejected key |
| 403 | `RuntimeAuthError` (`forbidden`) | key lacks access |
| 404 | `RuntimeNotFound` (or `RuntimeState.NOT_FOUND` from status) | unknown run |
| 409 | `RuntimeConflict` | idempotency/payload or execution conflict |
| 429 | `RuntimeRateLimited` | max concurrent runs / quota |
| 503 | `RuntimeUnavailable` | drain, storage or dependency failure |
| 400/415/422 | `RuntimeProtocolError` | malformed request/response |
| 405/501 | `RuntimeUnsupported` | operation not available |

## 3. Capability labels

The static labels below are returned by `capabilities()`. They come from the
0.22.1 real-runtime spike and the committed contract tests. `capabilities(probe=True)`
adds the live `runtime_version` from `/v1/capabilities` and marks it
`runtime_self_reported`; the adapter never upgrades a label just because a live
probe answered.

| Capability | Status | Notes |
|---|---|---|
| `task.submit` | `verified` | `POST /v1/runs` returns 202 + opaque `run_id`; replay/conflict paths covered. |
| `task.status` | `verified` | `GET /v1/runs/{id}`; vendor status mapped below. |
| `task.events.poll` | `verified` | SSE sequence ids + keepalive comments; client-side cursor. |
| `task.cancel` | `verified` | Stop requested vs stop confirmed are distinct. |
| `task.result` | `verified` | Text output/usage/runtime only; `structured_result=false`. |
| `runtime.health` | `verified` | `/health` plus authenticated `/v1/capabilities`. |
| `task.events.subscribe` | `unverified` | The adapter polls a bounded SSE stream; no durable subscription. |
| `task.lookup` | `unverified` | No lookup-by-idempotency-key endpoint; see §4.1. |
| `task.input.provide` | `unverified` | No generic input contract; unsupported. |
| `task.approval.resolve` | `partially_verified` | Endpoint exists; not triggered end-to-end in the spike. |
| `task.unknown.resolve` | `partially_verified` | Endpoint exists; not triggered end-to-end in the spike. |
| `runtime.isolation` | `partially_verified` | Logical session isolation only, not an OS boundary. |
| `runtime.task_scoped_credentials` | `unverified` | No per-run credential injection. |
| `runtime.tool_permissions` | `unverified` | No per-run tool allowlist; full configured toolset. |
| `runtime.managed_execution` | `unverified` | Host toolset + no task credentials + no OS isolation. |
| `runtime.structured_events` | `partially_verified` | SSE framing is stable; payload is opaque JSON with redaction. |

`unverified` is the fail-closed label: the core must not schedule a capability
whose status is not `verified` (or explicitly accept the documented
`partially_verified` limitation).

## 4. Semantics

### 4.1 Submit and idempotency

`submit_task(task_id, prompt, idempotency_key, instructions=None)` sends:

```json
{
  "input": "<prompt>",
  "session_id": "<AgentMgr task id>",
  "instructions": "<optional instructions>"
}
```

with `Idempotency-Key: <idempotency_key>`. A new run returns 202:

```json
{"run_id": "<opaque-run-id>", "status": "started", "replayed": false}
```

The same key with the same payload returns the **same** `run_id` and
`replayed=true`. The same key with a different payload returns 409 → the
adapter raises `RuntimeConflict`.

**Timeout/connection ambiguity.** Hermes has no independent
`find_by_idempotency_key` endpoint. The adapter therefore retries the exact same
POST (same key, same body) up to `submit_attempts` (default 2). This is an
idempotent replay, not a new submission. If the result is still ambiguous it
returns:

- `SubmitOutcome.TIMEOUT` for a request timeout;
- `SubmitOutcome.UNKNOWN` for a pre-response connection failure.

It never generates a new key and never reports `accepted` without a `run_id`.
This is the documented adapter deviation from contract §3.2; the core still must
not blind-resubmit after an ambiguous result.

`session_id` is the AgentMgr task id. It is a **logical** association, not an OS
identity or sandbox.

### 4.2 Status mapping

`get_status()` maps vendor status strings into the frozen neutral states:

| Hermes status | Neutral state | `stop_confirmed` | Notes |
|---|---|---|---|
| `queued` | `accepted` | false | accepted, not started |
| `running` | `running` | false | execution evidence |
| `waiting_for_approval` | `waiting_input` | false | `input_required=true` |
| `stopping` | `running` | false | **not stopped**; keep polling |
| `completed` | `completed` | true | execution ended; core still verifies |
| `failed` | `failed` | true | terminal failure |
| `cancelled` | `stopped` | true | only here may the core treat it as stopped |
| `interrupted` | `unknown` | false | ambiguous; fail closed |
| `unknown` | `unknown` | false | fail closed |
| unrecognized | `unknown` | false | external status preserved |

The frozen contract has no intermediate `stopping` state, so the adapter reports
it as still running and explicitly leaves `stop_confirmed=false`. The core must
not display it as stopped.

`get_status()` returns only safe fields: `state`, `external_state`, `observed_at`,
`heartbeat_at`, `progress`, `output` (text), `usage`, `runtime` label,
`session_id` and a structured error. It does not expose `pending_controls`,
`admission_id`, `execution_generation`, profile paths or internal objects.
A 404 becomes `RuntimeState.NOT_FOUND` (the core treats it as unknown and starts
reconciliation).

### 4.3 Events (SSE)

`poll_events(run_id, cursor=None, max_events=100, timeout=30.0)` reads
`text/event-stream`:

- `id:` is the sequence number and becomes the client-side cursor;
- `: keepalive` comment lines are ignored;
- multi-line `data:` fields are joined with `\n`;
- `event:`/JSON `type` become the neutral event type;
- event buffers expire after **5 minutes** on the runtime; reconnect from the
  last sequence id before expiry.

The returned `EventBatch.next_cursor` is the highest observed sequence id (or the
input cursor when nothing new arrived). Replaying a cursor is stable. Payloads
are recursively redacted for obvious credential keys and `Authorization`-style
strings before they leave the adapter; `redacted=true` means this sanitisation
ran, not that the payload is a stable business schema.

A read timeout with no events returns an empty batch with
`error.code=timeout`; it is not a fake success. Missing runs raise
`RuntimeNotFound`.

### 4.4 Cancel

`cancel_task()` POSTs `/stop` and then polls status until terminal or the bounded
`poll_timeout` expires:

| Situation | Adapter outcome | `stop_confirmed` |
|---|---|---|
| stop returned `stopping`, later `cancelled` | `stopped` | true |
| stop returned a terminal `completed`/`failed` | `already_terminal` | true |
| still `stopping` at the poll deadline | `requested` | false |
| stop endpoint 405/501 | `unsupported` | false |
| unknown/ambiguous run | `unknown` or `RuntimeNotFound` | false |

A request is never a stop. The core may enter `cancel_requested` on `requested`
but must not enter `cancelled` until a later `get_status()` proves `cancelled`
(or an equivalent stop evidence exists).

### 4.5 Result

`get_result()` returns the runtime's free-text output plus `usage`, `runtime` and
an opaque `result_ref`. It sets:

- `structured_result=false`;
- `structured_payload=null`;
- `artifacts=()`, `evidence_refs=()`, `verification_hints=()`;
- `stop_confirmed=true` only for a terminal `completed`/`failed`/`cancelled`
  status.

The core must still apply `goal.completion_criteria`; "the runtime said
completed" is not a verified business result. A non-terminal run returns
`ResultOutcome.UNKNOWN`, not a fabricated result.

### 4.6 Health

`health()` calls `GET /health` (unauthenticated) and then
`GET /v1/capabilities` (Bearer):

- both succeed → `healthy`;
- authenticated capabilities return 429 → `degraded` (alive but saturated);
- auth failure, 5xx/drain, 404 or transport failure → `unavailable`.

`details` contains only booleans, HTTP statuses and a stable reason code. It
never contains the key, headers or a response body. The core must not submit new
tasks when health is `unavailable`.

## 5. Limitations and remaining risk

1. **Full host toolset.** The runtime exposes its configured tools, including
   terminal access. There is no per-run tool permission or allowlist.
2. **No per-task credentials.** The adapter cannot inject short-lived,
   task-scoped credentials; Hermes uses its own configured credentials.
3. **Logical session isolation only.** `session_id` separates conversations, not
   OS users, filesystems, process trees or network egress.
4. **Managed Execution is unverified.** The adapter does not claim a sandbox or
   host boundary; `runtime.managed_execution` stays `unverified`.
5. **Structured results are absent.** Only text + usage are available.
6. **Approval/unknown controls are partial.** The endpoints exist, but their
   request/response schemas were not exercised end-to-end in the spike.
7. **Events expire.** SSE buffers are retained for about five minutes; a slow
   consumer can lose events and must cross-check status/result.
8. **No durable subscription.** Reconnect uses a last-event id/cursor; it is not
   an at-least-once durable queue.
9. **No `find_by_idempotency_key` endpoint.** A submit retry must replay the same
   POST with the same key and payload.

## 6. Integration test

The real-runtime test is skipped unless both variables are present:

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'
uv run pytest app/tests/runtime/test_hermes_integration.py -q
```

It performs a health check, a short "reply with OK" run, an idempotency replay,
and (only with `HERMES_INTEGRATION_STOP=1`) a stop scenario. It never writes a
token, host or profile path into the repository or test output. Mock tests run
without any runtime or database credentials:

```bash
uv run pytest app/tests/runtime/test_hermes_adapter.py -q
```

## 7. Security boundaries

- The adapter belongs to **B3**; it must use an independent workload identity and
  must not share the B1 client, B2 business-tool or B4 admin token.
- The Hermes API key is a runtime control credential, not a business Grant. It
  never enters a task prompt, event payload, audit message or log.
- The adapter does not accept a client/model-supplied Grant, capability or
  scope; the core resolves and re-validates the Grant before calling it.
- Business tool calls remain subject to the PEP; this adapter does not replace
  the PEP or the task state machine.
- Run the API server on a private address with a network allowlist and TLS (or a
  trusted local proxy). Rotate `API_SERVER_KEY` as a normal secret.
