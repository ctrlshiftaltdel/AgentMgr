# HANDOVER — 会话交接文档

- 生成时间：2026-10-18 15:45 CST（**第 4/5 轮收口：#32/#34/#35/#37/#39 均已合并并 VERIFIED；无活动锁、无开放 PR**）
- 主分支基线 head：`8c8c3d8`（PR #40 合并后；**本文档提交之前的 head**）
- **2026-10-18 历史重写说明**：为清除公开内容中的本机/个人标识，全部提交已重写；更早文档中记录的旧 SHA 已失效，PR / Issue 编号仍然有效。环境细节统一放在本地 `local_info.md`（不入库）。
- 上一版交接：`69d3f90`（PR #33，第 3 轮收口 #21/#22/#23）；更早 `3d9a193`（PR #25）、`aab3531`（PR #28，隐私 CI 落地）、`af1c27b`（多机口径）；完整历史见 git log
- 任务台账：**GitHub Issues**。开放 Issue：**#16**（Track A，后置）、**#41**（本次交接刷新）；#21/#22/#23 已 VERIFIED 关闭（PR #29/#30/#31），**第 4/5 轮的 #32/#34/#35/#37/#39 已 VERIFIED 关闭**（PR #33 / 无仓库变更 / #36 / #38 / #40）；#10/#12/#13/#17/#19/#24/#26/#27 此前已关闭。离线版 45 条台账已退役归档（不再更新）。
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

- 项目章程 `docs/PROJECT-CHARTER.md`（O3 已记录「百度为首选查验渠道」）；实施方案 `templates/PLAN.md`（v2.0）；部署要求 `docs/DEPLOYMENT-REQUIREMENTS.md`；调研 `docs/research/`（W0-1/2/4/5/7、W1-28 发票验真、W2 OCR/ASR 选型、W2-35 百度联调认知）
- **当前阶段**：Wave 1 主体 + 发票查验完整链条（渠道 Provider：腾讯云 W1-20、百度 W1-28；人工辅助录入 W1-22；报销单挂账 W1-23）+ **W1 冻结队列三项（W1-24 模型导入自足性、W1-16 迁移可复现性、W1-26 受理解析流水线接口与人工兜底）** 已完成；**2026-10-18 第 3 轮收口三项**：渠道消息入站与路由后端服务层（#21）、跨主机/异地恢复 Runbook 与 dry-run（#22）、OCR/ASR 供应商选型报告（#23）已完成。阶段 1 剩余均为**所有者阻塞**：真实微信适配与 Profile 验收（需授权）、OCR/ASR 真实接入（需拍板供应商并关闭报告 §9 的 11 项未验证）、真实异地恢复演练（需第二台主机/异地位）。**2026-10-16 完成规则层切换：离线版 → Agent-Orchestration GitHub 版**（仓库 `ctrlshiftaltdel/AgentMgr`，public）。**2026-10-17 财务对接方向变更**：金蝶接入取消，对外账务交接改为模板驱动 Excel 导出（模板待所有者提供），远期自研财务/ERP（章程 v1.1）；同日落库 AI 工具治理整合计划 v0.2（Track A/B/C/D，`docs/AgentMgr_AiNativeERP_Integration_Plan.md`）。

---

## 3 不可违反的硬约束

