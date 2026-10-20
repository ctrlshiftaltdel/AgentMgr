# Runtime Adapter 契约（v1.0.0）

- 状态：Phase 0 冻结候选；产品负责人确认前不得据此宣称运行时兼容性
- 契约版本：1.0.0
- 关联：Issue #77、[ADR-001](../architecture/ADR-001-product-boundary.md)、[task-envelope](../contracts/task-envelope.schema.json)、[execution-grant](../contracts/execution-grant.schema.json)、[task-state-machine](../architecture/task-state-machine.md)
- 适用运行时：Hermes（首个候选）、Rakazo（第二候选），以及未来通过契约测试的运行时

## 1. 目的与边界

Runtime Adapter 把 AgentMgr 的内部任务契约映射到具体 Agent 运行时。它属于**内部信任边界 B3**，不是公开 API，也不是 MCP 工具接口。

**它是受控执行模式（Managed Execution）的执行边界，不是唯一执行边界。** Advisory/BYOA（B1）与 Governed Resource Access（B2）分别由受控客户端调用/PEP 产生 `execution.*` 事实；任务状态机对三种模式使用同一套模式中立事件。不得把 Runtime Adapter 的能力要求套用到 Advisory/Governed，也不得因为某运行时通过 Adapter 契约测试就推断客户端本地工具已被治理（见 [task-state-machine](../architecture/task-state-machine.md) §3）。

硬性规则：

1. 核心业务只依赖本文定义的方法、字段和状态语义，不依赖 Hermes/Rakazo 的私有对象、数据库 schema、Profile 路径、会话文件或内部 HTTP 端点。
2. `runtime_task_id` 是**不透明句柄**。核心不得解析其格式、用它推断状态，或把它拼进文件路径。
3. Adapter 不得与客户端 MCP、服务端业务工具 MCP 或管理 REST 共用无限制管理员令牌。它使用独立 workload identity；运行时凭据按任务注入。
4. Adapter 只报告事实（受理、运行、停止、结果、健康），**不直接写任务状态机**。状态转换由核心服务验证并写入 task event。
5. Adapter 不得把凭据、令牌、完整个人数据或受保护业务字段写入日志、事件或错误响应。
6. **Grant 是执行授权的唯一权威。** Adapter 不得接受客户端、模型或运行时自报的 Grant、能力或范围；只接受 AgentMgr 核心在服务端解析的 Grant 或其安全引用。未知字段必须拒绝，不得静默忽略。

## 2. 能力声明与状态标签

### 2.1 `capabilities()`

```json
{
  "adapter_contract_version": "1.0.0",
  "boundary": "b3_runtime_adapter",
  "runtime_id": "rt_hermes_candidate_0001",
  "runtime_type": "hermes",
  "runtime_version": null,
  "version_source": "unknown",
  "observed_at": "2026-10-20T00:00:00Z",
  "capabilities": [
    {
      "name": "task.submit",
      "version": "1.0.0",
      "status": "unverified",
      "evidence_ref": null,
      "limitations": "No contract test has been run against a real runtime."
    }
  ]
}
```

`capabilities()` 必须返回：

- `adapter_contract_version`：本文版本；
- `boundary`：固定 `b3_runtime_adapter`，用于事件来源与审计；
- `runtime_id`：AgentMgr 侧的运行时不透明标识，不是主机名/路径；
- `runtime_type`：`hermes` / `rakazo` / `other`；
- `runtime_version`：运行时自报版本；未知时必须为 `null`，禁止用 `"unverified"`、`"unknown"` 等字符串冒充版本号；
- `version_source`：`runtime_self_reported` / `operator_verified` / `unknown`；只有 `operator_verified` 可作为兼容性证据；
- `capabilities[]`：逐项能力，包含 `name`、`version`、`status`、`evidence_ref`、`limitations`；
- `observed_at`：快照时间。

### 2.2 能力状态

