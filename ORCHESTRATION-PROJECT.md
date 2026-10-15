# ORCHESTRATION-PROJECT.md — Per-Project Configuration

> 这是项目适配层。首次接入时填写；不要把特定项目的技术栈、测试路径或产品约束写进通用规则文件。

## 项目身份

- 项目名称：中国大陆企业 Hermes Agent 原生管理系统
- 一句话目标：以 AI Agent 为主要工作入口的企业管理系统，首期交付报销闭环，面向 20–100 名员工，部署于中国大陆
- 现有项目 / 新项目：新项目（仓库内已有 Agent-Orchestration 规则包，业务代码从零开始）
- 本地主分支：`main`
- 语言 / 框架 / 运行方式：Python 3.13（由仓库根 `.python-version` 固定；uv 管理环境与依赖，依赖清单见 `pyproject.toml` 与 `uv.lock`）；FastAPI + PostgreSQL 17 + Alembic（PostgreSQL 已安装并建 `ajaimgr_dev`/`ajaimgr_test`，peer 认证；业务骨架尚未建立，当前仅 `app/` 下 11 个领域目录占位）；本机仅作编排与开发机，生产 Profile 主机为同类 Debian Linux（内存更多、CPU 更快，规格待定）
- 重要设计 / 需求文件：`templates/PLAN.md`（实施方案 v2.0）、`docs/PROJECT-CHARTER.md`（项目章程 v1.0）、`docs/research/` 下的 W0-1/W0-2/W0-5/W0-7 调研报告与 TASK-0008 环境报告、`docs/DEPLOYMENT-REQUIREMENTS.md`（生产部署要求）

## 范围与安全

- 允许改动路径：`app/`、`docs/`、`tests/`（每个任务需进一步收窄到具体文件或领域目录）
- 永远禁止改动路径 / 文件：`AGENTS.md`、`MASTER.md`、`AGENT-BOOTSTRAP.md`、`AGENT-ORCHESTRATION.md`、`AGENT-ORCHESTRATION-ACCEPTANCE.md`、`README.md`、`VERSION.txt`、`LICENSE.txt`、`scripts/`、`prompts/`、`templates/`（除非任务明确授权）、`.orchestration/`
- 秘密与隐私文件模式：`.env*`、`*.key`、`*.pem`、`*secret*`、`*credential*`，以及含个人数据的样例文件与运行日志
- 破坏性操作和迁移政策：数据库迁移必须与功能代码同一任务交付且可回滚；数据删除、迁移或批量覆盖一律先进入 NEEDS_HUMAN_DECISION
- 新依赖政策：引入第三方依赖必须在任务中声明用途与替代方案并经 Master 批准；依赖在本机的可用性必须真实探测，不得假定
- 需要人工批准的变化：权限与安全模型变更、数据删除或迁移、凭据处理、新增外部服务、不可逆操作、重大架构取舍、产品行为存在实质歧义

## 验证命令

每条命令必须在本机真实探测；未配置的检查写“待建立”，不要假定通过。

- 快速测试：`uv run pytest -q`（业务测试建立后生效；当前尚无业务测试用例，不得据此声称通过）
- 完整回归：`python3 -m unittest discover -s tests -v`（规则包测试，实测通过）
- 静态检查 / 类型检查：`uv run python -m py_compile <path1> <path2>` 与 `python3 scripts/validate_kit.py`
- 构建 / 打包：待建立（尚无打包需求）
- 手工 / UI 冒烟检查：待建立（尚无服务与前端）
- 合并后必须执行：`python3 scripts/validate_kit.py --project-ready && python3 -m unittest discover -s tests`
- 数据库连通性：`psql -d ajaimgr_dev -c "select 1"`（PostgreSQL 17，peer 认证）
- 附加说明：本机不存在 `python` 命令，必须使用 `python3`；Python 环境与依赖统一用 uv 管理，不得使用 pip

## 本地工作流

- 分支命名：`task/<ID>-<short-name>`
- Worktree 根目录：`.worktrees/`
- 工作目录要求：一个执行实例一个 Worktree；不共享可写工作区。
- 并发上限：1（依据：本机 2 vCPU / 1.9 GiB 内存；W0-7 实测多路复用下空闲 Profile 边际内存约 14 MB，但 2 vCPU 与单网关进程共享命运使并发执行实例仍限 1；本机存在 swap 约 3.0 GiB 但不计入常驻容量）
- 默认审查修复上限：`3`（可通过 `.orchestration/config.json` 的 `retry_cap` 调整；只有用户显式修改此值才可改变）
- 本地证据保留方式：`.orchestration/`；日志中需脱敏。
- 备份策略：定期执行 `python3 scripts/orchestrator.py backup` 备份任务库；数据库建议 `pg_dump`；仓库、`.orchestration/` 与备份需另存于生产主机之外；恢复演练与保留策略详见 `docs/DEPLOYMENT-REQUIREMENTS.md` 第 5 节
- Python 环境：解释器由仓库根 `.python-version` 固定（3.13）；依赖由 uv 管理（`pyproject.toml` + `uv.lock`），不得使用 pip；当前本地开发所有 Worktree 共享主工作树 `.venv`（通过 `UV_PROJECT_ENVIRONMENT`），提高并发上限时必须改回每 Worktree 独立（详见 `docs/DEPLOYMENT-REQUIREMENTS.md` 第 4 节）

## 新项目启动产物

最少应建立：项目章程 / 范围与非目标、关键假设、架构决策记录（若适用）、目录结构、首个可运行垂直切片、验证方式和后续任务路线。按项目规模调整；不要求为了模板完整而过度文档化。
