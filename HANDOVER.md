# HANDOVER — 会话交接文档

- 生成时间：2026-10-17 20:42 CST（**财务对接方向变更 + 整合计划/章程/README/PLAN 三批文档落库之后**）
- 主分支基线 head：`adfa7e9`（**本文档提交之前的 head**；本文档经 PR 合入，会出现在其之上）
- 上一版交接：`2d21bfc`（HANDOVER 刷新，PR #9）；更早 `cdbcd47`（GitHub 版启用后）、`b38cb45`（离线版暂停点）；完整历史见 git log
- 任务台账：**GitHub Issues**。开放 Issue：**#16**（Track A 差距分析，PLANNED）；#10/#12/#13 已完成关闭；#17 为本文档刷新。离线版 45 条台账已退役归档（不再更新）。
- 你的角色：**Master Agent** —— 计划、派发、独立复核、**唯一合并者**、阶段收口；默认不实现产品代码。
- 接手顺序：`AGENTS.md` → `AGENT-ORCHESTRATION.md` → `MASTER.md` → `ORCHESTRATION-PROJECT.md` → 本文档 → `prompts/START-MASTER-EXISTING-PROJECT.md`。

---

## 0 怎么用这份文档

1. §1–§5 是必读背景（角色、约束、环境、状态）。
2. **§6 是恢复点**：队列冻结中，等所有者示意；恢复后按 GitHub 流程（Issue → 活动登记评论 → 分支/PR → G1–G6 → 合并 → 合并后验证 → VERIFIED → 关 Issue）。
3. §7 未完成；§8 已完成（**不要重做**）；§9 已知取舍与决策（最容易丢）；§10 命令手册；§11 坑；§12 开局清单。
4. 离线版工具（`scripts/orchestrator.py`、`.orchestration/state.sqlite3`）**已退役**：不要用它们登记或推进任务；它们是历史归档。

---

## 1 角色与权威（GitHub 版要点）

- **事实来源**：GitHub Issues / PRs、仓库文件、Git 提交、当前 head 的真实检查结果。聊天记忆不得代替可核验记录。
- 派发前必须先在目标 Issue 写**活动任务登记评论**（`templates/ACTIVE-TASK-REGISTRY.md` 的 `ORCH-TASK-REGISTRY` 块；含锁定路径、运行模式、分支/PR、执行句柄、时间）。不能仅靠开放 PR 重建锁。
- **只有 Master 合并**；批准绑定 head SHA，head 一变即失效（CI 证据同理）；Execution/Reviewer 不得批准或合并。
- 单会话自动派发前必须真实探测 C1–C4（spawn/隔离/等待取回/区分结果）；不具备则降级为多会话交接。**不得虚假派发**（派发后核实实例确实在跑）。
- 合并门 **G1–G6**（验收/范围/基线/CI/证据/风险）与审查—修复 3 轮上限：见 `AGENT-ORCHESTRATION.md` §5/§6。

---

## 2 项目定位与当前阶段

面向中国大陆 20–100 人（后续扩到 100）企业的 Hermes Agent 原生管理系统，**首期模块：报销**。以 AI 对话/语音/图片/文件为主要工作入口；**不接入金蝶等特定第三方财务系统**，对外账务交接采用模板驱动的 Excel 导出（模板待所有者提供；远期自研财务/ERP）。