| 状态 | 定义 | 可对外声称 |
|---|---|---|
| `verified` | 共享契约测试套件在**真实运行时**上通过，证据可追溯（命令、退出码、运行时版本、提交 SHA），且覆盖成功与负向路径 | 可以声明该能力已支持 |
| `partially_verified` | 真实运行时上部分路径通过，但存在明确未覆盖的分支、限制或降级语义 | 只能按 `limitations` 声明，不得省略限制 |
| `unverified` | 仅有设计、Mock、文档或运行时自述；没有可复现的真实证据 | 不得声明支持 |

规则：

- **不得因为存在 MCP Server 就推断它是通用远程执行入口。** MCP 是工具接口协议，不等于任务提交、状态、取消、结果或事件协议。
- Mock 测试证据必须单独标记，不能冒充真实兼容性。
- 能力状态可在运行时升级后变化；变化必须重新运行契约测试并更新 `evidence_ref`。
- 未知能力按 `unverified` 处理，并 fail closed：核心不得调度依赖该能力的任务。

### 2.3 首版能力清单

Adapter 至少声明以下能力名（版本随契约升级）：

| 能力名 | 说明 | 必需 |
|---|---|---|
| `task.submit` | 提交任务并返回受理/拒绝/未知 | 是 |
| `task.lookup` | 按幂等键查询已提交的执行任务 | 是 |
| `task.status` | 查询任务状态、心跳与停止确认 | 是 |
| `task.events.poll` | 基于游标拉取事件 | 是（或 `task.events.subscribe`） |
| `task.events.subscribe` | 订阅事件流 | 否 |
| `task.input.provide` | 回答运行时的输入请求 | 否 |
| `task.cancel` | 发出取消并报告是否已停止 | 是 |
| `task.result` | 返回结构化结果与证据引用 | 是 |
| `runtime.health` | 健康检查 | 是 |
| `runtime.isolation` | 独立服务账户/沙箱/文件系统/网络出口 | 否（Managed Execution 必需） |
| `runtime.task_scoped_credentials` | 任务级短时凭据注入 | 否（Managed Execution 必需） |
| `runtime.structured_events` | 稳定、可映射的结构化事件 | 否 |

`task.events.subscribe` 与 `task.events.poll` 至少有一个为 `verified`，否则核心只能做低频状态查询，并必须在任务记录中标注事件能力缺失。

## 3. 方法与语义

所有方法都是 Adapter 内部契约；输入/输出使用 JSON 对象，**未知字段必须拒绝**（`invalid_request`），不得静默忽略或静默改变语义；拒绝时记录字段名与调用者。所有写方法必须带幂等键或稳定请求 ID。

### 3.1 `submit_task(task, grant)`

**Grant 是唯一权威授权来源。** 请求中的 `execution_mode`、`capabilities`、`resource_scope` 必须与 Grant 完全一致或是其子集；不一致时 Adapter 必须拒绝（`forbidden`），不得“按请求执行”。核心只传递由服务端签发并已重验的 Grant 或安全引用：

- `grant_source="inline_verified"`：核心已重验并内联完整 Grant 对象；
- `grant_source="server_side_reference"`：核心传入不透明 `grant_ref`，Adapter 必须通过受认证的内部接口解析，禁止从请求文本、提示词或客户端参数读取 Grant。

请求至少包含：

```json
{
  "task_id": "task_expense_0001",
  "idempotency_key": "idem_expense_0001",
  "grant_id": "grnt_expense_0001",
  "grant_source": "server_side_reference",
  "grant_ref": "grantref_expense_0001",
  "execution_mode": "managed_execution",
  "capabilities": ["expense.read@1.0.0"],
  "resource_scope": {"scope_type": "self", "resource_ids": ["res_expense_0001"]},
  "deadline_at": "2026-10-20T03:00:00Z",
  "input": {"...": "已最小化的任务输入"}
}
```

响应：

