# ORCHESTRATION-PROJECT.md — GitHub Project Configuration

> 项目适配层（Agent-Orchestration **GitHub 版**）。通用规则见 `AGENTS.md` / `AGENT-ORCHESTRATION.md` / `MASTER.md`；项目细节只写在本文件。

## Project

- Repository: `ctrlshiftaltdel/AgentMgr`（https://github.com/ctrlshiftaltdel/AgentMgr ，public）
- Default branch: `main`
- Product goal / one-sentence summary: 面向企业的独立 Agent 治理与企业决策服务；管理任务必要性、优先级、责任归属、职权、允许/禁止能力、审批、可强制执行边界与审计；不重复构建 WorkBuddy/Hermes/Rakazo 等 Agent 已有工具链。通过 MCP/REST 接入客户端，通过可替换 Runtime Adapter 接入服务器端 Agent。继续维护现有报销等企业业务模块；Hermes 是兼容运行时之一，不是产品硬依赖。
- Existing repository / new repository: 已有仓库（含完整开发历史；起源于单机离线版规则包，2026-10-16 切换为 GitHub 版规则并首次推送）。
- Primary design/spec files: `docs/AGENTMGR_STANDALONE_PLATFORM_PLAN.md`（新目标架构与执行计划）、`docs/PROJECT-CHARTER.md`（项目章程）、`templates/PLAN.md`（既有业务模块实施方案）、`docs/DEPLOYMENT-REQUIREMENTS.md`（部署与安全要求）、`docs/research/`（历史调研证据）。

## 架构方向（2026-10-20）

- AgentMgr 是独立的 Agent 治理与企业决策服务，不是 Hermes 专属产品。
- 复用已有 Agent Runtime 工具链，不平行开发 Shell、浏览器、文件和通用连接器。
- 客户端 MCP/REST、服务端 Agent 业务工具、运行时控制适配器属于不同信任边界。
- 强制治理必须有 PEP 或受控执行环境；仅靠提示词不能约束客户端本地工具。
- 在有真实需求前保持模块化单体；运行时兼容性必须由真实证据验证后才能标记为支持。

## Permissions