1. **严禁读取、复制或索要凭据值**（token/cookie/真实账号标识/口令）：仓库、日志、报告一律占位符。不要在聊天中接收凭据。
2. 不新建、不启动、不停止任何 Hermes Profile；不写入 `~/.hermes/` 下任何文件。
3. 不用 pip；统一 uv（在 PATH 中）；解释器由 `.python-version` 固定 3.13（生产同为 3.13）。
4. **不新增第三方依赖**，除非在任务 Issue 中声明理由与替代方案并获批准。当前 10 个直接依赖：fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2。
5. DSN 默认走本地 unix socket peer 认证；任何文件不得写入口令。
6. 不得为让测试通过而修改、删除或跳过既有断言（行为变化可修正并留证据）。
7. **`main` 受规则集保护**（`main-protection`，id 25216701）：必须经 PR 合入；required checks = `kit-validate` + `app-tests`（strict：分支须最新）；禁止 force push 与分支删除；**当前无 bypass**——直接 push main 会被拒绝，确需直推时由所有者调整规则集。
8. 子代理不得自批、自合并、自我验收；Master 不得虚假派发或把阻塞当完成。
9. **跨会话证据必须已推到远端**；本地提交、本地日志不算数。
10. 未被 Issue 列出的路径默认禁止；扩大范围须记录授权。受保护路径另见 `ORCHESTRATION-PROJECT.md`（含 `AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`.github/`、`scripts/`、`tests/`、`prompts/`、`templates/`、`HANDOVER.md`、`.orchestration/`、`.env*`）。
11. **不得提交可识别本机/个人的信息**（用户名、主机名/SSH 别名、绝对家目录、IP、代理地址、凭据）；环境细节只放本地 `local_info.md`（不入库），仓库内用 `<repo-root>`、`<test-host>` 等占位符。提交消息、Issue、PR 与评论同样适用；CI `privacy-scan` 必须通过。

---

## 4 环境事实

| 项 | 值 |
|---|---|
| 编排/开发机（多机：Windows / Linux / macOS） | 工作区路径各机自定（见本地 `local_info.md`，不入库）；`gh` 按需登录；`uv` 已装；编排机可不装 PostgreSQL（只开发）；`.workbuddy/` 与 `local_info.md` 已入各机 `.git/info/exclude` |
| 测试主机 | Debian 13 (trixie)，2 vCPU / 1.9 GiB（**仅编排与开发机**；生产 Profile 主机另配）；仓库、uv、`.venv` 与 PostgreSQL 17.11（`ajaimgr_dev` / `ajaimgr_test`，peer 认证；口令不入文件）齐备；具体路径与 SSH 别名见本地 `local_info.md`；全量测试与临时库实测在此执行 |
| GitHub CLI | 各编排机按需登录；remote `origin` = `ctrlshiftaltdel/AgentMgr`（public） |
| CI | GitHub Actions `ubuntu-latest`；`postgres:17` 服务容器（TCP+密码）；PG client 17 经 PGDG 安装并把 `/usr/lib/postgresql/17/bin` 注入 `$GITHUB_PATH`；libpq 环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD）供 socket DSN 的恢复演练使用 |
| 出网 | 默认直连 GitHub；如所在网络需要代理，按机器本地配置（见 `local_info.md`），不得写入仓库。直连偶发抖动时重试，或 `git -c http.version=HTTP/1.1 fetch` |
| venv 策略 | 测试主机所有 worktree 共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT` 指向该 `.venv`，实际路径见 `local_info.md`）；编排机 worktree 共享各自主工作树 `.venv`；提高并发上限时必须改回各自独立 |
| 并发上限 | **1**（派发前先确认没有子代理在跑） |

---

## 5 状态快照

- `main` head = `8c8c3d8`（PR #40 合并后；#21 → `7022f67`、#22 → `6046679`、#23 → `ec4b6c1`、#32 → `69d3f90`、#35 → `40b099f`、#37 → `fd4d197`、#39 → `8c8c3d8`；#34 无仓库变更）；工作区干净；无运行中子代理；**无活动锁、无开放 PR**；远端仅保留 `main`；测试主机已同步到 `8c8c3d8`
- 测试：**全量基线 840 passed**（2026-10-18，测试主机，`main` `8c8c3d8` 上实测 65.89s；构成：786 + #35 的 40 例 + #37 的 14 例）；本机 Fast（`validate_kit --project-ready` + `unittest discover -s tests` + `check_privacy --ci`）rc=0；PR #36 / #38 / #40 的 CI `app-tests` / `kit-validate` / `privacy-scan` 均对 head 通过，`main` push CI `8c8c3d8` 亦 success
- 迁移链 `0001_org_core` → `0002_identity_rbac` → `0003_permission_catalog`（**冻结快照**，W1-16）→ `0004_audit_events` → `0005_expense_core` → `0006_expense_claim_no_seq` → `0007_workflow_outbox` → `0008_intake_files` → `0009_perm_alignment` → `0010_invoice_verifications` → `0011_expense_invoices` → **`0012_inbound_messages`**（#21，可回滚）
- 领域目录 12 个：`app/{identity,intake,expense,workflow,documents,inventory,integrations,audit,api,db,core,runtime}`
- 规则集 `main-protection`（id 25216701）active；`python3 scripts/github_preflight.py` 通过
- 开放 Issue：**#16**（Track A，后置）、**#41**（本次交接刷新）；#21/#22/#23/#32/#34/#35/#37/#39 已 VERIFIED 关闭
- 离线归档：`.orchestration/`（不变；含 `archive/orchestrator.py`、`state-20261016-114915.sqlite3`、45 条任务台账）

---

