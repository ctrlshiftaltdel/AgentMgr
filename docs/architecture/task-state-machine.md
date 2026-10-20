# 任务状态机（v1.0.0）

- 状态：Phase 0 冻结候选；产品负责人确认前为 Proposed
- 契约版本：1.0.0
- 关联：Issue #77、[ADR-001](ADR-001-product-boundary.md)、[task-envelope](../contracts/task-envelope.schema.json)、[execution-grant](../contracts/execution-grant.schema.json)、[runtime-adapter](../contracts/runtime-adapter.md)、[threat-model](../security/threat-model.md)

## 1. 原则

1. **服务端唯一写状态。** 只有核心任务服务在验证事件、Grant、责任与前置条件后写状态并追加 task event；Runtime Adapter、客户端和模型只报告事实或提出请求。
2. **状态不是模型自述。** Agent 的“我完成了”“我已取消”不构成状态证据。
3. **取消请求 ≠ 已停止。** `cancel_requested` 只表示请求已登记；`cancelled` 需要停止证据与副作用对账。
4. **完成必须验证。** 进入 `succeeded` 之前必须经过 `verifying`；不得从 `running`、`unknown` 或 `cancel_requested` 直接成功。
5. **失败关闭。** 身份、策略、Grant、版本、责任或运行时事实无法确定时，进入 `denied`、`unknown` 或 `needs_human`，不得猜测成功。
6. **优先级不越权。** 优先级变化只影响队列排序、SLA 与升级，不产生新的状态转换权限。
7. **幂等与版本化。** 每次转换都带 `state_revision`，旧版本写入被拒绝；重复事件不重复产生副作用。

## 2. 状态

| 状态 | 含义 | 终态 | 可自动离开 |
|---|---|---|---|
| `draft` | 已接收但尚未评估；字段可能不完整 | 否 | 是 |
| `evaluated` | PDP 已给出机器可判定决策 | 否 | 是 |
| `awaiting_clarification` | 等待请求方补充目标、输入、范围或责任人 | 否 | 是（需新输入） |
| `awaiting_confirmation` | 等待受影响用户对具体 payload 确认 | 否 | 是（需有效确认） |
| `awaiting_approval` | 等待独立审批人审批 | 否 | 是（需有效审批） |
| `queued` | 已获授权，等待 Worker 调度 | 否 | 是 |
| `dispatched` | 已提交运行时，尚未确认运行 | 否 | 是 |
| `running` | 运行时有执行证据 | 否 | 是 |
| `verifying` | 运行时已结束，核心正在验证结果与证据 | 否 | 是 |
| `succeeded` | 验证通过且证据完整 | **是** | 否 |
| `denied` | 策略、身份、审批或安全边界拒绝 | **是** | 否 |
| `cancel_requested` | 取消已登记，尚未确认停止 | 否 | 是（需停止/对账证据） |
| `cancelled` | 已确认停止且副作用已对账 | **是** | 否 |
| `failed` | 本次执行尝试失败 | **尝试终态** | 仅 T65 有界重试 |
| `timed_out` | 已确认超时且执行边界关闭 | **是** | 否 |
| `needs_human` | 需要人工接管、裁决或补充证据 | 否 | 是（人工动作） |
| `partial_success` | 仅部分完成定义通过且有证据 | **是** | 否（新任务另立） |
| `unknown` | 无法确定运行时事实，等待对账 | 否 | 是（对账后） |

`unknown` 和 `needs_human` **不是终态**；不得把它们当作成功、失败或取消的替代。

## 3. 事件与守卫

### 3.1 事件类型

