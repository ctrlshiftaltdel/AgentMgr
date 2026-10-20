# ADR-001：产品边界、执行模式与信任边界

- 状态：Proposed（Phase 0 契约冻结候选；产品负责人确认是独立门禁）
- 日期：2026-10-20
- 契约版本：1.0.0
- 关联：Issue #77、Epic #74；目标架构 `docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md`；项目章程 `docs/PROJECT-CHARTER.md`
- 影响契约：[governance-decision](../contracts/governance-decision.schema.json)、[task-envelope](../contracts/task-envelope.schema.json)、[execution-grant](../contracts/execution-grant.schema.json)、[runtime-adapter](../contracts/runtime-adapter.md)、[task-state-machine](task-state-machine.md)、[threat-model](../security/threat-model.md)

## 1. 背景

AgentMgr 起源于单机离线规则包和 Hermes 兼容实现，但新的目标定位是**独立的 Agent 治理与企业决策服务**，不是任一 Agent 框架的管理面板。WorkBuddy、Hermes、Rakazo、Codex、OpenCode 等运行时已经具备命令行、文件读写、浏览器控制、连接器、模型推理和通用规划能力；AgentMgr 若平行重造这些能力，会同时失去可替换性和安全边界。

真正需要由 AgentMgr  deterministic 判定的，是执行前后的关口：任务是否必要、优先级如何、谁对结果负责、谁有权执行、允许和禁止哪些能力、资源范围、是否需要澄清/确认/审批、如何验证结果、失败后谁接手。这些问题必须落到机器可判定、可审计、可撤销的契约，而不是提示词或模型自述。

本 ADR 冻结三件事：

1. 产品做什么、不做什么；
2. 三种执行模式及各自可对外声称的治理强度；
3. 客户端、服务端业务工具、运行时控制之间的信任边界与凭据隔离。

## 2. 决策

### 2.1 产品边界

AgentMgr 是**独立 Agent 治理与决策服务**，负责：

- 可信 Principal 解析、人员/服务主体、角色、部门、委派与职责分离；
- 版本化策略、PDP 决策、PEP 执行点与 fail-closed 语义；
- 结构化任务、责任归属、优先级/SLA、澄清、确认、审批、取消与人工接管；
- 有范围、有期限、可撤销、绑定版本的 Execution Grant；
- 受控业务工具的 MCP/REST 暴露与每次调用重授权；
- 任务状态、事件、执行证据、审计与运行时不兼容项记录；
- 通过内部 Runtime Adapter 接入服务器端 Agent 运行时。

AgentMgr **不负责**：

- 重造通用 CLI、浏览器、文件管理、办公软件连接器、数据库客户端；
- 训练模型、建设基础模型、实现万能软件操控器；
- 替代 Agent 运行时的规划、记忆或对话能力；
- 在未部署 PEP 或受控执行环境时，声称能强制约束客户端本地全部操作。

完整非目标见 §6。

### 2.2 三种执行模式

每个任务、API 响应、界面和审计记录必须标明执行模式及剩余风险。模式不是营销标签，而是可验证的边界声明。

| 模式 | 含义 | 强制执行点 | 可以声称 | 不得声称 |
|---|---|---|---|---|
| **A. Advisory / BYOA**（客户端/建议模式） | 用户现有 Agent 连接 AgentMgr，获得策略建议、登记任务、调用受控业务工具 | 仅经过 AgentMgr 的 MCP/REST 调用与受控业务工具 | 对“经过本服务的调用”做治理、审计和授权 | 能约束客户端本地的 Shell、文件、浏览器、共享盘或用户自有凭据 |
| **B. Governed Resource Access**（企业资源受控模式） | 企业数据库、业务 API、文件存储、软件账号只允许经 AgentMgr 的 PEP 或同等受控代理访问 | 连接器/PEP、网络 ACL、防火墙、凭据托管、资源侧 ACL | 对企业资源访问做可靠授权、范围限制与审计 | 当同一用户/Agent 仍持有直连凭据或可绕过代理时，声称已完全阻断 |
| **C. Managed Execution**（受控执行模式） | Agent 在服务器端隔离服务账户/沙箱中运行，使用任务级凭据、受限文件系统、受限网络出口和运行时策略 | OS 身份、沙箱、短期凭据、出口规则、运行时 PEP | 对运行中的 Shell、浏览器和文件操作本身做强制边界 | 在隔离、凭据注入和出口限制未实测前，声称“受控执行”已商用 |

