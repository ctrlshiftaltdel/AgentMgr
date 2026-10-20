# 并行 Agent 开发与实现比较规则（v1.0.0）

- 状态：Phase 0 冻结候选；产品负责人确认契约前，不得启动依赖本契约的并行产品实现
- 契约版本：1.0.0
- 关联：Issue #77、[ADR-001](ADR-001-product-boundary.md)、[runtime-adapter](../contracts/runtime-adapter.md)、[task-state-machine](task-state-machine.md)、[threat-model](../security/threat-model.md)
- 依据：`docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md` §15

## 1. 目的

在同一套冻结契约上，允许多个 Agent/团队：

1. 从全新仓库独立实现，比较不同架构与工程质量；
2. 在同一仓库按路径不重叠的垂直工作包并行开发。

两种模式都必须保证**契约一致、证据可核验、职责分离、无自审自并**。并行不是降低门禁的理由；任何越权、凭据泄漏、审批旁路或数据越界都是硬失败。

## 2. 契约身份：版本 + 哈希 + fixtures

### 2.1 契约包

冻结的“Phase 0 契约包”包含：

- `docs/contracts/governance-decision.schema.json`
- `docs/contracts/task-envelope.schema.json`
- `docs/contracts/execution-grant.schema.json`
- `docs/contracts/runtime-adapter.md`
- `docs/architecture/task-state-machine.md`
- `docs/architecture/ADR-001-product-boundary.md`
- `docs/security/threat-model.md`
- `docs/architecture/parallel-agent-development.md`
- `docs/contracts/fixtures/valid/*.json`
- `docs/contracts/fixtures/invalid/*.json`

### 2.2 计算契约包哈希

所有候选必须使用同一份契约包与同一哈希。推荐在干净工作树执行：

```bash
git ls-files \
  'docs/contracts/*.schema.json' \
  'docs/contracts/runtime-adapter.md' \
  'docs/architecture/task-state-machine.md' \
  'docs/architecture/ADR-001-product-boundary.md' \
  'docs/architecture/parallel-agent-development.md' \
  'docs/security/threat-model.md' \
  'docs/contracts/fixtures/valid/*.json' \
  'docs/contracts/fixtures/invalid/*.json' \
| LC_ALL=C sort \
| while IFS= read -r f; do printf '%s ' "$f"; git hash-object "$f"; done \
| sha256sum
```

记录到 Issue/PR 的形式：

```text
contract_version: 1.0.0 (unreleased candidate; not yet accepted)
contract_bundle_sha256: <64-hex>
contract_bundle_includes_fixtures: true
plan_version: v1.0
fixtures: docs/contracts/fixtures/{valid,invalid}
```

规则：

- 哈希必须对应当前提交；文档或 fixture 任一字节变化都会改变哈希。
- **哈希包含 fixtures。** 不接受 schema-only 哈希、Mock fixture 或本地私有 fixture 作为候选身份。
- 候选不得修改契约包来“适配”自己的实现；需要修改时走 §6。
- 哈希不一致的候选不具可比性，必须重新同步后再比较。
- Mock、示例或本地私有 fixture 不得替代冻结 fixtures。
- 1.0.0 尚未被接受或发布；Phase 0 冻结前的破坏性修改仍以 1.0.0 候选处理，但必须在 Issue/PR 记录旧/新哈希。冻结后的任何破坏性变更必须升主版本。

## 3. 模式 A：全新仓库独立实现

### 3.1 输入

每个候选只收到：

- 同一份计划（`docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md`）与契约版本/哈希；
- 同一份工具链限制（Python 3.13、FastAPI、Pydantic、PostgreSQL、SQLAlchemy、Alembic；不擅自新增依赖）；
- 同一份验收 fixtures 与共享测试套件；
- 统一启动提示（计划 §16 或等价文本）；
- 明确的禁止事项与证据格式。

### 3.2 隔离

- 候选使用**独立仓库/分支或独立 Worktree**；不得共享工作树、分支、未提交文件或本地缓存。
- **不得读取另一个候选的实现细节后声称独立设计。** 包括源码、设计笔记、Issue 评论中的实现方案、PR Diff。
- 若发生意外接触，必须主动披露并标记该候选“非独立”；不得隐瞒。
- 评审者/比较者可以在所有候选完成后阅读实现，但不得把 A 的方案提前反馈给 B。

### 3.3 独立实现的验收

候选必须提交：

- 当前提交 SHA；
- 契约版本与哈希；
- 合法/非法 fixture 的运行结果；
- 共享契约测试、状态机测试、安全负向测试的真实命令、输出与退出码；
- 运行时能力证据（若接入真实运行时）：运行时版本、能力状态、限制；
- 未实现项与偏离规格的书面列表；
- Mock 证据单独标记，不得冒充真实兼容性。

### 3.4 独立性声明

每个候选在交付中声明：

```text
independence: independent | disclosed-contact | not-independent
contact_notes: <none or description>
```