## 6 当前操作点（队列已清空，等所有者输入）

**第 4/5 轮队列 #34 / #35 / #37 / #39 已全部合并并 VERIFIED（PR #36 / #38 / #40；#34 无仓库变更），#32 交接刷新亦已合并（PR #33）。** 当前无进行中任务、无活动锁（交接期间仅有 `.worktrees/41-handover`）、无开放 PR；并发上限 1 不变。

### 第 4/5 轮交付（详见 §8）

| 项 | 交付 | 结果 |
|---|---|---|
| #34 员工账号 + Profile（dev） | `ajaimgr_dev` 新增 `mlxzlas` / `tester` 两名员工（employee 角色）；Hermes Profile `emp-mlxzlas` / `emp-tester` 已生成（0700/0600） | **无仓库变更**，已 VERIFIED |
| #35 百度 OCR 适配器 | `app/integrations/baidu_invoice_ocr.py`（`multiple_invoice`；Client/Parser 分离、预检 fail closed、桩传输、40 例测试） | PR #36 → `40b099f` |
| #37 真实响应修正 | 置信度是 dict `{average,min}`、网约车票 snake_case、`result.<Field>` 单元素数组、凭据变量名（+14 例） | PR #38 → `fd4d197` |
| #39 认知落库与报告修正 | `docs/research/W2-35-baidu-multiple-invoice-ocr.md`、选型报告 §4.2 等修正、`.gitignore` 防凭据误提交 | PR #40 → `8c8c3d8` |

### 阶段 1 卡点（仍需所有者输入）

| 项 | 已交付 | 仍需所有者 |
|---|---|---|
| OCR/ASR 真实接入 | **供应商已拍板**：百度智能财务票据识别 `multiple_invoice`；适配器已合入 `main`，并在 `fd4d197` 上用真实票据**端到端 20/20 跑通**（证据见 `docs/research/W2-35-baidu-multiple-invoice-ocr.md` §8） | ①关闭合规项（DPA / PIPIA / 留存与是否用于训练）；②提供**带标注的**脱敏样本集做准确率验收（现有 20 份无真值，只证「跑通」不证「准确」）；③确认配额与计费 |
| 渠道消息入站与路由（后端侧） | #21：服务层 + 桩适配器 + 迁移 `0012` | 真实微信适配与 Profile 端到端验收的**授权**（`ORCHESTRATION-PROJECT.md` 禁止未经授权新建/启动 Profile） |
| 跨主机/异地恢复演练 | #22：Runbook + `ops/offsite_restore.py` dry-run（真实归档 preflight 6/6、rehearse 15/15） | **第二台主机或异地位**，用于真实跨主机演练与证据 |
| 导出模板与字段口径 | 模板文件已收到（`导入凭证模板_普通模板.xlsx`） | **结构解析受阻**：本机安全策略阻止直接读取该 xlsx（编辑器会话 `No workbook open`、直接解析被拒）→ 需所有者在编辑器中打开并给出 `file_id`，或直接贴表头列名，或提供无中文名副本 |
| 脱敏业务样例与流程盘点 | — | 脱敏后的真实 Excel/Word/PDF 样例、报销规则、库存流程 |

**可选下一批（所有者确认后才建 Issue）**：把 `BaiduInvoiceOcrParser` 注册进 `ParserRegistry` 并打通「解析 → 草稿 → 人工确认」端到端、一页多票拆分、导出映射（需模板结构）、真实异地演练。

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

| 项 | 需要什么 | 状态 |
|---|---|---|
| 导出模板与字段口径（未建 Issue） | 模板结构：工作表清单、表头行、列顺序、示例行、合并单元格 | 模板文件**已收到**（`导入凭证模板_普通模板.xlsx`），但**结构解析受阻**：本机安全策略阻止直接读取（编辑器会话 `No workbook open`、直接解析被拒）→ 需所有者给出 `file_id` / 直接贴表头 / 提供无中文名副本 |
| 脱敏业务样例与流程盘点（原 W0-6，未建 Issue） | 脱敏后的真实 Excel/Word/PDF 样例、报销规则、库存流程 | 仍阻塞（收集材料中） |
| 微信接入授权（未建 Issue） | 是否允许在指定主机创建 Profile 做真实路由与消息验收 | 仍阻塞；#34 已建 dev 员工账号与 Profile，但**渠道绑定与真实验收未授权** |
| 第二台主机或异地位（未建 Issue） | 用于跨主机/异地恢复的真实演练与证据 | 仍阻塞（收集材料中） |