模式判定由服务端策略给出，客户端不能自选为更强模式。`advisory` 任务即使用户要求，也不得自动升级为 `managed_execution`；升级必须重新评估、重新签发 Grant，并具备已验证的运行时能力（见 [runtime-adapter](../contracts/runtime-adapter.md)）。

**模式中立执行边界。** 三种模式共用同一任务状态机与 `execution.*` 事件：Advisory 由受控客户端调用（B1）产生，Governed 由 PEP/连接器（B2）产生，Managed 由 Runtime Adapter（B3）产生。不得把 Runtime Adapter 当成唯一执行边界，也不得要求 Advisory/Governed 具备运行时能力。能力守卫按模式求值；任务记录与审计必须保存模式及 `limitations`。

**授权型决策绑定任务。** `allow` / `managed_execution_required` 必须绑定非空 `task_id` 与 `task_revision`。任务创建前只能返回 `deny` / `needs_*` / `pre_evaluation`；不存在可空 `task_id` 的预授权。

**失败是尝试终态。** `failed` 不是任务终态；重试必须重新执行 PDP、签发新决策与新 Grant，并受 `max_attempts`/`retry_budget_remaining` 约束。禁止复用过期决策或旧 Grant。

**默认禁止自审。** 1.0.0 冻结 `Accountable Owner ≠ Approver`；不存在策略例外。未来若要允许例外，必须升主版本并增加例外引用、理由、策略版本与审计字段。职责分离按 **principal 身份**判定：同一 principal 不得审批自己的任务，同一组织角色下的不同人员可以审批；`separation_check` 由服务端计算，不接受客户端提交值。服务端必须检查 `approvals[]` 中所有 `decision=approved` 的实际审批人以及 Grant 的 `approval_ref.approver_principal_id`，不能只检查任务级 `approver`；任务级 `approver` 必须解析为实际行为人并与授权 Grant 引用的审批一致（S-14）。

**早期退出必须显式标注不完整。** 任务在 `draft`/`awaiting_clarification`，或在该阶段被 `denied`/`cancelled`/`needs_human`、登记为执行前 `cancel_requested`（以及由这些状态产生的 `failed`）时，允许 `goal=null`、`accountable_owner` 未解析、`execution_mode`/`scope_type=undetermined`，但记录必须标记 `record_completeness.completeness=pre_execution_incomplete` 并说明 `incomplete_reason`，且不得关联任何 Grant。执行开始（`attempt>=1`）后与执行后状态必须是完整记录。

**终态必须使 Grant 失效。** 任务或尝试进入 `succeeded`/`partial_success`/`failed`/`cancelled`/`timed_out`/`denied` 时，Grant 必须为 `revoked` 或 `consumed`；进入 `unknown` 时必须在同一事务内立即 `revoked`（`reason_code=execution_evidence_lost`）。被撤销的 Grant 永不复活、永不复用；PEP 除校验 Grant 外还必须读取当前任务状态，终态或 `unknown` 任务上的 active Grant 一律拒绝。

**`unknown` 必须 fail-closed。** 不存在 `unknown → running` / `unknown → verifying`（原 T53/T54 已撤销）。恢复只能通过：`unknown → queued`（T71，`attempt=0` + 无副作用证据 + 新决策 + 新 Grant）、`unknown → failed`（T55）后经 T65 有界重试、或 `unknown → cancelled`/`needs_human`/`timed_out` 终止/人工接管。若执行边界实际仍在运行，必须先停止并取得停止证据；不得直接恢复执行。

