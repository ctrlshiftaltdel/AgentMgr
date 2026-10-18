# HANDOVER — 会话交接文档

- 生成时间：2026-10-18 晚 CST（**#43–#68 全量交付 + 规则层更新（#63）；无活动锁、无开放 PR**）
- 主分支基线 head：`ed0c324`（PR #68 合并后；**本文档提交之前的 head**）
- **2026-10-18 历史重写说明**：为清除公开内容中的本机/个人标识，全部提交已重写；更早文档中记录的旧 SHA 已失效，PR / Issue 编号仍然有效。环境细节统一放在本地 `local_info.md`（不入库）。
- 上一版交接：`50292de`（PR #42，第 4/5 轮收口 #32/#34/#35/#37/#39）；更早 `69d3f90`（PR #33，第 3 轮收口 #21/#22/#23）；再早的交接刷新见 PR #25 / #18 / #9——**其旧 merge SHA 已被 2026-10-18 历史重写作废，本地历史中不可达，故只引 PR 编号**；完整历史见 git log
- 任务台账：**GitHub Issues**。开放 Issue：**#16**（Track A，后置）、**#69**（本次交接刷新）。**本轮 Issue #43 / #44 / #45 / #46 / #49 / #52 / #53 / #56 / #57 / #58 / #59 / #60 / #62 / #63 全部已 VERIFIED 并关闭**（#57 / #62 为运维执行、**无仓库变更**）；#10–#13、#17–#27、#29–#41 此前已关闭。离线版 45 条台账已退役归档（不再更新）。
- 你的角色：**Master Agent** —— 计划、派发、独立复核、**唯一合并者**、阶段收口；默认不实现产品代码。
- 接手顺序：`AGENTS.md` → `AGENT-ORCHESTRATION.md` → `MASTER.md` → `ORCHESTRATION-PROJECT.md` → 本文档 → `prompts/START-MASTER-EXISTING-PROJECT.md`。

---

## 0 怎么用这份文档

1. §1–§5 是必读背景（角色、约束、环境、状态）。
2. **§6 是恢复点**：队列已清空（仅 #16 后置），等所有者示意；恢复后按 GitHub 流程（Issue → 活动登记评论 → 分支/PR → G1–G6 → 合并 → 合并后验证 → VERIFIED → 关 Issue）。
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

面向中国大陆 20–100 人（后续扩到 100）企业的 Hermes Agent 原生管理系统，**首期模块：报销**。以 AI 对话/语音/图片/文件为主要工作入口；**不接入金蝶等特定第三方财务系统**，对外账务交接采用模板驱动的 Excel 导出（远期自研财务/ERP）。

- 项目章程 `docs/PROJECT-CHARTER.md`（O3 已记录「百度为首选查验渠道」）；实施方案 `templates/PLAN.md`（v2.0）；部署要求 `docs/DEPLOYMENT-REQUIREMENTS.md`；调研 `docs/research/`（W0-1/2/4/5/7、W1-28 发票验真、W2 OCR/ASR 选型、W2-35 百度 `multiple_invoice` 认知、W2-36 准确率验收、W2-37 端点对比）；Track D 映射设计 `docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`。
- **当前阶段**：Wave 1 主体 + 发票查验完整链条已完成。**2026-10-18 全天完成 #43–#68 共 12 个交付 PR + 2 个无仓库变更的运维任务（#57 真实跑批 / #62 查验刷新）**：
  - **Track D 导出**：#43（模板结构落库与映射设计）→ #53（导出机制：单文件 TOML 配置、模板装载 SHA-256、`录入数据` 31 列映射、12 条 fail-closed 校验、manifest、存储与审计）→ **#57 首版真实导出**（EC-00000001 达 `paid` 后导出 xlsx + manifest，交付目录在仓库外）。
  - **受理全链**：#45（百度 OCR 解析器装配点 + 生产级 `SourceReader`）→ #56（确认草稿 → 正式报销单草稿 + 明细行 + 发票挂账写入路径）。
  - **产品化端点**：#60（`POST /documents` 上传原件；`POST|GET /claims/{id}/invoices` 挂账/列表）。
  - **OCR 端点路由**：#52（`[ocr].routing = smart | routed`，默认 `smart`；`routed` 新增 `vat_invoice` / `online_taxi_itinerary` 专用适配器）。
  - **缺陷修复**：#49（空容器字段按无值处理，消除数电票 `invoice_code` 脏值）、#58（百度查验响应层级——结论字段顶层优先）、#59（存储层按容器结构识别 OFD）。
  - **验收**：#44（百度 `multiple_invoice` 字段级准确率验收，8 张带标注真实票据）、#46（专用端点对比与采购建议）。
  - **规则层**：#63（并发上限 4 / 按负载动态；全量测试 `flock` 串行；venv 并行策略改 `uv run --no-sync`）。
- 阶段 1 剩余均为**所有者阻塞**：真实微信适配与 Profile 验收（需授权）、OCR 合规（DPA / PIPIA / 留存）、真实异地恢复演练（需第二台主机/异地位）、会计科目生产替换（导出科目代码仍为占位）。
- **2026-10-16 完成规则层切换：离线版 → Agent-Orchestration GitHub 版**（仓库 `ctrlshiftaltdel/AgentMgr`，public）。**2026-10-17 财务对接方向变更**：金蝶接入取消，对外账务交接改为模板驱动 Excel 导出，远期自研财务/ERP（章程 v1.1）；同日落库 AI 工具治理整合计划 v0.2（Track A/B/C/D，`docs/AgentMgr_AiNativeERP_Integration_Plan.md`）。

---

## 3 不可违反的硬约束