### 待派发（PLANNED）

| Issue | 内容 | 状态 / 注意 |
|---|---|---|
| #16 | Track A：AiNativeERP 设计映射与 Track B 范围（仅文档） | PLANNED，后置：阶段 1 收口后再评估 |
| #41 | HANDOVER 刷新（第 4/5 轮收口） | 本次交接 PR；合并后关闭 |

（#21/#22/#23 与第 4/5 轮的 #32/#34/#35/#37/#39 均已合并并 VERIFIED，见 §8。）

### 冻结队列——已于 2026-10-16 全部完成

（#1 W1-24、#2 W1-16、#3 W1-26 已合并并 VERIFIED，Issue 已关闭；交付见 §8。）

### 阶段 1 剩余项（全部为所有者阻塞）

- **微信真实适配与 Profile 端到端验收**：#21 已交付服务层 + 桩适配器（迁移 `0012`）；真实适配需所有者授权建 Profile（依赖 W0-1 结论）。
- **OCR/ASR 真实接入**：**供应商已拍板百度 `multiple_invoice`**（见 §9.3，报告 §0.1 的腾讯云主 / 阿里云备**不再作为执行依据**）；#35 / #37 已交付适配器，并在 `fd4d197` 上用真实票据**端到端 20/20 跑通**，认知已落库 `docs/research/W2-35-baidu-multiple-invoice-ocr.md`。剩余阻塞：合规条款（DPA / PIPIA / 留存与是否用于训练）、**带标注**的脱敏样本集做准确率验收、配额与计费确认；之后可开「注册进 `ParserRegistry` + 解析→草稿→人工确认端到端」的实现 Issue。
- **真实跨主机/异地恢复演练**：#22 已交付 Runbook + dry-run（真实归档 preflight 6/6、rehearse 15/15）；真实执行需第二台主机或异地位。`--allow-missing-*` 误用边界见 `docs/BACKUP-RESTORE.md` §10 第 9 项。

---

## 8 已完成（不要重做）

- **第 4/5 轮队列（2026-10-18 下午；PR #36 / #38 / #40，#34 无仓库变更）**：
  - **#34 员工账号与 Profile（dev，无仓库变更）**：`ajaimgr_dev` 新增 employees id=3 `mlxzlas`、id=4 `tester`（均 `employee` 角色，最小权限）；Hermes Profile `emp-mlxzlas` / `emp-tester` 已生成（目录 0700、配置 0600），由现有 multiplexed gateway 托管（**未单独启动**）；生产布局的 Profile 计划包在测试主机上留存供参考。**未做**：渠道绑定（缺微信 `external_user_id`）、生产 systemd 账户。
  - **#35 百度 `multiple_invoice` 适配器（PR #36，merge `40b099f`）**：`app/integrations/baidu_invoice_ocr.py` —— `BaiduInvoiceOcrClient`（token 缓存、提前 60 秒过期、`110/111` 刷新一次后重试）+ `BaiduInvoiceOcrParser`（`ExternalParser` 实现，注入 `SourceReader`，预检 fail closed，输出只进草稿）；40 例全桩测试。**未接真实服务、未加依赖、未注册进 `ParserRegistry`。**
  - **#37 按真实响应修正（PR #38，merge `fd4d197`）**：①字段置信度实为 **dict** `{average, min}`（原按标量解析会全量静默回退 0.5）→ 改为优先取 `min`、回退 `average`、再回退 0.5；②`taxi_online_ticket` 用 **snake_case**（`total_fare` / `application_date` / `service_provider`），不能复用 `taxi_receipt` 的 PascalCase 别名；③`result.<Field>` 是**单元素数组**，需解包再取 `word`；④凭据名以 `BDOCR_API_KEY` / `BDOCR_SECRET_KEY` 为准（旧名仅作回退）。新增 14 例测试。
  - **#39 认知落库与报告修正（PR #40，merge `8c8c3d8`）**：新增 `docs/research/W2-35-baidu-multiple-invoice-ocr.md`（接口事实、三处偏差、字段与金额口径、票种能力矩阵、置信度真实分布、预检清单、隐私边界、未验证项）；修正 `docs/research/W2-ocr-asr-vendor-selection.md`（§4.2「百度无逐字段置信度」**已被真实响应推翻**、§0/§0.1/§0.3 标注百度已拍板为主用、§9 第 14 项部分关闭）；`.gitignore` 增加 `.env` / `.env.*`（保留 `!.env.example`）。
  - **真实联调证据（`fd4d197`，聚合口径）**：20 份真实票据（14 PDF + 6 OFD）经 `BaiduInvoiceOcrParser` **20/20 成功**；票种 `vat_invoice` 14 / `taxi_online_ticket` 6；每票字段数 min 4（网约车只有票种/金额/日期/销方）／中位 10（VAT 全字段）；164 个归一化字段、144 个非票种置信度 min 0.29／中位 0.912／max 0.973，其中 28 个（19.4%）低于默认阈值 0.75；契约不变量（键集合一致、值域、金额两位小数、日期 ISO）**0 违规**。**这是「跑通」证据，不是准确率证据**（样本无标注真值）。
