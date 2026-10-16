# AgentMgr

面向中国大陆 20–100 人企业的 **Hermes Agent 原生管理系统**。以 AI 对话/语音/图片/文件为主要工作入口；正式账务与付款留在金蝶等有正式授权的系统。**首期模块：报销**。

## 现状

- **Wave 1 已完成**：后端底座与迁移、身份与权限（RBAC + 越权矩阵）、报销域与状态机、工作流与事务性 Outbox、受理与文件服务、PDF/XLSX 导出、API 层、最小管理后台、端到端负面测试。
- **发票查验链条已完成**：渠道 Provider（腾讯云、百度为首选渠道）+ 人工辅助录入 + 报销单与发票挂账（`expense_invoices`）。
- 测试基线：**675 passed**（本地 PostgreSQL 17）；迁移链 `0001` → `0011`。
- 阶段 1 尚缺：微信消息路由、OCR/ASR 解析流水线、备份异地存储与恢复演练收口。

## 文档索引

- 项目章程：`docs/PROJECT-CHARTER.md`；实施方案：`templates/PLAN.md`；部署要求：`docs/DEPLOYMENT-REQUIREMENTS.md`
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
python3 -m unittest discover -s tests -v        # 规则包单元测试
uv run pytest app/tests -q -o addopts=""        # 全量测试（需要本地 PostgreSQL 17）
```

## 许可

规则包文本、模板与辅助脚本按 `LICENSE.txt`（MIT）提供；产品代码的许可与使用范围由项目所有者另行决定。