- 项目章程 `docs/PROJECT-CHARTER.md`（O3 已记录「百度为首选查验渠道」）；实施方案 `templates/PLAN.md`（v2.0）；部署要求 `docs/DEPLOYMENT-REQUIREMENTS.md`；调研 `docs/research/`（W0-1/2/4/5/7 与 W1-28）
- **当前阶段**：Wave 1 主体 + 发票查验完整链条（渠道 Provider：腾讯云 W1-20、百度 W1-28；人工辅助录入 W1-22；报销单挂账 W1-23）+ **W1 冻结队列三项（W1-24 模型导入自足性、W1-16 迁移可复现性、W1-26 受理解析流水线接口与人工兜底）** 已完成；阶段 1 尚缺：微信消息路由、OCR/ASR **真实供应商**接入（接口层已交付）、备份异地/跨主机恢复收口。**2026-10-16 完成规则层切换：离线版 → Agent-Orchestration GitHub 版**（仓库 `ctrlshiftaltdel/AgentMgr`，public）。**2026-10-17 财务对接方向变更**：金蝶接入取消，对外账务交接改为模板驱动 Excel 导出（模板待所有者提供），远期自研财务/ERP（章程 v1.1）；同日落库 AI 工具治理整合计划 v0.2（Track A/B/C/D，`docs/AgentMgr_AiNativeERP_Integration_Plan.md`）。

---

## 3 不可违反的硬约束

1. **严禁读取、复制或索要凭据值**（token/cookie/真实账号标识/口令）：仓库、日志、报告一律占位符。不要在聊天中接收凭据。
2. 不新建、不启动、不停止任何 Hermes Profile；不写入 `~/.hermes/` 下任何文件。
3. 不用 pip；统一 uv（`~/.local/bin/uv`）；解释器由 `.python-version` 固定 3.13（生产同为 3.13）。
4. **不新增第三方依赖**，除非在任务 Issue 中声明理由与替代方案并获批准。当前 10 个直接依赖：fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2。
5. DSN 默认走本地 unix socket peer 认证；任何文件不得写入口令。
6. 不得为让测试通过而修改、删除或跳过既有断言（行为变化可修正并留证据）。
7. **`main` 受规则集保护**（`main-protection`，id 25216701）：必须经 PR 合入；required checks = `kit-validate` + `app-tests`（strict：分支须最新）；禁止 force push 与分支删除；**当前无 bypass**——直接 push main 会被拒绝，确需直推时由所有者调整规则集。
8. 子代理不得自批、自合并、自我验收；Master 不得虚假派发或把阻塞当完成。
9. **跨会话证据必须已推到远端**；本地提交、本地日志不算数。
10. 未被 Issue 列出的路径默认禁止；扩大范围须记录授权。受保护路径另见 `ORCHESTRATION-PROJECT.md`（含 `AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`.github/`、`scripts/`、`tests/`、`prompts/`、`templates/`、`HANDOVER.md`、`.orchestration/`、`.env*`）。

---

## 4 环境事实

| 项 | 值 |
|---|---|
| 编排+开发主机（macOS，2026-10-16 起） | 工作区 `<repo-root>`（git clone）；`gh` 2.102.0 已登录；`uv` 已装；**无 PostgreSQL、不装 psql（只开发）**；`.workbuddy/` 已入 `.git/info/exclude`；`ssh test-host` 直连可用（SSH 命令可能触发沙箱授权，放行即可） |
| 调试/测试主机 `test-host` | Debian 13 (trixie)，内核 6.12.x，2 vCPU / 1.9 GiB（**仅编排与开发机**；生产 Profile 主机另配）；仓库 `~/ajaimgr`；uv 在 `~/.local/bin/uv`；`.venv` 与 PostgreSQL 17.11（`ajaimgr_dev` / `ajaimgr_test`，peer 认证；口令不入文件）齐备；全量测试与临时库实测在此执行 |
| GitHub CLI | 两端均已登录；remote `origin` = `ctrlshiftaltdel/AgentMgr`（public） |
| CI | GitHub Actions `ubuntu-latest`；`postgres:17` 服务容器（TCP+密码）；PG client 17 经 PGDG 安装并把 `/usr/lib/postgresql/17/bin` 注入 `$GITHUB_PATH`；libpq 环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD）供 socket DSN 的恢复演练使用 |
| 出网（test-host） | 经 Privoxy 代理 `<local-proxy>`；本机 git fetch 偶发代理 502 / HTTP2 framing 抖动（重试或 `-c http.version=HTTP/1.1`） |
| venv 策略 | `test-host` 上所有 worktree 共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`）；本机 worktree 共享 `<repo-root>/.venv`；提高并发上限时必须改回各自独立 |
| 并发上限 | **1**（派发前先确认没有子代理在跑） |