```json
{
  "outcome": "accepted",
  "runtime_task_id": "rtask_hermes_0001",
  "accepted_at": "2026-10-20T02:00:05Z",
  "reported_state": "accepted",
  "error": null
}
```

`outcome`：

| 值 | 语义 | 核心动作 |
|---|---|---|
| `accepted` | 运行时明确受理；可能尚未开始执行 | 绑定 `runtime_task_id`，T23 → `dispatched` |
| `rejected` | 运行时明确拒绝，且确认未产生副作用 | T66 → `failed`（不得自动重试） |
| `unsupported` | 运行时/Adapter 不支持该能力或模式 | T69 → `needs_human`；记录兼容性缺口，不自动重试 |
| `timeout` | 提交超时；**不知道是否已受理** | T67 → `unknown`；先查询，不重发 |
| `unknown` | 响应不可解析/连接中断；不知道是否已受理 | T68 → `unknown`；先查询，不重发 |

幂等规则：

- `(task_id, grant_id, idempotency_key)` 是提交幂等键；Adapter 必须保证同一键最多创建一个运行时任务。
- 相同键、相同 `payload_hash` 的重复提交必须返回**同一个** `runtime_task_id` 与 `outcome=accepted`，不得创建第二个任务。
- 相同键、不同 `payload_hash` 必须返回 `conflict`，而不是创建第二个任务。
- Adapter 必须在内部持久记录“请求已发出/已受理/已拒绝”的状态，不能只靠内存去重。
- 超时后核心先调用 `find_by_idempotency_key`；找到则采用原任务，明确不存在且无副作用才允许按预算重试。

安全引用解析规则：

- `grant_ref` 必须是不透明、带命名空间的引用；Adapter 只能通过受认证的内部解析接口按 ID 查找，禁止把它当作文件路径、URL、SQL 或任意对象引用解引用。
- 解析接口必须校验调用者 workload identity、Grant 的 `task_id`/`grant_id` 与当前提交一致，并返回完整、已重验的 Grant；解析失败一律 fail closed。
- 引用中不得包含令牌、凭据、个人数据或运行时私有路径；日志只记录引用 ID 与结果码。
- 客户端、模型或运行时提供的 Grant/引用一律不得采信；Adapter 不暴露“接受任意 Grant”的入口。

### 3.2 `find_by_idempotency_key(task_id, grant_id, idempotency_key)`

提交超时或连接中断后，核心**必须先调用本方法**，不得盲目重发创建请求。

响应：

```json
{
  "outcome": "found",
  "runtime_task_id": "rtask_hermes_0001",
  "reported_state": "running",
  "payload_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
  "accepted_at": "2026-10-20T02:00:05Z",
  "observed_at": "2026-10-20T02:00:40Z",
  "error": null
}
```

`outcome`：

| 值 | 语义 | 核心动作 |
|---|---|---|
| `found` | 同一幂等键已有执行任务 | 采用该 `runtime_task_id`，按实际状态走 T29/T31/T41 等；禁止重发 |
| `not_found` | 查询明确不存在 | 不等于可重试：仅当 `attempt=0`、有界窗口结束且 Adapter 出具无副作用保证 → T71 `reconcile.not_found` → `queued`（新决策 + 新 Grant）；`attempt>=1` 或无法出具保证 → T55 `failed` → T65，或 T73 转人工 |
| `conflict` | 同键但 payload hash 不同 | 拒绝重试；`conflict`，进入 `needs_human` 对账 |
| `unknown` | 查询超时/不可解析 | `unknown`，有界对账；不得重发 |

规则：

- 查询必须持久化、跨重启可用；不得只查内存缓存。
- 返回的 `payload_hash` 必须与提交时的 JCS 规范化 payload 一致。
- `not_found` 不是失败证明；执行边界必须显式声明“查询窗口内无副作用”才能重试。

### 3.3 `get_status(runtime_task_id)`

响应：