- May Master autonomously dispatch Execution Subagents? yes —— 本机具备 C1–C4（delegate_task 可启动独立实例、隔离上下文、等待并取回结果、区分成功/失败/取消，已多轮实测）。并发上限 4 或按负载动态（CPU / 内存有空余即可派发；路径两两不重叠为硬前提，详见 §Execution policy 的 `Parallelism cap`）；派发前必须先在目标 Issue 写活动任务登记评论。
- Who may merge? Master only；人类所有者始终可指定运行模式、缩小授权或要求暂停。
- Human decision triggers: 新增第三方依赖；权限/安全模型变更；凭据处理；数据删除/迁移；新增外部服务；不可逆操作；重大架构取舍；公开行为变化；产品行为实质歧义。以上一律进入 `NEEDS_HUMAN_DECISION`。
- Protected paths / sensitive data patterns: 受保护路径为 `AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`ORCHESTRATION-PROJECT.md`、`README.md`、`VERSION.txt`、`LICENSE.txt`、`HANDOVER.md`、`.github/`、`scripts/`、`tests/`、`prompts/`、`templates/`、`.orchestration/`、`.env*`（任务确需修改时必须在 Issue 中显式列出并说明）。敏感模式：密钥/令牌/cookie/真实账号标识/凭据值；含个人数据的样例文件与运行日志；`.env*`、`*secret*`、`*credential*`、`*.key`、`*.pem`。
- Privacy / local machine data policy: 公开内容（仓库文件、提交消息、Issue、PR、评论、CI 日志）不得包含个人用户名、主机名 / SSH 别名、绝对家目录、内网或公网 IP、本地代理地址、凭据或真实账号标识。环境细节写入**本地** `local_info.md`（`.gitignore` + `.git/info/exclude`，不得提交）；仓库内使用 `<repo-root>`、`<test-host>`、`<test-host-venv>`、`<local-proxy>` 等占位符；允许 `~`、`127.0.0.1`（仅测试）、RFC 5737 文档网段与 `example.com`。CI `privacy-scan` 阻断；`privacy-audit` 每周/手动扫描 Issue/PR 文本。泄漏处置：立即轮换凭据、按需经所有者批准重写历史，并在 Issue 记录处置结果。
- Dependency policy: 现有 10 个直接依赖（fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2）。新增依赖必须在 Issue 中声明用途与替代方案并经所有者批准；环境与依赖一律用 uv 管理（禁止 pip）；解释器由仓库根 `.python-version` 固定 3.13。

## Allowed change policy

- Default task paths: 每个 Issue 必须进一步收窄。当前代码分布：`app/`（领域代码）、`app/tests/`（pytest 测试）、`migrations/`（Alembic 迁移）、`ops/`（备份与恢复工具）、`docs/`（文档与调研）。
- Forbidden paths: 上方受保护路径一律禁止；未在 Issue 中列出的路径默认禁止（扩大范围须在 Issue 中记录授权）。
- Schema / database migration policy: 迁移必须与功能代码同一任务交付且可回滚（upgrade/downgrade 往返验证）；不得改写已应用迁移的历史语义；PostgreSQL 17，本地库 `ajaimgr_dev` / `ajaimgr_test`（peer 认证，任何文件不得写口令）。
- External service / credentials policy: 凭据只从环境变量读取、缺失即 fail closed；日志与审计不得含凭据值或明文敏感字段；不提交密钥/令牌/`.env`；测试一律使用桩或本地样本，不调用真实外部服务。

## Required CI checks

（名称以 GitHub 上真实 check run 为准；规则集/分支保护要求 `main` 仅允许经 PR 合并且下列检查通过，禁止 force push 与分支删除。）

- Required check 1: `kit-validate`（workflow `CI`：`validate_kit.py --project-ready` + 规则包单元测试）
- Required check 2: `app-tests`（workflow `CI`：PostgreSQL 17 服务容器 + 全量 `pytest app/tests`）
- Required check 3: `privacy-scan`（workflow `CI`：`scripts/check_privacy.py --ci`，扫描文件、提交消息与历史）
- Additional required checks: 无。`privacy-audit`（workflow `Privacy Audit`）为每周/手动审计，不作为 PR 门禁。

## Local validation

- Fast test command: `python3 scripts/validate_kit.py --project-ready && python3 -m unittest discover -s tests && python3 scripts/check_privacy.py --ci`
- Privacy check: `python3 scripts/check_privacy.py --ci`（仓库文件 + 提交消息 + 全部历史；`--github` 额外扫描 Issue/PR 文本）
- Full test command: `flock /tmp/ajaimgr-fulltest.lock bash -c 'export UV_PROJECT_ENVIRONMENT=<test-host-venv> && uv run --no-sync pytest app/tests -q -o addopts=""'`（需要 PostgreSQL 17；会重置测试库 `ajaimgr_test`，故经 flock 串行排队执行；`--no-sync` 见 §Agent workspace 的 venv 并行策略；实际路径见本地 `local_info.md`）
- Lint / typecheck: 未配置独立 linter/typechecker；语法探测用 `python3 -m py_compile <files>`
- Build / package: 待建立（尚无打包需求）
- Post-merge verification: Fast + Full 全部在默认分支上重跑并记录真实输出与退出码

## Execution policy

- Branch naming: `feat/<issue>-<short>` / `fix/<issue>-<short>` / `chore/<issue>-<short>`
- Draft PR expected early: yes（尽早开 Draft PR；确有理由时在 PR 中注明）
- Review-fix cap: 3 rounds by default.
- Parallelism cap: 4 或按负载动态（有空余即派发：CPU / 内存尚有余量即可派发下一个执行实例）。条件：(a) 同批次各任务的允许路径集合两两不重叠（含文档文件）；(b) 测试主机全量测试必须经 `flock /tmp/ajaimgr-fulltest.lock` 串行；(c) 派发前仍须完成活动任务登记与依赖检查（不变）。依据：编排开发机已升级为 4 vCPU / 7.7 GiB，原「2 vCPU / 1.9 GiB」的资源瓶颈不再成立；测试主机全量测试改由 `flock /tmp/ajaimgr-fulltest.lock` 串行化，不再需要全局并发上限。
- Active task registry: required Issue comment（见 `AGENT-ORCHESTRATION.md` §2 与 `templates/ACTIVE-TASK-REGISTRY.md`）。
- Issue closure timing: after post-merge verification；关闭前在登记评论记录最终 SHA 与验证结果。（不使用 `Closes #N` 自动关闭；PR 正文用 `Refs #N`。）

## Agent workspace / local machine notes（编排机约定）

- 开发机可为 Windows / Linux / macOS；具体路径、SSH 别名与连接参数见本地 `local_info.md`（不入库）。
- `local_info.md` 只存在于各机本地，已被 `.gitignore` 与 `.git/info/exclude` 忽略；不得提交、粘贴到 Issue/PR 或作为 CI 产物。
- 不新建、不启动、不停止任何真实 Hermes Profile；不写入 `~/.hermes/` 下任何文件（W0-7 一次性实验授权已过期，不得顺延）。
- 不读取、不打印任何真实凭据值；仓库、日志与报告中一律使用占位符；不向所有者索要凭据。
- 并行模式下继续共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT` 指向该 `.venv`，实际路径见本地 `local_info.md`）；不在 worktree 内生成独立 `.venv`。并行执行必须使用 `uv run --no-sync`（禁止并发 `uv sync` 就地改写环境）；任何会改动 `pyproject.toml` / `uv.lock` 的任务必须独占派发（不与其它执行实例并行）。
- TMPDIR 指向仓库外且不在 `~/.hermes` 下。
- 提交作者：仓库级占位 `Agent Orchestration <agent@localhost>`（所有者已明确不更换）。
- 本机为编排与开发机；生产 Profile 主机另行准备（规格待真实负载测试）。
- `.orchestration/` 为离线版规则包时期的历史归档（纸质台账 + 工具副本），不再更新；GitHub Issues/PR 是当前事实来源。
