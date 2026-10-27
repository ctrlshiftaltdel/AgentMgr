# 微信客户端接入设计（#83 增量 B，v2）

- 状态：**设计修订稿；不可据此授权生产路径。** v1 评审发现模式判定、advisory 执行、审批信任边界和 Python SDK 能力存在关键缺口；本稿修正这些问题，并明确增量依赖。
- 日期：2026-10-27
- 关联：#83、#80、#79、#82、#97、#100、#102、Epic #74；[#97 边界](../../security/hermes-managed-execution-boundary.md)、[商用就绪审查](../../security/commercial-readiness-review.md)。
- 参考客户端：`corespeed-io/wechatbot`（Python SDK）。本稿不声称该 SDK 的生产能力；需按固定版本/提交审计后再引入。

## 1 所有者已确认

- 微信 iLink Bot；扫码登录由所有者完成；Bridge 部署在测试机；专用测试账号；接受 iLink/账号条款风险。
- 测试阶段 Bridge 使用内部 token；微信用户不持有 token。
- 普通咨询 = advisory；文件转主管审批 = governed；进销存录入 = managed。
- 审批通知可以通过微信/Workbuddy 等终端，但关键审批必须在受信页面/二次验证完成。
- 试点使用真实业务数据**副本**（待所有者提供）；不写生产。
- #100 已修复并合并（`cac3396`）。

## 2 当前代码的真实边界（必须先读）

1. **MCP/REST `submit_task` 只能创建 advisory 任务**，且 owner/executor 固定为调用者；没有公开 governed/managed 提交入口。
2. **PDP 的 `allowed_execution_modes` 是拒绝条件，不是“自动升级”选择器。** `requires_managed_execution` 只改变决策约束；任务行在插入时固定 execution_mode，之后不会自动改。若任务模式与决策模式不一致，Grant 会 `execution_mode_mismatch`。
3. **没有服务端 advisory 执行器。** MCP/REST submit 只创建任务，不回答咨询；advisory/governed 由客户端/PEP 驱动。Hermes advisory `/v1/runs` 是另一条路径，本设计未接入。
4. **没有 governed 业务执行器/连接器。** 文件转发、审批后继续处理需要受信 connector；当前没有。
5. **`app/inventory/` 目前没有写服务/applier。** Managed artifact 不能直接写库；需要受信 server-side applier、唯一约束、幂等、读回验证和副本 DSN 隔离。
6. **审批只能由 `record_approval` 的人类 principal 记录。** MCP `request_approval` 只读；`workflow.service.record_decision` 不校验操作者是否为步骤审批人，不能作为微信回调的授权入口。
7. **微信 Python SDK（参考仓库 v0.3.0）有限制：** cursor/context_token 在内存；`-14` 会强制重新扫码；QR URL 默认写 stderr；credential 默认 `~/.wechatbot/credentials.json`；`IncomingMessage` 没有 message_id；poll 先进 cursor 再调 handler，失败不重投；冷用户 `send()` 可能 `NoContextError`；群消息不支持。这些必须作为 Bridge 的设计约束，不能假设 SDK 已提供。
8. **身份解析** `resolve_principal` 返回 identity Principal，不是 `submit_task` 需要的 GovernancePrincipal；不能把 `X-Employee-Id` 直接交给公开 API 当最终授权。必须由受信 intake 在服务端解析并调用核心，或使用签名断言。

## 3 安全增量（不要一次做完）

### 增量 B1：受信 Task Intake + 动作注册表（无模型模式选择）

- 新增服务端 `Task Intake`（内部服务/模块），只供 Bridge 调用。
- Bridge 不发送 `execution_mode`、owner、approver、employee id；只发送 `channel_type=weixin`、`external_user_id`、`external_message_id`、**显式 action_id**、payload/附件引用。
- 服务端：
  1. 用 `resolve_principal` 解析微信身份（未绑定/撤销/未验证/非 active 拒绝）。
  2. 查**闭集动作注册表**：`action_id -> capability, final_mode, policy_set, approval_required, allowed_scope`。
  3. 提交时直接写最终 mode；策略规则只允许该 mode（`allowed_execution_modes=[final_mode]`）。不依赖 PDP 自动升级。
  4. managed 任务只有在真实 adapter capabilities verified 后才入队；否则拒绝/转人工。