1. **严禁读取、复制或索要凭据值**（token/cookie/真实账号标识/口令）：仓库、日志、报告一律占位符。不要在聊天中接收凭据。
2. 不新建、不启动、不停止任何 Hermes Profile；不写入 `~/.hermes/` 下任何文件。
3. 不用 pip；统一 uv（在 PATH 中）；解释器由 `.python-version` 固定 3.13（生产同为 3.13）。
4. **不新增第三方依赖**，除非在任务 Issue 中声明理由与替代方案并获批准。当前 10 个直接依赖：fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2。
5. DSN 默认走本地 unix socket peer 认证；任何文件不得写入口令。
6. 不得为让测试通过而修改、删除或跳过既有断言（行为变化可修正并留证据）。
7. **`main` 受规则集保护**（`main-protection`，id 25216701）：必须经 PR 合入；required checks = `kit-validate` + `app-tests` + `privacy-scan`（strict：分支须最新）；禁止 force push 与分支删除；**当前无 bypass**——直接 push main 会被拒绝，确需直推时由所有者调整规则集。
8. 子代理不得自批、自合并、自我验收；Master 不得虚假派发或把阻塞当完成。
9. **跨会话证据必须已推到远端**；本地提交、本地日志不算数。
10. 未被 Issue 列出的路径默认禁止；扩大范围须记录授权。受保护路径另见 `ORCHESTRATION-PROJECT.md`（含 `AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`.github/`、`scripts/`、`tests/`、`prompts/`、`templates/`、`HANDOVER.md`、`.orchestration/`、`.env*`）。
11. **不得提交可识别本机/个人的信息**（用户名、主机名/SSH 别名、绝对家目录、IP、代理地址、凭据）；环境细节只放本地 `local_info.md`（不入库），仓库内用 `<repo-root>`、`<test-host>` 等占位符。提交消息、Issue、PR 与评论同样适用；CI `privacy-scan` 必须通过。

---

## 4 环境事实

| 项 | 值 |
|---|---|
| 编排/开发机（多机：Windows / Linux / macOS） | 本会话 Linux 机：**4 vCPU / 7.7 GiB**（#63 依据：资源已升级，原「2 vCPU / 1.9 GiB」瓶颈不再成立）；工作区路径各机自定（见本地 `local_info.md`，不入库）；`gh` 按需登录；`uv` 已装；编排机可不装 PostgreSQL（只开发）；`.workbuddy/` 与 `local_info.md` 已入各机 `.git/info/exclude` |
| 测试/执行主机 `<test-host>` | Debian 13 (trixie)，2 vCPU / 1.9 GiB；仓库、uv、`.venv` 与 PostgreSQL 17.11（`ajaimgr_dev` / `ajaimgr_test`，peer 认证；口令不入文件）齐备；具体路径与 SSH 别名见本地 `local_info.md`；**全量测试与临时库实测在此执行** |
| GitHub CLI | 各编排机按需登录；remote `origin` = `ctrlshiftaltdel/AgentMgr`（public） |
| CI | GitHub Actions `ubuntu-latest`；`postgres:17` 服务容器（TCP+密码）；PG client 17 经 PGDG 安装并把 `/usr/lib/postgresql/17/bin` 注入 `$GITHUB_PATH`；libpq 环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD）供 socket DSN 的恢复演练使用 |
| 出网 | 默认直连 GitHub；如所在网络需要代理，按机器本地配置（见 `local_info.md`），不得写入仓库。直连偶发抖动时重试，或 `git -c http.version=HTTP/1.1 fetch` |
| venv 策略 | 测试主机所有 worktree 共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT` 指向该 `.venv`，实际路径见 `local_info.md`）；编排机 worktree 共享各自主工作树 `.venv`。**并行执行必须用 `uv run --no-sync`（禁止并发 `uv sync` 就地改写环境）；任何会改动 `pyproject.toml` / `uv.lock` 的任务独占派发**（#63 / `DEPLOYMENT-REQUIREMENTS.md` §4） |
| 并发上限 | **4 或按负载动态**（CPU / 内存有空余即可派发下一个执行实例；#63）。硬前提：**(a)** 同批次各任务允许路径集合两两不重叠（含文档文件）；**(b)** 派发前完成活动登记与依赖检查；**(c)** 全量测试经 `flock` 串行 |
| 全量测试串行 | 测试主机全量 pytest 会重置共享 `ajaimgr_test`，**必须**经 `flock /tmp/ajaimgr-fulltest.lock` 串行排队（#63）。多个执行实例可排队，不得并发裸跑 |

---

## 5 状态快照

- `main` head = `ed0c324`（PR #68 合并后）；工作区干净；无运行中子代理；**无活动锁、无开放 PR**；远端仅保留 `main`；测试主机已同步到 `ed0c324`（`git status` 干净）
- 本轮 PR 合并链（merge commit）：#47→`8b063df`、#48→`a6adf34`、#50→`fc5ed04`、#51→`11e3e2a`、#54→`1e91e9f`、#55→`64f8a68`、#61→`8e44434`、#64→`b733980`、#65→`ff00502`、#66→`f15d25f`、#67→`76b7a4e`、#68→`ed0c324`
- 测试：**全量基线 1249 passed**（2026-10-18 晚，测试主机 `main` `ed0c324` 上**本任务实测**：`1249 passed, 1 warning, 7 subtests passed in 74.77s`，rc=0；唯一 warning 为既有 `StarletteDeprecationWarning`；与 Master 在 #52 合并后的独立实测值一致）。本机 Fast（`validate_kit --project-ready` + `unittest discover -s tests` + `check_privacy --ci`）rc=0
- CI：`ed0c324` 上 `kit-validate` / `app-tests` / `privacy-scan` 三个 check 的 head 均为 `ed0c324`、conclusion=success；`main` push CI（run 39139172604）success
- 迁移链 `0001_org_core` → `0002_identity_rbac` → `0003_permission_catalog`（**冻结快照**，W1-16）→ `0004_audit_events` → `0005_expense_core` → `0006_expense_claim_no_seq` → `0007_workflow_outbox` → `0008_intake_files` → `0009_perm_alignment` → `0010_invoice_verifications` → `0011_expense_invoices` → **`0012_inbound_messages`**（本轮 #43–#68 **全部无迁移**）
- 领域目录 **13** 个：`app/{identity,intake,expense,workflow,documents,inventory,integrations,audit,api,db,core,runtime,export}`（`export` 为 #53 新增）
- 规则集 `main-protection`（id 25216701）active；`python3 scripts/github_preflight.py` 通过（read-only）
- 开放 Issue：**#16**（Track A，后置）、**#69**（本次交接刷新）；#43–#68 已 VERIFIED 关闭
- 离线归档：`.orchestration/`（不变；含 `archive/orchestrator.py`、`state-20261016-114915.sqlite3`、45 条任务台账）

---

## 6 当前操作点（队列已清空，等所有者输入）