**`attempt` 只统计已开始的执行。** 首次 `queued` 的 `attempt=0`；只在 `dispatch.accepted` 递增。预派发失败（`attempt=0`）不消耗 `retry_budget_remaining`，也不能走 T65；`attempt=0` 的人工失败必须登记为 `cancelled` 或 `denied`。T65 要求 `attempt>=1` 且进入前 `retry_budget_remaining>0`，进入时递减 1；递减后的余额可为 0（最后一次允许的重试），Grant 的 `attempt.retry_budget_remaining` 记录同一余额。`unknown`/`needs_human` 不关闭尝试；T61 必须已有 `stop_confirmed=true` 或证明从未开始，且不得存在未对账的 active attempt。

**Owner 停用必须显式升级。** `queued`/`dispatched`/`running`/`verifying`/`awaiting_confirmation`/`awaiting_approval` 要求 Owner 已解析且 `active`，且不得携带 `owner_escalation`；终态/历史记录（含 `succeeded`/`partial_success`）允许 Owner 已解析但 `disabled`/`unbound`/`unknown`，但必须带完整 `owner_escalation`（原因、时间、接管/通知主体），Schema 已将其设为条件必填。`awaiting_clarification` 按 A1 仍可未解析，但离开该状态前必须解析并校验。

### 2.3 信任边界

系统至少区分以下边界。它们不得共享同一个无限制管理员令牌，也不得互相代替认证。

| 边界 | 参与者 | 信任假设 | 允许的凭据 | 禁止 |
|---|---|---|---|---|
| **B0 客户端本地环境** | 用户设备上的 Agent、Shell、浏览器、文件、个人凭据 | 不可信；可能被提示注入、恶意扩展或用户自行绕过 | 不适用 | 不得把本地工具访问当作已受 AgentMgr 治理 |
| **B1 客户端 MCP / REST** | WorkBuddy、Codex、OpenCode、Web 等 | 传输可认证，但请求内容（含模型生成参数）不可信 | 用户会话令牌、OAuth/mTLS，绑定已验证 Principal | 禁止自报身份、角色、owner、审批结果；禁止管理接口 |
| **B2 服务端 Agent 业务工具 MCP / PEP** | 当前任务的 Agent、业务连接器 | Agent 可能被注入；每次调用都不可信 | 任务级、短时、最小权限凭据；按调用重验 Grant | 禁止任意 SQL/Shell/万能 `execute_action`；禁止跳过 PEP |
| **B3 Runtime Adapter / 运行时控制** | AgentMgr Worker、Hermes/Rakazo 等适配器 | 高权限内部边界；实现缺陷影响大 | 独立 workload identity / mTLS；任务级运行时凭据注入 | 禁止与 B1/B2/B4 共用令牌；禁止把运行时私有对象/路径泄漏到核心 |
| **B4 管理 REST / 最小管理界面** | 系统管理员、审计员 | 人类管理员可信但权限必须最小化 | 独立管理会话、强认证、职责分离 | 管理员身份不得自动获得财务审批权；禁止用管理令牌调用 B1/B2 |
| **B5 数据、秘密与审计存储** | PostgreSQL、附件存储、Secret Manager、审计存储 | 只存凭据引用，不存明文秘密 | 数据库最小权限账号；DDL 独立身份 | 禁止把令牌、密码、完整票据写入任务提示、Trace 或普通日志 |

关键不变量：

- **认证一次不等于授权一次。** B1 的会话只证明“谁在调用”；B2/B3 的每次敏感动作必须重新检查任务绑定、Grant、策略版本、工具版本、资源范围和预算，以及**当前任务状态**（终态或 `unknown` 任务不得放行 active Grant）。
- **服务身份不是超级用户。** B2/B3 的非人身份只代表连接器/运行时的最小权限，不得用于绕过员工职权、审批或数据范围。
- **管理面与业务面隔离。** B4 不得直接签发 Grant、代替审批或调用运行时控制接口；确需运维操作时必须走单独审计流程。
- **凭据按边界与任务拆分。** 不存在一个“什么都能做”的共享管理员 token。

