# 任务状态机（v1.0.0）

- 状态：Phase 0 冻结候选；产品负责人确认前为 Proposed
- 契约版本：1.0.0（尚未冻结发布的候选版本）
- 关联：Issue #77、[ADR-001](ADR-001-product-boundary.md)、[task-envelope](../contracts/task-envelope.schema.json)、[execution-grant](../contracts/execution-grant.schema.json)、[runtime-adapter](../contracts/runtime-adapter.md)、[threat-model](../security/threat-model.md)

## 1. 原则

1. **服务端唯一写状态。** 只有核心任务服务在验证事件、Grant、责任与前置条件后写状态并追加 task event；Runtime Adapter、客户端和模型只报告事实或提出请求。
2. **状态不是模型自述。** Agent 的“我完成了”“我已取消”不构成状态证据。
3. **取消请求 ≠ 已停止。** `cancel_requested` 只表示请求已登记；`cancelled` 需要停止证据与副作用对账。从未 dispatch 的任务，`stop_confirmed=true` 表示“已确认未开始执行”，不是“已停止进程”。
4. **完成必须验证。** 进入 `succeeded` 之前必须经过 `verifying`；不得从 `running`、`unknown` 或 `cancel_requested` 直接成功。完成与否只由结构化完成条件、结果引用、证据引用和验证结果决定。
5. **失败关闭。** 身份、策略、Grant、版本、责任或运行时事实无法确定时，进入 `denied`、`unknown` 或 `needs_human`，不得猜测成功。
6. **优先级不越权。** 优先级变化只影响队列排序、SLA 与升级，不产生新的状态转换权限。
7. **幂等与版本化。** 每次转换都带 `state_revision`，旧版本写入被拒绝；重复事件不重复产生副作用。
8. **模式中立。** 状态机描述三种执行模式共用的任务生命周期。执行事实由当前模式对应的执行边界产生，不把 Runtime Adapter 当成唯一执行边界。

## 2. 状态

| 状态 | 含义 | 终态 | 可自动离开 |
|---|---|---|---|
| `draft` | 已接收但尚未评估；字段可能不完整 | 否 | 是 |
| `evaluated` | PDP 已给出机器可判定决策 | 否 | 是 |
| `awaiting_clarification` | 等待请求方补充目标、输入、范围或责任人 | 否 | 是（需新输入） |
| `awaiting_confirmation` | 等待受影响用户对具体 payload 确认 | 否 | 是（需有效确认） |
| `awaiting_approval` | 等待独立审批人审批 | 否 | 是（需有效审批） |
| `queued` | 已获授权，等待 Worker 调度或执行边界受理 | 否 | 是 |
| `dispatched` | 执行边界已受理，尚未确认开始执行 | 否 | 是 |
| `running` | 执行边界有开始/运行证据 | 否 | 是 |
| `verifying` | 执行已结束，核心正在验证结果与证据 | 否 | 是 |
| `succeeded` | 验证通过且证据完整 | **是** | 否 |
| `denied` | 策略、身份、审批或安全边界拒绝 | **是** | 否 |
| `cancel_requested` | 取消已登记，尚未确认停止 | 否 | 是（需停止/对账证据） |
| `cancelled` | 已确认停止（或确认从未开始）且副作用已对账 | **是** | 否 |
| `failed` | 本次执行尝试失败 | **尝试终态** | 仅 T65/T70 |
| `timed_out` | 已确认超时且执行边界关闭 | **是** | 否 |
| `needs_human` | 需要人工接管、裁决或补充证据 | 否 | 是（人工动作） |
| `partial_success` | 仅部分完成定义通过且有证据 | **是** | 否（新任务另立） |
| `unknown` | 无法确定执行边界事实，等待对账；进入时 Grant 立即撤销 | 否 | 是（对账后：T55–T58、T71–T73；不得直接恢复执行） |

`unknown` 和 `needs_human` **不是终态**；不得把它们当作成功、失败或取消的替代。`failed` 是尝试终态，不是任务终态：只能在有界预算内重新评估并重试，或转人工。`unknown` 采用 **fail-closed** 语义：进入 `unknown` 的同一事务内撤销当前 Grant（`reason_code=execution_evidence_lost`），撤销后的 Grant 永不复活、永不复用；恢复只能通过新决策 + 新 Grant（T71/T65）或转人工/终止（T56/T57/T58/T72/T73）。

## 3. 执行边界与模式（模式中立）

任务状态机不假设一定存在服务器端运行时。三种执行模式分别由不同的执行边界产生 `dispatch.*`（受理/提交）与 `execution.*`（受理后）事实：

| 模式 | 信任边界 | `dispatch.accepted` | `execution.started` | `execution.completed` | `execution.stopped` | 能力守卫 | 必须记录的剩余风险 |
|---|---|---|---|---|---|---|---|
| **Advisory / BYOA** | B1 客户端 MCP/REST + 受控业务工具 | 首个受治理调用被允许 | 首个受治理调用返回结果 | 客户端通过认证治理接口报告完成，或最后一次受治理调用结束 | 客户端报告停止且无新的受治理调用 | 不要求运行时能力；要求受控工具能力可解析 | `client_local_tools_uncontrolled`、`advisory_no_local_enforcement` |
| **Governed Resource Access** | B2 PEP / 连接器 | PEP 受理首个受控资源访问 | 首次成功资源动作 | 客户端报告完成或连接器/任务工具报告结束 | PEP 确认无新动作且连接已关闭 | 连接器/PEP 能力必须 `verified`；部署必须证明直连被阻断 | `governed_bypass_possible`（若无法证明阻断） |
| **Managed Execution** | B3 Runtime Adapter / 隔离运行时 | Adapter `submit_task` 返回 `accepted` | 运行时报告运行证据 | 运行时 `get_result` 可取 | runtime `stop_confirmed=true` | `task.submit`/`task.status` 必须 `verified`；宣称隔离时 `runtime.isolation` 与 `runtime.task_scoped_credentials` 必须 `verified` | `runtime_isolation_unverified`（Phase 5 前）、事件能力缺失 |

**“受理”只有一个规范事件名：`dispatch.accepted`。** 契约包中不存在 `execution.accepted`；任何实现、适配器映射或测试引用该名称都属于契约不一致。`dispatch.accepted` 的产生者按边界固定：

- **B1（Advisory）**：B1 受治理调用入口在首个受治理调用被授权时写入，并绑定 `execution_ref`。
- **B2（Governed）**：B2 PEP 在受理首个受控资源动作时写入，并绑定 `execution_ref`。
- **B3（Managed）**：Worker 在 Runtime Adapter `submit_task` 返回 `accepted` 时写入，并绑定 `runtime_task_id`。

`execution.*` 只由对应执行边界在受理之后产生，产生者必须是表中所列的同一 B1/B2/B3 边界。

规则：

- **模式只能由服务端决策确定。** 客户端偏好只能降级，不能自选为更强模式；升级必须重新评估、重新签发 Grant。
- **能力守卫按模式求值。** 不得要求 Advisory/Governed 任务具备 Runtime Adapter 能力，也不得因为 Managed Execution 能力已验证就推断客户端本地工具已被治理。
- **事件产生者必须记录在事件中。** 每个 `execution.*` 事件包含 `boundary`（`b1_client` / `b2_pep` / `b3_runtime`）、`task_id`、`grant_id`、`state_revision`、`evidence_ref` 与时间戳。
- **空集合不是全量。** 资源范围、数据分类和能力为空都表示“没有”，不是“全部”。`undetermined` 只允许出现在 `pre_execution_incomplete` 记录（`draft`、`awaiting_clarification`，以及执行前退出的 `denied`/`cancelled`/`needs_human`/`failed`）或 `decision_type=needs_clarification` 的 `evaluated` 记录。
- **记录完整性显式标注。** `record_completeness.completeness=pre_execution_incomplete` 必须带 `incomplete_reason` 且不得关联 Grant（`linkage.grant_id=null`、`attempt=0`）；它只允许出现在执行前状态：`draft`、`awaiting_clarification`、`decision_type=needs_clarification` 的 `evaluated`、执行前的 `cancel_requested`，以及执行前退出的 `denied`/`cancelled`/`needs_human`/`failed`。一旦开始执行（`attempt>=1`），或进入 `queued`/`dispatched`/`running`/`verifying`/`succeeded`/`partial_success`/`timed_out`/`unknown`/`awaiting_confirmation`/`awaiting_approval`，记录必须是 `complete`，即 `goal` 非空、`accountable_owner` 已解析、`execution_mode` 与 `scope_type` 均已确定。
- **`attempt` 只统计已开始的执行。** 首次 `queued` 的 `status.attempt=0`；只有 `dispatch.accepted`（T23）递增 `attempt`。预派发失败（`attempt=0`）不消耗 `retry_budget_remaining`，也不能走 T65；`attempt=0` 的人工“失败”必须登记为 `cancelled` 或 `denied`，不得进入 `failed` 后用 T64/T70 循环。`execution-grant.attempt.number` 是 Grant 授权的那次尝试序号：签发时为 `status.attempt + 1`，`dispatch.accepted` 后与 `status.attempt` 相等。
- **尝试的生命周期（unknown / needs_human 之后）。** 一次尝试在 `dispatch.accepted` 时**打开**，只有取得关闭证据时才**关闭**：`execution.stopped`、`execution.failed`、`execution.timed_out`（三者均要求 `stop_confirmed=true`）、`reconcile.stopped`/`reconcile.failed`/`reconcile.timed_out`/`reconcile.not_found`，或 `verifying` 中的 `verification.failed`（执行已结束）。**进入 `unknown` 不关闭尝试**：它只撤销 Grant 并标记执行证据丢失，尝试保持打开、等待对账。**进入 `needs_human` 也不关闭尝试**：只有 `human.stop_confirmed`（T63）或带 `stop_confirmed=true` 的对账/人工事件才关闭它。只要不存在关闭证据，该尝试就是**未对账的 active attempt**。`status.attempt` 表示“已打开过的尝试次数”，因此即使尝试仍打开也保持原值；T33 是唯一例外（作废一次从未开始的受理并回退 `attempt`）。未对账的 active attempt 阻止 T61；`attempt=0`（从未 `dispatch.accepted`）是“证明该次执行从未开始”的唯一 Schema 可判定形式。
- **Owner 必须可追责且状态有效。** `queued`/`dispatched`/`running`/`verifying`/`awaiting_confirmation`/`awaiting_approval` 要求 `accountable_owner` 已解析且 `account_status=active`，且不得携带 `owner_escalation`。终态/历史记录（`succeeded`、`partial_success`、`denied`、`cancelled`、`timed_out`、`failed`）允许 Owner 已解析但 `disabled`/`unbound`/`unknown`，但**必须**带 `owner_escalation`（`reason_code`、`recorded_at`、`escalated_to_principal_id`）；Schema 已把该字段设为条件必填，不得静默允许。`awaiting_clarification` 属于 A1 的执行前不完整记录，仍允许 Owner 未解析；一旦离开该状态进入门禁/执行，必须先解析并校验 active。

## 4. 事件类型（规范性、穷尽）

所有转换只使用下表事件；新增事件必须升次版本并同步 Schema/fixtures/实现。表中“产生者”是唯一允许写入该事件的主体。