**#43–#68 已全部合并并 VERIFIED（PR #47/#48/#50/#51/#54/#55/#61/#64/#65/#66/#67/#68；#57 / #62 无仓库变更）。** 当前无进行中任务、无活动锁（交接期间仅有 `.worktrees/69-handover`）、无开放 PR；并发上限 **4 / 按负载动态**。

### 本轮交付一览（详见 §8）

| 主题 | Issue | 交付 | 结果 |
|---|---|---|---|
| Track D 导出 | #43 / #53 / #57 | 模板结构落库与映射设计 → 导出机制（`app/export/`、单文件 TOML、31 列映射、12 条 fail-closed、manifest） → 首版真实导出（EC-00000001 `paid`） | PR #47 → `8b063df`；PR #55 → `64f8a68`；#57 无仓库变更 |
| 受理全链 | #45 / #56 | 百度 OCR 装配点 + 生产级 `SourceReader` → 确认草稿 → 报销单草稿 + 明细 + 挂账写入路径 | PR #51 → `11e3e2a`；PR #66 → `f15d25f` |
| 产品化端点 | #60 | `POST /documents`；`POST|GET /claims/{id}/invoices` | PR #67 → `76b7a4e` |
| OCR 端点路由 | #52 | `[ocr].routing = smart|routed`（默认 `smart`）+ 两个专用端点适配器 | PR #68 → `ed0c324` |
| 缺陷修复 | #49 / #58 / #59 | 空容器字段按无值；查验结论字段顶层优先；OFD 容器识别 | PR #54 → `1e91e9f`；PR #61 → `8e44434`；PR #65 → `ff00502` |
| 验收 | #44 / #46 | 字段级准确率验收（8 张带标注）；专用端点对比与采购建议 | PR #48 → `a6adf34`；PR #50 → `fc5ed04` |
| 规则层 | #63 | 并发上限 4 / 按负载动态；`flock` 串行；`uv run --no-sync` | PR #64 → `b733980` |
| 运维跑批 | #57 / #62 | 8 票全流程 → 导出交付；查验状态刷新 | 无仓库变更（额度 OCR 114→106、查验 20→12→4） |

### 阶段 1 卡点（仍需所有者输入）

| 项 | 已交付 | 仍需所有者 |
|---|---|---|
| OCR 合规与配额 | 百度 `multiple_invoice` 已拍板并真实联调；#44 准确率验收、#46 端点对比、#52 路由可配置均已合入 | ①关闭合规项（**DPA / PIPIA** / 留存与是否用于训练）；②确认配额与计费（免费额度已按纪律消耗，见 §9.4） |
| 真实微信适配 | #21 服务层 + 桩适配器 + 迁移 `0012` | 真实微信适配与 Profile 端到端验收的**授权** |
| 会计科目口径 | #53 导出机制 + #57 首版导出（科目代码为**占位值**） | 会计确认科目代码；替换占位集（`PLACEHOLDER-ACCOUNTS-待会计确认.txt` 在交付目录） |
| 上传权限面 | #60 端点（鉴权取 `expense_claim/create/own`） | 财务角色需上传查验证据 → 需新增权限目录项（**权限/安全模型变更**，属所有者决策） |
| 真实异地恢复演练 | #22 Runbook + dry-run | **第二台主机或异地位** |

**恢复后的标准循环**（对任意新任务）：

1. 读 Issue 全文 → 核对验收/路径/依赖（依赖须已 VERIFIED 且已合入 main）
2. 在 Issue 写活动登记评论（`DISPATCHED`、锁定路径、mode、runner、时间）
3. `git fetch --prune`；从最新 main 建分支 `feat|fix|chore/<issue>-<short>` + worktree `.worktrees/<name>`
4. 派发/启动 Execution（本机具备 C1–C4，可单会话）；**派发后核实实例确实在跑**；同批多任务时路径两两不重叠
5. Execution 尽早开 Draft PR（`Refs #N`）；交回复核前更新到最新 main 并重跑验证
6. Master 独立复核（不复述作者自述）：范围、当前 head 的 CI、自写探针复现核心断言
7. G1–G6 通过且批准 SHA = 待合并 head → `gh pr merge --merge`
8. 合并后在 main 上重跑验证 → 更新登记评论为 VERIFIED → 关闭 Issue

---

## 7 未完成清单

### BLOCKED（等所有者输入）

| 项 | 需要什么 | 状态 |
|---|---|---|
| OCR 合规（DPA / PIPIA / 留存） | 合规条款关闭；是否用于训练 | 仍阻塞（真实调用已按所有者逐次授权执行） |
| 微信接入授权（未建 Issue） | 是否允许在指定主机创建 Profile 做真实路由与消息验收 | 仍阻塞；#34 已建 dev 员工账号与 Profile，但**渠道绑定与真实验收未授权** |
| 第二台主机或异地位（未建 Issue） | 用于跨主机/异地恢复的真实演练与证据 | 仍阻塞 |
| 会计科目生产替换 | 会计确认科目代码（当前为占位集） | 仍阻塞（#57 交付目录含占位清单） |
| 上传权限面（权限模型变更） | 是否为「财务角色上传查验证据」新增权限目录项 | 仍阻塞（属权限/安全模型变更，须所有者决定） |

### 待派发（PLANNED / 建议立项）

| 项 | 内容 | 注意 |
|---|---|---|
| #16 | Track A：AiNativeERP 设计映射与 Track B 范围（仅文档） | PLANNED，后置：阶段 1 收口后再评估 |
| `routed` 真实联调 | 受理服务层接入解析后，验证 `routed` 模式在真实链路上生效 | 需授权（真实调用）；当前 `routed` 尚无生产调用方，会回退 `smart` |
| 挂账 `source_file_id` 归属校验 | 服务层校验引用文件归属（当前仅元数据关联） | 服务层语义变更，另立任务 |
| Track D 单张粒度 / 保留期 / 导出权限编号 | #53 明确另立 | 单张粒度、保留期参数、独立权限编号 |
| PLAN §8 类型清单同步 OFD | `.ofd` 是否纳入 PLAN §8 类型限制清单（#59 未做，`templates/PLAN.md` 不在其允许路径） | 文档同步任务 |
| 里程误识 / 跨源核对规则 | #52 未实现（产品决策） | 行程单里程数字约束 + 票/行程单跨源核对 |

### 阶段 1 剩余项（全部为所有者阻塞）

