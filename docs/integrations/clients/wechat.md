# 微信客户端接入设计（#83 / WeChat Bridge + Task Intake）

- 状态：**设计草案，未实现、未接入真实微信账号。** 需要所有者确认路径与试点范围后再开发。
- 日期：2026-10-27
- 关联：#83、#80、#79、#82、#97、#102、Epic #74；#97 边界、#83 商用就绪审查。
- 目标客户端：微信 iLink Bot（参考 `corespeed-io/wechatbot`，Python SDK `wechatbot-sdk`）。
- 所有者已确认：扫码登录由所有者完成；Bridge 部署在测试机；测试阶段用 token；使用真实业务数据副本；接受 iLink/个人微信自动化的账号与条款风险（专用测试号）。

## 1 结论与边界

- 微信 **不是** MCP 客户端；它是消息入口。真正的客户端是**受信 WeChat Bridge（服务端）**。
- 微信用户**不持有** AgentMgr token，不能自报 employee id，不能选择 advisory/governed/managed。
- 微信 Bridge 负责：长轮询、消息去重、媒体接收、员工绑定解析、调用受信 Task Intake、回传任务状态。
- 执行模式由**服务端确定性策略**决定：advisory / governed_resource_access / managed_execution；模型只能建议，不能决定。
- 关键数据变更、审批、下发必须 managed；普通咨询 advisory；可逆业务事务 governed。
- 微信里的“同意”不是关键审批的唯一证据；关键审批需要受信页面/二次验证，微信/Workbuddy 只是通知与入口。
- 本设计不改变 #97 的公共边界：不新增公开 Managed API；受信 Intake 只供服务端 Bridge/内部服务使用。

## 2 目标流程

```text
微信用户
  -> iLink Bot SDK（Bridge，长轮询）
  -> 消息去重 + 员工绑定解析（employee_channel_bindings）
  -> 受信 Task Intake（服务端，token/内网）
     -> 确定性模式判定 + PDP
     -> task_service / grant / dispatcher
  -> 状态/结果/审批通知回传微信
```

### 2.1 Advisory：制度咨询

例：“公司的报销规章制度是什么？”

- 只读、无业务副作用。
- 模式：`advisory`。
- Bridge 调用受信 Intake，任务只做只读问答/检索；不签发执行 Grant，不启动 managed sandbox。
- 微信回复直接给出答案/引用；不进入审批。

### 2.2 Governed：文件转主管审批

例：“提交一份需要主管审批的文件，转给主管；主管批准后继续处理。”

- 模式：`governed_resource_access`（若策略要求则升级 managed）。
- 文件先落到受控附件存储；任务记录文件引用，不把文件内容写进任务 payload/日志。
- 审批人由服务端解析（主管关系/职责分离），不能由微信消息指定。
- 微信/Workbuddy 发送审批通知；主管在受信页面或签名链接中审批；Bridge 只转发结果，不能把“微信回复同意”直接当成高权限审批。
- 审批通过后，后续“继续处理”作为新的受控动作/任务，重新走 PDP；不复用旧 Grant。
- 拒绝/超时/撤销都要有明确微信文案与审计。

### 2.3 Managed：录入进销存数据

例：“录入一笔公司的进销存数据。”

- 模式：`managed_execution`（关键数据变更）。
- Hermes sandbox **不直连数据库**；模型生成结构化 artifact（如 JSON），宿主计算哈希。
- 独立验证器检查字段、范围、总量、重复键、权限与业务规则；通过后由**受信 server-side applier** 写入副本数据库。
- 写入动作需要幂等键、事务、审计；失败/未知不得自动重试。
- 试点只写副本数据库/副本租户；生产写入不在本试点范围。
- 微信最终回复“已验证并写入副本/验证失败/转人工”，不能由模型自报成功。

## 3 模式判定

### 3.1 现有机制

- 任务模型已有 `advisory` / `governed_resource_access` / `managed_execution`。
- PDP 规则已有 `allowed_execution_modes` 与 `requires_managed_execution`；策略可以把请求升级为 managed。
- 受信 Intake 只提交**基线模式**与 capability/scope；由策略决定最终模式，客户端不传 `execution_mode`。

### 3.2 建议基线

| 业务 | capability 示例 | 基线 | 升级条件 |
|---|---|---|---|
| 制度/政策咨询 | `policy.read@1.0.0` | advisory | 无 |
| 文件整理/转发/日程草稿 | `document.forward@1.0.0` / `schedule.draft@1.0.0` | governed | 涉及审批/关键数据 -> managed |
| 进销存写入/关键数据变更 | `inventory.write@1.0.0` | managed | 固定 managed |
| 审批/下发 | `approval.issue@1.0.0` | managed | 固定 managed |

- 不确定 -> 升级（governed/managed）或要求澄清；**不得降级为 advisory**。
- 规则由确定性后端执行；模型输出只作为不可信数据。