---

## 5 状态快照

- `main` head = `adfa7e9`（PR #11 `4ac3141` → PR #14 `e100621` → PR #15 `adfa7e9`，均为文档变更）；本机与 `test-host` 均已 ff 至此；工作区干净；无 worktree 残留；无运行中子代理；远端仅保留 `main`
- 测试：本机 Fast（`validate_kit --project-ready` + `unittest discover -s tests`）rc=0（2026-10-17 20:33 实测）；**本机未跑全量 pytest**（无 PostgreSQL）；各 PR 的 CI `app-tests` 均对 head 通过（`68a5e57` / `a57cc50` / `eee12f2`）；最近一次全量基线仍为 **730 passed**（2026-10-16，`test-host`），本批为纯文档变更，未重跑
- 迁移链 `0001_org_core` → `0002_identity_rbac` → `0003_permission_catalog`（**冻结快照**，W1-16）→ `0004_audit_events` → `0005_expense_core` → `0006_expense_claim_no_seq` → `0007_workflow_outbox` → `0008_intake_files` → `0009_perm_alignment` → `0010_invoice_verifications` → `0011_expense_invoices`
- 领域目录 12 个：`app/{identity,intake,expense,workflow,documents,inventory,integrations,audit,api,db,core,runtime}`
- 规则集 `main-protection`（id 25216701）active；`python3 scripts/github_preflight.py` 通过
- 开放 Issue：**#16**（Track A 差距分析，PLANNED，等优先级确认）；#10/#12/#13 已 VERIFIED 关闭；#17 = 本文档刷新
- 离线归档：`.orchestration/`（不变；含 `archive/orchestrator.py`、`state-20261016-114915.sqlite3`、45 条任务台账）

---

## 6 当前操作点（队列已清空）

**队列已清空（2026-10-16 16:00）**：#1/#2/#3 冻结队列已全部完成并关闭（见 §8）。新一轮派发等待所有者指示；候选来源为 §7（BLOCKED 项与阶段 1 未开工项），部分需先做 §9.2 产品决策。当前无任何进行中的任务。

**恢复后的标准循环**（对任意新任务）：

1. 读 Issue 全文 → 核对验收/路径/依赖（依赖须已 VERIFIED 且已合入 main）
2. 在 Issue 写活动登记评论（`DISPATCHED`、锁定路径、mode、runner、时间）
3. `git fetch --prune`；从最新 main 建分支 `feat|fix|chore/<issue>-<short>` + worktree `.worktrees/<name>`
4. 派发/启动 Execution（本机具备 C1–C4，可单会话）；**派发后核实实例确实在跑**
5. Execution 尽早开 Draft PR（`Refs #N`）；交回复核前更新到最新 main 并重跑验证
6. Master 独立复核（不复述作者自述）：范围、当前 head 的 CI、自写探针复现核心断言
7. G1–G6 通过且批准 SHA = 待合并 head → `gh pr merge --merge`
8. 合并后在 main 上重跑验证 → 更新登记评论为 VERIFIED → 关闭 Issue

---

## 7 未完成清单

### BLOCKED（等所有者输入）

| 项 | 需要什么 |
|---|---|
| 导出模板与字段口径（原金蝶 W0-3 位置，未建 Issue） | 首个 Excel 模板文件、期间/币种/单据号口径、目标财务系统的导入要求 |
| 脱敏业务样例与流程盘点（原 W0-6，未建 Issue） | 脱敏后的真实 Excel/Word/PDF 样例、报销规则、库存流程 |

### 待派发（PLANNED）

| Issue | 内容 | 状态 |
|---|---|---|
| #16 | Track A：AiNativeERP 设计映射与 Track B 范围（仅文档） | PLANNED，等所有者确认优先级后再写活动登记并派发 |

### 冻结队列——已于 2026-10-16 全部完成