### 2.4 远程治理的明确限制

远程 MCP/REST **只能治理经过该服务的调用**。若客户端 Agent 持有本地高权限 Shell、浏览器会话、共享盘或 API 密钥，它仍可能绕过 AgentMgr。因此：

1. Advisory 模式只提供建议与受控工具，不得宣称强制治理本地全部操作。
2. 需要可靠企业数据权限时，必须采用 Governed Resource Access：资源只允许经 PEP/受控代理访问，并通过 ACL、网络和凭据托管阻断直连。
3. 需要约束 Shell、浏览器、文件操作本身时，必须采用 Managed Execution：隔离服务账户/沙箱、短期任务级凭据、受限文件系统与网络出口。
4. 若某项约束无法技术强制，必须在任务、API、审计和发布说明中记录剩余风险，并缩小安全承诺；不得用提示词、工具描述或“Agent 会自觉遵守”替代执行点。
5. **提示词与工具描述不是安全边界。** 它们可以改善行为，但不构成强制控制，也不作为任何验收证据。

### 2.5 术语与责任分离

本 ADR 采用计划 §4 的术语，并由 [task-envelope](../contracts/task-envelope.schema.json) 与 [governance-decision](../contracts/governance-decision.schema.json) 固化：

- **Principal**：经验证的用户、服务或运行时身份。
- **Requester**：任务发起者。
- **Accountable Owner**：对业务结果负责的人员/组织角色；必须可追责，不能以 Agent 名称替代。
- **Assignee**：负责推进工作的人员、Agent 或队列。
- **Executor**：实际执行的 Agent 实例或人工执行者。
- **Approver**：具有独立授权的审批人；遵守禁止自审。
- **Delegation**：有范围、有期限、可撤销的受限委派。
- **Service Identity**：连接器/运行时使用的非人身份，不是管理员身份。
- **Capability**：可请求的动作类别，不代表已授权。
- **Resource Scope**：允许访问的资源集合与数据分类。
- **PDP / PEP**：无副作用的策略判定 / 在调用点强制执行判定。
- **Execution Grant**：某次任务的授权快照。

这些角色不得合并为一个 `agent_id`。模型可以建议 owner、assignee 或优先级，但最终值由服务端身份目录、策略和职责分离规则解析；模型自报的身份与权限字段必须被忽略或拒绝（见 [threat-model](../security/threat-model.md) TH-01）。

**主体 ID 命名（三份契约统一）：**

| 前缀 | 用途 | 允许的角色 |
|---|---|---|
| `prin_` | 人员或 `agent_instance` 主体 | Requester、Accountable Owner、Assignee、Executor、Approver |
| `rt_` | 运行时主体 | `execution-grant.subject` / `governance-decision.subject`（运行时身份）；**不是** `task-envelope.executor` 的合法类型 |
| `svc_` | 服务身份 | Requester（服务调用）、连接器身份 |
| `role_` | 组织角色 | Accountable Owner、Assignee、Approver |
| `queue_` | 工作队列 | Assignee |
| `rtask_` | 运行时任务句柄（不是主体） | `linkage.runtime_task_id` |
| `exec_` | Advisory/Governed 执行句柄（不是主体） | `linkage.execution_ref` |

`accountable_owner` 与 `approver` 只能是 `person` 或 `role`，不得是 `agent_instance`、运行时或队列。`requester` 只能是 `prin_`/`svc_`/`rt_`（人员、服务、运行时或 agent_instance），不得是 `role_`/`queue_`。`resolution=server_resolved` 时 `principal_id` 必须非空；`pending` 只允许出现在 `record_completeness.completeness=pre_execution_incomplete` 的记录中，即 `draft`、`awaiting_clarification`、`decision_type=needs_clarification` 的 `evaluated`，以及执行前退出的 `denied`/`cancelled`/`needs_human`/`failed`。