| 类别 | 事件示例 | 产生者 |
|---|---|---|
| 任务 | `task.created`、`task.evaluated`、`task.cancel_requested`、`task.cancelled` | 核心服务 |
| 治理 | `governance.denied`、`clarification.provided`、`confirmation.recorded`、`approval.recorded` | 核心服务 / PDP |
| 调度 | `worker.leased`、`dispatch.accepted`、`dispatch.rejected`、`dispatch.timeout` | Worker / Adapter |
| 运行时 | `runtime.running`、`runtime.completed`、`runtime.failed`、`runtime.lost`、`runtime.stopped` | Adapter（事实） |
| 验证 | `verification.passed`、`verification.partial`、`verification.failed`、`verification.inconclusive` | 核心验证器 |
| 对账 | `reconcile.running`、`reconcile.completed`、`reconcile.stopped`、`reconcile.unresolved` | Worker / 人工 |
| 人工 | `human.takeover`、`human.decision`、`human.stop` | 管理面（审计） |

### 3.2 通用守卫

除状态特定守卫外，每次转换必须满足：

- 调用者已认证且不是停用/解绑主体；
- 任务 `state_revision` 与事件预期版本一致；
- 策略版本、Grant、工具契约版本在有效期内且匹配；
- 责任字段完整：`accountable_owner` 已解析，`approver` 在需要审批时独立；
- 事件幂等键未被消费，或重复事件返回原结果；
- 写入 task event 与审计事件在同一事务内完成（Outbox 至少一次投递）。

## 4. 允许的转换（共 66 条，含初始创建）

> 表格中的“Grant”均指当前任务绑定的、`status=active`、未撤销、未过期、未消费且范围/版本匹配的 Execution Grant。任何 Grant 校验失败都不得进入 `queued` 或 `dispatched`。

### 4.1 初始创建

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T00 | `(none)` → `draft` | `task.created` | 认证 Requester 存在；幂等键首次使用或相同 payload；目标摘要可缺失但必须记录缺失项 |

### 4.2 从 `draft`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T01 | `draft` → `evaluated` | `task.evaluated` | 必填字段可被 PDP 读取；策略版本可解析；决策已持久化 |
| T02 | `draft` → `denied` | `governance.denied` | PDP 明确 deny，或策略服务不可用/版本无效导致 fail-closed deny |
| T03 | `draft` → `cancelled` | `task.cancelled` | Requester 或授权服务身份取消；从未 dispatch，无副作用 |

### 4.3 从 `evaluated`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T04 | `evaluated` → `awaiting_clarification` | `governance.clarification_required` | 决策 `needs_clarification`；缺失项可枚举 |
| T05 | `evaluated` → `awaiting_confirmation` | `governance.confirmation_required` | 决策 `needs_confirmation`；payload hash 已固定 |
| T06 | `evaluated` → `awaiting_approval` | `governance.approval_required` | 决策 `needs_approval`；Approver 已解析且独立 |
| T07 | `evaluated` → `denied` | `governance.denied` | 决策 `deny`；记录稳定原因码 |
| T08 | `evaluated` → `queued` | `task.queued` | 决策 `allow` 或 `managed_execution_required`；Grant 已签发且 active；确认/审批门禁已满足；owner/assignee/executor 完整；所需运行时能力为 `verified` |
| T09 | `evaluated` → `cancel_requested` | `task.cancel_requested` | 取消请求在入队前到达；幂等键有效 |
| T10 | `evaluated` → `needs_human` | `human.escalation_required` | 责任冲突、委派无效或策略异常无法自动消解；不得据此签发 Grant |

### 4.4 从 `awaiting_clarification`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T11 | `awaiting_clarification` → `evaluated` | `clarification.provided` | 新输入完整；payload hash 更新；重新走 PDP（不得沿用旧决策） |
| T12 | `awaiting_clarification` → `denied` | `governance.denied` | 澄清窗口/SLA 过期，或请求方明确撤回且策略要求拒绝；记录原因码 |
| T13 | `awaiting_clarification` → `cancelled` | `task.cancelled` | Requester 或授权服务身份取消；无副作用 |
| T14 | `awaiting_clarification` → `needs_human` | `human.escalation_required` | 无法确定澄清责任人，或澄清内容涉及安全边界 |

