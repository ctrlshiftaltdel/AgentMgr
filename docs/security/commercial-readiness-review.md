# 商用就绪审查（Issue #83，测试机平台增量）

- 状态：**测试机平台可受控试用；商用/生产未就绪。**
- 日期：2026-10-27
- 关联：Issue #83、Epic #74、#78、#79、#80、#81、#82、#96、#97、#100；[真实客户端 E2E 证据](real-client-managed-e2e-readiness.md)、[#97 边界记录](hermes-managed-execution-boundary.md)、[备份恢复](../BACKUP-RESTORE.md)、[部署要求](../DEPLOYMENT-REQUIREMENTS.md)。
- 证据口径：测试机真实运行 + 仓库测试 + 文档；不包含生产部署、生产凭据、生产容量或跨主机恢复。**本审查不构成上线批准。**

## 1 结论（先读这一节）

- **可以做的**：仅在批准的测试机上，对受控任务做端到端试用：受信测试夹具创建 managed 任务 -> 真实 Managed Hermes runtime -> artifact -> 独立验证；真实 MCP 客户端只通过 `get_task`/`get_result` 读取结果（不通过公开 MCP 创建 managed 任务）。
- **不能做的**：不得声称生产已部署、可对外商用、容量已证明、备份可跨主机恢复、生产 key 轮换/告警已就绪、业务工具已受 PEP 治理。
- **Go/No-Go**：**No-Go（商用）**；**Conditional Go（仅测试机受控试用）**。条件：批准测试机、受控测试任务、loopback + 内部 token、不使用生产数据/生产 key、不接远程客户端、不对外提供服务。完成 §5 也不会自动把本文件变成商用 Go；必须重新提交证据审查。

## 2 就绪矩阵

| 领域 | 已有证据 | 状态 | 主要缺口 |
|---|---|---|---|
| 产品/契约 | 契约/schema/状态机/设计文档；**冻结候选，产品负责人尚未确认** | 测试机可用 | 冻结确认、生产变更/版本治理未演练 |
| 身份 / PDP | 真实 PostgreSQL + API/MCP 测试；内部 token + verified employee 头；PDP 决策测试 | 受控边界可用 | 无生产 IdP/网关；内部 token 不能给远程客户端 |
| PEP / 业务工具 | Hermes `terminal` 沙箱与稳定错误码；业务资源访问未接 PEP | **未验证** | 真实业务工具调用不经过 AgentMgr PEP |
| 任务生命周期 | 评估/提交/确认/审批/取消/结果/验证/租约/恢复测试；状态机测试 | 受控边界可用 | 生产 SLA/超时/告警未演练；#100 cancel 竞态 |
| 客户端 MCP / REST | 真实 MCP SDK over TCP + 活 uvicorn；MCP/REST 共享 operations；repo 内未认证/非法 schema 测试；live 只验证一个 outsider 读取拒绝 | 部分 | 远程 TLS/网关、完整 live 越权矩阵、真实第三方客户端矩阵、会话粘性/多 worker |
| Managed runtime | #97 真实 systemd 沙箱、in-process `run_oneshot`、真实 key artifact E2E、cancel/timeout/token 边界 | 测试机 live | 生产 service principal/CLI、容量、长时间稳定性 |
| 隔离 / 凭据 | #82 合成隔离矩阵 + #97 真实负向矩阵；真实 key 只在 broker | 测试机 live | 宿主 root/内核、world-readable 宿主文件、broker 单点 |
| 审计 | append-only DB trigger、审计测试、Outbox；MCP/REST 审计 | 部分 | 应用角色拥有表时 trigger 可被绕过；生产角色/GRANT 脚本、口令/证书认证、日志保留/告警/外部 SIEM 未演练 |
| 备份 / 恢复 | `ops/backup.py`/`restore.py` 13 项校验；同机真实数据演练 | 同机可用 | **跨主机/异地恢复未执行**；RTO/RPO 未测 |
| 可观测性 / 告警 | 健康检查、审计 details、稳定错误码；运维手册 | 部分 | 无生产结构化日志管线、告警路由/值班/演练证据 |
| 容量 / 性能 | 单 worker/单 unit 串行；无并发/吞吐/长稳 | **未验证** | 需容量基线、并发、启动开销、故障注入 |
| 密钥生命周期 | 专用测试 key；provisioner 显式轮换 fail-closed 策略 | 部分 | 生产 KMS/轮换/配额/吊销流程未演练 |
| 业务工具 / PEP | Hermes `terminal` 沙箱；业务资源访问未接 PEP | **未验证** | #83 后续增量：业务工具/PEP 连接器 |
| 事件流 | 无原生事件流；客户端轮询 task/result | 明确限制 | 不承诺事件订阅 |
| 商用/合规 | 隐私扫描、秘密排除、备份排除凭据 | 部分 | 合同、数据处理、许可证、保险等非技术项未覆盖 |