| 事件 | 产生者 | 语义 |
|---|---|---|
| `task.created` | 核心服务 | 创建 draft 任务 |
| `task.evaluated` | 核心服务 | PDP 决策已持久化并应用 |
| `task.queued` | 核心服务 | 授权、门禁与责任检查通过 |
| `task.cancel_requested` | 核心服务 | 取消请求已登记 |
| `task.cancelled` | 核心服务 | 未 dispatch 的任务取消并确认无副作用 |
| `task.deadline_exceeded` | Worker | 队列/SLA 截止到达且从未 dispatch |
| `task.retry_authorized` | 核心服务 | 重试已通过新决策、新 Grant 与预算检查 |
| `task.retry_exhausted` | 核心服务 | 尝试或重试预算耗尽，转人工 |
| `governance.denied` | 核心服务 / PDP | 拒绝决策或 fail-closed |
| `governance.clarification_required` | 核心服务 | 决策要求澄清 |
| `governance.confirmation_required` | 核心服务 | 决策要求确认 |
| `governance.approval_required` | 核心服务 | 决策要求审批 |
| `clarification.provided` | 核心服务 | 请求方补充了输入，需要重新评估 |
| `confirmation.recorded` | 核心服务 | 有效确认已记录 |
| `confirmation.rejected` | 核心服务 | 确认被拒绝、过期或 payload 变化 |
| `approval.recorded` | 核心服务 | 有效独立审批已记录 |
| `approval.rejected` | 核心服务 | 审批被拒绝、过期、撤销或自审 |
| `worker.leased` | Worker | 获得未过期租约与 fencing token |
| `worker.lease_expired` | Worker | 租约过期，且查询确认未受理/无副作用 |
| `dispatch.accepted` | B1 PEP / B2 PEP / Worker（B3 提交） | 执行边界明确受理；受理事件的唯一规范名称 |
| `dispatch.rejected` | Worker / Adapter | 提交被明确拒绝，确认无副作用 |
| `dispatch.unsupported` | Worker / Adapter | 运行时不支持该能力/模式 |
| `dispatch.unavailable` | Worker / Adapter | 无可用 Adapter 或配置不可恢复 |
| `dispatch.timeout` | Worker / Adapter | 提交超时，是否受理未知 |
| `dispatch.unknown` | Worker / Adapter | 提交响应不可解析或连接中断，是否受理未知 |
| `dispatch.deadline_exceeded` | Worker | 提交窗口截止且确认无运行时任务 |
| `execution.started` | 执行边界（B1 PEP / B2 PEP / B3 Runtime Adapter） | 有开始/运行证据 |
| `execution.completed` | 执行边界（B1/B2/B3） | 执行结束，结果可取 |
| `execution.failed` | 执行边界（B1/B2/B3） | 明确失败且已停止 |
| `execution.timed_out` | 执行边界（B1/B2/B3） | 确认超时且已停止 |
| `execution.lost` | 执行边界（B1/B2/B3）/ Worker | 心跳或查询超阈值，状态未知 |
| `execution.stopped` | 执行边界（B1/B2/B3） | 确认停止且不再产生副作用 |
| `verification.passed` | 核心验证器 | 全部结构化完成条件通过 |
| `verification.partial` | 核心验证器 | 只有策略允许的子集通过 |
| `verification.failed` | 核心验证器 | 验证不通过且无重试价值 |
| `verification.inconclusive` | 核心验证器 | 证据不足/冲突 |
| `verification.evidence_unavailable` | 核心验证器 | 证据源不可用 |
| `reconcile.running` | Worker / 人工 | 对账取得运行证据；必须立即请求停止并取证，不得恢复执行；停止未确认时经 T72 转人工 |
| `reconcile.completed` | Worker / 人工 | 对账取得结果；任务已处于 `unknown` 且 Grant 已撤销，不得直接进入验证；经 T73 转人工裁决 |
| `reconcile.failed` | Worker / 人工 | 对账确认失败且已停止 |
| `reconcile.not_found` | Worker / 人工 | 对账在有限窗口内确认执行边界从未受理，且执行边界出具无副作用保证 |
| `reconcile.stopped` | Worker / 人工 | 对账确认停止 |
| `reconcile.timed_out` | Worker / 人工 | 对账确认超时且已停止 |
| `reconcile.unresolved` | Worker / 人工 | 对账耗尽或证据冲突 |
| `human.escalation_required` | 核心服务 / 管理面 | 需要人工接管 |
| `human.takeover.running` | 管理面 | 人工确认任务确实在运行 |
| `human.result_submitted` | 管理面 | 人工提交结构化结果与证据 |
| `human.redispatch` | 管理面 | 人工决定重派 |
| `human.authorization_revoked` | 管理面 / 策略 | 人工或策略撤销授权 |
| `human.stop_confirmed` | 管理面 | 人工确认已停止且无新副作用 |
| `human.declared_failed` | 管理面 | 人工判定失败且不再自动重试 |

`dispatch.*` 只描述**提交阶段**事实；`execution.*` 只描述**受理后**事实。两者不得混用。Adapter 的供应商状态必须显式映射到这些事件（见 [runtime-adapter](../contracts/runtime-adapter.md) §4）。

事件表的“产生者”列是规范约束，不是示例：表中未列出的主体不得写入该事件；新增事件必须升次版本并同步 Schema/fixtures/实现。受理事件只有 `dispatch.accepted`（不存在 `execution.accepted`）。`reconcile.*` 与 `human.*` 由对账 Worker 或管理面在取得证据后写入，不得由模型或客户端触发。

## 5. 通用守卫

除状态特定守卫外，每次转换必须满足：

- 调用者已认证且不是停用/解绑主体；
- 任务 `state_revision` 与事件预期版本一致；
- 策略版本、Grant、工具契约版本在有效期内且匹配；
- 责任字段完整：`accountable_owner` 已解析为人员/组织角色；`approver` 在需要审批时独立且不是 Accountable Owner 本人；**职责分离必须检查 `approvals[]` 中所有 `decision=approved` 的实际 `approver_principal_id`，以及 `execution-grant.approval_ref.approver_principal_id`**，不能只检查任务级 `approver` 字段；`role_` 审批人必须解析为实际行为人后再比较；
- 记录完整性与状态一致：`pre_execution_incomplete` 记录不得关联 Grant 且 `attempt=0`；`attempt>=1` 与所有执行后状态必须是 `complete` 记录（执行前的 `cancel_requested` 可保持不完整）；
- `decision.decision_type` 与当前授权一致；授权型决策必须绑定非空 `task_id` 与 `task_revision`；
- **终态与 `unknown` 必须使 Grant 失效**：任务或尝试进入 `succeeded`/`partial_success`/`failed`/`cancelled`/`timed_out`/`denied` 时，Grant 必须为 `revoked` 或 `consumed`（`task_terminal` / `attempt_failed`）；进入 `unknown` 时必须在同一事务内立即 `revoked`（`reason_code=execution_evidence_lost`）。撤销后的 Grant 永不复活、永不复用；T71/T65 必须签发新的 `grant_id` 与新决策。PEP 除校验 Grant 外**还必须读取当前任务状态**，在终态或 `unknown` 任务上的 active Grant 一律拒绝；
- 主体前缀与类型一致：`prin_`↔person/agent_instance、`rt_`↔runtime、`svc_`↔service、`role_`↔role、`queue_`↔queue；`role_`/`queue_` 不得成为被认证 subject、Requester 或 Grant 执行主体；
- 事件幂等键未被消费，或重复事件返回原结果；
- 写入 task event 与审计事件在同一事务内完成（Outbox 至少一次投递）。

## 6. 允许的转换（共 72 条，含初始创建）

> 编号 T00–T73 是稳定标识，**T53/T54 已撤销并保留空号，不得重新使用**（原因见 §6.12）：`unknown` 不得直接恢复到 `running`/`verifying`。因此 T00–T73 中实际有效的允许转换是 72 条。
>
> 表格中的“Grant”均指当前任务绑定的、未撤销、未过期、范围/版本匹配、且 `decision.decision_hash` 与 `task_revision` 匹配的 Execution Grant；除 T29 明确允许“由本尝试消费的 `consumed`”之外，进入 `queued`/`dispatched` 必须是 `status=active` 且未消费（消费时点见 §8.3）。任何 Grant 校验失败都不得进入 `queued` 或 `dispatched`。**已 `revoked` 的 Grant 是终态，不得改回 `active` 或在恢复流程中复用。**

### 6.1 初始创建

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T00 | `(none)` → `draft` | `task.created` | 认证 Requester 存在；幂等键首次使用或相同 payload；目标可缺失，但必须记录 `missing_fields` |

### 6.2 从 `draft`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T01 | `draft` → `evaluated` | `task.evaluated` | 必填字段可被 PDP 读取；策略版本可解析；决策已持久化；`execution_mode` 与 `resource_scope` 已确定（`needs_clarification` 除外） |
| T02 | `draft` → `denied` | `governance.denied` | PDP 明确 deny，或策略服务不可用/版本无效导致 fail-closed deny |
| T03 | `draft` → `cancelled` | `task.cancelled` | Requester 或授权服务身份取消；从未 dispatch，无副作用；`stop_confirmed=true` 表示确认未开始 |

### 6.3 从 `evaluated`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T04 | `evaluated` → `awaiting_clarification` | `governance.clarification_required` | 决策 `needs_clarification`；缺失项可枚举 |
| T05 | `evaluated` → `awaiting_confirmation` | `governance.confirmation_required` | 决策 `needs_confirmation`；payload hash 已固定 |
| T06 | `evaluated` → `awaiting_approval` | `governance.approval_required` | 决策 `needs_approval`；Approver 已解析、独立、active，且不是 Accountable Owner |
| T07 | `evaluated` → `denied` | `governance.denied` | 决策 `deny`；记录稳定原因码 |
| T08 | `evaluated` → `queued` | `task.queued` | 决策 `allow` 或 `managed_execution_required` 且绑定当前 task/revision；Grant 已签发且 active；确认/审批门禁满足；owner/assignee/executor 完整；模式对应能力守卫通过（§3） |
| T09 | `evaluated` → `cancel_requested` | `task.cancel_requested` | 取消请求在入队前到达；幂等键有效 |
| T10 | `evaluated` → `needs_human` | `human.escalation_required` | 责任冲突、委派无效或策略异常无法自动消解；不得据此签发 Grant |

### 6.4 从 `awaiting_clarification`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T11 | `awaiting_clarification` → `evaluated` | `clarification.provided` | 新输入完整；payload hash 更新；重新走 PDP（不得沿用旧决策） |
| T12 | `awaiting_clarification` → `denied` | `governance.denied` | 澄清窗口/SLA 过期，或请求方明确撤回且策略要求拒绝；记录原因码 |
| T13 | `awaiting_clarification` → `cancelled` | `task.cancelled` | Requester 或授权服务身份取消；无副作用 |
| T14 | `awaiting_clarification` → `needs_human` | `human.escalation_required` | 无法确定澄清责任人，或澄清内容涉及安全边界 |

### 6.5 从 `awaiting_confirmation`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T15 | `awaiting_confirmation` → `evaluated` | `confirmation.recorded` | 确认 `status=satisfied`、未过期、未消费；payload hash 与当前 JCS 规范化 payload 一致；确认人有权确认；必须重新走 PDP |
| T16 | `awaiting_confirmation` → `denied` | `confirmation.rejected` | 确认被拒绝、过期或 payload 已变化且未重新确认 |
| T17 | `awaiting_confirmation` → `cancelled` | `task.cancelled` | Requester 取消；无副作用 |
| T18 | `awaiting_confirmation` → `needs_human` | `human.escalation_required` | 无可用确认人，或确认人与 Requester/Executor 冲突 |

### 6.6 从 `awaiting_approval`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T19 | `awaiting_approval` → `evaluated` | `approval.recorded` | 审批 `decision=approved`、`separation_check=passed`、未过期、payload hash 一致；Approver 独立于 Requester/Executor/Accountable Owner；必须重新走 PDP |
| T20 | `awaiting_approval` → `denied` | `approval.rejected` | 审批 `rejected`/`expired`/`revoked`，或检测到自审/代审；记录原因码 |
| T21 | `awaiting_approval` → `cancelled` | `task.cancelled` | Requester 取消；无副作用 |
| T22 | `awaiting_approval` → `needs_human` | `human.escalation_required` | 审批人不可用、审批链冲突或升级规则触发 |