```json
{
  "runtime_task_id": "rtask_hermes_0001",
  "state": "running",
  "external_state": "executing",
  "observed_at": "2026-10-20T02:01:00Z",
  "heartbeat_at": "2026-10-20T02:00:55Z",
  "progress": {"completed_steps": 2, "total_steps": 5},
  "stop_confirmed": false,
  "input_required": false,
  "error": null
}
```

`state` 是 Adapter 映射后的稳定枚举：

| `state` | 含义 |
|---|---|
| `accepted` | 已受理，尚未确认开始 |
| `running` | 正在执行 |
| `waiting_input` | 运行时请求输入；是否转 `needs_human` 由核心决定 |
| `completed` | 运行时认为执行结束；**不等于验证通过** |
| `failed` | 明确失败 |
| `stopped` | 已确认停止，且不再产生副作用 |
| `timed_out` | 运行时确认超时并已停止 |
| `unknown` | 无法查询、响应不可解析、心跳丢失或状态矛盾 |
| `not_found` | 查询明确返回不存在；核心按 `unknown` 处理并进入对账，不得直接判定成功、失败或可重派。仅当 `attempt=0`、有界窗口结束且执行边界出具无副作用保证时可经 T71；否则经 T55/T73 转失败或人工 |

`stop_confirmed=true` 只能由 Adapter 在获得运行时停止证据后设置；`cancel` 被受理不等于已停止。**若核心当前处于 `unknown`，`state=running` 不得被映射为恢复执行（原 T53 已撤销）。** Adapter 必须先执行 `cancel_task` 并取得 `stop_confirmed=true` 证据；核心保持 `unknown` 直到停止得到确认，再按 T56/T58/T72 处理。

### 3.4 `poll_events(runtime_task_id, cursor, limit)`

响应：

```json
{
  "events": [
    {
      "event_id": "rtevt_0001",
      "occurred_at": "2026-10-20T02:00:30Z",
      "type": "progress",
      "payload": {"step": "read_record"},
      "redacted": true
    }
  ],
  "next_cursor": "cur_0002",
  "has_more": false,
  "error": null
}
```

- 事件必须按运行时顺序稳定返回；重复拉取同一 cursor 必须返回相同事件。
- `redacted=true` 表示事件已去除敏感字段；核心不得要求 Adapter 回传凭据或完整个人数据。
- 事件只是证据来源之一，不能替代 `get_result` 与核心验证。

### 3.5 `subscribe_events(runtime_task_id, cursor)`

若支持订阅，必须提供：

- 断线重连与 cursor 恢复；
- 至少一次投递与去重键（`event_id`）；
- 明确的流结束/错误语义；
- 禁止把订阅存在当作 `task.result` 已可用。

不支持时返回 `unsupported`，核心降级到轮询，并在运行时不兼容报告中记录。

### 3.6 `provide_input(runtime_task_id, input_request_id, payload_hash, payload)`

响应 `accepted | rejected | expired | unsupported | unknown`。

- 必须绑定 `input_request_id` 与 `payload_hash`，防止回答错配或重放。
- 相同 `input_request_id` 的重复提交必须幂等；payload 不同则拒绝。
- 输入不得包含凭据；需要秘密时由运行时凭据注入机制处理。

### 3.7 `cancel_task(runtime_task_id, reason, idempotency_key)`

响应：

```json
{
  "outcome": "requested",
  "stop_confirmed": false,
  "observed_at": "2026-10-20T02:05:00Z",
  "error": null
}
```

`outcome`：

| 值 | 语义 | 核心动作 |
|---|---|---|
| `requested` | 取消请求已送达，尚未确认停止 | 状态 `cancel_requested`，继续查询 |
| `already_terminal` | 任务已结束 | 查询结果并进入 `verifying` 或对应终态 |
| `stopped` | 已确认停止且不再产生副作用 | 可直接进入 `cancelled`（仍需对账） |
| `unsupported` | 运行时不支持取消 | 记录缺口；升级 `needs_human`，不得谎报已停止 |
| `unknown` | 无法确认是否送达或是否停止 | 状态 `unknown`/`needs_human` |