（#1 W1-24、#2 W1-16、#3 W1-26 已合并并 VERIFIED，Issue 已关闭；交付见 §8。）

### 阶段 1 尚未开工

- **微信消息路由与统一输入格式**：依赖 W0-1 结论；真实验收需所有者授权在本机建 Profile
- **OCR/ASR 真实供应商接入**：W1-26 已交付可替换接口与人工兜底（`app/intake/parsing.py`），真实供应商由所有者决定
- **W1-25 遗留**：跨主机/异地恢复演练未做（生产验收必需项；同机 13/13 已通过；`--allow-missing-*` 误用边界见 `docs/BACKUP-RESTORE.md` §10）

---

## 8 已完成（不要重做）

- **财务对接方向变更与文档落库（2026-10-17，3 个 PR）**：
  - PR #11 `4ac3141`：章程升 v1.1（取消金蝶、模板驱动导出、远期自研财务/ERP）+ 整合计划 v0.2（Track A/B/C/D、契约字段、B-1/B-2 待决策、Track D 导出机制）。
  - PR #14 `e100621`：`README.md` / `ORCHESTRATION-PROJECT.md` / `HANDOVER.md` 定位对齐，BLOCKED 队列移除金蝶、改为导出模板。
  - PR #15 `adfa7e9`：`templates/PLAN.md` 升 v2.1，阶段 3/4 改为「财务导出与对账」与「财务能力扩展（远期自研）」；§8 新增 `export_templates` / `export_runs`。
  - 三个 Issue #10 / #12 / #13 均已 VERIFIED 并关闭；每批均通过 `kit-validate` + `app-tests`。

- 离线台账时期 **38 条 VERIFIED**：准备 8（骨架/章程/配置/规则包/环境等）、调研 5（W0-1/2/4/5/7）、Wave 1 主体（后端底座与迁移、身份权限、报销域与状态机、Outbox、受理与文件、导出、API、管理后台、权限目录、负面测试与修复、渠道身份、发票查验 Provider 与腾讯云/百度适配器、人工辅助录入、报销单挂账、Profile 生成器与校验器、备份与恢复工具及修复轮）。完整清单与事件流见 `.orchestration/state.sqlite3`（可用归档的 orchestrator.py 查阅）。
- **规则层切换（2026-10-16）**：Agent-Orchestration GitHub 版规则文件替换（AGENTS/MASTER/AGENT-ORCHESTRATION/ACCEPTANCE/prompts/scripts/tests/VERSION/LICENSE）、`.github/` 模板与 CI 工作流、`ORCHESTRATION-PROJECT.md` 按项目填写、README 合并、离线专属文件退役、CI 修复至全绿、主分支规则集启用、首批 Issue #1–#3 登记。
- **W1 冻结队列三项（2026-10-16，WorkBuddy 单会话编排；详见各 Issue 与 PR）**：
  - W1-24 模型模块导入自足性（PR #5，`83fcd95`）：`integrations` / `intake` / `expense` 三模块补 `app.documents.models` 显式导入；新增逐模块子进程测试 `app/tests/test_model_imports.py`（修复前 3/9 失败 → 修复后 9/9；全量 DDL 零变化）。
  - W1-16 迁移可复现性（PR #6，`810d120`）：`0003` 改**冻结快照**（7/19/31，不再读活动代码）；新文档 `docs/MIGRATION-REPRODUCIBILITY.md`；实测中间态 `7|19|31` → head `7|20|32`、往返 rc=0；`0009` 仍读活动代码为顺延项。
  - W1-26 受理解析流水线（PR #7，`8d6f0c9`）：`app/intake/parsing.py`（`IntakeParser` / `ManualEntryParser` / `ExternalParser` / `ParserRegistry` + 裁决与脱敏）+ `app/intake/pipeline.py`（草稿写入与确认追溯；**无结构变更**）；新文档 `docs/INTAKE-PARSING-PIPELINE.md`；纯逻辑 36 + 落库 8 测试全桩。