没有声明或声明不实的候选，比较结果无效。

## 4. 模式 B：同一仓库并行

### 4.1 垂直工作包

Phase 0 冻结后，把实现拆成**路径不重叠的垂直工作包**，例如：

| 工作包 | 典型路径 | 依赖 |
|---|---|---|
| 身份、Principal、RBAC、委派 | `app/identity/` | Phase 0 契约 |
| PDP/PEP、策略版本、Grant | `app/policy/`、`app/grants/` | 身份 |
| 任务生命周期、确认/审批、状态机 | `app/tasks/`、`app/workflow/` | PDP、Grant |
| 客户端 MCP + REST | `app/api/`、`app/mcp/` | 任务、身份 |
| Runtime Adapter + Worker | `app/runtime/`、`app/worker/` | 任务、Grant |
| 审计、Outbox、管理面 | `app/audit/`、`app/admin/` | 任务、身份 |

工作包可以调整，但必须满足：

- 允许路径两两不重叠（含文档文件）；父子路径重叠也视为冲突。
- 每个 Issue 记录单一目标、验收标准、允许/禁止路径、依赖、真实测试命令、风险与证据要求。
- 派发前检查开放 PR 路径和**已派发但尚无 PR 的活动任务登记**，重建锁表。
- 执行者只改自己的路径；不得“顺手重构”他人领域。
- 不新建第二套身份、任务、审计或 Outbox 模型来绕过契约。

### 4.2 单所有者共享面

以下共享面必须由**单个任务独占**，不得并行修改：

- Phase 0 契约包（三个 Schema、Runtime Adapter、状态机、ADR、威胁模型、并行规则文档与全部 fixtures）；
- 数据库迁移链（`migrations/`）与 schema 变更；
- 依赖清单与锁文件（`pyproject.toml`、`uv.lock`、`.python-version`）；
- 安全核心（Principal 解析、PDP/PEP、Grant 校验、审批/确认门禁、审计写入）；
- CI 配置、受保护脚本（`scripts/`）、共享测试基座；
- 公共错误码、公共 DTO/枚举、应用启动/配置。

共享面变更需要单独 Issue、单独分支与明确审查；不得由多个执行实例并行“各自改一点”。

### 4.3 串行全量测试闸门

- 全量测试必须经项目配置规定的串行闸门执行，例如：
  ```bash
  flock /tmp/ajaimgr-fulltest.lock bash -c \
    'export UV_PROJECT_ENVIRONMENT=<test-host-venv> && uv run --no-sync pytest app/tests -q -o addopts=""'
  ```
  具体路径与测试库以本地 `local_info.md` 和 `ORCHESTRATION-PROJECT.md` 为准，不得写入仓库。
- 并行执行使用 `uv run --no-sync`，禁止并发 `uv sync` 就地改写共享环境。
- 任何会改动 `pyproject.toml`/`uv.lock` 的任务必须独占派发，不得与其他执行实例并行。
- 全量测试失败时，相关任务不得交付；不得通过删测试、弱化断言或跳过用例取得通过。

### 4.4 自审与合并禁止

- 执行者不得批准、合并自己的 PR，也不得代替 Master 做验收结论。
- 只有 Master 可以批准与合并；批准绑定当时 head SHA。
- head 变化后旧批准与旧 CI 结论失效，必须重新验证。
- 同仓库并行时，一个执行者不得审阅自己参与实现的共享面变更。
- 发现安全硬失败时，Master 必须阻止合并并记录处置，不得用总分补偿。

### 4.5 冲突与依赖处理

- 依赖必须合入默认分支并完成合并后验证，下游才可派发。
- 路径冲突、共享面冲突或依赖未满足时串行；不得“先合并再修”。
- 执行分支必须基于最新默认分支；交回复核前 rebase/更新并重跑验证。
- 未推送到远端的产物不能作为跨会话交付证据。

## 5. 比较矩阵（与计划 §15.3 一致）

### 5.1 评分

| 维度 | 分值 | 评分要点 |
|---|---:|---|
| 规格符合度 | 20 | 是否逐条满足计划与冻结契约；偏离是否书面记录 |
| 安全与越权控制 | 25 | PEP、Grant、职责分离、负向测试、凭据边界 |
| 任务状态/恢复 | 15 | 状态机、幂等、取消、超时/未知、租约、人工接管 |
| 接口契约质量 | 10 | Schema、错误码、版本化、MCP/REST/Adapter 一致性 |
| 测试质量 | 15 | fixtures、负向测试、并发/恢复、证据真实性 |
| 可维护性 | 10 | 模块边界、可读性、迁移可回滚、依赖克制 |
| 部署/运维说明 | 5 | 部署、秘密、监控、备份恢复、运行手册 |
| **合计** | **100** | |

### 5.2 硬失败（无论总分）

出现以下任一情况，候选**直接不合格**，不得用总分补偿：