**取消请求与实际停止是不同事实。** 核心只能在 `stop_confirmed=true` 或等价证据后进入 `cancelled`；`cancel_requested` 不得被展示为“已停止”。

### 3.8 `get_result(runtime_task_id)`

响应：

```json
{
  "outcome": "succeeded",
  "result_ref": "result_expense_0001",
  "summary": "Synthetic expense submitted.",
  "structured_payload": {"record_id": "res_expense_0001"},
  "artifacts": [
    {"artifact_id": "art_0001", "kind": "json", "sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000", "ref": "evidence:art_0001"}
  ],
  "evidence_refs": ["evidence:evt_0001"],
  "verification_hints": ["record state == submitted"],
  "stop_confirmed": false,
  "completed_at": "2026-10-20T02:10:00Z",
  "error": null
}
```

`outcome` 与核心状态映射：

| Adapter `outcome` | 核心动作 |
|---|---|
| `succeeded` | T36/T50 → `verifying`；绝不直接 `succeeded`。若任务当前处于 `unknown`，结果不得直接进入验证，按 T73 转 `needs_human` |
| `partial_success` | → `verifying`，按 `completion_criteria` 判定 `verification.partial`。`unknown` 状态下的部分结果同样按 T73 转人工 |
| `failed` | T37/T55 → `failed`（确认已停止，且到达 `failed` 时 `attempt>=1` 或来自预派发失败事件）；是否重试由 T65/T70 决定 |
| `cancelled` | `stop_confirmed=true` → `cancel_requested`/`cancelled`（T48/T56）；否则 `unknown`（T51） |
| `timed_out` | `stop_confirmed=true` → `timed_out`（T38/T58）；否则 `unknown` |
| `needs_human` | → `needs_human`（T40/T45/T52/T57/T72/T73） |
| `unknown` | → `unknown`（T41/T46/T51/T57）；进入时立即撤销 Grant（`execution_evidence_lost`）；先对账，不重发，不恢复执行 |

规则：

- **运行时的“我完成了”不是完成定义。** 核心必须按 `goal.completion_criteria` 做确定性验证，证据不足进入 `verifying → needs_human/failed`。
- `result_ref` 与至少一个 `evidence_refs` 是进入 `verifying` 的最低要求；`succeeded` 还要求 `verification.result=passed`。
- `stop_confirmed` 只能由执行边界在获得停止证据后设置；取消被受理不等于已停止。
- `artifacts[].ref` 只能是受控引用；不得把绝对路径、运行时私有 URL 或凭据放进核心合同。
- 结构化结果必须区分“业务结果”和“过程证据”；不得把原始日志当作业务结果。
- 结果缺失或不可解析时返回 `unknown`，核心进入对账，不得默认成功。

### 3.9 `health()`

```json
{
  "status": "healthy",
  "checked_at": "2026-10-20T02:00:00Z",
  "details": {"queue_depth": 0, "auth_ok": true, "last_error_code": null}
}
```

`status`：`healthy | degraded | unavailable`。`unavailable` 时核心不得提交新任务；已提交任务进入 `unknown` 对账流程。

### 3.10 错误模型

统一错误码至少包含：

`unauthorized`、`forbidden`、`not_found`、`conflict`、`invalid_request`、`unsupported`、`rate_limited`、`timeout`、`unavailable`、`internal`、`unknown`。

错误响应不得包含令牌、凭据、完整个人数据、运行时内部路径或堆栈。`timeout`/`unknown` 不得被调用方解释为“未执行”或“已执行”。

## 4. 状态映射

