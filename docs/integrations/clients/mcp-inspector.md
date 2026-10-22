# MCP Inspector acceptance (client governance surface)

- Issue: #80 (Epic #74)
- Endpoint: Streamable HTTP at `http://<test-host>:<port>/mcp/`
- Inspector: `@modelcontextprotocol/inspector@2.12.0` (pinned; npm `latest` at the time of the run)
- Transport: Streamable HTTP (`--transport http`)
- Trusted principal: `Authorization: Bearer <internal-token>` + `X-Employee-Id: <employee-id>`

> **Scope of this evidence.** This proves the official Inspector can initialize,
> list the tools and invoke them against the local Streamable HTTP server. It
> does **not** claim WorkBuddy compatibility (see `workbuddy.md`) and it does
> **not** claim event streaming. All credentials below are placeholders.

## 1. What was run

The application was started locally against the test database:

```bash
AJAIMGR_DB_URL='<test-db-dsn>' \
AJAIMGR_INTERNAL_TOKEN='<internal-test-token>' \
uv run uvicorn app.api.main:app --host 127.0.0.1 --port 8765
```

The local database contained one active employee with an existing manager and a
published `polset_default` policy containing:

| capability | result |
|---|---|
| `expense.read@1.0.0` | allow (self scope), queues without gates |
| `expense.confirm@1.0.0` | requires a confirmation |
| `expense.approve@1.0.0` | requires an approval |
| `payment.execute@1.0.0` | hard deny |

Inspector was invoked headlessly. The same command shape was used for every
method (the header value and tool arguments changed):

```bash
npx --yes @modelcontextprotocol/inspector@2.12.0 \
  --cli \
  --server-url http://127.0.0.1:8765/mcp/ \
  --transport http \
  --method tools/call \
  --tool-name governance.get_task \
  --tool-arg 'task_id=<task-id>' \
  --header "Authorization: Bearer <internal-test-token>" \
          "X-Employee-Id: <employee-id>" \
  --format json
```

`tools/list` used the same connection flags with `--method tools/list`.

## 2. Recorded results

### 2.1 `tools/list`

The official client initialized successfully and returned exactly these tools:

```
governance.evaluate_task
governance.submit_task
governance.get_task
governance.list_my_tasks
governance.request_confirmation
governance.request_approval
governance.cancel_task
governance.get_result
```

Each `inputSchema` had `additionalProperties: false` and none exposed an
identity/owner/approver/role/priority field. The descriptions included side
effects, risk, non-applicable cases, and confirmation/approval requirements.

### 2.2 `governance.evaluate_task`

Without a resource scope the PDP returned `needs_clarification`
(`resource_scope_ambiguous`). With a concrete `self` scope it returned the
non-authorizing `pre_evaluation` decision (`pre_evaluation_only`), with
`task_id=null` and `read_only=true`. This matches ADR-001: there is no
pre-task `allow`.

### 2.3 `governance.submit_task`

Submitted an advisory read task. The response was:

```json
{
  "task_id": "task_<redacted>",
  "status": "queued",
  "state": "queued",
  "execution_mode": "advisory",
  "decision_type": "allow",
  "attempt": 0
}
```

A second submission (`expense.confirm@1.0.0`) returned
`status=awaiting_confirmation` / `decision_type=needs_confirmation`. A third
(`expense.approve@1.0.0`) returned `status=awaiting_approval` /
`decision_type=needs_approval`. In all three cases the server resolved owner,
executor, approver and execution mode; no such field was accepted from the
client.

### 2.4 `governance.get_task` / `governance.list_my_tasks`

`get_task` returned the safe summary (state, revision, attempt, execution mode,
decision type, timestamps) for the caller. `list_my_tasks` returned only the
caller's participant tasks (`count=1` at that point). No employee ids, payloads
or credentials were present.

### 2.5 `governance.request_confirmation`

Against the `awaiting_confirmation` task it returned a pending, single-use
confirmation bound to the task payload hash:

```json
{
  "confirmation_id": "conf_<redacted>",
  "status": "pending",
  "purpose": "high_impact_action",
  "payload_hash": "sha256:<redacted>",
  "single_use": true
}
```

### 2.6 `governance.request_approval`

Against the `awaiting_approval` task it returned the existing workflow instance,
not an approval decision:

```json
{
  "approval_request_id": "1",
  "workflow_type": "task_approval",
  "status": "in_progress",
  "approver_principal_id": "prin_<resolved-by-server>",
  "task_state": "awaiting_approval"
}
```

The approver was the employee's manager, resolved from the directory. The client
cannot specify an approver.

### 2.7 `governance.cancel_task` / `governance.get_result`

Cancelling the queued read task returned `cancel_requested` (a request is not a
stop). Cancelling the pre-execution confirmation task returned `cancelled` with
`record_completeness=complete`.

`get_result` before a result existed returned:

```json
{"task_id": "task_<redacted>", "state": "queued", "result": null,
 "note": "result_not_available"}
```

After the pre-execution cancellation it returned only the authorized result
references:

```json
{
  "state": "cancelled",
  "result": {
    "outcome": "cancelled",
    "result_ref": null,
    "evidence_refs": [],
    "verification": null,
    "stop_confirmed": true,
    "side_effects_reconciled": true
  },
  "note": "terminal"
}
```

### 2.8 Unauthenticated call

`tools/call` without headers produced an `isError` result with the stable
envelope, and no stack trace:

```json
{"error": {"code": "unauthorized", "message": "authentication required"}}
```

## 3. Reproduction checklist

1. Start PostgreSQL and run `alembic upgrade head`.
2. Seed an active employee, their manager, and a published policy set (the
   test suite's `make_org` / `publish_policy` helpers are a reference).
3. Start uvicorn with `AJAIMGR_INTERNAL_TOKEN` set.
4. Run the Inspector commands in §1 with the real employee id and token.
5. Confirm the eight tools are listed, submit a task, poll `get_task`, cancel a
   pre-execution task, and read `get_result`.

## 4. Limitations

- **Rate limiting is in-process.** Each process has its own per-principal /
  per-operation bucket; a multi-process deployment needs a shared limiter.
- **No event streaming is claimed.** The tools return task ids/status and the
  documented `get_task`/`get_result` polling contract.
- **Inspector is not WorkBuddy.** Inspector exercises the remote Streamable HTTP
  path. WorkBuddy's local bridge is a different trust path and is documented
  separately in `workbuddy.md`; no compatibility claim is made.
- The run used a test token and test employee only. No production credential or
  real user data is recorded here.