- **微信真实适配与 Profile 端到端验收**：#21 已交付服务层 + 桩适配器（迁移 `0012`）；真实适配需所有者授权建 Profile。
- **OCR 合规与配额**：供应商已拍板百度 `multiple_invoice`（见 §9.3）；#44/#46/#52 已交付；剩余阻塞为 DPA / PIPIA / 留存与计费确认。
- **真实跨主机/异地恢复演练**：#22 已交付 Runbook + dry-run；真实执行需第二台主机或异地位。

---

## 8 已完成（不要重做）

- **Track D 导出链（2026-10-18）**：
  - **#43 模板结构落库与映射设计（PR #47，merge `8b063df`）**：新增 `docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`（模板 14 个工作表逐表结构、四候选布局对比、逐列映射、校验规则、manifest 草案、决策清单 D1–D12）与 `docs/export-templates/template-structure.json`（纯结构 sidecar）。D1 推荐 `录入数据`（一行 = 一条分录，31 列）。
  - **#53 导出机制（PR #55，merge `64f8a68`）**：新增 `app/export/`（`config` `template` `mapping` `validation` `writer` `manifest` `service` 等）：单文件 TOML 配置（`AJAIMGR_CONFIG_FILE`，默认 `config/ajaimgr.toml`）、模板运行时装载 + SHA-256 绑定（不入库）、`录入数据` 31 列映射引擎、E1–E7/C1–C5 与 12 条 fail-closed、单元格注入转义、manifest（JSON）与内容寻址存储 + 审计；服务层 `export_claims_period`（只读投影、幂等、字节可复现——导出文件不含时间戳）。新增 `config/ajaimgr.example.toml`（逐项详尽注释、零真实值）；`.gitignore` 忽略运行时配置、保留 example。**无端点 / 无 UI / 无权限编号 / 无迁移 / 无新依赖。**
  - **#57 首版真实导出（运维，无仓库变更）**：见下方「真实跑批」。
- **受理全链（2026-10-18）**：
  - **#45 百度 OCR 装配（PR #51，merge `11e3e2a`）**：新增生产级 `SourceReader`（`app/intake/source_reader.py`：按 `source_file_id` 读 `source_files` → 存储字节 → sha256 校验，错误分类 `missing_file`/`hash_mismatch`/`storage_error`，fail closed）与默认装配点（`app/intake/assembly.py`：凭据缺失时优雅降级、不注册该解析器、返回说明 + warning 双通道）。端到端集成测试覆盖 `parsed` / `needs_review` / 供应商失败码 / 凭据缺失回退人工录入。**未改判别/草稿/追溯逻辑、未加端点/依赖/迁移。**
  - **#56 写入路径（PR #66，merge `f15d25f`）**：确认草稿 → 正式报销单草稿 + 明细行（+ 同事务发票挂账）：`FOR UPDATE` 行锁 → 授权（`create/own` + `submitted_by` 归属）→ 幂等（审计查询，不新增迁移）→ 结构校验全链 fail-closed **先于**写入 → 同事务写入（flush 不 commit）→ 审计（低置信度只记字段名）。口径定稿：`expense_date` 默认取票面开票日期、确认时可覆盖；粒度 = 报销单草稿 + 明细行；含票面字段时同事务建 `expense_invoices`（`duplicate_key` 幂等）。**未实现 UI / HTTP 端点。**
- **产品化端点（#60，PR #67，merge `76b7a4e`）**：`POST /documents`（上传原件，复用 `store_file` 四道闸门；先鉴权后落盘；multipart 超限先于缓冲拒绝）、`POST /claims/{claim_id}/invoices`（复用 W1-23 `attach_invoice`，脱敏摘要）、`GET /claims/{claim_id}/invoices`（只读、脱敏）。身份沿用 `X-Employee-Id` 过渡方案；请求体 `employee_id` 显式忽略。**无 UI、无查验触发端点、无迁移、无新依赖。**
- **OCR 端点路由（#52，PR #68，merge `ed0c324`）**：新增 `app/integrations/ocr_routing.py`（`[ocr].routing` 装载；缺文件/缺节/缺键 → 默认 `smart`；非法值 → 明确报错且只报键名）与 `app/integrations/baidu_dedicated_ocr.py`（`BaiduVatInvoiceParser` / `BaiduTaxiItineraryParser` / `RoutedOcrParser` / `decide_ocr_endpoint`）。`smart`（默认，= 现有 `multiple_invoice` 行为）；`routed` 按 `ParseRequest.metadata` 的显式 `ocr_route` 提示 → 文件名特征 → 回退 `smart`。敏感字段（手机号等）**丢弃 + 二次掩码**两层；里程数字 + 区间校验失败保留原值并标记 `distance_needs_review`；`vat_invoice` 无字段级置信度 → 统一 `0.5` 回退。新增 132 例全桩测试。**无真实调用、无端点、无迁移、无新依赖。**
- **缺陷修复（2026-10-18）**：
  - **#49 空容器字段（PR #54，merge `1e91e9f`）**：展开/清洗层把空数组/空映射（`[]`、`{}`、仅空白元素）按**无值**处理，消除数电票 `invoice_code == "[]"` 脏值与误导性置信度回退；空数组字段不产出归一化键（`fields` 与 `field_confidence` 键集合仍一致）。既有 54 例适配器测试零放宽。
  - **#58 查验响应层级（PR #61，merge `8e44434`）**：`baidu_vat_verify.py` 的 `_parse` 对结论/身份字段（`VerifyResult`/`InvalidSign`/`VerifyMessage` 等）改为**顶层优先、`words_result` 回退**；票面明细仍从 `words_result` 取。新增真实结构桩测试；旧结构保持兼容、两者都缺维持 fail closed。
  - **#59 OFD 存储（PR #65，merge `ff00502`）**：`sniff_mime` 按 ZIP 中央目录 + 根级 `OFD.xml`（容忍 `./` 前缀）识别 OFD 容器；`.ofd` 入 `KNOWN_BINARY_EXTS` 白名单；只读中央目录、**不解压成员**（无压缩炸弹放大面）。`.ofd` 由「拒绝」变为「接受（含结构校验）」；假 OFD 声明一律拒绝且零落盘。