Adapter 把外部状态映射为内部事实；核心再把这些事实映射为模式中立的 `dispatch.*` / `execution.*` 事件并写入 [task-state-machine](../architecture/task-state-machine.md)。本表只适用于 **Managed Execution / B3**；Advisory（B1）与 Governed（B2）由受控调用/PEP 产生同名 `dispatch.*` / `execution.*` 事件。受理事件的唯一规范名称是 `dispatch.accepted`（不存在 `execution.accepted`）；本表中 `dispatch.accepted` 的 B3 产生者是 Worker/Adapter，B1/B2 的对应产生者分别是 B1 PEP 与 B2 PEP。

| Adapter 事实 | 核心事件 | 核心状态 | 说明 |
|---|---|---|---|
| submit `accepted` / status `accepted` | `dispatch.accepted` | `dispatched` | 已绑定 `rtask_` 句柄，尚未确认运行 |
| status `running` | `execution.started` | `running` | 有新鲜心跳或可验证执行证据；**仅限非 `unknown` 任务**。`unknown` 任务必须先停止并取得停止证据，不得经本映射恢复 |
| status `waiting_input` | `human.escalation_required` 或保持 | `running` 或 `needs_human` | 有授权自动回答源则保持运行；否则需人工输入 |
| status `completed` + `get_result` 可取 | `execution.completed` | `verifying` | 绝不直接 `succeeded` |
| status `failed` | `execution.failed` | `failed` | 明确失败且已停止；是否重试由 T65/T70 决定 |
| status `stopped` + `stop_confirmed` | `execution.stopped` | `cancelled` | 仍需对账副作用 |
| status `timed_out` + `stop_confirmed` | `execution.timed_out` | `timed_out` | 未确认停止时保持 `unknown` |
| status `unknown` / `not_found` / 查询超时 | `execution.lost` | `unknown` | 进入即撤销 Grant；先对账，不重发创建请求；仍在运行必须先停止 |
| cancel `requested` | `task.cancel_requested` | `cancel_requested` | 不是停止 |
| cancel `already_terminal` | 按结果走 `execution.completed` / 对应终态 | `verifying` / 终态 | 按结果对账，不得丢弃已完成结果 |
| submit `rejected` | `dispatch.rejected` | `failed` | 确认无副作用；不自动重试 |
| submit `unsupported` | `dispatch.unsupported` | `needs_human` | 记录能力缺口，不自动重试 |
| submit `timeout` | `dispatch.timeout` | `unknown` | 先 `find_by_idempotency_key` |
| submit `unknown` | `dispatch.unknown` | `unknown` | 先查询，不猜测 |

同一个外部状态在不同运行时可能含义不同；Adapter 必须按本表显式映射，不能把供应商状态字符串直接透传给核心。Adapter 的 `get_status.state` 与 `get_result.outcome` 枚举必须与本节及状态机一致；新增枚举值必须升次版本并同步 fixtures。

**`needs_human` 之后的恢复语义（T61）。** Adapter 只报告事实，不决定恢复路径：

- **尝试仍未关闭。** 进入 `unknown` 只撤销 Grant，不关闭正在进行的尝试；进入 `needs_human` 也不关闭它。尝试只有在取得停止证据（`execution.stopped` / `execution.failed` / `execution.timed_out` 且 `stop_confirmed=true`）、`reconcile.stopped`/`failed`/`timed_out`/`not_found`，或执行结束后的 `verification.failed` 时才关闭。未关闭且无 `not_found` 证据的尝试是**未对账的 active attempt**。
- **`unknown` 来源的 `needs_human` 禁止 T59/T60。** 该状态的 Grant 已 `revoked`；Adapter 即使查询到 `state=running` 或拿到结果，也不得映射为 `running`/`verifying`（状态机 F41）。Adapter 必须先 `cancel_task` 并取得 `stop_confirmed=true`，再由核心按 T61/T62/T63/T64/T70 处理。
- **T61 重派需要停止证据或“从未开始”证明。** 只有当 `stop_confirmed=true`（已关闭尝试），或 Adapter 能按幂等键证明从未受理（`attempt=0` 且 `not_found` + 无副作用保证）时，核心才允许 T61；否则不得重新入队，只能继续对账或转人工。
- **重试预算的余额在 T65 递减后记录。** Adapter 不解释预算，只透传核心写入的 `attempt.retry_budget_remaining`；最后一次允许的重试该值为 0。