- **第 3 轮队列（2026-10-18，PR #29 / #30 / #31）**：
  - **#21 渠道消息入站与路由（PR #29，merge `7022f67`）**：`app/intake/inbound.py`（`ingest_inbound_message`、`InboundChannelAdapter` 协议、`IngestResult`）、`InboundMessage` 模型 + 迁移 `0012`（唯一键 `(channel_type, external_message_id)`）、`app/tests/intake/test_inbound_ingest.py` 30 例。语义：结构校验 → 身份解析 → profile 名计算 → 幂等去重 → 建作业 + 锚点 + 受理审计（同事务）；拒绝不落库；并发冲突由唯一约束兜底；审计只记加盐哈希。**无 HTTP 端点、未接真实微信、未建 Profile。**
  - **#22 跨主机/异地恢复（PR #30，merge `6046679`）**：`ops/offsite_restore.py`（`preflight` / `rehearse`，无网络、无 DB 恢复）、`docs/BACKUP-RESTORE.md` §11、`app/tests/ops/test_offsite_restore.py` 26 例。测试主机真实归档：`preflight` 6/6 PASS、`rehearse` 15/15 PASS、证据 `database_step=NOT_EXECUTED`。**未执行真实跨主机/异地演练。**
  - **#23 OCR/ASR 选型（PR #31，merge `ec4b6c1`）**：`docs/research/W2-ocr-asr-vendor-selection.md`（678 行；百度/腾讯/阿里，24 个官方来源、45 处「未验证」）；推荐腾讯云主 / 阿里云备；指出 `ParseRequest` 缺少源文件字节的契约缺口与 `SourceReader` 注入方案。**未接真实服务、未加依赖。**
- **财务对接方向变更与文档落库（2026-10-17，4 个 PR）**：
  - PR #11：章程升 v1.1（取消金蝶、模板驱动导出、远期自研财务/ERP）+ 整合计划 v0.2（Track A/B/C/D、契约字段、B-1/B-2 决策块、Track D 导出机制）。
  - PR #14：`README.md` / `ORCHESTRATION-PROJECT.md` / `HANDOVER.md` 定位对齐，BLOCKED 队列移除金蝶、改为导出模板。
  - PR #15：`templates/PLAN.md` 升 v2.1，阶段 3/4 改为「财务导出与对账」与「财务能力扩展（远期自研）」；§8 新增 `export_templates` / `export_runs`。
  - PR #20：B-1/B-2 决策落库（章程 D8；整合计划 §5.6/§5.7 改「已决策」）、阶段 1 优先。
  - 四个 Issue #10 / #12 / #13 / #19 均已 VERIFIED 并关闭；每批均通过 `kit-validate` + `app-tests`。**注：旧 PR 的 commit SHA 因 2026-10-18 历史重写失效，PR 编号仍有效。**

- **第 3 轮（2026-10-18）**：队列 #21/#22/#23 已全部合并并 VERIFIED（见本节首条）；此前 #17 HANDOVER 刷新（PR #18）与 #24 交接刷新（PR #25）已完成。

