# WorkBuddy integration notes (remote vs local bridge)

- Issue: #80 (Epic #74)
- Status: **not verified**. No WorkBuddy version was available for acceptance in
  this task, and no compatibility claim is made.

## 1. Two different trust paths

| Path | What it is | Trusted principal | What this task can claim |
|---|---|---|---|
| **Remote MCP** | WorkBuddy connects through a trusted gateway to AgentMgr's Streamable HTTP endpoint (`/mcp/`) | `Authorization` + `X-Employee-Id` must be injected by the gateway for every request; the gateway must authenticate the user first | The endpoint and tools are verified with the official Inspector (`mcp-inspector.md`). WorkBuddy itself is **not** verified. |
| **Local bridge** | WorkBuddy launches a local process/connector on the user's device and forwards tool calls | The bridge, not the model, must inject the verified employee context; a local process cannot simply forward a model-supplied `employee_id` | **Not verified.** A local bridge is B0/B1-adjacent: model arguments remain untrusted. |

The server never accepts a principal, owner, approver, role or priority from a
tool argument. Therefore a local bridge that cannot attach trusted headers has
no supported authentication path in this release.

## 2. Remote MCP configuration (candidate, unverified)

Use these values as the target of a future WorkBuddy acceptance run. They are
not evidence that WorkBuddy supports them:

| Setting | Value |
|---|---|
| Transport | Streamable HTTP (server is stateless; no session affinity needed) |
| URL | `http://127.0.0.1:<port>/mcp/` for local development only; a remote deployment must use the TLS-terminated gateway URL |
| Header | `Authorization: Bearer <internal-token>` (injected by the gateway, never by the model/client) |
| Header | `X-Employee-Id: <verified-employee-id>` (injected by the gateway after authentication) |
| Server env | `AJAIMGR_MCP_ALLOWED_HOSTS=<gateway-host>:*` when the gateway's `Host` is not loopback |
| Tools | `governance.evaluate_task`, `governance.submit_task`, `governance.get_task`, `governance.list_my_tasks`, `governance.request_confirmation`, `governance.request_approval`, `governance.cancel_task`, `governance.get_result` |

The server enforces a transport-level bearer check on `initialize`,
`tools/list` and session creation. That protects the SDK surface, but it does
**not** make the shared token safe to give to a remote client: a gateway must
authenticate the user and inject both headers. Direct remote use with the shared
token is not a supported trust path.

Requirements before this can be called compatible:

1. WorkBuddy reaches AgentMgr through a gateway that injects both headers on
   every MCP HTTP request, including the first initialize and every tool call.
2. Neither WorkBuddy nor the model can supply or override those headers.
3. WorkBuddy's MCP client supports the Streamable HTTP transport and the
   server's negotiated protocol version.
4. A recorded acceptance run shows `tools/list`, a successful `submit_task`, a
   `get_task` poll and an `unauthenticated` rejection with the pinned WorkBuddy
   version.

## 3. Local bridge differences

A local bridge (stdio or a loopback child process) has different constraints:

- The server's authentication dependency is HTTP-header based. A stdio bridge
  cannot carry `Authorization`/`X-Employee-Id` unless the bridge itself performs
  the HTTP call and injects them.
- If the bridge forwards model-generated arguments directly, it must not add
  identity fields; the server rejects them (`invalid_arguments`).
- A local bridge that runs with a user's OS credentials must not be represented
  as "managed execution": AgentMgr can only govern calls that pass through it
  (ADR-001 §2.4).
- Do not reuse the internal token in a client-side binary. A future local bridge
  needs a short-lived, user-bound credential or a signed assertion, not a shared
  server token.

## 4. Current conclusion

- **Remote MCP path:** implemented and verified with the official Inspector over
  the loopback/Gateway boundary; not a direct remote-client API.
- **WorkBuddy remote integration:** not tested; no compatibility claim.
- **WorkBuddy local bridge:** not supported by this release's header-based
  principal adapter; no compatibility claim.