## 5. 超时、重试与幂等

### 5.1 提交超时

提交超时后：

1. **禁止盲目重发创建请求。**
2. 先调用 `find_by_idempotency_key(task_id, grant_id, idempotency_key)`；若已获得 `runtime_task_id`，同时调用 `get_status`。
3. 找到任务：采用该任务，按实际事实更新；即使 payload 相同也不得再创建第二个执行任务。
4. 明确不存在且执行边界出具无副作用保证、且任务 `attempt=0`：只能经 T71（`reconcile.not_found` → `queued`）重新执行 PDP、签发新决策与新 Grant 后提交；不得直接重发。`attempt>=1` 或无法取得该保证时经 T55 进入 `failed`，再由 T65 决定是否重试（`attempt=0` 时只能 T70 转人工）。
5. 无法确定：状态 `unknown`，进入有界对账；进入时立即撤销 Grant。超过阈值转 `needs_human`（该来源的 T59/T60 不可用，F41；只能先停止取证再 T61，或 T62/T63/T64/T70）。
6. 同键不同 payload：`conflict`，进入 `needs_human` 对账，不得覆盖或重发。
7. **不得从 `unknown` 恢复执行。** 若对账发现运行时仍在运行，必须先 `cancel_task` 并取得 `stop_confirmed=true`；停止未确认时保持 `unknown`/`needs_human`，不得把 `state=running` 直接映射回核心 `running`。

### 5.2 查询与事件失败

- 连续查询失败达到阈值 → `unknown`，不是 `failed`。
- 事件缺口不等于任务失败；必须用状态/结果接口交叉验证。
- 对账必须有上限与审计；不得无限轮询或静默丢弃。

### 5.3 侧效应去重

- Adapter 在调用运行时前持久化 `(task_id, idempotency_key, payload_hash, call_id)` 的“已发出”状态。
- 业务工具 handlers 必须幂等或提供唯一约束；重复请求返回同一结果或明确冲突。
- 运行时重启、Worker 租约过期或网络重试不得产生第二次不可逆副作用。
- 无法证明幂等的动作不得自动重试；必须 `needs_human`。
- `find_by_idempotency_key` 必须能在 Adapter 重启后返回同一 `runtime_task_id`；查询结果与提交时的 `payload_hash` 一起持久化。

### 5.4 取消竞态

- 取消请求与完成事件可能交叉：以运行时事实和结果证据为准。
- 若取消送达前任务已完成：进入 `verifying`，不得因取消请求把成功结果丢弃。
- 若取消已受理但停止未确认：保持 `cancel_requested`；超过阈值转 `unknown`/`needs_human`。
- 核心不得在 `cancel_requested` 状态下继续签发新 Grant 或调用新工具。

## 6. 安全要求

1. **独立凭据**：Adapter 使用独立 workload identity；运行时使用任务级短时凭据。禁止复用 B1/B2/B4 令牌。
2. **最小权限**：Adapter 只能提交/查询/取消被授权的任务；禁止任意列出其他任务、读取其他用户结果或访问运行时管理数据库。
3. **凭据注入**：秘密只注入执行环境，不进入提示词、任务输入、事件、Trace、Git 或普通日志。
4. **隔离验证**：Managed Execution 必须验证独立 UID/服务账户、文件系统边界、临时目录、网络出口和取消/终止行为。未验证时能力状态为 `unverified`。
5. **审计**：记录 adapter 调用、运行时任务 ID、Grant ID、策略/工具版本、结果和错误码；拒绝与失败同样记录，但避免泄漏受保护数据。
6. **版本绑定**：Adapter 契约、工具契约、策略版本变化必须使旧 Grant 失效或进入显式兼容窗口。
7. **无私有对象泄漏**：核心不得出现 Hermes/Rakazo 的 Profile 路径、数据库表名、内部会话 ID 或私有文件结构。