### 4.5 从 `awaiting_confirmation`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T15 | `awaiting_confirmation` → `evaluated` | `confirmation.recorded` | 确认未过期、未消费；payload hash 与当前规范化 payload 完全一致；确认人有权确认 |
| T16 | `awaiting_confirmation` → `denied` | `confirmation.rejected` | 确认被拒绝、过期或 payload 已变化且未重新确认 |
| T17 | `awaiting_confirmation` → `cancelled` | `task.cancelled` | Requester 取消；无副作用 |
| T18 | `awaiting_confirmation` → `needs_human` | `human.escalation_required` | 无可用确认人，或确认人与 Requester/Executor 冲突 |

### 4.6 从 `awaiting_approval`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T19 | `awaiting_approval` → `evaluated` | `approval.recorded` | 审批 `approved`、未过期、payload hash 一致；Approver 独立；`separation_check=passed`；重新评估 |
| T20 | `awaiting_approval` → `denied` | `approval.rejected` | 审批 `rejected`/`expired`/`revoked`，或检测到自审/代审；记录原因码 |
| T21 | `awaiting_approval` → `cancelled` | `task.cancelled` | Requester 取消；无副作用 |
| T22 | `awaiting_approval` → `needs_human` | `human.escalation_required` | 审批人不可用、审批链冲突或升级规则触发 |

### 4.7 从 `queued`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T23 | `queued` → `dispatched` | `worker.leased` + `dispatch.accepted` | Worker 持有未过期租约与 fencing token；Grant 每项绑定匹配；门禁满足；无其他活动 attempt；所需运行时能力 `verified` |
| T24 | `queued` → `cancel_requested` | `task.cancel_requested` | 取消请求在 dispatch 前到达；幂等 |
| T25 | `queued` → `denied` | `governance.denied` | Grant 被撤销、策略/审批在排队期间失效；未 dispatch，无副作用 |
| T26 | `queued` → `needs_human` | `human.escalation_required` | 超过排队 SLA 仍无合格执行者，或运行时能力降级 |
| T27 | `queued` → `timed_out` | `task.deadline_exceeded` | 队列/SLA 截止已过且从未 dispatch；确认无副作用 |
| T28 | `queued` → `failed` | `dispatch.unavailable` | 无可用 Adapter/配置不可恢复；确认无副作用；记录兼容性缺口 |

### 4.8 从 `dispatched`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T29 | `dispatched` → `running` | `runtime.running` | 已绑定 `runtime_task_id`；Adapter 报告运行证据/新鲜心跳；Grant 仍 active |
| T30 | `dispatched` → `failed` | `dispatch.rejected` | 运行时明确拒绝且确认无副作用；记录错误码 |
| T31 | `dispatched` → `unknown` | `dispatch.timeout` | 提交超时/连接中断，无法确认是否受理；**禁止重发创建请求** |
| T32 | `dispatched` → `cancel_requested` | `task.cancel_requested` | 取消请求已登记；未确认停止 |
| T33 | `dispatched` → `queued` | `worker.lease_expired` | 查询确认运行时未受理、无副作用；重试预算未耗尽；幂等键已记录 |
| T34 | `dispatched` → `timed_out` | `dispatch.deadline_exceeded` | 提交窗口截止，且查询明确无运行时任务/无副作用；否则走 T31 |
| T35 | `dispatched` → `needs_human` | `human.escalation_required` | 适配器不支持和/或受理结果互相矛盾，无法自动判定 |

### 4.9 从 `running`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T36 | `running` → `verifying` | `runtime.completed` | 运行时报告结束且 `get_result` 可取；结构化结果/证据引用已持久化；Grant 在执行期间有效 |
| T37 | `running` → `failed` | `runtime.failed` | 运行时明确失败且已停止；记录错误码；无未对账副作用 |
| T38 | `running` → `timed_out` | `runtime.timed_out` | 运行时确认超时且 `stop_confirmed=true`；否则走 T40 |
| T39 | `running` → `cancel_requested` | `task.cancel_requested` | 取消请求已登记；不视为已停止 |
| T40 | `running` → `needs_human` | `human.escalation_required` | 运行时要求输入且无授权自动回答源，或人工接管 |
| T41 | `running` → `unknown` | `runtime.lost` | 心跳/查询超阈值或状态矛盾；进入对账，不得重发 |