## 4 组件

### 4.1 WeChat Bridge（新）

- Python `wechatbot-sdk`（async/aiohttp），扫码登录、长轮询、富媒体、消息分片、会话恢复。
- 单账号单实例；独立 storage；多机用 lease/advisory lock，避免游标互相覆盖。
- 消息去重：`(channel_type, external_message_id)` -> 任务/回复；重投不创建第二个任务。
- 员工绑定：`employee_channel_bindings` 为唯一真源；未绑定/撤销/未验证/非 active 一律拒绝，不暴露具体原因。
- 附件：大小/类型/病毒扫描；存受控副本存储；消息正文不写审计。
- 回复：受理、执行中、验证中、需确认/审批、成功、失败、取消请求、needs_human、unknown；不承诺流式。
- 日志：不记录 token、key、prompt、消息正文、员工明文；只记任务号/渠道哈希/稳定码。
- Token：测试阶段 Bridge 持有内部 token（secret store/env），微信用户不可见；每租户独立 token/配额，支持轮换/吊销。

### 4.2 受信 Task Intake（新）

- 只供 Bridge/内部服务调用；不暴露公网；测试阶段 token + loopback/内网。
- 输入：`(channel_type, external_user_id, external_message_id, capability, goal, resource_scope, payload, attachments)`。
- 服务端解析身份：调用 `app.identity.channel_routing.resolve_principal`；**不接受**客户端传 employee id。
- 幂等：channel message id -> `idem_...`；同消息重投返回同一任务。
- 模式：基线 + PDP `requires_managed_execution`；managed 需要真实 adapter 能力 verified 后才入队。
- Owner/executor/approver 由服务端目录/策略解析；客户端不能指定。
- 不新增公开 Managed API；不写任务 payload 到日志。

### 4.3 审批/确认

- 通知：微信/Workbuddy 发任务号、动作、风险、过期时间、一次性链接/验证码。
- 审批：受信页面/管理台/签名链接；验证审批人 = 服务端解析的 approver，且分离职责。
- 微信回复可用于“提醒/打开链接”，不作为关键审批唯一证据；低风险确认可按策略放宽。
- 过期/撤销/重放：一次性、绑定 payload hash、TTL；不复用旧确认。
- `needs_human`/`unknown`：微信明确“已转人工核对，不会自动重试”。

### 4.4 结果应用

- Managed artifact 由宿主哈希并独立验证；applier 是受信服务，不在 sandbox 内。
- 写入副本数据库需要事务/幂等/审计；失败不自动重试，转人工。
- 生产写入必须另行通过 #83 P0（备份/容量/告警/PEP）。

## 5 数据与部署（试点）

- 专用测试微信账号/租户；Bridge 单实例部署在测试机。
- 真实业务数据副本：独立副本 DB/存储；环境标记 `pilot`；禁止误写生产。
- 外部系统（主管审批、进销存）使用副本/模拟目标；不接生产。
- 网络：Bridge -> AgentMgr 走 loopback/私网；MCP/Intake 不暴露公网；TLS 由部署层。
- 密钥：测试 token 只在 Bridge secret store；provider key 只在 #97 host broker；不提交/打印。
- 二维码登录由所有者完成；凭证存专用 storage，不进仓库。

## 6 验收

### 6.1 单元/契约

- 消息去重、身份绑定拒绝、token 失败关闭、附件限制。
- 模式判定：advisory/governed/managed/升级/不确定->澄清。
- 审批：非 approver 拒绝、自审拒绝、过期/重放/ payload 改动拒绝。
- 微信文案：needs_human/unknown/cancel 不误导。

### 6.2 真实 E2E

- 真实微信测试号扫码 -> 文本消息 -> advisory 回答。
- 文件消息 -> governed 审批 -> 通知 -> 受信审批 -> 后续处理。
- 进销存文本/表格 -> managed artifact -> 独立验证 -> 副本 applier -> 微信结果。
- 未绑定用户、越权审批、重复消息、取消、超时、转人工。
- 证据：命令、计数、SDK/客户端版本、任务号、artifact 哈希；不含 key/token/个人路径。

### 6.3 不证明

- 生产部署、生产容量、跨主机恢复、生产 key 轮换、业务工具 PEP 的完整治理。
- 微信平台条款/账号稳定性；试点专用号风险由所有者接受。

## 7 需要所有者确认的路径/范围

生产实现需要新增/修改（建议）：

- `app/integrations/wechat/**`（Bridge、消息适配、身份/审批回调）
- `app/tasks/intake.py` 或 `app/api/internal/**`（受信 Task Intake）
- `ops/wechat/**`（部署单元、storage、QR/运行配置）
- `app/tests/integrations/wechat/**`、`app/tests/e2e/**`
- `docs/integrations/clients/wechat.md`（本文件）

在路径授权前，本设计不写生产代码；先评审模式判定、审批信任边界和数据副本范围。