## 7. 运行时能力现状

| 运行时 | 定位 | `task.submit` | `task.status` | `task.events` | `task.cancel` | `task.result` | 隔离/任务级凭据 | 总体状态 |
|---|---|---|---|---|---|---|---|---|
| **Hermes** | 首个候选；现有兼容实现 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | `unverified` |
| **Rakazo** | 第二候选；先只读 Spike | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | `unverified` |
| 其他 | 未评估 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | 未验证 | `unverified` |

说明：

- 表中“未验证”表示 Phase 0 没有真实契约测试证据；现有 Hermes 兼容实现不等于 Adapter 已验证。
- Rakazo 在只读兼容性 Spike 和稳定接口契约测试通过前，不开发适配器；若适配需要修改核心业务/授权/状态机，应先复审契约设计。
- 任何运行时不得因为存在 MCP Server、命令行工具或浏览器能力而被推断为通用远程执行入口。
- 能力矩阵必须在 Phase 4/6 用真实测试结果更新，并附 `evidence_ref`、运行时版本、命令与退出码。

## 8. 契约测试要求

Adapter 验收必须包含：

1. **提交幂等**：重复 `submit_task` 只创建一个运行时任务并返回同一句柄；同键不同 payload 返回 `conflict`。
2. **提交超时**：模拟超时后用 `find_by_idempotency_key` 查到已创建任务，核心采用而非重发；查询不到且确认无副作用时才允许重试。
3. **状态映射**：每个外部状态映射到本文枚举与状态机事件；未知状态返回 `unknown`；`succeeded` 绝不直接进入核心 `succeeded`。
4. **结果映射**：`cancelled`/`timed_out`/`needs_human`/`unknown` 按 §3.8 表映射，`stop_confirmed` 缺失时不得进入 `cancelled`/`timed_out`。
5. **取消语义**：`requested` 不产生 `cancelled`；只有 `stop_confirmed` 才允许。
6. **结果验证**：运行时声称完成但证据不足时不得进入 `succeeded`。
7. **输入幂等**：重复/错配 `input_request_id` 被拒绝。
8. **权限负向**：越权任务、跨任务结果、未授权取消全部拒绝。
9. **模式负向**：提交的 `execution_mode`/capabilities/scope 与 Grant 不一致时拒绝；Advisory/Governed 任务不得被要求具备运行时能力。
10. **未知字段**：请求/响应中的未知字段被拒绝，不得静默忽略。
11. **隔离负向**：读取其他任务目录、未授权网络、访问宿主凭据全部失败（Managed Execution）。
12. **凭据与日志**：日志/事件/错误中无令牌、密码、API Key、完整个人数据。
13. **健康降级**：运行时不可用时不再提交新任务，已提交任务进入 `unknown` 对账。
14. **终态/未知 Grant**：任务已进入终态或 `unknown` 后，用同一 `grant_id` 继续提交/调用必须被拒绝；进入 `unknown` 时 Grant 必须在同一事务撤销（`execution_evidence_lost`），且不得复活或复用；Adapter 只报告事实，由核心撤销 Grant 并拒绝放行。

Mock 测试只能证明 Adapter 与契约映射，不得作为运行时兼容性证据；真实运行时证据必须单独记录。

## 9. 版本与兼容

- 本文与三个 JSON Schema 同属契约包；破坏性变更升主版本，并同步更新 fixtures、状态机与威胁模型。
- Adapter 必须声明 `adapter_contract_version`；核心拒绝不兼容主版本。
- 运行时升级后必须重新运行共享契约测试；能力状态和限制更新前保持原标记。
- 新增可选能力属于次版本；删除、改义或改变失败语义属于主版本。