**值得知道的验收结论（累计摘要）**：越权矩阵 70 格逐格断言；主管不能审批自己的报销；状态机 49 组合穷举；Outbox `SKIP LOCKED` 并发正确；幂等（提交/审批/付款/查验限次）；7 种路径穿越全拒（含符号链接逃逸）；渠道身份零明文；W1-23 挂账探针 32/32（verified_fail 阻提交 → 翻转后放行且 live 同步）；W1-28 百度文档 sha256 逐字节比对。

---

## 9 已知取舍、限制与决策（最容易丢的一节）

### 9.1 仍然存在的取舍（不要当成缺陷重新修）

- `authorized` scope 仍是占位实现（`visible_employee_ids` 只返回自身；显式授权表未建）；不得假装已支持其语义。
- 单条越权返回 403 而非 404（防枚举是待决产品项）。
- 所有 `IntegrityError` 映射 422/409（兜底接受，归因精度下降）。
- 无效会话不写业务审计（安全监控落应用日志）。
- `sniff_mime` fail-closed（偏严）；XLSX 边界一次显式 `float()`（有注释与测试锁定）；PDF 中文用内置 CID 字体。
- `resolve_principal` 拒绝时 commit 调用方会话（调用方须视为事务边界）。
- 模型模块导入不自足（W1-24）与 `0003` 读活动代码（W1-16）**均已修复**（PR #5 / #6）；**`0009_perm_alignment` 仍读活动代码**，为 W1-16 顺延项（见 `docs/MIGRATION-REPRODUCIBILITY.md` §5），是否冻结待定。
- 解析流水线（W1-26）：关键字段口径（`expense_claim` → `amount`/`expense_date`）与置信度阈值 `0.75` 为**保守默认**（产品参数，可覆盖；见 `docs/INTAKE-PARSING-PIPELINE.md` §9）。
- 双机约定（2026-10-16 起）：本机 macOS **不装 psql**（只开发）；一切 DB 测试/实测走 `test-host`（见 §4）。
- Outbox 用应用侧时钟；W1-8 只有 service 原语、无常驻 Worker。
- API 身份仍是 `X-Employee-Id` 过渡方案；管理后台登录=内部令牌（生产必须接 SSO，已写 docstring）。
- 微信通道「有条件 Go」，11 项待实测。
- `expense_invoices`（W1-23）：`suspected_duplicate` 为授权新增列；`attach_invoice` 不校验 `source_file_id` 存在性（FK 兜底）；同单并发重复以 IntegrityError 失败（可重试）；**尚无 HTTP 端点**。
- `invoice_verifications` 不在 conftest TRUNCATE 清单（模块自清理；套件后残留实测 0）。
- 百度为首选查验渠道（腾讯云/聚美智数备用；无渠道选择开关）；凭据 env-only（`AJAIMGR_BAIDU_API_KEY`/`AJAIMGR_BAIDU_SECRET_KEY`，缺失即 fail closed）。
- **CI 环境差异**：`test_profile_provisioning` 有一例依赖本机 `~/.hermes`，CI 上合法 skip；CI 中 `pg_dump` 必须与服务同主版本（已处理）。
- 规则集当前**无 bypass**：所有者如需直接 push main 可自加 bypass actor（并接受绕过 PR/CI 的风险）。
- 仓库 **public**（所有者决定）；历史作者均为占位身份。

### 9.2 所有者决策清单（未决）