### 6.7 从 `queued`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T23 | `queued` → `dispatched` | `dispatch.accepted` | Worker 持有未过期租约与 fencing token；Grant 每项绑定匹配；门禁满足；无其他活动 attempt；模式能力守卫通过；已绑定 `runtime_task_id`（Managed）或 `execution_ref`（Advisory/Governed）；**本转换为唯一 `attempt += 1` 时点，`grant.attempt.number` 必须等于递增后的 `status.attempt`** |
| T24 | `queued` → `cancel_requested` | `task.cancel_requested` | 取消请求在 dispatch 前到达；幂等 |
| T25 | `queued` → `denied` | `governance.denied` | Grant 被撤销、策略/审批在排队期间失效；未 dispatch，无副作用 |
| T26 | `queued` → `needs_human` | `human.escalation_required` | 超过排队 SLA 仍无合格执行者，或运行时能力降级 |
| T27 | `queued` → `timed_out` | `task.deadline_exceeded` | 队列/SLA 截止已过且从未 dispatch；确认无副作用 |
| T28 | `queued` → `failed` | `dispatch.unavailable` | 无可用 Adapter/配置不可恢复；确认无副作用；记录兼容性缺口；Grant 必须撤销。此时 `attempt=0`：不消耗重试预算，且只能经 T70 转人工（不得走 T65） |
| T66 | `queued` → `failed` | `dispatch.rejected` | 执行边界明确拒绝提交且确认无副作用；记录错误码；此时 `attempt=0`，**不得在缺少新决策/新 Grant 时重试**；`attempt=0` 不满足 T65，只能经 T70 转人工或由新任务/新决策重新授权 |
| T67 | `queued` → `unknown` | `dispatch.timeout` | 提交超时且无法确认是否受理；**先按幂等键查询**；禁止重发创建请求 |
| T68 | `queued` → `unknown` | `dispatch.unknown` | 提交响应不可解析或连接中断；先查询，不得猜测未执行 |
| T69 | `queued` → `needs_human` | `dispatch.unsupported` | 运行时/连接器不支持该模式或能力；确认无副作用；记录能力缺口，不自动重试 |

T66–T69 与 Adapter 提交结果一一对应：`rejected → T66`、`unsupported → T69`、`timeout → T67`、`unknown → T68`、`accepted → T23`。

### 6.8 从 `dispatched`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T29 | `dispatched` → `running` | `execution.started` | 已绑定执行句柄；执行边界报告开始/新鲜心跳；Grant 仍 `active`，或已被**本次尝试**原子消费（`status=consumed` 且 `consumed_by_call_id` 属于本尝试）；`revoked`/`expired` 一律拒绝 |
| T30 | `dispatched` → `failed` | `execution.failed` | 执行边界明确失败且确认已停止；记录错误码；无未对账副作用 |
| T31 | `dispatched` → `unknown` | `execution.lost` | 查询超阈值或状态矛盾；进入对账，不得重发 |
| T32 | `dispatched` → `cancel_requested` | `task.cancel_requested` | 取消请求已登记；未确认停止 |
| T33 | `dispatched` → `queued` | `worker.lease_expired` | 查询确认执行边界未受理、无副作用；幂等键已记录；**本次受理作废**：`attempt` 回退 1（该次受理从未开始执行，回到受理前的值），`retry_budget_remaining` 不递减，原 Grant 保持有效（其 `attempt.number` 仍等于回退后的 `attempt + 1`）。`attempt` 回退只允许发生在 T33，且必须记录作废事件与证据。回退后 `attempt>=1` 时仍须保留更早尝试的停止证据（即 `result.stop_confirmed=true`）；只有回退到 0 时才不需要停止证据 |
| T34 | `dispatched` → `timed_out` | `dispatch.deadline_exceeded` | 提交窗口截止，且查询明确无执行任务/无副作用；否则走 T31 |
| T35 | `dispatched` → `needs_human` | `human.escalation_required` | 能力不支持或受理结果互相矛盾，无法自动判定 |

### 6.9 从 `running`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T36 | `running` → `verifying` | `execution.completed` | 执行边界报告结束且结果可取；`result_ref` 与 `evidence_refs` 已持久化；Grant 在执行期间有效 |
| T37 | `running` → `failed` | `execution.failed` | 执行边界明确失败且已停止；记录错误码；无未对账副作用 |
| T38 | `running` → `timed_out` | `execution.timed_out` | 执行边界确认超时且 `stop_confirmed=true`；否则走 T41 |
| T39 | `running` → `cancel_requested` | `task.cancel_requested` | 取消请求已登记；不视为已停止 |
| T40 | `running` → `needs_human` | `human.escalation_required` | 执行边界要求输入且无授权自动回答源，或人工接管 |
| T41 | `running` → `unknown` | `execution.lost` | 心跳/查询超阈值或状态矛盾；进入对账，不得重发 |

### 6.10 从 `verifying`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T42 | `verifying` → `succeeded` | `verification.passed` | 每条结构化完成条件通过；`result_ref`、`evidence_refs`、`verification.result=passed`、`verified_at` 完整；审批全为 approved、确认全为 satisfied/consumed |
| T43 | `verifying` → `partial_success` | `verification.partial` | 仅策略显式允许的子集通过；未完成项已列明；owner 接受并审计 |
| T44 | `verifying` → `failed` | `verification.failed` | 验证不通过且无重试价值；记录失败原因与副作用状态 |
| T45 | `verifying` → `needs_human` | `verification.inconclusive` | 证据不足/冲突，需人工复核；不得默认成功 |
| T46 | `verifying` → `unknown` | `verification.evidence_unavailable` | 证据源不可用；进入对账/补证 |
| T47 | `verifying` → `cancel_requested` | `task.cancel_requested` | 用户取消后续动作；已产生的副作用必须保留并进入对账 |

### 6.11 从 `cancel_requested`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T48 | `cancel_requested` → `cancelled` | `execution.stopped` | `stop_confirmed=true`；不再产生新副作用；结果/副作用已对账 |
| T49 | `cancel_requested` → `running` | `execution.started` | 新鲜状态证明执行边界仍在执行且未接受取消；继续尝试停止 |
| T50 | `cancel_requested` → `verifying` | `execution.completed` | 取消生效前任务已完成；结果可取；不得丢弃结果 |
| T51 | `cancel_requested` → `unknown` | `execution.lost` | 无法确认停止状态；进入有界对账 |
| T52 | `cancel_requested` → `needs_human` | `human.escalation_required` | 停止未确认超过阈值，或执行边界明确不支持取消 |

### 6.12 从 `unknown`

`unknown` 是**授权已失效**的状态。进入 `unknown` 的同一事务必须把当前 Grant 置为 `revoked`（`reason_code=execution_evidence_lost`）；被撤销的 Grant 永不复活。**不存在从 `unknown` 直接恢复到 `running` 或 `verifying` 的转换**：T53（`unknown` → `running`，`reconcile.running`）与 T54（`unknown` → `verifying`，`reconcile.completed`）已撤销，编号保留但不得实现。若对账发现执行边界实际仍在运行，必须先发送停止并取得停止证据，再走下列终止/人工路径；不得直接恢复执行。

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| ~~T53~~ | ~~`unknown` → `running`~~ | ~~`reconcile.running`~~ | **已撤销**：撤销后的 Grant 不得复活；仍在运行时必须先停止并取证，经 T72 转人工 |
| ~~T54~~ | ~~`unknown` → `verifying`~~ | ~~`reconcile.completed`~~ | **已撤销**：对账取得的结果不得绕过停止确认与新授权进入验证，经 T73 转人工 |
| T55 | `unknown` → `failed` | `reconcile.failed` | 对账确认明确失败且已停止（`stop_confirmed=true`）；副作用已记录；`attempt>=1` 时失败不消耗新的重试预算，后续只能经 T65（新决策 + 新 Grant）；`attempt=0` 时只能经 T70 |
| T56 | `unknown` → `cancelled` | `reconcile.stopped` | 对账确认已停止且无新副作用；`stop_confirmed=true` |
| T57 | `unknown` → `needs_human` | `reconcile.unresolved` | 对账尝试耗尽或证据冲突；转人工，不猜测 |
| T58 | `unknown` → `timed_out` | `reconcile.timed_out` | 对账取得超时证据且 `stop_confirmed=true`；否则保持 `unknown`/`needs_human` |
| T71 | `unknown` → `queued` | `reconcile.not_found` | **仅当 `attempt=0`（从未 dispatch.accepted）**；有界查询窗口已结束；执行边界**显式保证无副作用**（`no_side_effect` 证据）且按幂等键确认从未受理；**必须重新执行 PDP 并签发新决策与新 Grant（新 `decision_id`、新 `grant_id`）**；旧 Grant 必须已 `revoked`/`consumed`/`expired`，不得复用。`attempt` 保持 0，本转换不消耗 `retry_budget_remaining` |
| T72 | `unknown` → `needs_human` | `reconcile.running` | 对账发现执行边界仍在运行；**不得恢复执行**；必须先请求停止并取得停止证据（`stop_confirmed`）；停止未确认或运行时拒绝停止时转人工接管 |
| T73 | `unknown` → `needs_human` | `reconcile.completed` | 对账取得结果/证据，但任务已处于 `unknown` 且原 Grant 已撤销；不得直接进入 `verifying` 或 `succeeded`，交由人工裁决（新建任务、重新授权或人工补证）。该来源的 T59/T60 不可用（F41）；重派必须先取得停止证据或证明从未开始（T61） |

**`unknown` 的可执行出口只有：** T71（`attempt=0` + 无副作用证据 → 新决策 + 新 Grant 重新排队）、T55（失败 → T65 有界重试）、T56（确认停止）、T58（确认超时）、T57/T72/T73（人工接管）。不得从 `unknown` 直接恢复执行，也不得复用被撤销的 Grant。

### 6.13 从 `needs_human`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T59 | `needs_human` → `running` | `human.takeover.running` | 人工确认任务确实在运行；**仅当 `attempt>=1` 且当前 Grant 仍为 `active`，或已由本尝试消费（`consumed`），且执行证据证明同一尝试仍在运行时可用**；来自 `unknown`（T57/T72/T73）的 Grant 已 `revoked`，来自 `failed`（T70）的尝试已处于尝试终态；两者都**不得走 T59（F41）**。记录操作者与证据；不新增能力 |
| T60 | `needs_human` → `verifying` | `human.result_submitted` | 人工提交结构化结果与证据；**仅当 `attempt>=1`、该尝试刚结束待验证且 Grant 为 `active`/本尝试 `consumed` 时可用**；来自 `unknown`（T57/T72/T73）的 Grant 已 `revoked`，来自 `failed`（T70）的尝试已是尝试终态；两者都**不得走 T60（F41）**。仍需走验证，不得直接成功 |
| T61 | `needs_human` → `queued` | `human.redispatch` | 已决定重派；**不得存在未对账的 active attempt**：要么 `stop_confirmed=true`（已关闭该尝试），要么 `attempt=0` 且能证明从未 `dispatch.accepted`（从未开始）。`attempt>=1` 且无停止证据时 T61 禁止。**必须重新执行 PDP 并签发新决策与新 Grant**（新 `decision_id`/`grant_id`，`decision_revision` 递增；旧 Grant 不得复用）；`task_revision`、确认、审批全部重验；`retry_budget_remaining > 0`（T61 不执行 T65 的自动递减）；无未对账副作用 |
| T62 | `needs_human` → `denied` | `human.authorization_revoked` | 人工/策略撤销授权；记录原因码与审计；**不得存在未对账的 active attempt**：必须 `result.stop_confirmed=true`（已关闭该尝试）或 `attempt=0`（从未 `dispatch.accepted`）。`attempt>=1` 且无停止证据时 T62 禁止（Schema 已拒绝 `denied` + `attempt>=1` + `stop_confirmed=false`，见 S-20） |
| T63 | `needs_human` → `cancelled` | `human.stop_confirmed` | 人工确认已停止且无新副作用；`stop_confirmed=true`；这是关闭仍打开尝试的人工路径 |
| T64 | `needs_human` → `failed` | `human.declared_failed` | 人工判定失败且不再重试；记录证据；**要求 `attempt>=1`**。`attempt=0` 的人工失败不得进入 `failed`，只能登记为 `cancelled`（T63）或 `denied`（T62），避免 T64/T70 的空转循环 |