## 3 已证明的端到端链

在批准测试机上：

1. 真实 MCP 客户端经 TCP/Streamable HTTP 连接活 uvicorn，完成 `initialize`/`tools/list`。
2. 受信测试夹具在真实 PostgreSQL 创建 `managed_execution` 任务；不是公开 MCP 提交。
3. 真实 `RuntimeDispatcher` + 真实 helper/broker + 真实 provider key 驱动 Hermes；模型写入 `OK`。
4. dispatcher 停在 `verifying`；独立 `complete_task` 用 artifact SHA-256 才进入 `succeeded`。
5. 客户端 `get_task`/`get_result` 读取 `succeeded`/`result_ref`/`evidence_refs`；outsider 被拒。
6. provider key 在 unit argv/env、任务文件、journal、持久化 result 与 MCP 响应中未出现；prompt/token 在 MCP 响应中未出现；清理完成；没有 auto-`succeeded` 或 `side_effects_reconciled`。cancel 语义不在本 E2E 断言范围内，见 #97 边界与 #100。

具体命令/计数/版本见[真实客户端 E2E 证据](real-client-managed-e2e-readiness.md)和[#97 边界记录](hermes-managed-execution-boundary.md)。本审查不重复记录主机信息。

## 4 已知限制与风险

1. **生产未部署**：测试机安装、systemd、sudoers、broker、CLI 不等于生产配置。
2. **真实 key 生命周期**：只有专用测试 key；生产 key 的 KMS/轮换/配额/吊销未验证。
3. **容量/并发/长稳**：单进程 uvicorn、单 dispatcher、单 unit；未测多 worker、队列积压、故障注入。
4. **远程信任边界**：测试是 loopback + 内部 token；远程客户端必须由受信网关终止 TLS 并注入 token/employee 头，未验证。
5. **备份恢复**：同机演练有 13 项校验；跨主机/异地恢复仍 **NOT_EXECUTED**，RTO/RPO 未测。
6. **告警/值班**：无生产告警路由、值班与故障演练证据。
7. **业务工具/PEP**：Hermes 内工具调用不经过 AgentMgr PEP；真实业务资源访问未治理。
8. **#100 cancel 竞态**：完成 artifact 与 cancel 在 #96 dispatcher 层未闭环；失败安全为 `needs_human`，不会 auto-success/cancel/reconcile。
9. **事件流**：无原生订阅；只能轮询。
10. **多客户端矩阵**：只验证了官方 MCP SDK 客户端；其他客户端（WorkBuddy/Codex/OpenCode 等）未逐一验证。

## 5 上线前必须补齐

**P0（阻塞商用）**

- 生产部署 Runbook 与演练：service principal、生产配置、TLS/网关、sudoers/broker、回滚。
- 跨主机/异地备份恢复演练：真实数据、干净目标、13 项校验、RTO/RPO 记录。
- 容量/并发/长稳基线：目标负载、启动开销、故障注入、队列积压。
- 生产 key 生命周期：KMS/轮换/配额/吊销/审计。
- 生产告警/值班：健康、失败率、队列、unit/broker、备份失败。
- 生产数据库角色分离：应用角色不得拥有审计表；建立 GRANT/角色脚本、口令/证书认证，并验证 append-only trigger 不可被应用角色绕过。
- 业务工具/PEP 边界：真实业务资源必须经受控接口；否则明确产品限制。

**P1（上线前尽量补齐）**

- #100 cancel 竞态修复。
- 更多真实客户端矩阵与远程 MCP 配置。
- 审计外部留存/SIEM。
- 数据保留/删除、合规与合同评审。

## 6 审批口径

- 本文件只批准“测试机受控试用”，不批准生产/商用上线。
- 任何“已商用/已生产/容量已验证/备份已跨机恢复”的声明都必须引用新的真实命令、退出码、计数和恢复报告，不得引用本文件、`real-client-managed-e2e-readiness.md` 或 #97 的测试机证据。
- 完成 §5 的 P0 项后，必须重新提交商用就绪审查与新证据；本文件不会自动转为商用 Go。
- 若产品决定接受剩余风险上线，必须由所有者记录风险接受、补偿控制和回滚方案；本审查不代为接受。