主体前缀与类型必须一致，并由 Schema 与服务端同时校验：`prin_`↔person/agent_instance、`rt_`↔runtime、`svc_`↔service、`role_`↔role、`queue_`↔queue。`role_`/`queue_` 不是可认证主体，不得出现在决策 subject 或 Grant subject 中。

### 2.6 优先级不提升权限

优先级只影响调度顺序、SLA、预算排序和升级路径。它不能：

- 覆盖硬性拒绝、法律/安全边界或数据分类；
- 跳过澄清、确认或审批；
- 扩大 capability、resource scope、TTL 或预算；
- 把 `denied`、`cancel_requested`、`unknown` 等状态直接改为可执行状态。

契约中 `priority.authority_effect` 固定为 `scheduling_only`；任何以优先级为由的授权都必须被拒绝（原因码 `priority_cannot_override_deny`）。

### 2.7 MVP 采用模块化单体

MVP 在**一个可独立部署的服务**内按领域分层：身份、策略、任务、Grant、工具契约、Runtime Adapter、Worker、审计与管理 API。持久化使用 PostgreSQL，事务性 Outbox 与 Worker 处理异步调度。

不在没有真实容量或隔离证据前拆微服务，不默认引入 Kubernetes、Redis、Kafka 或外部消息系统。Runtime Adapter 是**模块边界和信任边界**，不是必须独立部署的微服务；当隔离或容量证据要求时可以独立部署，但核心业务不得依赖运行时私有对象。

## 3. 后果

### 3.1 正面

- 产品定位清晰：不与管理面板或 Agent 框架竞争，专注治理与决策。
- 可替换运行时：Hermes/Rakazo 通过同一 Adapter 契约接入，核心不绑定私有对象。
- 安全声明可验证：三种模式分别对应可检查的执行点，避免“提示词即治理”。
- 契约可并行：身份、任务、Grant、状态机和威胁模型为独立实现提供机器可判定基线。
- 失败关闭：身份、策略、版本或 Grant 任一不确定时拒绝有副作用动作。

### 3.2 代价与约束

- 必须投资 PEP、连接器、隔离运行环境和任务级凭据；纯文档无法交付强制治理。
- 部署与运维复杂度高于单机 Agent 面板；需要 Secret Manager、独立 DDL 身份、审计存储和恢复演练。
- Advisory 模式的商业承诺受限；销售/交付必须按模式标注能力，不能统一宣传为“全面治理任意 Agent”。
- 运行时接入成本真实存在：每个能力必须通过契约测试才能标记“已验证”，MCP Server 的存在不构成能力证据。
- 用户会感知额外确认/审批摩擦；必须通过明确原因码和最小必要门禁降低阻力，但不能为体验删除安全门禁。

### 3.3 对现有资产的影响

- 现有报销、工作流、RBAC、审计、Outbox、文件和导出能力继续作为业务基础，不复制第二套领域模型。
- 现有 Hermes 兼容实现保留，但它是**首个候选 Runtime Adapter**，不是产品硬依赖；完整适配能力在 Phase 4 真实验证前保持“未验证”。
- 旧文档中把 Hermes Profile、会话或内部 API 作为永久强制前提的内容按新计划迁移；本 ADR 不追溯宣称旧行为已验证。

### 3.3.1 与现有业务契约的映射/取代

新契约不静默改义旧字段；下表明确映射或取代关系。无法映射的值必须由目录显式登记，不得推断。