- 离线台账时期 **38 条 VERIFIED**：准备 8（骨架/章程/配置/规则包/环境等）、调研 5（W0-1/2/4/5/7）、Wave 1 主体（后端底座与迁移、身份权限、报销域与状态机、Outbox、受理与文件、导出、API、管理后台、权限目录、负面测试与修复、渠道身份、发票查验 Provider 与腾讯云/百度适配器、人工辅助录入、报销单挂账、Profile 生成器与校验器、备份与恢复工具及修复轮）。完整清单与事件流见 `.orchestration/state.sqlite3`（可用归档的 orchestrator.py 查阅）。
- **规则层切换（2026-10-16）**：Agent-Orchestration GitHub 版规则文件替换（AGENTS/MASTER/AGENT-ORCHESTRATION/ACCEPTANCE/prompts/scripts/tests/VERSION/LICENSE）、`.github/` 模板与 CI 工作流、`ORCHESTRATION-PROJECT.md` 按项目填写、README 合并、离线专属文件退役、CI 修复至全绿、主分支规则集启用、首批 Issue #1–#3 登记。
- **W1 冻结队列三项（2026-10-16，WorkBuddy 单会话编排；详见各 Issue 与 PR；commit SHA 因历史重写失效，以 PR 编号为准）**：
  - W1-24 模型模块导入自足性（PR #5）：`integrations` / `intake` / `expense` 三模块补 `app.documents.models` 显式导入；新增逐模块子进程测试 `app/tests/test_model_imports.py`（修复前 3/9 失败 → 修复后 9/9；全量 DDL 零变化）。
  - W1-16 迁移可复现性（PR #6）：`0003` 改**冻结快照**（7/19/31，不再读活动代码）；新文档 `docs/MIGRATION-REPRODUCIBILITY.md`；实测中间态 `7|19|31` → head `7|20|32`、往返 rc=0；`0009` 仍读活动代码为顺延项。
  - W1-26 受理解析流水线（PR #7）：`app/intake/parsing.py`（`IntakeParser` / `ManualEntryParser` / `ExternalParser` / `ParserRegistry` + 裁决与脱敏）+ `app/intake/pipeline.py`（草稿写入与确认追溯；**无结构变更**）；新文档 `docs/INTAKE-PARSING-PIPELINE.md`；纯逻辑 36 + 落库 8 测试全桩。

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
- 多机约定：编排机可不装 psql（只开发）；一切 DB 测试/实测走测试主机（见 §4 与本地 `local_info.md`）。
- Outbox 用应用侧时钟；W1-8 只有 service 原语、无常驻 Worker。
- API 身份仍是 `X-Employee-Id` 过渡方案；管理后台登录=内部令牌（生产必须接 SSO，已写 docstring）。
- 微信通道「有条件 Go」，11 项待实测。
- `expense_invoices`（W1-23）：`suspected_duplicate` 为授权新增列；`attach_invoice` 不校验 `source_file_id` 存在性（FK 兜底）；同单并发重复以 IntegrityError 失败（可重试）；**尚无 HTTP 端点**。
- `invoice_verifications` 不在 conftest TRUNCATE 清单（模块自清理；套件后残留实测 0）。
- 百度为首选查验渠道（腾讯云/聚美智数备用；无渠道选择开关）；凭据 env-only（`AJAIMGR_BAIDU_API_KEY`/`AJAIMGR_BAIDU_SECRET_KEY`，缺失即 fail closed）。
- **CI 环境差异**：`test_profile_provisioning` 有一例依赖本机 `~/.hermes`，CI 上合法 skip；CI 中 `pg_dump` 必须与服务同主版本（已处理）。**测试不得依赖真实 `~/.hermes`**（#21 修复：以 `tmp_path` 隔离 HOME）。
- `inbound_messages`（#21）：**拒绝不落库**——若为拒绝写幂等锚点，会让「绑定建立前的投递」永久占位、之后无法受理；可追溯性由 `channel_routing` 的拒绝审计承担。同一消息 ID 换发送者/换归属 → `rejected(route_conflict)`。并发去重靠唯一约束 + `IntegrityError` 回滚回查，**不是**先查后插。`intake_job_id NOT NULL`（因此没有 `rejected` 行，`status` CHECK 的 `rejected` 值为将来预留）。**尚无 HTTP 端点。**
- `ops/offsite_restore.py`（#22）：**不建立任何网络连接**（`--target-host` 只是取证标签）；`rehearse` 只做传输模拟 + 附件/任务库落盘校验，**数据库恢复步骤不执行**；`--allow-missing-*` 传入即拒绝；`--target-dir` 必须为空，归档须放独立 `<staging-dir>`（文档 §11）。证据文件禁止放仓库内。
- OCR/ASR 选型（#23）：推荐腾讯云主 / 阿里云备，**不是实测最优**；报告 §9 的 11 项「未验证」未关闭前不得宣称「选型完成」。已识别的契约缺口：`ParseRequest` 只有 `source_file_id`，`ExternalParser._parse` 拿不到文件字节，需 `SourceReader` 注入（报告 §6.2）。
- 规则集当前**无 bypass**：所有者如需直接 push main 可自加 bypass actor（并接受绕过 PR/CI 的风险）。
- 仓库 **public**（所有者决定）；历史作者均为占位身份。