- **验收（2026-10-18）**：
  - **#44 准确率验收（PR #48，merge `a6adf34`）**：`docs/research/W2-36-baidu-accuracy-acceptance.md`——8 张带逐票真值的真实票据（OFD 6 / PDF 2）经 `multiple_invoice` 真实识别；9 个可比字段 exact-match 100%（`invoice_code` 除外，见 D1，已由 #49 修复）；配额 8 次（预算 12）。缺陷 D1/D2/D3 列出且未越界修复。
  - **#46 端点对比（PR #50，merge `fc5ed04`）**：`docs/research/W2-37-ocr-endpoint-comparison.md`——`vat_invoice`（8 票）+ `online_taxi_itinerary`（6 份）真实调用，与 W2-36 基线对比、按票种给出主用/备选端点与采购建议（不含价格数值）；配额 15 ≤ 18（含 1 次 `probability` 探针）。**重要发现 D5：行程单端点返回明文手机号（6/6）**，生产接入须专项脱敏与留存控制。
- **规则层更新（#63，PR #64，merge `b733980`）**：`ORCHESTRATION-PROJECT.md` 并发上限 **1 → 4 / 按负载动态**（条件：路径两两不重叠、`flock` 串行全量测试、活动登记 + 依赖检查）；全量测试改经 `flock /tmp/ajaimgr-fulltest.lock` 串行；venv 并行策略改为共享 `.venv` + `uv run --no-sync` + 依赖变更任务独占。同步章程 R8 与整合计划口径。
- **真实跑批与查验刷新（运维，无仓库变更）**：
  - **#57 真实跑批（8 票全流程 → 导出）**：测试主机 `main` = `64f8a68`（与编排机同 SHA），dev 库 `ajaimgr_dev`。8/8 存文件 → 真实 OCR（百度 `multiple_invoice`）解析 → 草稿 → 人工确认（载体 OFD 6 / PDF 2；每票 9 键、键集合一致；受理终态 `parsed` 3 / `needs_review` 5）；报销单 **`EC-00000001` 达 `paid`**（8 明细 + 8 挂账；`submit`→`approve`→`finance_review`→`record_payment`；审计链完整）。**Track D 导出**：`export_claims_period(period="2026-10", statuses=["paid"], date_basis="expense_date")` → `expense-export-2026-10.xlsx`（9 条分录 / 1 张凭证、借贷平衡、10 项校验全绿、`录入数据` 31 列结构复核通过）+ manifest；模板 SHA-256 `1a235166…`（与登记值一致）；**科目代码为占位值**并标注「待会计确认」。**交付物在编排机仓库外目录**（xlsx + manifest + `ajaimgr.toml` + 占位科目清单）。dev 库变更仅本任务新增（业务行 + 必要角色授予，清单见 #57 证据评论）；两台机器 `git status` 干净。
  - **#62 查验刷新**：以 #58 修复后的 `main`（`8e44434`）**仅重跑查验**（不重复 OCR、不新建单据、不改其它状态）：8/8 调用成功（`VerifyResult=0001`×8、`InvalidSign=N`×8、0 重试）；产品状态 `unable_to_verify`×8 → **`verified_pass`×8**；挂账行状态同步；无 `verified_fail`。`invoice_verifications` 行数不变（8，同 `duplicate_key`）。
  - **额度变化（证据评论口径）**：OCR `multiple_invoice` **114 → 106**；查验 **20 → 12 → 4**（#57 消耗 8、#62 消耗 8；均经所有者逐次授权）。
- **第 4/5 轮队列（2026-10-18 下午；PR #36 / #38 / #40，#34 无仓库变更）**：见上一版交接（`50292de` / PR #42）。要点：#35 百度 `multiple_invoice` 适配器、#37 按真实响应修正、#39 认知落库与选型报告修正、#34 dev 员工账号与 Profile 计划。**注：旧 commit SHA 因历史重写失效，以 PR 编号为准。**
- **第 3 轮队列（2026-10-18，PR #29 / #30 / #31）**：#21 渠道消息入站与路由（`app/intake/inbound.py`、`InboundMessage` + 迁移 `0012`）、#22 跨主机/异地恢复 Runbook 与 dry-run（`ops/offsite_restore.py`）、#23 OCR/ASR 选型报告。**#21 无 HTTP 端点、未接真实微信、未建 Profile；#22 未执行真实异地演练。**
- **财务对接方向变更与文档落库（2026-10-17，4 个 PR）**：PR #11 章程 v1.1 + 整合计划 v0.2；PR #14 定位对齐；PR #15 实施方案 v2.1；PR #20 B-1/B-2 决策落库。Issue #10/#12/#13/#19 均已 VERIFIED 关闭。**旧 commit SHA 因历史重写失效，PR 编号仍有效。**
- 离线台账时期 **38 条 VERIFIED**：准备 8、调研 5（W0-1/2/4/5/7）、Wave 1 主体（后端底座与迁移、身份权限、报销域与状态机、Outbox、受理与文件、导出、API、管理后台、权限目录、负面测试与修复、渠道身份、发票查验 Provider 与腾讯云/百度适配器、人工辅助录入、报销单挂账、Profile 生成器与校验器、备份与恢复工具及修复轮）。完整清单与事件流见 `.orchestration/state.sqlite3`。
- **规则层切换（2026-10-16）**：Agent-Orchestration GitHub 版规则文件替换、`.github/` 模板与 CI 工作流、`ORCHESTRATION-PROJECT.md` 按项目填写、CI 修复至全绿、主分支规则集启用。
- **W1 冻结队列三项（2026-10-16，PR #5 / #6 / #7）**：W1-24 模型模块导入自足性、W1-16 迁移可复现性（`0003` 冻结快照）、W1-26 受理解析流水线（`app/intake/parsing.py` / `pipeline.py`）。**commit SHA 因历史重写失效，以 PR 编号为准。**

**值得知道的验收结论（累计摘要）**：越权矩阵 70 格逐格断言；主管不能审批自己的报销；状态机 49 组合穷举；Outbox `SKIP LOCKED` 并发正确；幂等（提交/审批/付款/查验限次）；7 种路径穿越全拒（含符号链接逃逸）；渠道身份零明文；W1-23 挂账探针 32/32；W1-28 百度文档 sha256 逐字节比对；#44 八票九字段 exact-match 100%；#52 132 例全桩（含零网络断言）；#53 31 列表头与 sidecar 程序化比对全等。

---

## 9 已知取舍、限制与决策（最容易丢的一节）

### 9.1 仍然存在的取舍（不要当成缺陷重新修）