1. 导出模板：首个 Excel 模板与字段口径（阶段 3 前置）；金蝶接入已于 2026-10-17 取消
2. 脱敏业务样例与现有流程
3. OCR/ASR 供应商（决定 #3 之后适配器方向）
4. 微信接入授权（是否允许本机建 Profile 做真实路由验收）
5. 单条越权 403 vs 404
6. `description` 长度 1–500 字符（PLAN 未规定的引入默认值）
7. 生产 Profile 主机规格（需真实负载测试）
8. 百度查验正式开通（账号/费用/合规确认；原 W0-4 §7 项）
9. 生产备份必须存放于生产主机之外（章程成功标准未满足）
10. 工具层 B-1：谁负责选工具（Agent 侧 / 后端路由 / 混合）——见整合计划 §5.6；建议 Agent 侧 + `needs_more_info` 一等结果
11. 工具层 B-2：确认请求持久化（新增 `tool_confirmations` / 复用审批实例 / 不持久化）——见 §5.7；「不持久化」应否决（微信异步必丢）
10. GitHub 协作策略微调（直推 bypass？合并方式？——当前：PR + G1–G6 + strict checks）
11. 解析流水线产品参数确认（关键字段口径、置信度阈值——当前均为保守默认）

---

## 10 操作手册（命令级）

### 双机注记（2026-10-16 起）

- 编排（gh / git / Issues / PR）：**本机 macOS**（`<repo-root>`）直接执行。
- 全量测试、临时库实测、迁移往返：`ssh test-host`（SSH 命令可能触发沙箱授权，放行即可；后台任务内跑 SSH 会被沙箱拦截，需前台执行）。

### 状态恢复（每次接手先跑）

```bash
cd <repo-root>        # 本机（macOS）
git fetch --prune && git log --oneline -3 && git status --short
gh issue list --state open
gh pr list --state open
python3 scripts/github_preflight.py
```

先读所有开放 Issue 的**活动登记评论**（含尚无 PR 的任务），再核对开放 PR 的实际 Diff 路径，重建锁表与状态。

### 派发一个任务

1. 核对 Issue：单一目标/验收/允许路径/禁止路径/依赖/验证命令
2. **先写活动登记评论**（模板 `templates/ACTIVE-TASK-REGISTRY.md`），再真实启动 Execution
3. 分支 `feat|fix|chore/<issue>-<short>`；worktree `.worktrees/<name>`
4. 交付物：分支/PR、head SHA、命令与真实输出、验收矩阵、未完成项

### 复核与合并

```bash
gh pr checks <N> --watch             # 当前 head 的必需检查
gh pr view <N> --json mergeable,headRefOid,files
git fetch && git diff origin/main...origin/<branch> --stat
gh pr merge <N> --merge --delete-branch   # 仅 Master；G1–G6 通过且 SHA 一致
gh run list --limit 3                # 合并后 main 的验证运行
```

合并后验证通过 → 更新 Issue 登记评论（VERIFIED + 最终 SHA）→ 关闭 Issue。

### 本地验证（在运行 head 上必须真跑）

```bash
python3 scripts/validate_kit.py --project-ready           # 规则包（快；本机或 test-host 均可）
python3 -m unittest discover -s tests                     # 规则包单测（快；同上）

# 全量（在 test-host 上执行；需要 PostgreSQL 17；会重置 ajaimgr_test，勿与其他实例并行）：
export UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv
uv run pytest app/tests -q -o addopts=""
```

### 其他速查

```bash
gh issue comment <N> --body "..."     # 登记评论 / 更新
gh run view <id> --log-failed         # 失败日志
gh api repos/ctrlshiftaltdel/AgentMgr/rulesets   # 规则集
psql -d ajaimgr_test -c "\d <table>"  # 看表结构
```

---

## 11 坑与教训