- 自由文本不能由模型选模式。B1 的自由文本只做意图澄清和显式动作引导；在 B2 完成前不执行 advisory 回答，也不入队 `policy_lookup`。涉及写/审批/关键数据时必须让用户选择显式动作模板/按钮/命令；无法确定则要求澄清。
- 微信绑定只能由现有 admin bind 创建（设置 `verified_at`）；Bridge 不能自建绑定。`from_user_id` 跨重新登录的稳定性尚未验证；变化时 fail closed，试点应预期重新绑定。
- B1 只允许消息/身份/去重/通知；在 B2 完成前不得把 `policy_lookup` 入队，避免产生无人执行的 advisory 任务。
- 不新增公开 Managed API；Intake 只监听内部网络/loopback + token。

### 增量 B2：只读 advisory 执行器

- 需要一个真正的只读回答服务：固定政策/制度索引 + 引用，不启用写工具；由服务端执行并记录来源。
- 不允许 Bridge 用自己的 token 直接调模型回答，也不允许把“创建了 advisory 任务”当成已回答。
- 没有 B2 前，微信只能确认“已受理”，不能承诺回答。

### 增量 B3：Governed 文件审批

- 文件存受控副本存储；任务只保存引用。
- 审批人由服务端目录/职责分离解析；`required_approval`，不是 `confirmation`。
- 微信/Workbuddy 只发通知 + 任务号 + 受信页面链接；审批在 B4 受信页面完成，页面重新认证目录 approver，然后调用 `record_approval`。
- 审批通过后，“继续处理”是新的受控动作/任务，重新走 PDP；不复用旧 Grant。
- 审批回调不放在微信消息适配包内；微信 Bridge 不能自行批准。

### 增量 B4：Managed 进销存写入

- Hermes sandbox 只产出结构化 artifact；不连 DB、不持有生产 key。
- 独立 verifier 检查 schema/范围/重复键/业务规则；通过后由受信 applier 写**副本**数据库。
- applier：
  - 幂等键由 **task_id + artifact_sha256** 派生，不由模型/消息文本决定。
  - 副本 DB 有**业务自然键**唯一约束（例如 tenant + SKU + 仓库 + 期间），不能只约束 `task_id + artifact_sha256`；验证通过不等于约束。
  - 写后读回校验，微信成功文案只能来自 applier 的读写结果。
  - 独立副本 DSN/凭据，不在 Hermes egress allowlist；启动时拒绝生产 DSN。
  - 写时重新读取 Grant；失败/未知不自动重试；已提交写入不因 cancel 回滚。
  - 需要业务 schema；在所有者提供副本数据/schema 前不能实现 applier。

## 4 动作注册表（示例，非最终策略）

| action_id | capability | final mode | 执行者 | 审批 |
|---|---|---|---|---|
| `policy_lookup` | `policy.read@1.0.0` | advisory | B2 只读执行器 | 无 |
| `document_forward` | `document.forward@1.0.0` | governed | B3 受信 connector | 主管 `required_approval` |
| `inventory_entry` | `inventory.write@1.0.0` | managed | #97 sandbox + B4 applier | 按策略/关键数据 |
| `approval_issue` | **不注册为 capability** | — | 人类 B4 页面 | 由审批系统记录 |

- 客户端不能传 capability；只能传 action_id。未知 action_id -> 拒绝/澄清。
- 策略规则必须只允许 final_mode；不能依赖“自动升级”。
- 表是唯一规则来源；`inventory_entry` 固定 managed，`document_forward` 固定 governed + `required_approval`；审批/下发不是 runtime capability，只由 B4 人类页面记录。未知/不确定 -> 拒绝/澄清，不能让客户端/模型升级或降级。

## 5 WeChat Bridge 约束

- Python SDK 版本/提交固定；`wechatbot-sdk==0.3.0` 已按 Issue #104 B1 授权加入 `pyproject.toml` / `uv.lock`（B1 实现状态见 §10）。
- 单账号单实例；独立 root-only credential 路径；多机 lease/advisory lock；QR URL 不写日志。
- 消息去重：SDK 没有 message_id，必须从 `raw` 中提取稳定事件 ID；提取不到就 fail closed，不能拿消息文本哈希当幂等键。
- poll 先 cursor 后 handler：失败不重投；Bridge 必须自己持久化处理状态/重试，不能声称 at-least-once。
- `-14` 会强制重新扫码：运维必须能通知所有者扫码；不能声称无人值守自动恢复。
- 冷用户 `send()` 可能 `NoContextError`：Python SDK 不能主动创建 context；审批通知必须有 Workbuddy/其他通道，或等用户先发消息建立 context；不能假设微信随时可推送。
- 群消息不支持；只支持单聊。
- 附件：大小/类型/病毒扫描（扫描器需单独选型）；消息正文/附件不写审计。
- 日志：不记录 token、key、QR、消息正文、员工明文；只记任务号/渠道哈希/稳定码。
- 每个 Bridge 进程只有一个内部 token；不要声称已有多租户 token。试点单租户。