### 9.2 所有者决策清单（未决）

1. 导出模板：首个 Excel 模板与字段口径（阶段 3 前置）；金蝶接入已于 2026-10-17 取消
2. 脱敏业务样例与现有流程
3. ~~OCR/ASR 供应商~~ —— **已决（2026-10-18 所有者拍板：百度智能财务票据识别 `multiple_invoice`）**，见 §9.3；余下的是合规与验收（DPA / PIPIA / 留存、带标注样本集、配额与计费），不是选型
4. 微信接入授权（是否允许本机建 Profile 做真实路由验收）
5. 单条越权 403 vs 404
6. `description` 长度 1–500 字符（PLAN 未规定的引入默认值）
7. 生产 Profile 主机规格（需真实负载测试）
8. 百度查验正式开通（账号/费用/合规确认；原 W0-4 §7 项）
9. 生产备份必须存放于生产主机之外（章程成功标准未满足）
### 9.3 已决策（2026-10-17 所有者）

- B-1 工具选择：Agent 侧选（Hermes 经 MCP / function calling），后端只做契约校验、权限与澄清；不建后端意图路由（章程 D8、整合计划 §5.6）。
- B-2 确认持久化：新增 `tool_confirmations` 轻表；参数 hash + TTL 15 分钟 + 一次性消费 + 执行前复检（整合计划 §5.7）；建表放在 Track B 的确认类 Issue。
- 优先级：先收口阶段 1 剩余项（渠道消息路由、OCR/ASR 真实供应商、跨主机恢复演练），Track A 后置。
- **（2026-10-18 所有者补充）**
  - **OCR 供应商：百度智能财务票据识别 `multiple_invoice`**（替代 #23 报告推荐的腾讯云主 / 阿里云备；该排序保留为过程记录，**不再作为执行依据**）。适配器已合入 `main` 并真实联调 20/20。
  - **凭据变量名**：`BDOCR_API_KEY` / `BDOCR_SECRET_KEY`（旧名 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY` 仅作回退）；只从环境变量读取，缺一即构造失败（fail closed）。
  - **置信度口径**：字段级取 `probability.min`（缺则 `average`，再缺回退 `0.5`）；整票 `probability` **只允许**用于 `invoice_type` 这一个字段。默认阈值 0.75 不变，低于阈值即人工复核——**阈值以下不是故障**。
  - **金额口径**：报销金额只取 `AmountInFiguers`（价税合计）；`TotalAmount` 是不含税合计，**取错会系统性少报**。
  - **凭据文件必须被 Git 忽略**：`.env` / `.env.*` 已入 `.gitignore`（保留 `!.env.example`），各机另有 `.git/info/exclude` 兜底。**任何凭据值不得进入仓库、日志、Issue 与对话。**
  - **员工账号（dev）**：`mlxzlas`（所有者本人）、`tester`，均为 `employee` 角色（最小权限）；Hermes Profile 为 `emp-mlxzlas` / `emp-tester`。
10. GitHub 协作策略微调（直推 bypass？合并方式？——当前：PR + G1–G6 + strict checks）
11. 解析流水线产品参数确认（关键字段口径、置信度阈值——当前均为保守默认）

---

## 10 操作手册（命令级）

### 多机注记（2026-10-18 起）

- 编排（gh / git / Issues / PR）：在任一编排机（Windows / Linux / macOS）执行；工作区路径见本地 `local_info.md`（不入库）。
- 全量测试、临时库实测、迁移往返：`ssh <test-host>`（**占位名**：真实别名以本机 `~/.ssh/config` 的 `Host` 条目为准，连接参数见本地 `local_info.md`；后台任务内跑 SSH 可能被沙箱拦截，需前台执行；`git fetch` 可能挂在网络上，见 §11 第 33 条）。

### 状态恢复（每次接手先跑）

```bash
cd <repo-root>        # 各机路径见本地 local_info.md
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
python3 scripts/validate_kit.py --project-ready           # 规则包（快；编排机或测试主机均可）
python3 -m unittest discover -s tests                     # 规则包单测（快；同上）
python3 scripts/check_privacy.py --ci                     # 隐私/凭据扫描（文件 + 提交消息 + 历史）