### 4.10 从 `verifying`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T42 | `verifying` → `succeeded` | `verification.passed` | 所有 success criteria 通过确定性检查；证据引用完整、可追溯；无未解决冲突 |
| T43 | `verifying` → `partial_success` | `verification.partial` | 仅策略显式允许的子集通过；未完成项已列明；owner/责任人接受并有审计 |
| T44 | `verifying` → `failed` | `verification.failed` | 验证不通过且无重试价值；记录失败原因与副作用状态 |
| T45 | `verifying` → `needs_human` | `verification.inconclusive` | 证据不足/冲突，需人工复核；不得默认成功 |
| T46 | `verifying` → `unknown` | `verification.evidence_unavailable` | 证据源不可用；进入对账/补证 |
| T47 | `verifying` → `cancel_requested` | `task.cancel_requested` | 用户取消后续动作；已产生的副作用必须保留并进入对账 |

### 4.11 从 `cancel_requested`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T48 | `cancel_requested` → `cancelled` | `runtime.stopped` | `stop_confirmed=true`；不再产生新副作用；结果/副作用已对账 |
| T49 | `cancel_requested` → `running` | `runtime.running` | 新鲜状态证明运行时仍在执行且未接受取消；继续尝试停止 |
| T50 | `cancel_requested` → `verifying` | `runtime.completed` | 取消生效前任务已完成；结果可取；不得丢弃结果 |
| T51 | `cancel_requested` → `unknown` | `runtime.lost` | 无法确认停止状态；进入有界对账 |
| T52 | `cancel_requested` → `needs_human` | `human.escalation_required` | 停止未确认超过阈值，或运行时明确不支持取消 |

### 4.12 从 `unknown`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T53 | `unknown` → `running` | `reconcile.running` | 对账获得新鲜运行证据；Grant 未过期/未撤销 |
| T54 | `unknown` → `verifying` | `reconcile.completed` | 对账取得结构化结果与证据；进入验证 |
| T55 | `unknown` → `failed` | `reconcile.failed` | 对账确认明确失败且已停止；副作用已记录 |
| T56 | `unknown` → `cancelled` | `reconcile.stopped` | 对账确认已停止且无新副作用 |
| T57 | `unknown` → `needs_human` | `reconcile.unresolved` | 对账尝试耗尽或证据冲突；转人工，不猜测 |
| T58 | `unknown` → `timed_out` | `reconcile.timed_out` | 对账取得运行时超时证据且 `stop_confirmed=true`；否则保持 `unknown`/`needs_human` |

### 4.13 从 `needs_human`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T59 | `needs_human` → `running` | `human.takeover.running` | 人工确认任务确实在运行；记录操作者与证据；不新增能力 |
| T60 | `needs_human` → `verifying` | `human.result_submitted` | 人工提交结构化结果与证据；仍需走验证，不得直接成功 |
| T61 | `needs_human` → `queued` | `human.redispatch` | 已决定重派；**重新签发 Grant**；检查重试预算与幂等 |
| T62 | `needs_human` → `denied` | `human.authorization_revoked` | 人工/策略撤销授权；记录原因码与审计 |
| T63 | `needs_human` → `cancelled` | `human.stop_confirmed` | 人工确认已停止且无新副作用 |
| T64 | `needs_human` → `failed` | `human.declared_failed` | 人工判定失败且不再重试；记录证据 |

### 4.14 从 `failed`