**`needs_human` 的可达性与出口按进入来源区分。** T59/T60 只在 `attempt>=1` 且当前 Grant 为 `active` 或由本尝试 `consumed` 时可用，并分别需要“同一尝试仍在运行”/“该尝试刚结束待验证”的执行证据：T40/T45/T52/T35 进入的 `needs_human` 保留 T59 或 T60 的可能（按证据二选一）。T10/T14/T18/T22/T26/T69 是 `attempt=0` 的执行前/预派发来源，没有可接管的运行尝试，T59/T60 不可用。**T57/T72/T73（来自 `unknown`）的 Grant 已 `revoked`（T72 时尝试可能仍在运行），不满足 T59/T60 的 `active`/本尝试 `consumed` 条件，禁止实现（F41）。T70（来自 `failed`）的尝试已处于尝试终态且 Grant 已失效：T59 要求同一尝试仍在运行、T60 要求该尝试刚结束待验证，两者都不成立，同样禁止（F41）。** `needs_human` 的可执行出口是 **T61**（停止证据或 `attempt=0` + 新决策/新 Grant）、**T62**（同样要求 `stop_confirmed=true` 或 `attempt=0`）、**T63**、**T64**。**T70 不是 `needs_human` 的出边**：它是 `failed → needs_human` 的入边（预派发失败或重试耗尽后转人工），列在 §6.14；把 T64（`needs_human → failed`）后再经 T70 回到 `needs_human` 视为 T64/T70 循环，不得作为退出路径。T73 本身仍可达（`unknown → needs_human`），但它的结果不得直接进入验证或执行。

### 6.14 从 `failed`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T65 | `failed` → `queued` | `task.retry_authorized` | `attempt >= 1`（存在已开始的尝试）且 `attempt < max_attempts` 且 `retry_budget_remaining > 0`（**递减前**）；**进入本转换时原子递减 `retry_budget_remaining`（减 1），递减后可为 0，表示这是最后一次允许的重试**；**重新执行 PDP 并签发新决策与新 Grant**；决策 revision 必须递增（>=2）且 `decision_purpose=retry`（`governance-decision.schema.json`）；新 Grant 的 `attempt.number = status.attempt + 1`、`retry_of_attempt = status.attempt`、`attempt.retry_budget_remaining` 记录递减后的余额（最后一次为 0）、`retry_decision_id` 指向新决策；确认/审批/预算重验；确认无未对账副作用；幂等策略成立；不得复用过期决策或旧 Grant |
| T70 | `failed` → `needs_human` | `task.retry_exhausted` | `attempt >= max_attempts`、`retry_budget_remaining = 0`、`attempt = 0`（从未开始执行，含 T28/T66 的预派发失败），或动作不可安全幂等；不得自动重试，转人工裁决 |

### 6.15 终态

`succeeded`、`denied`、`cancelled`、`timed_out`、`partial_success` 没有任何出边。需要继续工作时应创建新任务，并通过 `linkage.supersedes_task_id`/`parent_task_id` 关联，不得改写历史状态。`failed` 只允许 T65/T70。

## 7. 禁止的转换

以下转换必须在服务层显式拒绝并记录安全事件；不得仅靠 UI 隐藏。

| ID | 禁止转换 | 理由 |
|---|---|---|
| F01 | `draft`/`evaluated` → `running`/`succeeded` | 跳过评估、入队、派发与验证 |
| F02 | `queued` → `running`/`succeeded` | 跳过派发与执行事实 |
| F03 | `dispatched` → `succeeded` | 跳过运行与验证 |
| F04 | `running` → `succeeded`/`partial_success` | 跳过 `verifying` 与证据检查 |
| F05 | `unknown` → `succeeded` | 对账未完成即宣告成功 |
| F06 | `cancel_requested` → `succeeded` | 必须经 `verifying`，且不得丢弃已完成结果 |
| F07 | `cancel_requested` → `cancelled` 无 `stop_confirmed` | 把“请求取消”当作“已停止” |
| F08 | `denied` → `queued`/`dispatched`/`running` | 用优先级、重试或人工口头指令覆盖硬拒绝 |
| F09 | `awaiting_approval` → `queued` 无 `approved` + `separation_check=passed` | 跳过或伪造审批 |
| F10 | `awaiting_confirmation` → `queued` 无有效、未过期、hash 匹配的确认 | 跳过确认或重放确认 |
| F11 | `timed_out` → `running` | 已关闭的执行边界不应复活；需新任务 |
| F12 | `failed` → `succeeded` | 跳过验证与证据 |
| F13 | `partial_success` → `succeeded` 静默补齐 | 未完成项必须有新验证/新任务 |
| F14 | 任一任务终态（`succeeded`/`denied`/`cancelled`/`timed_out`/`partial_success`）→ 任一非终态 | 改写历史；新工作必须新任务。注意 `failed` 是**尝试终态**（不是任务终态），只允许 T65/T70 |
| F15 | 使用过期/撤销/已消费 Grant 进入 `dispatched` | Grant 生命周期约束 |
| F16 | `single_use` Grant 第二次调用 | 一次性消费语义 |
| F17 | 用任务 A 的 Grant 执行任务 B | Grant 必须绑定 `task_id` 及 `task_revision` |
| F18 | 自审批（Requester/Executor/Accountable Owner，或任一实际审批人 principal 与三者相同） | 职责分离；1.0.0 无策略例外 |
| F19 | 高优先级修改导致 `denied` 重开 | 优先级不提升权限 |
| F20 | `unknown` → `cancelled` 无停止/对账证据 | 未知不等于已停止 |
| F21 | `dispatched` → `running` 无执行句柄或 Grant 已失效 | 无绑定的运行事实不可信 |
| F22 | `running` → `verifying` 无结构化结果/证据 | 运行时自述“完成”不构成结果 |
| F23 | Worker 无租约/过期 fencing token 写状态 | 防止脑裂与重复副作用 |
| F24 | 模型或客户端直接写 `status.state` | 状态只能由服务端验证转换写入 |
| F25 | Grant 过期后继续运行且不进入停止/取消/对账 | Grant 过期必须关闭执行边界 |
| F26 | 用 Advisory/Governed Grant 执行 Managed Execution，或反向套用 | 模式混淆；模式必须与决策一致 |
| F27 | `running` 且 `status.attempt=0` | 运行事实必须对应至少一次尝试 |
| F28 | `queued`/`dispatched`/`running` 且模式或范围为 `undetermined` | 评估后必须确定模式与范围 |
| F29 | `succeeded` 无 `verification.result=passed`、`result_ref` 或 `evidence_refs` | 完成定义不可绕过 |
| F30 | `cancelled`/`timed_out` 无 `stop_confirmed=true` | 未确认停止不得作为终态 |
| F31 | `goal` 缺少 `completion_criteria` 进入 `queued` | 自由文本不能作为完成定义 |
| F32 | 重试复用旧决策/旧 Grant，或未递增 attempt | 重试必须新鲜评估、重新签发与预算约束 |
| F33 | Agent、运行时、队列作为 `accountable_owner` 或 `approver` | 责任必须由人员/组织角色承担 |
| F34 | 任务/尝试已进入终态或 `unknown` 却保留 `active` Grant，或 PEP 在终态任务上放行 active Grant | D3/A2：终态必须使 Grant 失效，PEP 必须检查当前任务状态 |
| F35 | `pre_execution_incomplete` 记录关联 Grant，或在 `attempt>=1` / 执行后状态使用该标记 | 未执行记录不得持有授权；执行后必须补齐资料 |
| F36 | `unknown` 任务在缺少 `reconcile.not_found` 无副作用证据时直接重派或重新入队 | 未知不等于未执行；必须先对账 |
| F37 | `unknown` → `running`（原 T53） | **R1**：进入 `unknown` 即撤销 Grant，撤销后不得恢复执行；仍在运行必须先停止并取证，经 T72 转人工 |
| F38 | `unknown` → `verifying`（原 T54） | **R1**：对账结果不得绕过停止确认与重新授权直接进入验证；经 T73 转人工裁决 |
| F39 | `attempt=0` 的人工“失败”经 `human.declared_failed` 进入 `failed` 后再走 T64/T70 循环 | **R3**：从未开始执行的失败必须登记为 `cancelled`/`denied`；`failed` 需要已开始的尝试或预派发失败事件 |
| F40 | 将被 `revoked` 的 Grant 改回 `active`、复活或复用于 T71/T65 | Grant 撤销是终态；恢复必须签发新 `grant_id` + 新决策 |
| F41 | 由 `unknown`（T57/T72/T73）或 `failed`（T70）进入的 `needs_human` 经 T59/T60 恢复 `running`/`verifying` | 撤销后的 Grant 不得授权执行或验证，且 T70 来源的尝试已是尝试终态；`unknown` 来源只能 T61/T62/T63/T64（T70 不是 `needs_human` 的出边），T61 仍需停止证据与全新授权 |

## 8. 确认与审批生命周期

### 8.1 确认（confirmation）

| 状态 | 允许进入 | `confirmed_by` | `satisfied_at` | `consumed_at` / `consumed_by_call_id` | 可执行的转换 |
|---|---|---|---|---|---|
| `pending` | 创建 | null | null | null / null | 被满足、被拒绝、过期 |
| `satisfied` | 用户确认且 payload hash 匹配 | 非空 | 非空 | null / null | 被消费、过期 |
| `consumed` | 首次授权调用原子消费 | 非空 | 非空 | 非空 / 非空 | 无（重用拒绝） |
| `rejected` | 用户拒绝或职责冲突 | 可为空 | null | null / null | 无 |
| `expired` | TTL 到期 | 可为空 | 可为空 | null / null | 无 |

规则：

- `single_use` 恒为 true；`consumed` 后任何重用返回 `replay_detected`。
- 确认只能绑定一个 Grant；Grant 引用 `status=satisfied` 且 `consumed_at=null` 的确认。
- **被拒绝的确认必须 `satisfied_at=null`**：拒绝表示从未生效，不得留下满足时间；`consumed_at`/`consumed_by_call_id` 也必须为 null。
- `expired` 允许 `satisfied_at` 为空或保留原满足时间，但 `consumed_at`/`consumed_by_call_id` 必须为 null。
- `payload_hash` 必须覆盖 RFC 8785 JCS 规范化后的执行 payload；字段顺序、Unicode、数字表示必须一致（§16.1 给出 fixture 参考 payload 与真实哈希）。
- `channel_binding` 保留现有 `tool_confirmations` 的渠道语义；跨渠道消费默认拒绝。

### 8.2 审批（approval）

| 状态 | 允许进入 | `decided_at` | `separation_check` | 可执行的转换 |
|---|---|---|---|---|
| `pending` | 创建 | null | `not_required` | 被批准、被拒绝 |
| `approved` | 独立审批人批准 | 非空 | `passed` | 过期、撤销 |
| `rejected` | 独立审批人拒绝 | 非空 | `passed`/`failed`/`not_required` | 无 |
| `expired` | 原批准过期 | 非空 | `passed` | 无 |
| `revoked` | 授权/审批被撤销 | 非空 | `passed` | 无 |

规则：

- `approved` 必须 `separation_check=passed`，且审批的实际行为人 ≠ Requester、Executor、Accountable Owner。
- 1.0.0 **不允许** Accountable Owner 审批自己的任务；未来策略例外必须升主版本并增加例外引用、理由、策略版本与审计字段。
- **职责分离按实际 principal 身份判定，由服务端计算 `separation_check`。** 服务端必须逐一检查 `approvals[]` 中每个 `decision=approved` 的 `approver_principal_id`，以及授权 Grant 的 `approval_ref.approver_principal_id`，并与 `accountable_owner`、`requester`、`executor` 比较；**不能只检查任务级 `approver` 字段**。`role_` 审批人必须先解析为实际行为人（principal_id）再比较：同一 `principal_id` 不得自审；同一组织角色下的不同人员可以审批（角色相同不构成冲突）。JSON Schema 无法比较两个 `principal_id`，因此 `owner==实际审批人` 是服务端强制断言（见 §19 S-01/S-02/S-14），不是 Schema 校验。
- **任务级 `approver` 必须与实际审批一致。** 已解析的 `approver.principal_id` 必须等于授权 Grant 所引用审批（`approval_ref.approval_id` 对应的 `approvals[]` 条目）的实际 `approver_principal_id`；角色审批人按解析后的行为人比较。不一致时 fail closed（S-14）。
- **过期或撤销只能作用于已通过的批准**：`expired`/`revoked` 必须 `separation_check=passed`、`decided_at` 与 `expires_at` 非空；分离检查失败（`failed`）的审批只能停在 `rejected`。
- `pending` 必须 `decided_at=null`、`separation_check=not_required`；`rejected` 必须 `decided_at` 非空。
- 审批绑定 payload hash、TTL 与审批人身份；引用 `rejected`/`expired`/`revoked` 审批的 Grant 非法。