# 全量（在测试主机上执行；需要 PostgreSQL 17；会重置 ajaimgr_test，勿与其他实例并行）：
export UV_PROJECT_ENVIRONMENT=<test-host-venv>   # 实际路径见 local_info.md
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
6. **worktree 里 `uv run` 会生成 126 MB `.venv`**：显式 `export UV_PROJECT_ENVIRONMENT=<test-host-venv>`（见 `local_info.md`）；出现即删。
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
24. **同一 worktree 并发跑 pytest 会互相争用**：执行实例重复启动多个 pytest 时，表现为「某个用例挂起」而非报错（#22 实测：4 个进程互抢，单进程复跑 26 例全绿）。派发提示应写明「同一时刻只跑一个测试进程」；卡住时先 `Get-CimInstance Win32_Process` 查进程再重跑。
25. **文档里的命令必须按文档原样跑一遍才算验收**：#22 的 Runbook 曾把归档放进 `--target-dir` 再做 preflight，而工具要求该目录为空——照文档执行第一步即 rc=2。复核文档类交付时，至少逐条执行关键命令。
26. **测试不得依赖真实 `~/.hermes`**：#21 的 `_hermes_snapshot()` 比对整棵真实目录树，在测试主机上因并发写入偶发失败（5 次中 1 次）；改为 `tmp_path` + `HOME`/`USERPROFILE` 隔离后稳定。
27. **`gh issue view --json comments` 返回的是 node id**（`IC_kw...`），更新评论须用 REST 接口的数字 `id`（`gh api repos/.../issues/<n>/comments --jq '.[].id'`）。
28. **真实响应才是事实来源，文档与选型报告会错**：#35 严格照官方文档与选型报告实现，真实一跑就暴露三处偏差——字段置信度是 **dict** 不是标量（原实现全部静默回退 0.5）、`taxi_online_ticket` 用 **snake_case**、`result.<Field>` 是**单元素数组**。凡「按文档推断的响应形态」，首次真实调用后必须逐项回验，并把结论写进文档（见 `docs/research/W2-35-baidu-multiple-invoice-ocr.md` §2）。
29. **「4M」这类上限要取十进制下界**：文档只写「4M」不写进制，4 MiB（4,194,304）会越过服务端限制。实现取 `4_000_000`，宁可本地早拒（#35 复核时修正）。
30. **凭据文件必须入库忽略**：`.env` 曾未被 `.gitignore` 覆盖，`git status` 显示未跟踪项——一次 `git add -A` 就会把真实凭据提交到 public 仓库。各机 `.git/info/exclude` 只是止血，**仓库里必须有规则**（#39 已补）。
31. **SSH 别名以本机 `~/.ssh/config` 为准**：交接文档里的 `test-host` 是**占位符**，按它直接连接会 `Could not resolve hostname`；先 `grep '^Host' ~/.ssh/config` 取真实别名。
32. **本地敏感文件可能被安全策略阻断读取**：所有者提供的 xlsx（模板/样例）直接解析会 `PermissionError`，表格子代理也可能报 `No workbook open`。改走「所有者在编辑器打开并给 `file_id` / 直接贴表头列名 / 提供无中文名副本」；**被拒后不要反复重试等价命令**。
33. **测试主机的 `git fetch` 会挂在网络上**：表现为后台任务十几分钟无输出、`git-remote-http` 常驻、pytest 根本没启动。跑全量前若仓库已同步就跳过 pull；必要时用 `timeout` 包裹 fetch 并加 `git -c http.version=HTTP/1.1`。

---

## 12 开局检查清单（下一任）

- [ ] 读 `AGENTS.md`、`AGENT-ORCHESTRATION.md`、`MASTER.md`、`ORCHESTRATION-PROJECT.md`、`prompts/START-MASTER-EXISTING-PROJECT.md`
- [ ] 读本文档 §3、§4、§6、§9
- [ ] 跑 §10 的「状态恢复」命令并核对 §5 快照（head、CI 状态、开放 Issue/PR）
- [ ] 确认没有遗留运行中的子代理（WorkBuddy 任务列表 / `delegate_task(action='list')`）
- [ ] 队列已清空：**不要**重做 #21/#22/#23/#32/#34/#35/#37/#39；下一步等所有者输入（§6 的卡点表）
- [ ] 若所有者给出输入：按 §6「标准循环」建 Issue → 登记 → 派发；#16 仍后置
- [ ] 如向所有者汇报：§6 卡点表 + §7 BLOCKED + §9.2 决策清单；**OCR 供应商已拍板**，重点催的是模板结构与脱敏/标注样例