| ID | From → To | 事件 | 守卫 |
|---|---|---|---|
| T65 | `failed` → `queued` | `task.retry_authorized` | 失败可安全重试；`attempt < max_attempts`；**重新签发 Grant**；确认无未对账副作用；幂等策略成立 |

### 4.15 终态

`succeeded`、`denied`、`cancelled`、`timed_out`、`partial_success` 没有任何出边。需要继续工作时应创建新任务，并通过 `linkage.supersedes_task_id`/`parent_task_id` 关联，不得改写历史状态。

## 5. 禁止的转换

以下转换必须在服务层显式拒绝并记录安全事件；不得仅靠 UI 隐藏。

| ID | 禁止转换 | 理由 |
|---|---|---|
| F01 | `draft`/`evaluated` → `running`/`succeeded` | 跳过评估、入队、派发与验证 |
| F02 | `queued` → `running`/`succeeded` | 跳过派发与运行时事实 |
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
| F14 | 任一终态 → 任一非终态 | 改写历史；新工作必须新任务 |
| F15 | 使用过期/撤销/已消费 Grant 进入 `dispatched` | Grant 生命周期约束 |
| F16 | `single_use` Grant 第二次调用 | 一次性消费语义 |
| F17 | 用任务 A 的 Grant 执行任务 B | Grant 必须绑定 `task_id`（及 revision） |
| F18 | 自审批（Requester/Executor 作为唯一 Approver） | 职责分离；策略显式例外也需审计与风险记录 |
| F19 | 高优先级修改导致 `denied` 重开 | 优先级不提升权限 |
| F20 | `unknown` → `cancelled` 无停止/对账证据 | 未知不等于已停止 |
| F21 | `dispatched` → `running` 无 `runtime_task_id` 或 Grant 已失效 | 无绑定的运行事实不可信 |
| F22 | `running` → `verifying` 无结构化结果/证据 | 运行时自述“完成”不构成结果 |
| F23 | Worker 无租约/过期 fencing token 写状态 | 防止脑裂与重复副作用 |
| F24 | 模型或客户端直接写 `status.state` | 状态只能由服务端验证转换写入 |

## 6. 幂等、重复副作用与并发

### 6.1 幂等键作用域

| 操作 | 幂等键 | 作用域 |
|---|---|---|
| 创建任务 | `idempotency_key` | 已验证 Principal + 操作类型；同键同 payload 返回同一 `task_id` |
| 提交运行时 | `(task_id, grant_id, idempotency_key)` | 任务 + Grant；Adapter 保证最多一个运行时任务 |
| 工具调用 | `(grant_id, capability, payload_hash, call_id)` | Grant + 能力 + 规范化 payload |
| 取消 | `(task_id, cancel_id)` | 任务；重复取消返回原结果 |
| 确认/审批 | `confirmation_id` / `approval_id` | 一次性消费；重复提交返回原状态 |
| 状态事件 | `(task_id, state_revision)` | 唯一约束；过期 revision 拒绝 |

同键不同 payload 必须返回冲突（`conflict`），不得创建第二个副作用。

### 6.2 重复副作用防护

- 写动作前先在 PEP 中预占 `call_id` 并持久化；成功后记录结果，失败后记录可重试状态。
- 业务 handler 必须使用唯一约束或幂等表；不得依赖“应该只调用一次”。
- 重试前检查该动作是否幂等；不可安全幂等的动作不得自动重试（T40/T45/T57）。
- 取消与完成竞态以运行时事实为准；不得因取消丢弃已完成结果。
- 超过一次的业务副作用必须进入 `needs_human` 对账，不得静默继续。

## 7. Worker 租约与 fencing

- Worker 通过 `worker.leased` 获取租约，记录 `lease_owner`、`lease_expires_at`、`fencing_token`。
- 只有持有当前 fencing token 的 Worker 能写入 `dispatched`、`running`、`verifying` 等转换。
- 租约过期后，新 Worker 必须先用 `get_status`/幂等查询确认无活动运行时任务；否则进入 `unknown`，不得直接重派。
- 心跳丢失但租约未过期：保持租约，不重复派发。
- 状态写入使用 `state_revision` 乐观锁；旧 revision 返回冲突并触发对账。