### 8.3 Grant 消费时点（按模式）

| 消费模式 | 消费时点 | 记录字段 | 说明 |
|---|---|---|---|
| `single_use`（默认） | **首个受控调用产生副作用之前**原子消费 | `status=consumed`、`consumed_at`、`consumed_by_call_id`、`remaining_calls=0` | 先持久化消费再执行；消费失败即拒绝调用。带确认的 Grant 必须是 `single_use`。 |
| `multi_use` | 每次调用递增 `usage.calls_consumed`；`remaining_calls=0` 或任务进入终态时消费 | `remaining_calls = max_calls - calls_consumed` | 并发调用必须原子扣减，不得超发。 |
| 任一模式 | 任务/尝试进入终态或 `unknown` | `status=revoked` + `revocation.reason_code` | 与 `succeeded`/`failed`/`cancelled` 等状态同事务写入。 |

规则：

- `running`/`verifying` 允许 Grant 为 `active` 或**由本尝试消费**的 `consumed`；T29 因此接受这两种状态（见 §6.8）。
- `queued`/`dispatched` 必须是 `active`；终态与 `unknown` 必须是 `revoked`/`consumed`。**进入 `unknown` 必须使用 `revoked` + `reason_code=execution_evidence_lost`**；`revoked` 是终态，不得改回 `active`，也不得在 T71/T65 中复用（必须签发新 `grant_id`）。
- 消费记录与副作用提交必须在同一事务/幂等单元内完成；不得“先执行后记账”。
- 任何 `active` 且 `usable=true` 的 Grant 在终态或 `unknown` 任务上都是缺陷状态，必须由对账任务告警。

## 9. 幂等、重复副作用与并发

### 9.1 幂等键作用域

| 操作 | 幂等键 | 作用域 |
|---|---|---|
| 创建任务 | `idempotency_key` | 已验证 Principal + 操作类型；同键同 payload 返回同一 `task_id` |
| 提交执行 | `(task_id, grant_id, idempotency_key)` | 任务 + Grant；执行边界保证最多一个执行任务 |
| 工具调用 | `(grant_id, capability, payload_hash, call_id)` | Grant + 能力 + JCS 规范化 payload |
| 取消 | `(task_id, cancel_id)` | 任务；重复取消返回原结果 |
| 确认/审批 | `confirmation_id` / `approval_id` | 一次性消费；重复提交返回原状态 |
| 状态事件 | `(task_id, state_revision)` | 唯一约束；过期 revision 拒绝 |

同键不同 payload 必须返回 `conflict`，不得创建第二个副作用。提交超时后，Adapter 必须支持 `find_by_idempotency_key(task_id, grant_id, idempotency_key)`；找到则采用原执行任务，明确不存在且无副作用才允许按预算重试。

### 9.2 重复副作用防护

- 写动作前先在 PEP 中预占 `call_id` 并持久化；成功后记录结果，失败后记录可重试状态。
- 业务 handler 必须使用唯一约束或幂等表；不得依赖“应该只调用一次”。
- 重试前检查该动作是否幂等；不可安全幂等的动作不得自动重试（T40/T45/T57/T70）。
- 取消与完成竞态以执行边界事实为准；不得因取消丢弃已完成结果。
- 超过一次的业务副作用必须进入 `needs_human` 对账，不得静默继续。

## 10. Worker 租约与 fencing

- Worker 通过 `worker.leased` 获取租约，记录 `lease_owner`、`lease_expires_at`、`fencing_token`。
- 只有持有当前 fencing token 的 Worker 能写入 `dispatched`、`running`、`verifying` 等转换。
- 租约过期后，新 Worker 必须先调用 `find_by_idempotency_key`/`get_status` 确认无活动执行任务；否则进入 `unknown`，不得直接重派。
- 心跳丢失但租约未过期：保持租约，不重复派发。
- 状态写入使用 `state_revision` 乐观锁；旧 revision 返回冲突并触发对账。

## 11. 超时与 `unknown` 恢复（fail-closed）

1. **先查询，后重试。** 提交超时/连接中断时禁止重发创建请求；用 `runtime_task_id`、`execution_ref` 或幂等键查询。
2. **进入 `unknown` 立即撤销 Grant。** 同一事务写 `grant_status=revoked` 与 `execution-grant.revocation.reason_code=execution_evidence_lost`；撤销后的 Grant 永不复活、永不复用，`unknown` 期间不得签发新 Grant、不得调用新工具、不得显示为成功或已取消。PEP 在 `unknown` 任务上不得放行任何 active Grant。
3. **不得直接恢复执行。** 已撤销的 T53/T54（`unknown → running` / `unknown → verifying`）不存在。若对账发现执行边界实际仍在运行，必须先发送停止并取得停止证据（`stop_confirmed=true`），再走 §6.12 的终止/人工路径；运行时拒绝停止或停止无法确认时经 T72 转 `needs_human`，不得恢复。
4. **可执行恢复路径（都必须重新授权）：**
   - **(a) `reconcile.not_found` → `queued`（T71）**：仅当 `attempt=0`（从未 `dispatch.accepted`）。有界查询窗口结束时，执行边界按幂等键**显式保证从未受理且无副作用**（`no_side_effect` 证据，含查询次数、窗口起止、边界标识）；核心必须重新执行 PDP、签发新决策与新 Grant（新 `decision_id`/`grant_id`），旧 Grant 必须已 `revoked`/`consumed`/`expired`。缺少无副作用证据或 `attempt>=1` 时不得走本路径。重复的重新入队受对账窗口与队列 SLA 约束，超限转 T57，不得无限循环。
   - **(b) `reconcile.failed` → `failed`（T55）→ T65**：`attempt>=1` 时先进入 `failed`（尝试终态），再由 T65 在有界预算内重新评估并签发新决策/新 Grant；`attempt=0` 时只能 T70 转人工。
   - **(c) 对账取得结果（`reconcile.completed`，T73）或仍在运行（`reconcile.running`，T72）→ `needs_human`**：不得直接进入 `verifying`/`succeeded`；由人工裁决是否新建任务、重新授权或补证。该来源的 Grant 已 `revoked`，因此 T59/T60 不可用（F41）；若仍需继续，只能先取得 `stop_confirmed=true`（或证明从未开始）再走 T61 的新决策/新 Grant，或 T62/T63/T64 终止（T70 不是 `needs_human` 的出边，见 §6.13）。
   - **(d) `reconcile.stopped` → `cancelled`（T56）、`reconcile.timed_out` → `timed_out`（T58）、`reconcile.unresolved` → `needs_human`（T57）**：终止或人工接管，不再执行。
   禁止把 `not_found` 直接视为可入队或可重派；T71 与 T65 的前置条件一致（新决策 + 新 Grant + 无未对账副作用）。
5. 状态查询超时/心跳丢失 → `unknown`；按退避策略对账（次数与总时长有上限）。
6. 执行边界恢复后，已过期/已撤销 Grant 不得复活；需要继续时必须重新评估并重签 Grant。
7. **Grant 在运行中过期**（`expires_at` 到达）时，核心必须立即停止产生新副作用：进入 `cancel_requested` 并尝试停止；无法确认停止则进入 `unknown`/`needs_human`。不得继续运行到完成。Grant 的 `binding.runtime_within_grant_ttl` 必须为 true，且 `budget.max_runtime_seconds` 不得超过 `ttl_seconds`。

## 12. 取消语义

- `cancel_requested` 只表示请求已登记，不是停止。
- 取消后不再允许新工具调用；已发出的调用必须等待结果或进入对账。
- 只有 `stop_confirmed=true`（或等价执行边界证据）才能进入 `cancelled`；结束后仍需对账副作用。
- 取消前已完成的任务走 `verifying`，不得因取消请求丢弃结果。
- 执行边界不支持取消 → `needs_human`，不得虚报已停止。
- 取消竞态不得通过“最后写入者获胜”解决；以执行边界事实 + 证据为准。

## 13. 尝试与重试记账

- **`attempt` 的定义。** `attempt` 是**已开始的执行尝试次数**：首次 `queued` 为 0，只在 `dispatch.accepted`（T23）递增。`dispatched`/`running`/`verifying`/`succeeded`/`partial_success` 必须 `attempt>=1`；`failed`/`cancelled`/`timed_out` 可因从未开始而为 0。
- **尝试何时打开、何时关闭。** 尝试在 `dispatch.accepted` 打开；只有在取得关闭证据时关闭（`execution.stopped`/`execution.failed`/`execution.timed_out` 且 `stop_confirmed=true`，或 `reconcile.stopped`/`failed`/`timed_out`/`not_found`，或 `verifying` 中的 `verification.failed`）。`unknown`、`needs_human`、`cancel_requested` 都**不关闭**尝试；进入 `unknown` 只撤销 Grant。未关闭且无 `not_found` 无副作用证据的尝试称为**未对账的 active attempt**：它阻止 T61，直到取得 `stop_confirmed=true` 或证明该次执行从未开始。
- **`needs_human → queued`（T61）的额外守卫。** T61 要求：`attempt=0`（从未 `dispatch.accepted`，即证明从未开始），或 `attempt>=1` 且 `result.stop_confirmed=true`（尝试已关闭）；不得存在未对账的 active attempt。T61 必须重新执行 PDP、签发新决策与新 Grant，`retry_budget_remaining>0`，但不执行 T65 的自动递减。T59/T60 对来自 `unknown` 的 `needs_human` 不可达（F41）。
- **预派发失败不消耗重试预算。** `queued → failed`（T28/T66）不消耗 `retry_budget_remaining`，且 `attempt=0` 的 `failed` 只能经 T70 转人工。`dispatched → queued`（T33）作废一次从未开始执行的受理：`attempt` 回退 1、预算不变、原 Grant 继续有效；回退后 `attempt>=1` 时仍须保留更早尝试的停止证据（`result.stop_confirmed=true`），回退到 0 时不需要。这是唯一允许 `attempt` 回退的转换，且必须有作废证据。
- **`failed` 只在有界条件下经 T65 重试。** 必须 `attempt>=1` 且 `attempt < max_attempts` 且 `retry_budget_remaining>0`（**递减前**）；**进入 T65 时递减 `retry_budget_remaining`（减 1）**，递减后的余额可为 0（最后一次允许的重试）。`attempt=0` 的失败不得走 T65。`denied`、`cancelled`、`timed_out`、`partial_success` 不得自动重试。`unknown` 必须先经 T71 或 T55 才能重新进入队列。
- **重试必须有新决策与新 Grant。** `decision_revision` 递增且 >=2、PDP 决策 `decision_purpose=retry`（并写入 Grant 的 `decision.decision_purpose`）、`supersedes_decision_id` 必填并指向被替代的旧决策；不得复用旧决策或旧 Grant。Grant 的 `attempt.retry_decision_id` 必须指向该新决策，`attempt.number = status.attempt + 1`，`retry_of_attempt = status.attempt`，`attempt.retry_budget_remaining` 是 T65 递减后的余额。
- **`attempt.number>=2` 与 `is_retry=true` 必须同时成立**；`is_retry=true` 必须 `retry_decision_id` 非空；`retry_budget_remaining` 是授权后的余额，允许为 0（最后一次重试）。同时检查 `max_attempts`；耗尽时走 T70（`attempt.number<=max_attempts` 由服务端比较）。
- **人工在 `attempt=0` 宣告失败**必须登记为 `cancelled`（T63）或 `denied`（T62），不得进入 `failed` 后再走 T64/T70 循环。
- 重试不得扩大 capability、resource scope、TTL 或预算；确认/审批必须按新 payload 重新检查。
- 不可安全幂等的动作不得自动重试；需要人工确认或补偿。
- T61 人工重派与 T65 自动重试遵循相同的新决策/新 Grant 规则。

## 14. 人工接管