## 6 审批信任

- 通知可以走微信/Workbuddy，但关键审批只在 B4 受信页面完成：
  - 页面重新认证员工（SSO/密码/现有 admin session），确认其为服务端解析的 approver。
  - 一次性、绑定 payload hash、TTL、防重放。
  - 调用 `record_approval`（人类 principal），不调用 `workflow.record_decision` 绕过 approver 校验。
- 聊天“同意”、签名链接本身都不作为唯一授权；签名链接只用于打开受信页面。
- 低风险 `confirmation` 不能替代主管 `required_approval`。

## 7 数据/部署/观测

- 专用测试微信账号/租户；Bridge 单实例在测试机；QR 由所有者扫。
- 副本 DB/存储独立凭据；环境标记 `pilot`；applier 启动拒绝生产 DSN；Hermes 看不到副本 DSN。
- 外部系统（主管审批、进销存）先用副本/模拟目标。
- 网络：Bridge -> Intake 走 loopback/私网；不暴露公网；token 只在 Bridge secret store。
- 观测（不含正文）：poller lag、QR required、`-14`、dedupe hit、binding rejection、NoContextError、task state、needs_human/unknown、applier outcome。
- 备份/恢复、容量、告警仍属 #83 P0，不因本设计自动完成。

## 8 路径与依赖（需所有者逐项确认）

建议拆分，避免把审批/执行器塞进微信包：

- `app/integrations/wechat/**`：仅消息 Bridge、SDK 适配、去重、通知；**不含审批授权回调**。
- `app/tasks/intake.py`（或 `app/intake/task_router.py`）：受信 Task Intake + 动作注册表。
- `app/governance/policies.py` / policy seed/config：动作注册表对应的 singleton `allowed_execution_modes` 策略发布与测试。
- `app/security/approval/**` 或现有 admin/B4 页面：受信审批页面/回调（需单独设计）。
- `app/integrations/documents/**` 或 `app/tasks/connectors/**`：B3 文件转发 connector（需单独设计）。
- `app/inventory/**` + `app/tasks/appliers/**`：B4 applier（需 schema/副本）。
- `app/runtime/advisory/**`：B2 只读执行器（需单独设计）。
- `ops/wechat/**`：部署单元、root-only credential、日志/告警。
- `app/tests/integrations/wechat/**`、`app/tests/e2e/**`。
- `pyproject.toml` / `uv.lock`：仅在新依赖批准后修改。
- `docs/integrations/clients/wechat.md`（本文件）。

**未确认前不写生产代码。** 第一项可做的是 B1（Intake + 动作注册表）与 Bridge 的消息/身份/去重，仍不回答 advisory、不执行 governed/managed。

## 9 验收（按增量）

- B1：身份拒绝、消息去重、action_id 闭集、mode 固定、客户端不能传 mode/owner/approver、未知动作拒绝。
- B2：只读回答有来源/引用，无写工具；未实现前不得回复“已回答”。
- B3：非 approver/自审/过期/重放/payload 改动拒绝；审批页面重新认证；审批后新动作重新走 PDP。
- B4：artifact 校验 + applier 幂等/唯一约束/读回；副本 DSN 隔离；失败/未知不自动重试；cancel 不回滚已提交写入。
- 微信 E2E：扫码、文本、文件、通知、审批、结果；未绑定/重复/超时/转人工。
- 证据：命令、计数、SDK 版本/提交、任务号、artifact 哈希；不含 key/token/QR/个人路径。

不证明：生产部署、容量、跨主机恢复、生产 key 轮换、业务工具 PEP 完整治理、微信群聊。

## 10 B1 实现状态（2026-10-27）

本节只记录 B1 已落地的事实与边界；设计正文（§1–§9）仍是权威约束。

### 10.1 已实现路径

- `app/tasks/intake.py`：选择此路径（而非 `app/intake/task_router.py`），因为它是
  任务域的受信提交入口，避免与既有 `app/intake/inbound.py`（入站受理作业）混淆。
  - `ActionSpec` / `ActionRegistry`：闭集查找；未知 -> `unknown_action`，已登记但
    停用 -> `action_not_available`。
  - `submit_action(...)`：只接受渠道键 + 显式 `action_id` + 业务 payload；身份经
    `resolve_principal` -> `resolve_employee_principal` 服务端解析；`final_mode`
    只来自 `ActionSpec`；requester/owner/executor 都是解析出的同一名员工；不接受
    客户端 mode/owner/approver/employee id；不自动执行、不调用 dispatcher。
  - 幂等键由 `(channel_type, external_user_id, external_message_id)` 的 SHA-256
    派生，前缀 `idem_wechat_`，不使用消息正文；同一事件 + 同一 canonical
    request 返回同一任务，同一事件 + 不同 payload 返回
    `task_idempotency_conflict` 拒绝。
  - 不新增表/迁移：复用 `tasks (requester_principal_id, idempotency_key)` 既有
    唯一约束。