## 8. 超时与 `unknown` 恢复

1. **先查询，后重试。** 提交超时/连接中断时禁止重发创建请求；用 `runtime_task_id` 或幂等键查询。
2. `not_found` 不等于失败；只有在运行时明确保证无副作用且查询窗口结束后，才可按 T30/T33/T34 处理。
3. 状态查询超时/心跳丢失 → `unknown`；按退避策略对账（次数与总时长有上限）。
4. 对账取得运行证据 → T53；取得结果 → T54；确认停止 → T56；确认失败 → T55；证据冲突/耗尽 → T57。
5. `unknown` 期间不得签发新 Grant、不得调用新工具、不得显示为成功或已取消。
6. 运行时恢复后，已过期 Grant 不得复活；需要继续时必须重新评估并重签 Grant。

## 9. 取消语义

- `cancel_requested` 只表示请求已登记，不是停止。
- 取消后不再允许新工具调用；已发出的调用必须等待结果或进入对账。
- 只有 `stop_confirmed=true`（或等价运行时证据）才能进入 `cancelled`；结束后仍需对账副作用。
- 取消前已完成的任务走 `verifying`，不得因取消请求丢弃结果。
- 运行时不支持取消 → `needs_human`，不得虚报已停止。
- 取消竞态不得通过“最后写入者获胜”解决；以运行时事实 + 证据为准。

## 10. 重试

- 仅 `failed` 可在有界条件下经 T65 重试；`denied`、`cancelled`、`timed_out`、`partial_success` 不得自动重试。
- 重试必须有新的 Grant（策略/时间/范围可能变化）、`attempt` 递增、幂等检查与预算检查。
- `max_attempts` 由策略固定；达到上限进入 `needs_human`/最终失败，不得无限重试。
- 不可安全幂等的动作不得自动重试；需要人工确认或补偿。
- 重试不得扩大 capability、resource scope、TTL 或预算。

## 11. 人工接管

- `needs_human` 必须记录接管原因、责任人、截止时间与所需动作。
- 人工接管不自动获得新能力；需要新能力时必须重新评估并签发 Grant。
- 人工只能通过审计化命令触发 T59–T64；不得直接改数据库状态。
- 人工提交结果仍须 `verifying`；人工不能把“我看过了”当作验证通过。
- 人工停止仍需停止证据；无法确认时保持 `unknown`/`needs_human`。

## 12. 与契约字段的对应

| 状态机概念 | 契约字段 |
|---|---|
| 当前状态 | `task-envelope.status.state` |
| 状态版本 | `task-envelope.status.state_revision` |
| 最后事件 | `task-envelope.status.last_event_id` |
| 尝试次数 | `task-envelope.status.attempt` |
| Grant 绑定 | `execution-grant.task_ref` / `task-envelope.linkage.grant_id` |
| 运行时句柄 | `task-envelope.linkage.runtime_task_id` |
| 确认/审批 | `task-envelope.confirmations[]` / `approvals[]` |
| 决策 | `governance-decision.decision_type` / `reason_codes` |
| 优先级不越权 | `task-envelope.priority.authority_effect = scheduling_only` |
| 取消请求 | `task-envelope.status.state = cancel_requested`，无 `stop_confirmed` 字段时不得推断已停止 |

## 13. 验收要点

- 每个状态至少有一个允许入边和一个允许出边（终态除外）。
- 66 条允许转换均有事件、守卫和测试用例；F01–F24 均有拒绝用例。
- 每条负向测试必须断言“状态未变 + 安全事件已记录”，而不仅是返回错误。
- 状态机测试必须覆盖并发 revision、重复事件、取消竞态、提交超时与对账恢复。
- 未实现的转换不得注册为可调用 API；不得用文档承诺替代测试。