- `needs_human` 必须记录接管原因、责任人、截止时间与所需动作。
- 人工接管不自动获得新能力；需要新能力时必须重新评估并签发 Grant。
- 人工只能通过审计化命令触发 T59–T64；不得直接改数据库状态。
- 人工提交结果仍须 `verifying`；人工不能把“我看过了”当作验证通过。
- 人工停止仍需停止证据；无法确认时保持 `unknown`/`needs_human`。
- 人工重派（T61）必须重新评估，不能复用人工判断作为授权；必须已有停止证据或证明从未开始，且不得存在未对账的 active attempt。
- 人工不能通过 T59/T60 把 `unknown` 来源的 `needs_human` 直接恢复执行或验证：其 Grant 已撤销（F41），只能重新授权（T61）或终止。
- 人工撤销授权（T62）同样必须先关闭仍打开的尝试：`attempt>=1` 时要求 `stop_confirmed=true`，或 `attempt=0` 证明从未开始；未取得停止证据时只能保持 `needs_human` 并继续对账（S-20）。

## 15. 完成定义、结果与证据

- `goal.completion_criteria` 必须是结构化数组，每项包含 `criterion_id`、`type`、`verifier.verifier_id/version`、`resource_ref`、`expected`、`evidence_ref`、`evidence_hash`。
- 允许的类型：`business_state_equals`、`resource_exists`、`artifact_hash`、`verifier_result`。每种类型都有固定的必填字段；自由文本不得作为完成条件。
- `verifying` 要求 `result_ref` 与至少一个 `evidence_refs`；`succeeded` 要求 `verification.result=passed`、`verified_at`、`verifier_id`、以及通过全部完成条件。
- `partial_success` 要求 `verification.result=partial` 与未完成项清单。
- **`failed` 有两条产生路径，验证结论不同：**
  - **验证失败**（T44，事件 `verification.failed`）：必须 `verification.result=failed`、`verifier_id`、`verified_at` 与证据；表示执行完成但未满足完成定义。
  - **提交/执行失败**（T28/T30/T37/T55/T66/T64）：`verification.result` 保持 `pending`（或 `inconclusive`），**不得**伪造成 `failed`；表示没有可验证结果。
  - 两条路径都要求 `result.evidence_refs` 至少一条，且 `stop_confirmed=true`。无法确认停止时必须停留在 `unknown`（或 `needs_human`），不得声明 `failed`。
- `cancelled` 与 `timed_out` 要求 `stop_confirmed=true` 与至少一条停止证据；从未 dispatch 的 `cancelled` 以“确认未开始”作为停止证据（`stop_confirmed=true` 表示确认未开始执行）。
- `denied` 且 `attempt>=1`（T62，从 `needs_human` 撤销授权）同样要求 `result.stop_confirmed=true` 与至少一条停止证据；`attempt=0` 的执行前拒绝不需要停止证据。Schema 直接拒绝 `denied` + `attempt>=1` + `stop_confirmed=false`，S-20 另行断言不存在未对账的 active attempt。
- 运行时或客户端的 `succeeded` 只是输入；核心验证器必须独立重算完成条件，不得直接复制运行时结论。

## 16. 规范化与哈希（RFC 8785 JCS）

所有 payload hash、确认 hash、审批 hash 与 `decision_hash` 使用 **RFC 8785 JSON Canonicalization Scheme (JCS)**：

1. 只允许 JSON 数据模型（对象、数组、字符串、数字、布尔、null）；金额、时间、ID 等易变值使用字符串。
2. 对象键按 UTF-16 码元排序；数组保持原顺序。
3. 字符串按 JCS 规则转义（保留非 ASCII UTF-8，仅转义控制字符、引号和反斜杠）。
4. 数字按 ECMAScript `Number` 序列化；不得使用 `NaN`/`Infinity`。金额禁止使用 JSON 浮点数，必须用十进制字符串。
5. `decision_hash` 的计算输入是决策对象移除 `decision_hash` 字段后的 JCS 字节；其他字段全部参与。
6. 验证器必须重算并比较，不得信任调用方提供的 hash。

测试向量：

```text
vector 1 canonical: {"amount":"100.00","currency":"CNY","payee":"res_vendor_0001","purpose":"expense.submit"}
vector 1 sha256:   sha256:f0470abd268f709bf23a3d2d93c51e801fb59c997ce072d60f8fa29e9d66a9fa

vector 2 canonical: {"confirmation_id":"conf_expense_0001","payload":{"amount":100.5,"note":"café","resource_ids":["res_expense_0001","res_expense_0002"]},"single_use":true}
vector 2 sha256:   sha256:8f624db9d2c9cea16272730704c4e0e5d22ed37d43afecf4115860c877fda80d
```

未通过任一测试向量的实现不得签发或校验 Grant。

### 16.1 Fixture 参考决策、参考 payload 与哈希约定（规范性）

JSON Schema 只能校验结构与格式，无法比较“确认 hash 是否等于当前 payload”或“Grant 是否在决策子集内”。为了让负向 fixtures 表达可复核的语义，契约包固定以下参考对象与计算过程：

**参考决策**（Grant 与 task fixtures 的 `decision` 引用来源）：

| 参考决策文件 | `decision_id` | 用途 |
|---|---|---|
| `fixtures/valid/governance-decision.allow.json` | `gdec_allow_0001` | 无门禁授权决策；`allowed_capabilities=["expense.read@1.0.0"]`、`scope_type=self`、`ttl_seconds=900`、`max_calls=20` |
| `fixtures/valid/governance-decision.allow-with-gates.json` | `gdec_allow_gates_0001` | 带确认+审批门禁的授权决策；`allowed_capabilities=["expense.submit@1.0.0"]` |
| `fixtures/valid/governance-decision.retry-allow.json` | `gdec_retry_0002` | 重试参考决策：`decision_revision=2`、`decision_purpose=retry`、`supersedes_decision_id=gdec_allow_0001`；`execution-grant.retry-attempt` 的 `decision` 引用 |
| `fixtures/valid/governance-decision.deny-policy.json` | `gdec_deny_policy_0001` | 已创建任务的策略拒绝参考决策（`task_id` 非空、`execution_mode=none`、无能力、无 Grant） |
| `fixtures/valid/governance-decision.deny-early.json` | `gdec_deny_early_0001` | 任务创建前的早期拒绝参考决策（`task_id=null`、`task_revision=null`） |

**参考 payload（P1，用于确认/审批 hash）**：

```text
canonical P1: {"amount":"100.00","currency":"CNY","payee":"res_vendor_0001","purpose":"expense.submit","resource_ids":["res_expense_0001"]}
P1 sha256:    sha256:187e8c992233a586b5b581356c758f5f43bd649aa7ae5d9e3c6dd654cd41810f
```

**变更后 payload（P2，仅用于表达 payload 变化）**：

```text
canonical P2: {"amount":"999.00","currency":"CNY","payee":"res_vendor_0001","purpose":"expense.submit","resource_ids":["res_expense_0001"]}
P2 sha256:    sha256:1decdbfffaafb454911d751efd460c23afe6e3cab47230c2d567211c993a5d94
```

**复核过程（每次签发与每次调用）**：

1. **生产环境**：取参考决策对象，移除 `decision_hash` 后按 RFC 8785 JCS 规范化并计算 SHA-256，结果必须等于 `grant.decision.decision_hash`；不等即拒绝。fixtures 中的 `decision_hash`、`policy_version.content_hash` 与 `tool_contract.content_hash` 是结构性占位（`sha256:0000…`/`1111…`），不参与该等式，见本节末尾说明。
2. Grant 的 `capabilities.allowed`、`resource_scope`、`budget`、`ttl_seconds`、`expires_at`、`execution_mode`、`gates` 必须分别是对应参考决策 `constraints` 的子集/一致值；对应 `binding.*` 断言必须为 true。
3. `confirmation_ref.payload_hash` / `approval_ref.payload_hash` 必须等于当前执行 payload 的 JCS 哈希。`fixtures/invalid/execution-grant.confirmation-payload-hash-mismatch.json` 使用**格式合法但属于 P2** 的哈希，并置 `binding.confirmation_payload_hash_bound=false`，因此结构校验即可拒绝。
4. `fixtures/invalid/execution-grant.grant-outlives-decision.json` 的 `expires_at` 晚于参考决策的 `expires_at`，并置 `binding.expires_within_decision=false`。
5. `fixtures/invalid/execution-grant.capability-not-subset-of-decision.json` 的 `capabilities.allowed` 含参考决策未授予的 `expense.approve@1.0.0`，并置 `binding.capabilities_subset_of_decision=false`。

**结构性占位值。** fixtures 中形如 `sha256:0000…`、`sha256:1111…`、`sha256:2222…`、`sha256:3333…` 的值是结构性占位符，只用于满足格式与唯一性，不代表真实内容哈希；只有 §16 测试向量与本节 P1/P2 是真实计算值。实现与评审不得把占位符当作可复算证据。

**日期合法性。** `dateTime` 的正则已排除 `2026-02-30`、`2026-04-31` 等不可能日期，但正则无法判断闰年（`2026-02-29` 会通过正则）。契约要求验证器启用 `format: date-time` 断言；**若运行环境未安装/未启用 date-time format checker（本仓库独立校验器即如此），则闰年 2 月 29 日与闰秒必须由服务端按 RFC 3339 复核（S-17），不得把“Schema 通过”当作日期合法证明**。fixtures 目录只放结构可判定的非闰日/不合法日期样本，不放仅依赖 format 才可拒绝的样本，以保持 invalid 目录“Schema 可判定为非法”的约定。

## 17. 与契约字段的对应

| 状态机概念 | 契约字段 |
|---|---|
| 当前状态 | `task-envelope.status.state` |
| 状态版本 | `task-envelope.status.state_revision` |
| 最后事件 | `task-envelope.status.last_event_id` |
| 尝试次数 / 上限 / 重试预算 | `task-envelope.status.attempt` / `max_attempts` / `retry_budget_remaining`；`attempt` 只在 `dispatch.accepted` 递增 |
| Grant 尝试绑定 | `execution-grant.attempt.number`（签发时 = `status.attempt+1`；dispatch 后 = `status.attempt`）/ `retry_of_attempt` / `retry_decision_id` |
| Grant 绑定 | `execution-grant.task_ref` / `task-envelope.linkage.grant_id` |
| 决策绑定 | `task-envelope.decision` / `execution-grant.decision`；重试决策另带 `governance-decision.decision_purpose` |
| 重试决策用途 | `governance-decision.decision_purpose=retry` ↔ `execution-grant.decision.decision_purpose=retry`（S-08） |
| Managed 执行句柄 | `task-envelope.linkage.runtime_task_id` (`rtask_`) |
| Advisory/Governed 执行句柄 | `task-envelope.linkage.execution_ref` (`exec_`) |
| 确认/审批 | `task-envelope.confirmations[]` / `approvals[]`（与 decision/grant 同形） |
| 完成条件 | `task-envelope.goal.completion_criteria[]` |
| 结果/证据/停止 | `task-envelope.result.result_ref` / `evidence_refs` / `stop_confirmed` / `verification` |
| 优先级不越权 | `task-envelope.priority.authority_effect = scheduling_only` |
| 模式与范围 | `execution_mode` / `resource_scope.scope_type`（`undetermined` 仅限 `pre_execution_incomplete` 记录） |
| 记录完整性 | `task-envelope.record_completeness.completeness` / `incomplete_reason` |
| Owner 停用升级 | `task-envelope.owner_escalation.reason_code` / `recorded_at` / `escalated_to_principal_id` |
| 取消请求 | `status.state=cancel_requested`，不得据 `cancel_requested` 推断已停止 |
| Grant 终态失效 | `task-envelope.linkage.grant_status` ∈ {`revoked`,`consumed`}；`execution-grant.revocation.reason_code` = `task_terminal`/`attempt_failed`/`execution_evidence_lost` |
| Grant 消费时点 | `execution-grant.consumption.mode` / `consumed_at` / `consumed_by_call_id` / `remaining_calls` |
| 重试标记一致性 | `execution-grant.attempt.number` / `is_retry` / `retry_decision_id` |

## 18. 验收要点