- `authorized` scope 仍是占位实现（`visible_employee_ids` 只返回自身；显式授权表未建）；不得假装已支持其语义。
- 单条越权返回 403 而非 404（防枚举是待决产品项）。
- 所有 `IntegrityError` 映射 422/409（兜底接受，归因精度下降）。
- 无效会话不写业务审计（安全监控落应用日志）。
- `sniff_mime` fail-closed（偏严）；XLSX 边界一次显式 `float()`（有注释与测试锁定）；PDF 中文用内置 CID 字体。
- `resolve_principal` 拒绝时 commit 调用方会话（调用方须视为事务边界）。
- 模型模块导入不自足（W1-24）与 `0003` 读活动代码（W1-16）**均已修复**；**`0009_perm_alignment` 仍读活动代码**，为 W1-16 顺延项（见 `docs/MIGRATION-REPRODUCIBILITY.md` §5），是否冻结待定。
- 解析流水线（W1-26）：关键字段口径与置信度阈值 `0.75` 为**保守默认**（产品参数，可覆盖；见 `docs/INTAKE-PARSING-PIPELINE.md` §9）。
- 多机约定：编排机可不装 psql（只开发）；一切 DB 测试/实测走测试主机。
- Outbox 用应用侧时钟；W1-8 只有 service 原语、无常驻 Worker。
- API 身份仍是 `X-Employee-Id` 过渡方案；管理后台登录=内部令牌（生产必须接 SSO）。
- 微信通道「有条件 Go」，11 项待实测。
- `expense_invoices`（W1-23）：`suspected_duplicate` 为授权新增列；`attach_invoice` 不校验 `source_file_id` 存在性（FK 兜底）；同单并发重复以 IntegrityError 失败（可重试）。
- `invoice_verifications` 不在 conftest TRUNCATE 清单（模块自清理；套件后残留实测 0）。
- 百度为首选查验渠道（腾讯云/聚美智数备用；无渠道选择开关）；凭据 env-only（缺失即 fail closed）。
- **CI 环境差异**：`test_profile_provisioning` 有一例依赖本机 `~/.hermes`，CI 上合法 skip；CI 中 `pg_dump` 必须与服务同主版本。**测试不得依赖真实 `~/.hermes`**。
- `inbound_messages`（#21）：**拒绝不落库**（可追溯性由 `channel_routing` 的拒绝审计承担）；同一消息 ID 换发送者/归属 → `rejected(route_conflict)`；并发去重靠唯一约束 + `IntegrityError` 回滚回查。`intake_job_id NOT NULL`。**尚无 HTTP 端点。**
- `ops/offsite_restore.py`（#22）：**不建立任何网络连接**；`rehearse` 只做传输模拟 + 附件/任务库落盘校验，**数据库恢复步骤不执行**；`--allow-missing-*` 传入即拒绝；`--target-dir` 必须为空。
- 规则集当前**无 bypass**；仓库 **public**（所有者决定）；历史作者均为占位身份。
- **Track D 导出（#53）**：模板**不入库**、运行时装载 + SHA-256 指纹 + 表头/列数校验（不静默降级）；导出文件**不含时间戳**（字节可复现）；示例配置的 `template.sha256` 为全 0 占位、`template.path` 为通用服务路径占位，**部署方必须替换**；权限复用现有 RBAC 三元组（`[permissions]` 可配，**未新增权限编号**）；新增显式 fail closed：`currency_unmapped` / `storage_error` / `period_invalid`；**无 HTTP 端点 / 无 UI**；**单张报销单导出粒度未实现**（本期按期间批量）；**保留期机制仍不存在**。
- **写入路径（#56）**：一草稿 = 一行；日期优先级（票面开票日期优先、可人工覆盖）；金额必须为正数且拒绝 `float`；**跨进程幂等依赖 `FOR UPDATE` 行锁而非 DB 约束**（约束级需迁移另行授权）。
- **产品化端点（#60）**：上传鉴权取 `expense_claim/create/own` → **财务角色暂不能经本端点上传查验证据**（`POST /invoice-verifications/manual` 的 `source_file_id` 需要该能力）；放开需新增权限目录项（权限/安全模型变更）。挂账 `source_file_id` **仅元数据关联、未做归属校验**（无下载端点、无外泄路径）。`scan_status` 初始为 `pending`（未实现扫描器）。金额量化阶段的 `decimal.InvalidOperation` 在 API 层映射为既有 422（未新增错误码）。
- **OFD 存储（#59）**：`.ofd` 由「拒绝」变为「接受（含容器结构校验）」；OFD 结构内容声明 `.zip` 时**按结构优先**返回 `application/ofd`；判定只读 ZIP 中央目录、不解压成员。**`templates/PLAN.md` §8 的类型限制清单尚未同步 `.ofd`**（不在 #59 允许路径）。
- **查验响应层级（#58）**：结论/身份字段**顶层优先、`words_result` 回退**；票面明细仍从 `words_result` 取。金额 `face_mismatch` 采用「与不含税**或**价税合计任一相等即一致」——与所有者价税合计口径一致；若将来收紧为「必须等于不含税」，本批 8 票会被判为票面不一致（观察项，未另立）。
- **OCR 端点路由（#52）**：`routed` 模式**尚无生产调用方**（受理服务层未接解析）→ 落地前元数据提示会全部**回退 `smart`**；敏感字段「丢弃 + 二次掩码」两层；无字段级置信度 → 统一 `0.5` 回退（专用端点草稿全量人工复核）；里程区间为本地保守默认（调参留待产品决策）；新归一化键只进草稿、**写回白名单未动**（不改变挂账语义）。
- **空容器字段（#49）**：空数组/空映射一律按无值处理（通用机制，非单字段补丁）；此前「产出脏值」的字段将变为「缺失」——属修正而非破坏。
- **真实响应才是事实来源**：#35 按文档实现、真实一跑暴露三处偏差；#58 再次证明桩结构与真实结构不符会掩盖缺陷。凡「按文档推断的响应形态」，首次真实调用后必须逐项回验并写进文档。

### 9.2 所有者决策清单（未决）

