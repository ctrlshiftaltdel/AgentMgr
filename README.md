# AgentMgr

面向企业的 **独立 Agent 治理与企业决策服务**。AgentMgr 管理任务必要性、优先级、责任归属、职权、可执行/禁止能力、确认与审批、执行边界及审计；WorkBuddy、Hermes、Rakazo、Codex、OpenCode 等已有 Agent 工具链由各自运行时提供，AgentMgr 不重复造轮子。项目继续承载现有企业业务模块（首期业务为报销），通过 MCP / REST 对外提供受控接口；Hermes 是现有兼容实现与首个候选 Runtime Adapter；完整适配能力待 Phase 4 真实验证，不再是产品硬依赖。

## 现状

- **Wave 1 已完成**：后端底座与迁移、身份与权限（RBAC + 越权矩阵）、报销域与状态机、工作流与事务性 Outbox、受理与文件服务、PDF/XLSX 导出、API 层、最小管理后台、端到端负面测试。
- **发票查验链条已完成**：渠道 Provider（腾讯云、百度为首选渠道）+ 人工辅助录入 + 报销单与发票挂账（`expense_invoices`）。
- 历史测试基线以 HANDOVER.md 和当前 head 对应的 CI / 真实运行结果为准；README 中的历史数字不得作为当前验收证据。
- 阶段 1 尚缺：微信消息路由、OCR/ASR 解析流水线、备份异地存储与恢复演练收口。

## 新产品定位与架构基线

- 完整可独立启动的目标与实施规格： [docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md](docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md)。
- AgentMgr 是策略、任务、职权、责任、审批、执行边界与审计层；不要复制已有 Agent 的 CLI、文件、浏览器或通用连接器。
- 旧文档中把 Hermes Profile、会话或内部 API 规定为永久强制前提的内容，应按新计划逐步迁移。现有 Hermes Profile 与业务模块属于兼容实现/已有资产，不要求立即废弃。
- 强制治理必须在策略执行点（PEP）或受控运行环境落地。远程 AgentMgr 无法拦截客户端 Agent 在本地任意使用的 Shell、文件、浏览器或用户自有凭据。
- 当前阶段为架构切换与契约冻结；未经过真实测试的客户端和运行时能力一律标记为“未验证”。

## 文档索引

- 目标架构与独立执行蓝图：`docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md`
- 项目章程：`docs/PROJECT-CHARTER.md`；既有业务模块实施方案（Hermes 兼容/迁移背景）：`templates/PLAN.md`；部署要求：`docs/DEPLOYMENT-REQUIREMENTS.md`
- 调研报告：`docs/research/`（微信、Profile 隔离、发票查验、数据合规、多 Profile 实测）
- 会话交接：`HANDOVER.md`

## 目录结构

```
app/        领域代码（identity/intake/expense/workflow/documents/inventory/
            integrations/audit/api/db/core/runtime）
app/tests/  pytest 测试套件
migrations/ Alembic 迁移
ops/        备份与恢复工具
docs/       文档与调研报告
```

## 开发与协作（Agent-Orchestration — GitHub Edition）

本仓库由 AI Agent（Master / Execution / Reviewer 角色）按 **Agent-Orchestration GitHub 版规则**开发协作：

- **GitHub Issues** 是任务需求与验收标准的权威记录；**PR** 是实现与评审载体；当前 head SHA 对应的 CI 与本地验证才是当前证据。
- 角色授权与协作契约：`AGENTS.md`；状态机、活动任务锁、合并门禁 G1–G6：`AGENT-ORCHESTRATION.md`；Master 手册：`MASTER.md`；项目适配配置：`ORCHESTRATION-PROJECT.md`。
- 启动提示词：`prompts/`；Issue / PR 模板：`.github/`。
- 只允许 Master 合并；head 任何变化使既有批准与 CI 证据失效；派发前必须在 Issue 写活动任务登记评论。

## 自检

```bash
python3 scripts/validate_kit.py                 # 规则包静态检查
python3 scripts/validate_kit.py --project-ready # 项目配置已填写
python3 scripts/github_preflight.py             # git/gh 只读预检
python3 scripts/check_privacy.py --ci           # 隐私/凭据扫描
python3 -m unittest discover -s tests -v        # 规则包单元测试
uv run pytest app/tests -q -o addopts=""        # 全量测试（需要本地 PostgreSQL 17）
```

## 隐私与本地信息

公开仓库不保存任何可识别本机或个人的信息（用户名、主机名 / SSH 别名、绝对家目录、内网或公网 IP、代理地址、凭据、真实账号标识）。环境细节写入本地 `local_info.md`（已忽略，不入库）；仓库内一律使用 `<repo-root>`、`<test-host>`、`<test-host-venv>` 等占位符。CI 的 `privacy-scan` 会扫描文件、提交消息与历史；`Privacy Audit` 工作流每周及手动审计 Issue / PR 文本。

## 许可

规则包文本、模板与辅助脚本按 `LICENSE.txt`（MIT）提供；产品代码的许可与使用范围由项目所有者另行决定。