- 每个状态至少有一个允许入边和一个允许出边（终态除外）。
- 72 条有效允许转换（T00–T73，其中 T53/T54 已撤销）必须各有事件、守卫和测试用例；F01–F41 必须各有拒绝用例（F41 的拒绝用例见 §19.1 V3）。
- 每条负向测试必须断言“状态未变 + 安全事件已记录”，而不仅是返回错误。
- 状态机测试必须覆盖并发 revision、重复事件、取消竞态、提交超时、幂等重取、对账恢复（T55–T58、T71–T73，含撤销 Grant 后禁止 running/verifying）、三种模式的能力守卫、重试新决策与新 Grant（`supersedes_decision_id` 必填）、attempt 记账（首次 queued=0、T65 递减预算且最后一次重试余额为 0、attempt=0 不走 T65）、T61/T62 停止证据与未对账 active attempt、Owner 停用升级（终态可带 disabled Owner + `owner_escalation`）、终态 Grant 失效。
- 未实现的转换不得注册为可调用 API；不得用文档承诺替代测试。

## 19. 服务端强制断言矩阵（Schema 无法表达）

JSON Schema 只能校验单实例结构与格式。下表断言必须在服务端签发、状态写入与每次 PEP 调用时计算；任何一条为 false 都必须 fail closed，并记录安全事件。表中 ID 是测试矩阵编号，负向测试必须逐条引用。

| ID | 断言 | 关联条目 |
|---|---|---|
| S-01 | 对每个 `approvals[].decision=approved` 的实际 `approver_principal_id`，以及 `execution-grant.approval_ref.approver_principal_id`，都要求 `!= accountable_owner.principal_id`（角色审批人先解析为实际行为人；同角色不同人员允许） | D3、R2、F18、F33、TH-08/TH-28 |
| S-02 | 对每个 `approvals[].decision=approved` 的实际 `approver_principal_id`，以及 `execution-grant.approval_ref.approver_principal_id`，都要求 `!= requester.principal_id` 且 `!= executor.principal_id` | R2、F18、T19、TH-08 |
| S-03 | `expires_at > issued_at`；`grant.expires_at <= decision.expires_at`；`decision.expires_at > evaluated_at` | TH-06、TH-26 |
| S-04 | `decision.decision_hash` = JCS(decision − decision_hash) 的 SHA-256 | C2、TH-25 |
| S-05 | `confirmation.payload_hash` / `approval.payload_hash` = JCS(当前执行 payload) 的 SHA-256 | TH-10、§16.1 |
| S-06 | Grant 的 capabilities/scope/ttl/budget 是决策 `constraints` 的子集，模式一致 | C2、TH-26 |
| S-07 | `attempt.number <= max_attempts`；Grant 签发时 `grant.attempt.number == task.status.attempt + 1`，`dispatch.accepted` 后 `== task.status.attempt` | D2、R3、F32 |
| S-08 | `attempt.is_retry=true` 时 `retry_decision_id == decision.decision_id`、`retry_of_attempt == status.attempt` 且 `decision_revision` 单调递增；被引用的 PDP 决策必须 `decision_purpose=retry`（`governance-decision.schema.json`） | D2、R3、§13 |
| S-09 | 终态或 `unknown` 任务上的 active Grant 拒绝授权（PEP 读取当前任务状态） | A2、R1、F34 |
| S-10 | 进入终态时同事务写 `grant_status=revoked|consumed` 与 `revocation.reason_code`；进入 `unknown` 时同事务写 `revoked` + `execution_evidence_lost` | A2、R1、§8.3 |
| S-11 | `pre_execution_incomplete` 记录无 Grant、`attempt=0`；`attempt>=1` 必须 `complete` | A1、F35 |
| S-12 | `unknown → queued`（T71）必须有 `attempt=0`、`no_side_effect` 证据、新决策与新 `grant_id`，旧 Grant 为 `revoked`/`consumed`/`expired` | A3、R1、F36 |
| S-13 | `separation_check` 由服务端依据 principal 身份计算，不接受客户端提交值；`role_` 审批人解析为实际行为人 | D3、R2、TH-01 |
| S-14 | 已解析的任务级 `approver.principal_id` 必须等于授权 Grant 的 `approval_ref.approval_id` 所指向 `approvals[]` 条目的实际 `approver_principal_id`（角色审批人按解析后的行为人比较）；不相等即 fail closed | R2、D3、F18 |
| S-15 | `attempt` 只在 `dispatch.accepted` 递增；首次 `queued` 为 0；`attempt=0` 的记录不得走 T65；T65 必须递减 `retry_budget_remaining`（递减后可为 0）；`attempt=0` 的人工失败只允许 T62/T63，`human.declared_failed` 进入 `failed` 必须 `attempt>=1`；仅 T33 可作废一次未开始执行的受理并回退 `attempt` | R3、F39、§13 |
| S-16 | 已 `revoked` 的 Grant 是终态：不得改回 `active`，不得在 T71/T65 中复用；恢复必须使用新 `grant_id` + 新决策 | R1、F40、TH-06 |
| S-17 | 闰年/闰秒与 RFC 3339 合法性由服务端复核（`2026-02-29` 能通过正则）；`format` 未启用时不得把结构校验当作日期合法证明 | §16.1、TH-26 |
| S-18 | `queued`/`dispatched`/`running`/`verifying`/`awaiting_confirmation`/`awaiting_approval` 的 Owner 必须已解析且 active，且不得带 `owner_escalation`；终态/历史记录（含 `succeeded`/`partial_success`）的 disabled/unbound/unknown Owner 必须带完整 `owner_escalation`；`awaiting_clarification` 按 A1 可暂未解析 | R4、TH-02、TH-22 |
| S-19 | T61 必须满足：`attempt=0`（从未 `dispatch.accepted`）或 `result.stop_confirmed=true`；不得存在未对账的 active attempt（无关闭证据）；并携带新决策与新 `grant_id`、`retry_budget_remaining>0`。不满足即拒绝入队 | T61、F41、TH-02/TH-26 |
| S-20 | T62 必须满足：`attempt=0`（从未 `dispatch.accepted`）或 `result.stop_confirmed=true` 且至少一条停止证据；不得存在未对账的 active attempt。`denied` 且 `attempt>=1` 时 `stop_confirmed=false` 由 Schema 直接拒绝；跨对象部分（停止证据是否真实关闭该尝试）由本断言拒绝 | T62、§13、§15 |

### 19.1 服务端断言测试向量（完整记录，Schema-valid，必须被服务端断言拒绝）

以下三个向量都是**完整的 `task-envelope` 记录**（含全部必填字段），可以通过 JSON Schema 2020-12 校验；它们必须被服务端断言 **S-01**、**S-15/F39** 与 **F41** 拒绝。它们**不得**放入 `fixtures/invalid/`：该目录的约定是“Schema 可判定为非法”，而这些向量只能由服务端跨字段/跨对象比较或状态转换检查发现。三个向量的结构合法性由契约包校验器逐条确认（见 PR 验证证据）。

#### V1 — owner == 实际审批人（S-01，`queued`）

`accountable_owner`、任务级 `approver` 与 `approvals[]` 的实际 `approver_principal_id` 都是 `prin_owner_0001`；Grant `grnt_self_0001` 的 `approval_ref.approval_id` 必须指向 `appr_self_0001`。Schema 允许该结构（字段类型与取值都合法），但 **S-01** 必须 fail closed：不写 `queued`、不签发/不放行 Grant、记录 `self_approval_forbidden` 安全事件，任务保持 `awaiting_approval` 或转 `denied`。该向量中任务级 `approver` 与实际审批人相同，因此 **S-14 通过**；S-14 的负向向量是本节末段的任务级/实际审批人不一致用例。

```json
{
  "schema_version": "1.0.0",
  "envelope_kind": "record",
  "task_id": "task_selfapproval_0001",
  "idempotency_key": "idem_selfapproval_0001",
  "client_task_ref": "cref_selfapproval_0001",
  "requester": {
    "principal_id": "prin_user_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "accountable_owner": {
    "principal_id": "prin_owner_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "assignee": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "executor": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "approver": {
    "principal_id": "prin_owner_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "delegations": [],
  "goal": {
    "summary": "Submit a synthetic expense report for review.",
    "completion_criteria": [
      {
        "criterion_id": "crit_expense_state_0001",
        "type": "business_state_equals",
        "verifier": {
          "verifier_id": "ver_expense_state_0001",
          "version": "1.0.0"
        },
        "resource_ref": "res_expense_0001",
        "expected": "submitted",
        "evidence_ref": "evidence:expense_state_0001",
        "evidence_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
      }
    ],
    "required_inputs": [
      "synthetic receipt reference"
    ],
    "out_of_scope": [
      "No payment execution."
    ]
  },
  "priority": {
    "requested_level": "high",
    "effective_level": "high",
    "authority_effect": "scheduling_only",
    "rationale": "Synthetic deadline."
  },
  "due_at": "2026-10-27T09:00:00Z",
  "sla": {
    "response_by": "2026-10-21T09:00:00Z",
    "complete_by": "2026-10-27T09:00:00Z",
    "escalation_after_minutes": 240
  },
  "execution_mode": "governed_resource_access",
  "requested_capabilities": [
    "expense.submit@1.0.0",
    "expense.read@1.0.0"
  ],
  "resource_scope": {
    "scope_type": "record_set",
    "resource_ids": [
      "res_expense_0001"
    ],
    "connector_ids": [
      "conn_finance_0001"
    ],
    "data_classifications": [
      "internal"
    ],
    "exclude_resource_ids": []
  },
  "policy_version": {
    "policy_set_id": "polset_default_0001",
    "version": "1.0.0",
    "content_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "risk": {
    "level": "medium",
    "score": 40,
    "source": "server_policy"
  },
  "requested_limits": {
    "max_calls": 20,
    "max_runtime_seconds": 600,
    "max_cost_units": 0
  },
  "confirmations": [
    {
      "confirmation_id": "conf_expense_0001",
      "payload_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "canonicalization": "rfc8785_jcs",
      "purpose": "data_release",
      "confirmed_by_principal_id": "prin_user_0001",
      "status": "satisfied",
      "satisfied_at": "2026-10-20T02:05:00Z",
      "expires_at": "2026-10-20T02:30:00Z",
      "consumed_at": null,
      "consumed_by_call_id": null,
      "single_use": true,
      "channel_binding": {
        "channel": "mcp",
        "channel_identity_ref": "chan_user_session_0001"
      },
      "issued_at": "2026-10-20T02:03:00Z"
    }
  ],
  "approvals": [
    {
      "approval_id": "appr_self_0001",
      "approver_principal_id": "prin_owner_0001",
      "payload_hash": "sha256:187e8c992233a586b5b581356c758f5f43bd649aa7ae5d9e3c6dd654cd41810f",
      "canonicalization": "rfc8785_jcs",
      "decision": "approved",
      "decided_at": "2026-10-20T02:05:00Z",
      "expires_at": "2026-10-20T02:15:00Z",
      "separation_check": "passed"
    }
  ],
  "limitations": [],
  "result": {
    "outcome": "none",
    "result_ref": null,
    "evidence_refs": [],
    "stop_confirmed": false,
    "verification": {
      "result": "pending",
      "verifier_id": null,
      "verified_at": null,
      "evidence_refs": []
    },
    "completed_at": null
  },
  "timestamps": {
    "created_at": "2026-10-20T01:55:00Z",
    "updated_at": "2026-10-20T02:10:00Z",
    "submitted_at": "2026-10-20T01:56:00Z",
    "evaluated_at": "2026-10-20T02:00:00Z",
    "last_event_at": "2026-10-20T02:10:00Z"
  },
  "status": {
    "state": "queued",
    "state_revision": 5,
    "last_event_id": "evt_queued_0001",
    "attempt": 0,
    "max_attempts": 3,
    "retry_budget_remaining": 2
  },
  "linkage": {
    "grant_id": "grnt_self_0001",
    "grant_status": "active",
    "runtime_task_id": null,
    "execution_ref": null,
    "parent_task_id": null,
    "supersedes_task_id": null,
    "correlation_id": "corr_expense_0001"
  },
  "decision": {
    "decision_id": "gdec_allow_gates_0001",
    "decision_type": "allow",
    "decision_revision": 1,
    "decided_at": "2026-10-20T02:05:00Z"
  },
  "record_completeness": {
    "completeness": "complete",
    "incomplete_reason": null
  },
  "labels": {
    "fixture": "synthetic",
    "path": "§19.1 S-01: owner == actual approver; S-14 passes because task-level approver equals actual approver"
  }
}
```