1. 脱敏业务样例与现有流程盘点（原 W0-6；#44 的 8 张带标注样本只覆盖准确率验收）
2. 微信接入授权（是否允许建 Profile 做真实路由验收）
3. 单条越权 403 vs 404
4. `description` 长度 1–500 字符（PLAN 未规定的引入默认值）
5. 生产 Profile 主机规格（需真实负载测试）
6. 百度查验正式开通（账号/费用/合规确认；原 W0-4 §7 项）
7. 生产备份必须存放于生产主机之外（章程成功标准未满足）
8. OCR 合规（**DPA / PIPIA** / 留存与是否用于训练）+ 配额与计费确认
9. **上传权限面**：是否为「财务角色上传查验证据」新增权限目录项（权限/安全模型变更）
10. 会计科目生产替换（当前占位集）
11. GitHub 协作策略微调（直推 bypass？合并方式？——当前：PR + G1–G6 + strict checks）
12. 解析流水线产品参数确认（关键字段口径、置信度阈值——当前均为保守默认）
13. Track D：单张粒度 / 保留期 / 导出权限编号；行程单里程区间与跨源核对规则

### 9.3 已决策（2026-10-17 所有者）

- B-1 工具选择：Agent 侧选（Hermes 经 MCP / function calling），后端只做契约校验、权限与澄清；不建后端意图路由。
- B-2 确认持久化：新增 `tool_confirmations` 轻表；参数 hash + TTL 15 分钟 + 一次性消费 + 执行前复检；建表放在 Track B 的确认类 Issue。
- 优先级：先收口阶段 1 剩余项，Track A 后置。

### 9.4 已决策（2026-10-18 所有者）

- **OCR 供应商：百度智能财务票据识别 `multiple_invoice`**（替代 #23 报告推荐的腾讯云主 / 阿里云备；该排序保留为过程记录，**不再作为执行依据**）。
- **凭据变量名**：`BDOCR_API_KEY` / `BDOCR_SECRET_KEY`（旧名 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY` 仅作回退）；只从环境变量读取，缺一即构造失败（fail closed）。
- **置信度口径**：字段级取 `probability.min`（缺则 `average`，再缺回退 `0.5`）；整票 `probability` **只允许**用于 `invoice_type` 一个字段。默认阈值 0.75 不变，低于阈值即人工复核——**阈值以下不是故障**。
- **金额口径**：报销金额只取 `AmountInFiguers`（价税合计）；`TotalAmount` 是不含税合计，**取错会系统性少报**。
- **凭据文件必须被 Git 忽略**：`.env` / `.env.*` 已入 `.gitignore`（保留 `!.env.example`）。**任何凭据值不得进入仓库、日志、Issue 与对话。**
- **dev 员工账号**：`employee#3`（所有者本人）与 `employee#4`（`tester`），均为 `employee` 角色；Hermes Profile 计划包在测试主机留存（详见 #34 / §8）。
- **端点路由设为可选项**：`[ocr].routing = smart | routed`，**默认 `smart`**（与当前生产行为一致）；`routed` 按类型路由（票据 → `vat_invoice`、行程单 → `online_taxi_itinerary`，无法判定回退）。
- **配额纪律**：测试期**均衡使用**百度 OCR 免费资源，确定后再付费购买；真实调用逐次授权并记账（本批：OCR `multiple_invoice` 114→106；查验 20→12→4）。
- **Track D 决策**：D1 目标布局 = `录入数据`（31 列，一行 = 一条分录）；D2–D8 全部**可配置**；配置载体 = **单一 TOML 文件**（详尽注释、零真实值；运行时文件不入库、路径经环境变量指定）；模板运行时装载（不入库、SHA-256 绑定）；未配置且无法推断 → fail closed 不产出文件。
- **真实跑批路线**：路线 A（运维级先出文件；真实查验；报销人 = dev 员工 #3）。

---

## 10 操作手册（命令级）