1. **越权/权限提升**：伪造身份、绕过 Grant、扩大 capability/resource scope、优先级覆盖硬拒绝。
2. **凭据泄漏**：令牌、密码、API Key、完整票据进入提示词/日志/Trace/审计/Git/错误响应。
3. **审批旁路**：自审、Accountable Owner 自审、缺审批直接执行、伪造审批、确认 payload 不符仍执行。
4. **数据越界**：跨任务/部门/项目读取或写入未授权数据，或客户端直连绕过 PEP 而声称已治理。
5. **审计伪造/删除**：无法追溯的授权或执行记录，或删除/改写历史状态。
6. **虚假证据**：声称未运行的测试通过，或 Mock 冒充真实运行时兼容性。
7. **破坏契约**：修改冻结 Schema/fixtures 以适配实现，且未走 §6。
8. **决策/Grant 重放**：复用过期决策、旧 Grant 重签、Grant 晚于决策过期仍执行。
9. **模式混淆**：宣称 Advisory/Governed 具备未验证的强制治理，或用一种模式的 Grant 执行另一种模式。
10. **完成检查绕过**：无结构化完成条件、结果引用或验证证据仍标记 `succeeded`。

硬失败必须书面记录证据与影响范围；不得私下“修复后再算通过”。

### 5.3 偏离规格

- 任何偏离必须书面列出：契约条款、实际行为、原因、风险、替代缓解。
- 偏离由统一决策同步到所有候选；不得只让一个候选“特批”。
- 未声明的偏离按规格不符合处理；涉及硬失败的直接不合格。

### 5.4 比较流程

1. 所有候选使用同一契约哈希、同一 fixtures、同一共享测试套件。
2. 各候选在隔离环境完成实现并提交证据。
3. 比较者先独立跑共享测试与硬失败检查，再读实现。
4. 评分必须引用具体证据（命令、退出码、文件/测试名），不得只给印象分。
5. 结果记录：候选、SHA、契约哈希、各维度得分、硬失败、偏离、未验证项。
6. 比较结论不改变 Phase 0 契约；需要改契约时走 §6。

## 6. 契约变更流程

契约是并行实现共同依赖的公共面，任何变更必须同步、版本化、可追溯：

1. **提出**：新 Issue 或 ADR，说明问题、影响候选、是否破坏兼容。
2. **决策**：由项目所有者/Master 按治理规则批准；涉及安全边界、权限模型、数据删除/迁移、公开行为变化时进入 `NEEDS_HUMAN_DECISION`。
3. **版本**：
   - 破坏性变更（删除字段、改语义、改状态、改失败行为）升**主版本**；
   - 向后兼容的新增字段/原因码/状态升**次版本**；
   - 纯文字澄清不改语义可升**修订版本**，但必须更新哈希并通知所有候选。
4. **同步更新**：Schema、fixtures、状态机、Runtime Adapter、威胁模型、ADR 一起改；不得只改 Schema。
5. **重跑**：所有候选在相同新契约哈希上重跑共享测试与硬失败检查。
6. **记录**：在 Issue/PR 记录旧/新版本、哈希、变更原因、受影响候选、验证结果。
7. **禁止**：任一候选私自扩字段、放宽校验、删除 fixture 或修改状态机来让测试通过。

## 7. Phase 0 门禁与当前状态

- Issue #77 的验收标准 6 明确：**产品负责人确认 Phase 0 契约前，不启动依赖这些契约的并行产品实现。**
- 当前状态：1.0.0 契约包已作为冻结候选提交；产品负责人确认仍是独立门禁；Issue #78–#83 不得据此提前启动。
- 已确认的产品负责人决策（2026-10-20）：授权型决策绑定任务、`failed` 为尝试终态且重试必须新鲜决策/新 Grant、1.0.0 默认禁止自审。详见 [ADR-001](ADR-001-product-boundary.md) §7。
- 确认前允许的活动：文档/契约评审、fixtures 校验、威胁与状态机审查、运行时只读 Spike（不写产品实现、不改核心契约）。
- 确认后：按 §4 拆垂直工作包，先建立身份/策略/任务骨架与共享测试，再接入客户端与运行时。

## 8. 证据格式

每个交付至少包含：

```text
contract_version: 1.0.0 (unreleased candidate; not yet accepted)
contract_bundle_sha256: <64-hex>
contract_bundle_includes_fixtures: true
head_sha: <git sha>
commands:
  - <command>
    exit: <code>
    output: <summary or path to retained log>
fixtures:
  valid: <n> passed
  invalid: <n> failed as expected
hard_fail_checks:
  privilege_escalation: pass|fail
  credential_leak: pass|fail
  approval_bypass: pass|fail
  data_escape: pass|fail
  decision_replay: pass|fail
  mode_confusion: pass|fail
  completion_check_bypass: pass|fail
unverified:
  - <item>
deviations:
  - <item>
```

未运行的项目必须写“未运行”，不得留空或声称通过。
