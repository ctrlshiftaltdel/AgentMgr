# Hermes Runtime Adapter (B3)

- Issue: #81 (Epic #74)
- Contract: [Runtime Adapter 1.0.0](../../contracts/runtime-adapter.md)
- State machine: [task-state-machine](../../architecture/task-state-machine.md)
- Spike evidence: [Hermes 0.22.1](../../research/runtime-compatibility/hermes-0.22.1.md)
- Implementation: `app/runtime/hermes.py`, `app/runtime/contract.py`,
  `app/runtime/errors.py`, `app/runtime/http.py`
- Contract tests: `app/tests/runtime/test_hermes_adapter.py` (mock server),
  `app/tests/runtime/test_hermes_integration.py` (env-gated real runtime)

> **Scope.** This adapter is the internal B3 runtime boundary for Hermes. It
> maps the Hermes `/v1/runs` API onto the neutral Runtime Adapter contract and
> serves `advisory` execution only. Managed execution is a **different** path
> (#97: `app/runtime/hermes_managed.py`); this adapter always refuses a managed
> grant. It does not write task state, issue or accept Grants, expose Hermes
> profile/database/session internals, or claim OS-level isolation.
>
> **Evidence split.** Mock tests prove the adapter/contract mapping and the
> negative paths (redirects, 408/504, malformed 2xx, keepalive-only, cross-task).
> A committed real-runtime contract suite run covering health/auth and the
> positive advisory lifecycle is recorded in
> [the spike evidence](../../research/runtime-compatibility/hermes-0.22.1.md)
> (§3.6, command, exit code, runtime version, commit SHA). Because the negative
> paths remain mock-only, every supported capability stays
> `partially_verified`; none is upgraded to `verified`, and mock tests are never
> presented as runtime compatibility.

## 0. Two execution paths (do not conflate)

Hermes has **two separate integrations** in this repository. They share the
neutral Runtime Adapter contract but not a transport, a configuration or an
evidence record:

| Path | Implementation | Managed (`managed_execution`)? | Toolset / isolation | Current labels |
|---|---|---|---|---|
| **Advisory `/v1/runs`** | `app/runtime/hermes.py`, `HermesConfig` (`HERMES_API_URL`/`HERMES_API_KEY`) | No. A managed grant is always `unsupported`; there is no opt-in. | Runtime's full configured toolset; no OS boundary. | `partially_verified` (0.22.1 spike + mock contract tests). |
| **Managed one-shot supervisor** | `app/runtime/hermes_managed.py`, `app/runtime/hermes_managed_cli.py`, `ops/security/agentmgr_*` | Yes, but only with a valid runtime attestation and the #96 dispatcher. | Target: exactly `terminal`; one transient systemd unit per task, `DynamicUser`, read-only runtime root, private `/run`/`/tmp`, isolated loopback, task-scoped egress token. | **`unverified` pending real-key artifact E2E;** sandbox isolation, in-process lifecycle and stub-broker token/cancel/timeout live bodies passed on the approved test host. |

Do not use advisory runs as evidence for managed execution, and do not route a
managed task to the advisory adapter. The two paths have separate deployment,
configuration, credentials and evidence:

- The advisory path never selects, imports or falls back to the managed
  supervisor (and vice versa). `HermesConfig` has no managed switch.
- The managed path never starts or mounts a personal/default Hermes gateway,
  profile, authentication file or `.env`; it runs one in-process
  `hermes_cli.oneshot.run_oneshot` per transient unit with a generated minimal
  profile. It never invokes the gateway-backed `hermes chat` CLI.
- The managed launcher has no default configuration. Without the trusted
  `/etc/agentmgr/hermes/managed-cli.json`, a provisioned runtime attestation,
  key files, the installed helper and an active service principal, it exits
  non-zero instead of falling back.
- See the [managed execution design](../../security/hermes-managed-execution-design.md),
  [operations manual](../../operations/hermes-managed-execution.md) and
  [boundary record](../../security/hermes-managed-execution-boundary.md) for the
  fixed paths, install/rollback/verification, and the live evidence still
  required before any managed label leaves `unverified`.

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
raise `RuntimeUnavailable` at construction time. Leading/trailing whitespace in
the key is stripped; embedded whitespace is rejected.

For TLS deployments, terminate TLS at the API server or a trusted local proxy
and keep the key out of command history, repository files and logs.

### Managed execution on this adapter is always refused

**For this advisory adapter** `managed_execution` is **unverified**: the
`/v1/runs` transport cannot enforce per-tool capability/scope, task-scoped
credentials or OS isolation. A managed grant is therefore **always** rejected
with `SubmitOutcome.UNSUPPORTED`, and nothing is sent to the runtime. There is
no environment variable, configuration field or test-only bypass in
`HermesConfig`: the advisory adapter serves only `advisory` execution. The
supported managed path is the separate one-shot supervisor described in §0; it
uses its own adapter, launcher and attestation gate; it remains `unverified`
until the real-key artifact E2E passes (sandbox/lifecycle live evidence is
recorded in the boundary document). A managed task must never be routed here to obtain a
different answer.

## 2. Endpoints and authentication

| Purpose | Method and path | Auth | Adapter method |
|---|---|---|---|
| Liveness | `GET /health` | none | `health()` |
| Capabilities/version | `GET /v1/capabilities` | Bearer | `capabilities(probe=True)`, `health()` |
| Submit | `POST /v1/runs` | Bearer + `Idempotency-Key` | `submit_task()` |
| Lookup (local only) | `POST /v1/runs` replay | Bearer + `Idempotency-Key` | `find_by_idempotency_key()` |
| Status | `GET /v1/runs/{id}` | Bearer | `get_status()` |
| Events | `GET /v1/runs/{id}/events` (SSE) | Bearer | `poll_events()` |
| Stop | `POST /v1/runs/{id}/stop` | Bearer | `cancel_task()` |
| Approval (unverified) | `POST /v1/runs/{id}/approval` | Bearer | `resolve_approval()` |
| Unknown resolution (unverified) | `POST /v1/runs/{id}/resolve-unknown` | Bearer | `resolve_unknown()` |

Authentication is `Authorization: Bearer <API_SERVER_KEY>`. The adapter sends
that header only to the configured API URL. **Redirects are refused**: the
transport uses a custom opener whose redirect handler returns `None`, so a 3xx
is surfaced as an ordinary response and mapped to `unknown`; the bearer key is
never replayed to a `Location` target. Do not add a redirect-following opener.

HTTP errors map to stable adapter errors:

| HTTP | Adapter error | Meaning |
|---|---|---|
| 401 | `RuntimeAuthError` (`unauthorized`) | missing/rejected key |
| 403 | `RuntimeAuthError` (`forbidden`) | key lacks access / Grant rejected |
| 404 | `RuntimeNotFound` (or `RuntimeState.NOT_FOUND` from status) | unknown run |
| 408/504 | `RuntimeTimeout` (`timeout`) | request/gateway timeout; acceptance ambiguous |
| 409 | `RuntimeConflict` | idempotency/payload or execution conflict |
| 429 | `RuntimeRateLimited` | max concurrent runs / quota |
| 503 | `RuntimeUnavailable` | drain, storage or dependency failure |
| 400/415/422 | `RuntimeProtocolError` | malformed request (submit returns `rejected`) |
| 405/501 | `RuntimeUnsupported` | operation not available |
| 3xx | `RuntimeUnknown` | redirect refused; request outcome ambiguous |

`submit_task` returns `SubmitResult` for the definite `rejected`/`unsupported`/
`conflict` outcomes. Auth, rate-limit and 503 failures raise the corresponding
stable exception. Timeouts, connection drops and malformed 2xx bodies are
ambiguous and are returned as `timeout`/`unknown` with the idempotency key
preserved.

## 3. Capability labels

The static labels below are returned by `capabilities()`. They come from the
**partial** 0.22.1 real-runtime spike and the committed mock contract tests.
`capabilities(probe=True)` adds the live `runtime_name`/`runtime_version` from
`/v1/capabilities` and marks the version `runtime_self_reported`; the adapter
never upgrades a label just because a live probe answered.

| Capability | Status | Notes |
|---|---|---|
| `task.submit` | `partially_verified` | Real suite covers submit + replay; 409 conflict and other negative paths are mock-only. Requires a server-resolved Grant. |
| `task.status` | `partially_verified` | Real suite polls to `completed` and `cancelled`; other statuses are manual-spike or mock-only. |
| `task.events.poll` | `partially_verified` | SSE sequence ids + keepalive, mock-tested; not exercised by the real suite. Last-Event-ID support unconfirmed; client-side filtering may miss events older than the 5-minute buffer. |
| `task.cancel` | `partially_verified` | Stop requested vs stop confirmed are distinct; real suite covers stop → `cancelled`. |
| `task.result` | `partially_verified` | Text output/usage/runtime only; real suite asserts `partial_success`; no independent evidence. |
| `runtime.health` | `partially_verified` | `/health` plus authenticated `/v1/capabilities`; real suite asserts `healthy` with `api_ok`/`auth_ok`. |
| `task.events.subscribe` | `unverified` | The adapter polls a bounded SSE stream; no durable subscription. |
| `task.lookup` | `unverified` | No lookup endpoint; process-local submission map + exact POST replay only. |
| `task.input.provide` | `unverified` | No generic input contract; unsupported. |
| `task.approval.resolve` | `unverified` | Endpoint exists; not triggered end-to-end in the spike. |
| `task.unknown.resolve` | `unverified` | Endpoint exists; not triggered end-to-end in the spike. |
| `runtime.isolation` | `unverified` | Logical session association only; no OS-level evidence. |
| `runtime.task_scoped_credentials` | `unverified` | No per-run credential injection. |
| `runtime.tool_permissions` | `unverified` | No per-run tool allowlist; full configured toolset. |
| `runtime.managed_execution` | `unverified` | Host toolset + no task credentials + no OS isolation. |
| `runtime.structured_events` | `partially_verified` | SSE framing is stable; payload is opaque JSON with redaction. |

The labels are fail-closed: the core must not schedule a capability unless its
status and documented limitations are explicitly accepted. `unverified` is never
sufficient.

**Probe binding.** A probe that reports a different runtime **name** downgrades
every label to `unverified`; a different **version** adds an explicit limitation
to every non-unverified label. This prevents reusing 0.22.1 evidence for another
build or product.

## 4. Semantics

### 4.1 Submit and Grant validation

```python
submit_task(
    task_id: str,
    prompt: str,
    idempotency_key: str,
    grant: RuntimeGrant,
    instructions: str | None = None,
) -> SubmitResult
```

`RuntimeGrant` is the **server-resolved projection** defined in
`app/runtime/contract.py`: `grant_id`, `task_id`, `task_revision`,
`execution_mode`, `capabilities`, `resource_scope`, `expires_at`. The adapter
never resolves an arbitrary `grant_ref`, never reads a Grant from the prompt or
client payload, and never widens scope. It enforces what it locally can:

- `grant.task_id` must equal `task_id` (otherwise `forbidden`);
- `task_revision` must be a positive integer;
- `execution_mode` must be `advisory` (the only mode this adapter serves until
  this advisory path; `managed_execution` is always `unsupported`, and
  `governed_resource_access` is refused as a mode mismatch);
- `expires_at` must be present and in the future;
- `capabilities` must be non-empty, well-formed, duplicate-free, and must not
  name adapter-level capabilities such as `runtime.tool_permissions`;
- `resource_scope.scope_type` must be an authorising value (not
  `none`/`undetermined`) and must declare `data_classifications`.

`managed_execution` itself remains `unverified` and is always refused: the
submit returns `SubmitOutcome.UNSUPPORTED` and sends nothing, with no opt-in.
Hermes cannot enforce per-tool capability or resource scope, task-scoped
credentials, or OS isolation; these fields are binding/audit metadata, not
runtime enforcement.

The POST body is:

```json
{
  "input": "<prompt>",
  "session_id": "<AgentMgr task id>",
  "instructions": "<optional instructions>"
}
```

with `Idempotency-Key: <idempotency_key>`. The body is encoded as canonical
UTF-8 JSON: object keys sorted recursively, compact `,`/`:` separators, and
non-ASCII characters emitted literally (`ensure_ascii=false`). The same bytes are
hashed for `payload_hash` (SHA-256, JCS-style canonicalisation), so a Chinese
prompt hashes to exactly the bytes sent instead of an ASCII-escaped form. A new
run returns 202:

```json
{"run_id": "<opaque-run-id>", "status": "started", "replayed": false}
```

The same key with the same payload returns the **same** `run_id` and
`replayed=true`. The same key with a different payload returns 409 → the adapter
returns `SubmitOutcome.CONFLICT`.

**Timeout/connection ambiguity.** Hermes has no independent lookup endpoint. The
adapter stores the exact key/body in a process-local map, retries the same POST
(same key, same body) up to `submit_attempts` (default 2), and then returns
`timeout`/`unknown`. It never generates a new key and never reports `accepted`
without a `run_id`. `accepted_at` is `None` when the runtime omitted it.

### 4.2 `find_by_idempotency_key`

```python
find_by_idempotency_key(
    task_id, grant_id, idempotency_key, payload_hash=None
) -> LookupResult
```

Because Hermes has no lookup endpoint, the adapter can only query its
**process-local** submission map. It replays the exact stored POST with the same
`Idempotency-Key` and canonical body when the submission outcome is still
ambiguous; it never sends a request for a key it has not seen (which would create
a new run). A definite `rejected`/`unsupported`/`conflict` submit outcome is
recorded and returned from that record without replaying the create POST. A 404
on the create route maps to `unknown` — it does not prove that no run exists.
Outcomes:

| Result | Meaning |
|---|---|
| `found` | The replay returned the same `run_id`; the payload hash is returned. |
| `not_found` | A recorded definite rejection/unsupported outcome proves no run was created (the original error is attached). |
| `conflict` | The supplied `payload_hash` differs, a definite conflict was recorded, the replay returned 409, or the replayed `run_id` differs. |
| `unknown` | No local record (not proof of absence), a 404 on the create route, timeout, transport failure or malformed response. |

The map is **not durable across restarts**, so `task.lookup` stays `unverified`.
The core must not treat `unknown` as "no task exists" and must not blind-resubmit.

### 4.3 Status mapping

`get_status(runtime_task_id, *, task_id=None)` maps vendor status strings:

| Hermes status | Neutral state | `stop_confirmed` | Notes |
|---|---|---|---|
| `queued` | `accepted` | false | accepted, not started |
| `running` | `running` | false | execution evidence |
| `waiting_for_approval` | `waiting_input` | false | `input_required=true` |
| `stopping` | `running` | false | **not stopped**; keep polling |
| `completed` | `completed` | false | terminal; core still verifies |
| `failed` | `failed` | false | terminal failure; not a confirmed stop |
| `cancelled` | `stopped` | true | runtime stop evidence |
| `timed_out` | `timed_out` | true | runtime timeout evidence |
| `interrupted` | `unknown` | false | ambiguous; fail closed |
| `unknown` | `unknown` | false | fail closed |
| unrecognized | `unknown` | false | external status preserved |

**Task binding.** Pass `task_id` to require the run's `session_id` to match. A
cross-task handle raises `forbidden` (`task_binding_mismatch`); a run without a
binding raises `forbidden` (`task_binding_unverifiable`). When no `task_id` is
supplied, `session_id` is **not** returned, because the adapter cannot verify it.

`get_status()` returns only safe fields: `state`, `external_state`, `observed_at`,
`heartbeat_at`, `progress`, redacted `output`, `usage`, `runtime` label, a
verified `session_id` and a structured error. It does not expose
`pending_controls`, `admission_id`, `execution_generation`, profile paths or
internal objects. A 404 becomes `RuntimeState.NOT_FOUND`; a malformed/ambiguous
2xx becomes `unknown`, not an exception.

### 4.4 Events (SSE)

`poll_events(run_id, cursor=None, max_events=100, timeout=30.0, *, task_id=None)`
reads `text/event-stream`:

- `id:` is the sequence number and becomes the client-side cursor;
- `: keepalive` comments are ignored, but **each keepalive advances the poll
  deadline**, so a keepalive-only stream cannot hang past `timeout`;
- the remaining time is used as the socket timeout;
- multi-line `data:` fields are joined with `\n`;
- `event:`/JSON `type` become the neutral event type;
- `occurred_at` is `None` when the runtime omitted it (never fabricated);
- event buffers expire after **5 minutes** on the runtime; reconnect from the
  last sequence id before expiry.

When a cursor is supplied the adapter sends the standard `Last-Event-ID` header,
but Hermes did not confirm honoring it in the spike. Client-side filtering is
therefore authoritative; reconnect can miss events that already expired. This is
why `task.events.poll` is `partially_verified`.

Payloads are recursively redacted for credential keys (including normalized
`x-api-key`, `api-key`, `cookie`, `set-cookie`, `jwt`, `session`, token/password/
secret/authorization names) and embedded `Authorization:`/`token=` strings.
`redacted=true` is set **only when a value was actually filtered**. A clean event
has `redacted=false`; the core must not interpret that as a stable business
schema. Usage counters such as `input_tokens` are not credentials and stay
readable.

`has_more` is true only when an additional event was actually observed beyond
`max_events`. A read timeout with no events returns an empty batch with
`error.code=timeout`; it is not a fake success. Missing runs raise
`RuntimeNotFound`.

### 4.5 Cancel

`cancel_task(run_id, *, reason=None, idempotency_key=None, task_id=None,
poll_timeout=None, poll_interval=None)` POSTs `/stop` with `Idempotency-Key` and
then polls status until terminal or the bounded `poll_timeout` expires:

| Situation | Adapter outcome | `stop_confirmed` |
|---|---|---|
| stop returned `stopping`, later `cancelled` | `stopped` | true |
| stop returned `timed_out` | `already_terminal` | true |
| stop returned a terminal `completed`/`failed` | `already_terminal` | false |
| still `stopping` at the poll deadline | `requested` | false |
| stop accepted, confirmation poll failed | `requested` | false |
| stop endpoint 405/501 | `unsupported` | false |
| 409 race (run completed while stopping) | status-derived | false unless cancelled/timed_out |
| ambiguous response / unknown run | `unknown` | false |

A request is never a stop. The core may enter `cancel_requested` on `requested`
but must not enter `cancelled` until a later `get_status()` proves `cancelled`
(or an equivalent stop evidence exists). If `task_id` is supplied, the binding is
checked before the stop is sent; a cross-task handle never reaches `/stop`.

### 4.6 Result

`get_result(run_id, *, task_id=None)` returns the runtime's free-text output plus
`usage`, `runtime` and an opaque `result_ref`. It sets:

- `structured_result=false`;
- `structured_payload=null`;
- `artifacts=()`, `evidence_refs=()`, `verification_hints=()`;
- `outcome=partial_success` for a completed run, because there are no
  independent evidence refs; it is **not** a verified business result;
- `stop_confirmed=true` only for `cancelled`/`timed_out`;
- `completed_at=null` when the runtime omitted it.

`summary`/`output` are redacted and truncated; the core must still apply
`goal.completion_criteria`. A non-terminal run returns `ResultOutcome.UNKNOWN`,
not a fabricated result. Cross-task handles are rejected as in `get_status`.

### 4.7 Health

`health()` calls `GET /health` (unauthenticated) and then
`GET /v1/capabilities` (Bearer):

- both succeed → `healthy`;
- authenticated capabilities return 429 → `degraded` (alive but saturated);
- auth failure, 5xx/drain, 404 or transport failure → `unavailable`.

`details` contains only booleans, HTTP statuses and a stable reason code. It
never contains the key, headers or a response body. Unexpected errors are caught
and reported as `reason=unknown`; health never raises. The core must not submit
new tasks when health is `unavailable`.

## 5. Limitations and remaining risk

1. **Full host toolset.** The runtime exposes its configured tools, including
   terminal access. There is no per-run tool permission or allowlist.
2. **Hermes tools bypass the AgentMgr PEP.** Tool calls executed inside Hermes do
   **not** pass through the AgentMgr policy enforcement point. Until the #97
   managed supervisor passes live evidence and #83 governs business tools, this
   adapter cannot claim that tool scope is enforced. Managed execution and
   `runtime.tool_permissions` stay `unverified`.
3. **No per-task credentials.** The adapter cannot inject short-lived,
   task-scoped credentials; Hermes uses its own configured credentials.
4. **Logical session binding only.** `session_id` associates a run with the
   AgentMgr task id; it is not an OS user, filesystem, process or network
   boundary.
5. **Managed Execution is unverified.** This advisory adapter always rejects
   managed submissions as `unsupported`; there is no test-only opt-in. The
   separate managed supervisor (§0) exists, but every managed capability stays
   `unverified` until the #97 live gates pass.
6. **Structured results are absent.** Only text + usage are available;
   completion is `partial_success`.
7. **Approval/unknown controls are unverified.** The endpoints exist, but their
   request/response schemas were not exercised end-to-end. Only core-resolved
   opaque references are accepted; arbitrary payloads are rejected.
8. **Events expire.** SSE buffers are retained for about five minutes; a slow
   consumer can lose events and must cross-check status/result.
9. **No durable subscription or lookup.** Reconnect uses a client-side cursor;
   the submission map is process-local and lost on restart.
10. **The real suite is positive-path only.** The recorded run at commit
    `d46c2431aa798e550d1fd00e820b73c7ba78b980` against Hermes
    `v0.22.1+72.g450ad50` passed (`4 passed in 36.44s`, exit 0) for health/auth,
    managed-execution refusal, submit/replay/completed and stop→cancelled.
    Redirects, 408/504, malformed 2xx, keepalive-only and cross-task rejection
    are mock-only, so no label is `verified`.

## 6. Integration test

The real-runtime test is skipped unless both variables are present:

```bash
export HERMES_API_URL='http://<test-host>:<api-port>'
export HERMES_API_KEY='<api-key>'    # test-only, >= 16 characters
export HERMES_INTEGRATION_STOP=1     # include the stop scenario
uv run pytest app/tests/runtime/test_hermes_integration.py -q -o addopts='' --noconftest
```

The recorded run on the approved test host at commit
`d46c2431aa798e550d1fd00e820b73c7ba78b980` against Hermes Agent
`v0.22.1+72.g450ad50` (2026.9.24) produced `4 passed in 36.44s` with exit code
0. `-o addopts='' --noconftest` bypasses the repository-wide conftest and its
PostgreSQL autouse fixture; these runtime tests do not use the database.

It performs a health check (asserting `healthy` with `api_ok`/`auth_ok`), a
refusal of a `managed_execution` grant (always `unsupported`, no opt-in), a
short "reply with OK" advisory run with idempotency replay, and (with
`HERMES_INTEGRATION_STOP=1`) a stop scenario that reaches `cancelled`.
It never writes a token, host or profile path into the repository or test output.
Mock tests run without any runtime or database credentials:

```bash
uv run pytest app/tests/runtime/test_hermes_adapter.py -q
```

## 7. Security boundaries

- The adapter belongs to **B3**; it must use an independent workload identity and
  must not share the B1 client, B2 business-tool or B4 admin token.
- The Hermes API key is a runtime control credential, not a business Grant. It
  never enters a task prompt, event payload, audit message or log.
- The adapter does not accept a client/model-supplied Grant, capability or
  scope; it only accepts the server-resolved `RuntimeGrant` projection and
  re-checks the local invariants.
- Redirects are refused and buffered response bodies are size-limited; a
  truncated body maps to `unknown`.
- Business tool calls executed **inside Hermes** bypass the PEP until controlled
  execution (#82) exists; the adapter does not replace the PEP.
- Run the API server on a private address with a network allowlist and TLS (or a
  trusted local proxy). Rotate `API_SERVER_KEY` as a normal secret.