- `app/integrations/wechat/**`：`config.py`、`sdk.py`（懒加载 SDK + 稳定回复码）、
  `bridge.py`（`/help` / `/whoami`、事件键 fail-closed、可选测试用
  `handle_action`）、`run.py` / `__main__.py`（本地入口）。
- `ops/wechat/**`：systemd 模板（`flock -n` 单实例）、无秘密 env 示例、ops README。
- `docs/operations/wechat-bridge.md`：所有者 QR 登录、root-only 凭据、`-14` 重新
  扫码、冷用户 `NoContextError`、单实例与日志隐私的运行手册。
- 依赖：`wechatbot-sdk==0.3.0`（仅此一个新增直接依赖；`aiohttp` / `cryptography`
  为其传递依赖）。

### 10.2 B1 生产注册表

`B1_ACTION_REGISTRY` **没有启用的建任务动作**。设计表中的三个动作以停用条目
登记，便于区分“完全未知”和“已登记但本增量不可用”：

| action_id | capability | final_mode | 状态 |
|---|---|---|---|
| `policy_lookup` | `policy.read@1.0.0` | `advisory` | 停用（B2） |
| `document_forward` | `document.forward@1.0.0` | `governed_resource_access` | 停用（B3） |
| `inventory_entry` | `inventory.write@1.0.0` | `managed_execution` | 停用（B4） |

注意：代码中的 governed 模式字符串是 `governed_resource_access`，不是设计正文
简写的 `governed`；`ActionSpec` 只接受治理层的实际枚举值。

### 10.3 Bridge 行为

- 稳定事件键：`raw["from_user_id"]` + `raw["create_time_ms"]`（正整数）；缺失/零/
  非法 -> 回复并记录 `event_key_missing`，不解析身份、不建任务。**不**用消息正文
  哈希。
- `/help`、`/whoami` 由 Bridge 自身处理；`/whoami` 只回答“已绑定/未绑定”，不返回
  员工 ID、姓名、角色或公司。
- 其它文本只回复“B1 未开放”；不调用模型、不回答 advisory、不创建任务。
- 没有审批回调、没有公开 HTTP API、没有内网 token 校验；B1 是测试机上的受信本地
  服务。若后续网络化，token/loopback 限制必须在单独增量中实现并失败关闭。
- 日志只含稳定码、渠道事件哈希（SHA-256 前缀）、任务号、动作 ID 与内部原因；不
  记录 token/key/QR/`context_token`/消息正文/渠道用户或员工明文。

### 10.4 SDK v0.3.0 限制的落地处理

- QR：SDK 默认把 URL 写 stderr；适配器只通过调用方回调展示，入口仅在交互式 TTY
  输出，非 TTY 直接失败关闭，避免 journal 记录 QR。
- SDK 私有 `_log` 会把 `user_id` 与错误文本写 stderr；适配器固定版本下覆盖并丢弃，
  不绕过适配器直接调用 SDK。
- 冷用户 `send()` 会 `NoContextError`；B1 只回复入站消息，不主动 `send()`。入站
  `reply` 失败（网络/API）映射为 `reply_send_failed` 并记录稳定码；不假装可主动推送。
- `-14` 会清空凭据并要求重新扫码；入口会记录 `qr_required`，不做无人值守自动恢复。
  systemd 模板给凭据目录 `ReadWritePaths`，避免 SDK 清理/重写凭据时被只读挂载挡住。
- poll 先 cursor 后 handler：B1 不声称 at-least-once；失败不重投，需运维重新扫码/
  重发。
- 群消息不支持；只支持单聊。

### 10.5 验证状态（实现者）

- DB-free 测试：`app/tests/integrations/wechat/test_action_registry.py`、
  `test_bridge.py`、`test_config.py` 共 88 项通过（`--noconftest`）。
- 真实 PostgreSQL 测试：`test_intake_db.py` 在测试机由 Master 运行；B1 全目录
  **97 passed**。
- 真实微信扫码/收发 E2E：未执行；B1 只用假 SDK 验证消息/身份/去重/日志隐私。
- 未声称：生产部署、容量、跨主机恢复、真实账号验证、B2–B4 能力。