即使任务级 `approver` 写成别人，只要 `approvals[]` 或 Grant 的 `approval_ref` 指向 owner 本人，S-01 同样必须拒绝。

**S-14 负向测试向量**：任务级 `approver.principal_id=prin_approver_0001`，但 `approvals[]` 中 `approval_id=appr_expense_0001` 的 `approver_principal_id=prin_approver_0002`，且 Grant 的 `approval_ref.approval_id=appr_expense_0001`；断言 S-14 拒绝（`separation_of_duties_violation`），不得入队、不得签发 Grant，并记录安全事件。

**角色审批人**：任务级 `approver.principal_id=role_finance_manager_0001`、`approvals[].approver_principal_id=role_finance_manager_0001` 合法（同角色）；但若解析后的实际行为人是 `prin_owner_0001`，S-01 必须拒绝。

#### V2 — `attempt=0` 的人工“失败”（S-15/F39，`failed`）

该记录的 `status.attempt=0`、`status.last_event_id` 对应 `human.declared_failed`，并带有已 `revoked` 的 Grant（因此满足 Schema 对 `attempt=0` 的 `failed` 必须携带 Grant 的结构要求）。Schema 通过，但 S-15/F39 必须拒绝：从未 `dispatch.accepted` 的人工失败只能登记为 `cancelled`（T63）或 `denied`（T62），不得进入 `failed` 后经 T64/T70 循环。

```json
{
  "schema_version": "1.0.0",
  "envelope_kind": "record",
  "task_id": "task_expense_0001",
  "idempotency_key": "idem_expense_0001",
  "client_task_ref": "cref_client_0001",
  "requester": {
    "principal_id": "prin_user_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "accountable_owner": {
    "principal_id": "prin_owner_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "assignee": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "executor": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "approver": {
    "principal_id": "prin_approver_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "delegations": [],
  "goal": {
    "summary": "Submit a synthetic expense report for review.",
    "completion_criteria": [
      {
        "criterion_id": "crit_expense_state_0001",
        "type": "business_state_equals",
        "verifier": {
          "verifier_id": "ver_expense_state_0001",
          "version": "1.0.0"
        },
        "resource_ref": "res_expense_0001",
        "expected": "submitted",
        "evidence_ref": "evidence:expense_state_0001",
        "evidence_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
      }
    ],
    "required_inputs": [
      "synthetic receipt reference"
    ],
    "out_of_scope": [
      "No payment execution."
    ]
  },
  "priority": {
    "requested_level": "high",
    "effective_level": "high",
    "authority_effect": "scheduling_only",
    "rationale": "Synthetic deadline."
  },
  "due_at": "2026-10-27T09:00:00Z",
  "sla": {
    "response_by": "2026-10-21T09:00:00Z",
    "complete_by": "2026-10-27T09:00:00Z",
    "escalation_after_minutes": 240
  },
  "execution_mode": "governed_resource_access",
  "requested_capabilities": [
    "expense.submit@1.0.0",
    "expense.read@1.0.0"
  ],
  "resource_scope": {
    "scope_type": "record_set",
    "resource_ids": [
      "res_expense_0001"
    ],
    "connector_ids": [
      "conn_finance_0001"
    ],
    "data_classifications": [
      "internal"
    ],
    "exclude_resource_ids": []
  },
  "policy_version": {
    "policy_set_id": "polset_default_0001",
    "version": "1.0.0",
    "content_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "risk": {
    "level": "medium",
    "score": 40,
    "source": "server_policy"
  },
  "requested_limits": {
    "max_calls": 20,
    "max_runtime_seconds": 600,
    "max_cost_units": 0
  },
  "confirmations": [],
  "approvals": [],
  "limitations": [],
  "result": {
    "outcome": "failed",
    "result_ref": "result_expense_cancel_0001",
    "evidence_refs": [
      "evidence:human_declared_failed_0001"
    ],
    "stop_confirmed": true,
    "verification": {
      "result": "inconclusive",
      "verifier_id": null,
      "verified_at": null,
      "evidence_refs": []
    },
    "completed_at": "2026-10-20T02:20:00Z"
  },
  "timestamps": {
    "created_at": "2026-10-20T01:55:00Z",
    "updated_at": "2026-10-20T02:20:00Z",
    "submitted_at": "2026-10-20T01:56:00Z",
    "evaluated_at": "2026-10-20T02:00:00Z",
    "last_event_at": "2026-10-20T02:20:00Z"
  },
  "status": {
    "state": "failed",
    "state_revision": 7,
    "last_event_id": "evt_human_declared_failed_0001",
    "attempt": 0,
    "max_attempts": 3,
    "retry_budget_remaining": 2
  },
  "linkage": {
    "grant_id": "grnt_expense_0001",
    "grant_status": "revoked",
    "runtime_task_id": null,
    "execution_ref": "exec_governed_0001",
    "parent_task_id": null,
    "supersedes_task_id": null,
    "correlation_id": "corr_expense_0001"
  },
  "decision": {
    "decision_id": "gdec_allow_0001",
    "decision_type": "allow",
    "decision_revision": 1,
    "decided_at": "2026-10-20T02:00:00Z"
  },
  "record_completeness": {
    "completeness": "complete",
    "incomplete_reason": null
  },
  "labels": {
    "fixture": "synthetic",
    "path": "§19.1 S-15/F39: attempt=0 human-declared failed with revoked grant (schema-valid, server must reject)"
  }
}
```

#### V3 — `unknown` 来源的 `needs_human` 禁止 T59/T60（F41，`needs_human`）

该记录由 T73（`reconcile.completed`）或 T72（`reconcile.running`）进入 `needs_human`：`status.state=needs_human`、`status.attempt=1`、`status.last_event_id` 对应 `reconcile.completed`，Grant `grnt_expense_lost_0001` 已 `revoked`（`execution_evidence_lost`），`result.stop_confirmed=false`。Schema 允许该结构（`needs_human` 不要求停止证据，Grant 终态也已满足终态失效规则），但服务端必须拒绝把同一记录按 `human.takeover.running`（T59）映射为 `running` 或按 `human.result_submitted`（T60）映射为 `verifying`（F41）：Grant 已撤销，不能授权执行或验证。该来源只能先 `cancel_task` 取得 `stop_confirmed=true` 后走 T61 的新决策/新 Grant，或走 T62/T63/T64 终止；T70 是 `failed → needs_human` 的入边，不是本状态的出口。

```json
{
  "schema_version": "1.0.0",
  "envelope_kind": "record",
  "task_id": "task_expense_0001",
  "idempotency_key": "idem_expense_0001",
  "client_task_ref": "cref_client_0001",
  "requester": {
    "principal_id": "prin_user_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "accountable_owner": {
    "principal_id": "prin_owner_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "assignee": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "executor": {
    "principal_id": "prin_agent_0001",
    "principal_type": "agent_instance",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "approver": {
    "principal_id": "prin_approver_0001",
    "principal_type": "person",
    "resolution": "server_resolved",
    "account_status": "active",
    "identity_assurance": "verified_session",
    "role_ids": [
      "role_manager_0001"
    ],
    "department_id": "dept_finance_0001",
    "service_identity_id": null,
    "delegation_ref": null
  },
  "delegations": [],
  "goal": {
    "summary": "Submit a synthetic expense report for review.",
    "completion_criteria": [
      {
        "criterion_id": "crit_expense_state_0001",
        "type": "business_state_equals",
        "verifier": {
          "verifier_id": "ver_expense_state_0001",
          "version": "1.0.0"
        },
        "resource_ref": "res_expense_0001",
        "expected": "submitted",
        "evidence_ref": "evidence:expense_state_0001",
        "evidence_hash": "sha256:2222222222222222222222222222222222222222222222222222222222222222"
      }
    ],
    "required_inputs": [
      "synthetic receipt reference"
    ],
    "out_of_scope": [
      "No payment execution."
    ]
  },
  "priority": {
    "requested_level": "high",
    "effective_level": "high",
    "authority_effect": "scheduling_only",
    "rationale": "Synthetic deadline."
  },
  "due_at": "2026-10-27T09:00:00Z",
  "sla": {
    "response_by": "2026-10-21T09:00:00Z",
    "complete_by": "2026-10-27T09:00:00Z",
    "escalation_after_minutes": 240
  },
  "execution_mode": "governed_resource_access",
  "requested_capabilities": [
    "expense.submit@1.0.0",
    "expense.read@1.0.0"
  ],
  "resource_scope": {
    "scope_type": "record_set",
    "resource_ids": [
      "res_expense_0001"
    ],
    "connector_ids": [
      "conn_finance_0001"
    ],
    "data_classifications": [
      "internal"
    ],
    "exclude_resource_ids": []
  },
  "policy_version": {
    "policy_set_id": "polset_default_0001",
    "version": "1.0.0",
    "content_hash": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "risk": {
    "level": "medium",
    "score": 40,
    "source": "server_policy"
  },
  "requested_limits": {
    "max_calls": 20,
    "max_runtime_seconds": 600,
    "max_cost_units": 0
  },
  "confirmations": [
    {
      "confirmation_id": "conf_expense_0001",
      "payload_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "canonicalization": "rfc8785_jcs",
      "purpose": "data_release",
      "confirmed_by_principal_id": "prin_user_0001",
      "status": "satisfied",
      "satisfied_at": "2026-10-20T02:05:00Z",
      "expires_at": "2026-10-20T02:30:00Z",
      "consumed_at": null,
      "consumed_by_call_id": null,
      "single_use": true,
      "channel_binding": {
        "channel": "mcp",
        "channel_identity_ref": "chan_user_session_0001"
      },
      "issued_at": "2026-10-20T02:03:00Z"
    }
  ],
  "approvals": [
    {
      "approval_id": "appr_expense_0001",
      "approver_principal_id": "prin_approver_0001",
      "payload_hash": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
      "canonicalization": "rfc8785_jcs",
      "decision": "approved",
      "separation_check": "passed",
      "decided_at": "2026-10-20T02:10:00Z",
      "expires_at": "2026-10-21T02:10:00Z"
    }
  ],
  "limitations": [],
  "result": {
    "outcome": "unknown",
    "result_ref": null,
    "evidence_refs": [
      "evidence:reconcile_completed_0001"
    ],
    "stop_confirmed": false,
    "verification": {
      "result": "pending",
      "verifier_id": null,
      "verified_at": null,
      "evidence_refs": []
    },
    "completed_at": null
  },
  "timestamps": {
    "created_at": "2026-10-20T01:55:00Z",
    "updated_at": "2026-10-20T02:35:00Z",
    "submitted_at": "2026-10-20T01:56:00Z",
    "evaluated_at": "2026-10-20T02:00:00Z",
    "last_event_at": "2026-10-20T02:35:00Z"
  },
  "status": {
    "state": "needs_human",
    "state_revision": 10,
    "last_event_id": "evt_reconcile_completed_0001",
    "attempt": 1,
    "max_attempts": 3,
    "retry_budget_remaining": 2
  },
  "linkage": {
    "grant_id": "grnt_expense_lost_0001",
    "grant_status": "revoked",
    "runtime_task_id": null,
    "execution_ref": "exec_governed_0001",
    "parent_task_id": null,
    "supersedes_task_id": null,
    "correlation_id": "corr_expense_0001"
  },
  "decision": {
    "decision_id": "gdec_allow_0001",
    "decision_type": "allow",
    "decision_revision": 1,
    "decided_at": "2026-10-20T02:00:00Z"
  },
  "record_completeness": {
    "completeness": "complete",
    "incomplete_reason": null
  },
  "labels": {
    "fixture": "synthetic",
    "path": "§19.1 F41: needs_human from unknown with revoked Grant; T59/T60 forbidden (schema-valid)"
  }
}
```