| 现有契约/字段 | 新契约位置 | 关系 | 规则 |
|---|---|---|---|
| RBAC `permissions.scope`：`own` / `team` / `company` / `authorized` | `resource_scope.scope_type`：`self` / `department` / `company` / `approval_chain`（+ `project`/`record_set`/`connector`/`none`/`undetermined`） | 映射 | `own→self`、`team→department`、`company→company`、`authorized→approval_chain`；`authorized` 不再表示“任意已授权”，必须落为具体 `resource_ids`/`connector_ids`。空集合表示没有资源，不是全部。 |
| 业务工具 `version`：整数 | `capability` 的 `name@x.y.z`、`tool_contract.tool_versions` 的语义化版本 | 取代 | 整数版本必须由工具目录显式映射为 `x.y.z`（例如登记 `3 → 3.0.0`），不得隐式补零；新工具只允许语义化版本。 |
| 工具结果类别：`ok` / `needs_more_info` / `needs_confirmation` / `invalid_argument` / `unauthenticated` / `forbidden` / `not_found` / `conflict` / `internal_error` | 决策类型、任务状态、`result.outcome`、运行时错误码 | 映射 | `ok→allow/succeeded`；`needs_more_info→needs_clarification/awaiting_clarification`；`needs_confirmation→needs_confirmation`；`unauthenticated→deny(identity_unverified)`；`forbidden→deny(capability_not_granted|resource_scope_denied)`；`not_found/conflict/internal_error` 映射为运行时错误码，不得作为成功。 |
| `tool_confirmations` 的渠道绑定（微信/Web/MCP） | `confirmation.channel_binding.channel` + `channel_identity_ref` | 取代/扩展 | 现有渠道语义保留；确认只能在其绑定渠道消费，跨渠道默认拒绝；仍绑定 JCS payload hash、TTL 与一次性消费。 |
| 现有审批工作流实例 | `approvals[]` + `approval_ref` | 扩展 | 工作流可作为审批来源，但必须产出 `decision=approved`、`separation_check=passed`、payload hash 与 TTL；不得用工作流“完成”替代独立审批。 |
| 现有任务/审计表 | `taskRecord`、`task_events`、审计关联 | 扩展 | 保留现有存储；新契约字段是权威映射层，不要求推翻旧表；迁移时以契约字段为准补齐。 |

## 4. 备选方案与被拒理由

| 方案 | 描述 | 拒绝理由 |
|---|---|---|
| Hermes 专属管理面板 | 以 Hermes Profile/会话为核心模型 | 与运行时强耦合，无法支持第二运行时；违反“协议与运行时分离” |
| 通用 Agent 框架 | 内建规划、记忆、工具执行 | 重造已有工具链，扩大攻击面，偏离治理定位 |
| 仅提示词/工具描述治理 | 靠系统提示和 Agent 自律 | 不是强制安全边界；无法通过越权负向测试 |
| 纯 Advisory 产品 | 只做建议和审计 | 无法满足企业资源权限与受控执行需求 |
| 客户端 SDK 强制 | 在客户端库内拦截工具调用 | 客户端可绕过、可替换；不能作为服务端信任依据 |
| 先拆微服务/K8s | 一开始按多服务部署 | 无容量/隔离证据，增加运维和一致性成本；MVP 应模块化单体 |
| 跨边界共享管理员 token | 客户端、业务工具、运行时共用一个高权限令牌 | 任一边界失陷即全系统失陷；无法审计真实调用者 |
| 万能 `execute_action` / 任意 SQL/Shell | 一个工具转发所有动作 | 无法做确定性能力、范围和风险判定；违反最小权限 |

## 5. 验证与验收关联

本 ADR 的契约由以下可执行证据支撑（Phase 0 仅验证文档/契约，不声称产品能力已实现）：

- 三个 JSON Schema 通过 JSON Schema 2020-12 元 schema 校验，且每个 schema 的合法/非法 fixtures 全部可验证；
- 状态机逐条列出 72 条有效允许转换（T00–T73，T53/T54 已撤销）、F01–F41 禁止转换，并给出 Schema 无法表达的 S-01–S-20 服务端断言矩阵；
- 威胁模型至少 15 项（本版 30 项），覆盖身份、越权、审批、Grant、决策重放、模式混淆、完成检查、取消、绕行、凭据、审计等；
- Runtime 能力以 `verified / partially_verified / unverified` 标注，并保留证据引用字段；
- 契约包哈希包含三个 Schema、五份文档与全部 fixtures；候选必须固定完整哈希；
- 产品负责人确认 Phase 0 契约前，不启动依赖这些契约的并行产品实现。