1. **虚假派发**：登记写了 DISPATCHED 却没真正启动实例。核实方法：`delegate_task(action='list')`。**派发后必须核实。**
2. **实例中断**：模型响应超时/主机重启会吃掉执行轮次；派发包要紧凑；中断后先查 worktree 有无未提交改动，再决定保留或重置。
3. **验证脚本会往共享测试库写残留**，破坏后续 teardown；自己的临时脚本自己清（结束时复核残留=0）；演练库用完 drop。
4. **测试库角色/权限目录会被 pytest 清掉**：临时脚本建员工前先 `seed_permission_catalog(session)`。
5. **TMPDIR**：默认在 `~/.hermes/cache/scratch`；受约束场景显式改到仓库外且不在 `~/.hermes` 下。
6. **worktree 里 `uv run` 会生成 126 MB `.venv`**：显式 `export UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`；出现即删。
7. **管道吃退出码**：`cmd | head` 的 `$?` 是 `head` 的；要真退出码就重定向到文件再读。
8. **规则集语义**：head 一变，旧批准与旧 CI 证据失效；strict 模式要求 PR 与 main 同步后才能合并。
9. **并发上限 1 是资源约束**：路径不冲突也不要并跑两个执行实例；派发前先看有没有在跑的。
10. **`grep -vE '^dir/'` 之类排除式易错**；范围检查用 `git show --name-status` / `gh pr view --json files`。
11. **子代理可能"更新"既有测试**：逐字检查新断言强度，不能只看"已说明理由"（W1-23 对 `test_migration_0010_roundtrip` 的适配经逐行检查确认强度未降）。
12. **子代理会纠正上一实例的假账**：重试时要求逐条核实旧产物。
13. **离线版 `task add --acceptance` 曾是重复标志只留最后一条**；GitHub 版验收写进 Issue，多写 checklist 后要核对渲染完整。
14. **复核探针必须指向被测代码**：未合并模块在 main 里不存在（探针指向 worktree/PR 分支）。
15. **新增业务表要同步 conftest 的 `BUSINESS_TABLES`**（W1-23 已加入 `expense_invoices`）；`invoice_verifications` 仍未入列，注意清理责任。
16. **CI 里 pg_dump 必须与服务端同主版本**：runner 自带 16 会与 17 服务「server version mismatch」；PGDG 安装 client-17 并把 `/usr/lib/postgresql/17/bin` 注入 `$GITHUB_PATH`（pg_wrapper 不会自动切）。
17. **CI 里 socket DSN 连不上容器**：用 libpq 环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD）把 `postgresql+psycopg:///...` 与裸 `psql`/`dropdb` 指向服务容器。
18. **规则集生效后不能直推 main**：改 main 一律走分支 + PR（首个 PR 即本文档的合入 PR）。
19. **WorkBuddy 单会话编排（2026-10-16 起）**：派发子代理后必须核实 running（不得虚假派发）；子代理触发的沙箱授权需放行；**后台任务内跑 SSH 会被沙箱直接拦**（需前台 + escalation）。
20. **本机 git fetch 偶发网络抖动**：代理 502 / HTTP2 framing error——重试即可；push 遇同类错误用 `git -c http.version=HTTP/1.1 push`。
21. **陈旧锁文件**：`~/.gitconfig.lock` 残留会阻断 `gh auth setup-git`（确认无进程占用后先备份再清理；本轮已处理过一次）。
22. **临时库名跑全量会误伤 1 例**：`test_restore_refuses_protected_database` 期望目标库为受保护名（`ajaimgr_test`）；用自定义临时库名跑全量会出现 1 failed（环境差异，非缺陷）。全量请用标准库名或在报告中说明。
23. **子代理的「与改动无关」结论须独立核实**：用相关代码路径（如 `ops/restore.py::_guard_target` 的分支逻辑）与 CI 对照验证后才可采信（本轮已实践）。

---

## 12 开局检查清单（下一任）

- [ ] 读 `AGENTS.md`、`AGENT-ORCHESTRATION.md`、`MASTER.md`、`ORCHESTRATION-PROJECT.md`、`prompts/START-MASTER-EXISTING-PROJECT.md`
- [ ] 读本文档 §3、§4、§6、§9
- [ ] 跑 §10 的「状态恢复」命令并核对 §5 快照（head、CI 状态、开放 Issue/PR）
- [ ] 确认没有遗留运行中的子代理（WorkBuddy 任务列表 / `delegate_task(action='list')`）
- [ ] 开放 Issue 仅 #16（Track A，PLANNED）：派发前先确认优先级并写活动登记评论；其余任务从 §7（BLOCKED / 阶段 1 未开工）与所有者指示中选择
- [ ] 如向所有者汇报：BLOCKED 两项 + §9.2 决策清单