### 多机注记

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
4. 并行前提：同批各任务允许路径集合**两两不重叠**；依赖检查通过；全量测试经 `flock` 串行
5. 交付物：分支/PR、head SHA、命令与真实输出、验收矩阵、未完成项

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
```

### 全量测试（在测试主机上执行；需要 PostgreSQL 17）

会重置 `ajaimgr_test`，**必须经 `flock` 串行**（多个实例可排队，不得并发裸跑）；并行执行一律用 `uv run --no-sync`：

```bash
export UV_PROJECT_ENVIRONMENT=<test-host-venv>   # 实际路径见 local_info.md
flock /tmp/ajaimgr-fulltest.lock uv run --no-sync pytest app/tests -q -o addopts=""
```

### 测试主机同步：bundle 直传通道（`git fetch` 不稳时）

测试主机外网 `git fetch` 偶发失败（connect 超时 / `GnuTLS recv error`）且重试常无效；可靠通道：

```bash
# 编排机：把分支打成 bundle（^<测试主机已有 head> 增量）
git bundle create /tmp/<n>.bundle ^<test-host-head> <branch>
cat /tmp/<n>.bundle | ssh <test-host> 'cat > /tmp/<n>.bundle'
# 测试主机：从 bundle 取引用后建 worktree / merge
ssh <test-host> 'cd <test-host-repo> && git fetch /tmp/<n>.bundle <branch>:refs/remotes/origin/<branch>'
ssh <test-host> 'cd <test-host-repo> && git worktree add .worktrees/<name> FETCH_HEAD'
```

测试主机非交互 SSH 下 `uv` 不在 PATH（用其绝对路径，见 `local_info.md`）；跑完 `git worktree remove`（删除前确认 `git status --short` 为空）。

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
7. **管道吃退出码**：`cmd | head` 的 `$?` 是 `head` 的；要真退出码就重定向到文件再读（或用 `${PIPESTATUS[0]}`）。
8. **规则集语义**：head 一变，旧批准与旧 CI 证据失效；strict 模式要求 PR 与 main 同步后才能合并。
9. **并发上限已改为 4 / 按负载动态（#63）**：但**路径两两不重叠是硬前提**；测试主机全量测试必须经 `flock /tmp/ajaimgr-fulltest.lock` 串行；并行一律 `uv run --no-sync`，禁止并发 `uv sync`。
10. **`grep -vE '^dir/'` 之类排除式易错**；范围检查用 `git show --name-status` / `gh pr view --json files`。
11. **子代理可能"更新"既有测试**：逐字检查新断言强度，不能只看"已说明理由"。
12. **子代理会纠正上一实例的假账**：重试时要求逐条核实旧产物。
13. **离线版 `task add --acceptance` 曾是重复标志只留最后一条**；GitHub 版验收写进 Issue，多写 checklist 后要核对渲染完整。
14. **复核探针必须指向被测代码**：未合并模块在 main 里不存在（探针指向 worktree/PR 分支）。
15. **新增业务表要同步 conftest 的 `BUSINESS_TABLES`**；`invoice_verifications` 仍未入列，注意清理责任。
16. **CI 里 pg_dump 必须与服务端同主版本**：runner 自带 16 会与 17 服务「server version mismatch」；PGDG 安装 client-17 并注入 `$GITHUB_PATH`。
17. **CI 里 socket DSN 连不上容器**：用 libpq 环境变量（PGHOST/PGPORT/PGUSER/PGPASSWORD）把 DSN 与裸 `psql`/`dropdb` 指向服务容器。
18. **规则集生效后不能直推 main**：改 main 一律走分支 + PR。
19. **单会话编排**：派发子代理后必须核实 running（不得虚假派发）；**后台任务内跑 SSH 会被沙箱直接拦**（需前台 + escalation）。
20. **本机 git fetch 偶发网络抖动**：代理 502 / HTTP2 framing error——重试即可；push 遇同类错误用 `git -c http.version=HTTP/1.1 push`。
21. **陈旧锁文件**：`~/.gitconfig.lock` 残留会阻断 `gh auth setup-git`（确认无进程占用后先备份再清理）。
22. **临时库名跑全量会误伤 1 例**：`test_restore_refuses_protected_database` 期望目标库为受保护名（`ajaimgr_test`）；用自定义临时库名跑全量会出现 1 failed（环境差异，非缺陷）。全量请用标准库名或在报告中说明。
23. **子代理的「与改动无关」结论须独立核实**：用相关代码路径与 CI 对照验证后才可采信。
24. **同一 worktree 并发跑 pytest 会互相争用**：表现为「某个用例挂起」而非报错；派发提示应写明「同一时刻只跑一个测试进程」。
25. **文档里的命令必须按文档原样跑一遍才算验收**：文档类交付至少逐条执行关键命令。
26. **测试不得依赖真实 `~/.hermes`**：`HOME`/`USERPROFILE` 用 `tmp_path` 隔离。
27. **`gh issue view --json comments` 返回的是 node id**（`IC_kw...`），更新评论须用 REST 接口的数字 `id`（`gh api repos/.../issues/<n>/comments --jq '.[].id'`）。
28. **真实响应才是事实来源，文档与选型报告会错**：#35 按官方文档实现、真实一跑暴露三处偏差；#58 再次证明——桩测试把 `VerifyResult` 放在 `words_result` 内，与真实结构（结论在**顶层**）不符，导致 8/8 误判。凡「按文档推断的响应形态」，首次真实调用后必须逐项回验。
29. **「4M」这类上限要取十进制下界**：文档只写「4M」不写进制，4 MiB 会越过服务端限制；实现取 `4_000_000`。
30. **凭据文件必须入库忽略**：`.env` 曾未被 `.gitignore` 覆盖；各机 `.git/info/exclude` 只是止血，**仓库里必须有规则**。
31. **SSH 别名以本机 `~/.ssh/config` 为准**：文档里的 `<test-host>` 是**占位符**，按它直接连接会 `Could not resolve hostname`；先 `grep '^Host' ~/.ssh/config` 取真实别名。
32. **本地敏感文件可能被安全策略阻断读取**：所有者提供的 xlsx 直接解析可能 `PermissionError`；改走「编辑器打开给 `file_id` / 直接贴表头 / 提供无中文名副本」；**被拒后不要反复重试等价命令**。
33. **测试主机的 `git fetch` 会挂在网络上**：表现为后台任务十几分钟无输出、`git-remote-http` 常驻、pytest 根本没启动。**可靠通道 = bundle 直传**（见 §10）；跑全量前若仓库已同步就跳过 pull。
34. **真实调用要记账、要限额、要逐次授权**：OCR 与查验额度是紧缺资源；派发提示须写明端点数、上限与授权来源，超限停下回报（#44 ≤12、#46 ≤18、#57 8、#62 8）。
35. **`routed` 模式无调用方 = 静默回退**：配置生效 ≠ 行为生效。`routed` 在受理服务层接入解析前会全部回退 `smart`；声称「已支持按类型路由」前必须确认存在构造 `ParseRequest` 的生产调用方。
36. **「跑通」不等于「准确」**：20/20 成功只证链路可达；准确率必须有**带标注真值**的样本集（#44 用 8 张带标注票据补齐）。
37. **接口响应里可能有敏感字段**：行程单端点返回**明文手机号**（#46 D5，6/6）。适配器必须白名单映射 + 二次掩码，草稿/追溯/日志/审计四处都要断言不含敏感值。
38. **OFD 是 ZIP 容器**：仅凭魔数无法与普通 ZIP 区分；须按容器内根级 `OFD.xml` 判定，且**只读中央目录、不解压成员**（无压缩炸弹放大面）；假 OFD 声明必须零落盘拒绝。
39. **导出文件要字节可复现就得固定时间戳**：openpyxl 的 `core.xml` 与 zip 条目时间戳必须固定为常量，否则同输入两次导出哈希不同、幂等断言无从谈起（#53）。
40. **写 Excel 要防单元格注入**：文本以 `=`/`+`/`-`/`@` 开头必须转义（#53）。

---

## 12 开局检查清单（下一任）

- [ ] 读 `AGENTS.md`、`AGENT-ORCHESTRATION.md`、`MASTER.md`、`ORCHESTRATION-PROJECT.md`、`prompts/START-MASTER-EXISTING-PROJECT.md`
- [ ] 读本文档 §3、§4、§6、§9
- [ ] 跑 §10 的「状态恢复」命令并核对 §5 快照（head、CI 状态、开放 Issue/PR）
- [ ] 确认没有遗留运行中的子代理（`delegate_task(action='list')`）
- [ ] 队列已清空：**不要**重做 #43–#68；下一步等所有者输入（§6 卡点表 + §7 BLOCKED）
- [ ] 若所有者给出输入：按 §6「标准循环」建 Issue → 登记 → 派发；#16 仍后置
- [ ] 如向所有者汇报：§6 卡点表 + §7 BLOCKED + §9.2 决策清单；**OCR 供应商与路由已拍板**，重点催的是**合规（DPA/PIPIA）**、**会计科目口径**、**上传权限面**与**微信授权**