本 ADR 不声称任何运行时、PEP、隔离环境或端到端链路已经验证。

## 6. 非目标（MVP）

- 重造通用命令行、浏览器、文件管理、办公软件连接器、数据库客户端或 Agent 规划/记忆系统。
- 训练模型、自建基础模型、实现万能软件操控器。
- 远程强制约束客户端本地已经可访问的任意 Shell、文件、浏览器或个人凭据。
- 一次性兼容所有 Agent、所有 MCP 扩展或多 Agent 协商协议。
- 无必要地拆微服务，或默认引入 Kubernetes、Redis、Kafka。
- 提供万能 SQL、万能 Shell、任意 `execute_action`，或由模型自行生成并批准权限。
- 自动执行付款、生产数据删除、不可撤回外发等高风险动作而无明确授权门槛。
- MVP 实现完整 SaaS 多租户；数据模型可预留组织边界。
- 用 Agent 记忆、对话历史或模型自述替代任务、审批、授权和审计的权威记录。

## 7. 产品负责人决策与剩余待确认事项

### 7.1 已确认决策（2026-10-20）

以下决策由产品负责人确认，已写入契约并作为 1.0.0 冻结约束：

1. **授权型决策必须绑定任务。** `allow` / `managed_execution_required` 必须包含非空 `task_id` 与 `task_revision`。任务创建前的评估只能返回 `deny` / `needs_clarification` / `needs_confirmation` / `needs_approval`，或独立的非授权 `pre_evaluation` 类型；不存在可空 `task_id` 的 `allow`。
2. **`failed` 是尝试终态，不是任务终态。** 保留有界重试，但 T65/T61 必须重新执行 PDP、签发新决策与新 Grant，并受 `max_attempts`/`retry_budget_remaining` 约束；禁止复用过期决策或无新决策重签 Grant。
3. **默认禁止自审。** 1.0.0 冻结职责分离：`Accountable Owner` 不得审批自己的任务，无策略例外。**已采纳，不再是待确认项。**

### 7.2 复审建议的默认处理（已采纳）

1. **高风险阈值留在版本化策略层。** 付款、删除、不可撤回外发的确认/审批阈值不在 schema 中写死，由 `policy_version` 管理。
2. **Advisory 不宣称本地强制治理。** 任何对外表述必须包含“无法强制约束客户端本地工具”；Advisory 任务必须记录 `client_local_tools_uncontrolled`。
3. **Managed Execution 暂不宣称已受控。** 在 Phase 5 隔离、任务级凭据与出口限制负向测试通过前，能力状态保持 `unverified`，不得对外宣称“受控执行”已商用。
4. **候选固定完整契约包哈希。** 哈希包含三个 Schema、五份文档（Runtime Adapter、状态机、本 ADR、威胁模型、并行规则）与全部 valid/invalid fixtures；不接受 schema-only 哈希或本地私有 fixture。
5. **`pre_evaluation` 仅用于任务创建前评估。** 它不得签发 Grant，且 `task_id`/`task_revision` 必须为 null。
6. **1.0.0 版本状态。** 1.0.0 尚未被接受或发布；本轮修复产生 1.0.0 候选。冻结后任何破坏性变更必须升主版本。

### 7.3 仍需产品负责人确认的事项

1. 高风险动作（付款、删除、不可撤回外发）的具体阈值与审批层级属于业务策略，不在本 ADR 内替产品负责人决定。
2. Phase 0 契约的最终验收与冻结；在此之前 Issue #78–#83 不得启动依赖实现。

Accountable Owner 自审在 1.0.0 已明确禁止，不是待确认项；未来若要引入例外，必须在新主版本中增加例外引用、理由、策略版本与审计字段，并重新进行威胁评估。

在上述事项确认前，本 ADR 状态保持 Proposed。
