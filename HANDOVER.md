# HANDOVER — 会话交接文档

- 生成时间：2026-10-16 09:15 CST（会话切换前）
- 主分支基线 head：`d5b7ce6cb4b6bf25691f86ff62a96805d2150c9a`（**这是本文档提交之前的 head**；本文档自身的提交位于其之上，是 main 的最新提交）
- 台账：42 条任务（34 VERIFIED / 2 BLOCKED / 5 PLANNED / 1 IN_PROGRESS）
- 本文档由上一任 Master Agent 撰写，供下一任（下一个会话）接手。**先读本文档，再读 AGENTS.md / AGENT-ORCHESTRATION.md / ORCHESTRATION-PROJECT.md / templates/PLAN.md。**

---

## 0 怎么用这份文档

1. 第 1–5 节是必读背景（角色、约束、环境、状态）。
2. **第 6 节是立即可做的事**：一条已退回修复的任务，含完整派发包要点，可直接派发。
3. 第 7 节是全部未完成项，第 8 节是已完成项（**不要重做**）。
4. 第 9 节是已知缺陷、设计取舍与开放决策——**这一节最容易丢，也最容易踩**。
5. 第 10–11 节是命令级操作手册与踩过的坑。

---

## 1 你的角色与入口

你是本仓库的 **Master Agent**：负责计划、派发、独立复核、合并、阶段收口与汇报。

- **默认不实现产品代码**。不替 Execution Agent 编辑实现文件、不代做修复、不以直接改文件绕过任务与审查流程。你维护编排元数据与任务记录。
- 每个会话开始时按 `AGENT-BOOTSTRAP.md` 的入口流程恢复状态并路由。
- 权威顺序：项目所有者最近一次明确决定 > `AGENTS.md` > 其余手册与提示词。发现冲突登记为任务，不自行发明权限。

---

## 2 项目定位与当前阶段

面向中国大陆 20–30 人（后续扩到 100）企业的 Hermes Agent 原生管理系统，**首期模块：报销**。以 AI 对话/语音/图片/文件为主要工作入口；正式账务与付款留在金蝶等有正式授权的系统。

- 计划文档：`templates/PLAN.md`（实施方案 v2.0，**注意：这是规则包 REQUIRED 清单里的文件，不得移动或删除**）
- 项目章程：`docs/PROJECT-CHARTER.md`
- 部署要求：`docs/DEPLOYMENT-REQUIREMENTS.md`
- 调研报告：`docs/research/W0-1`（微信）、`W0-2`（Profile 隔离）、`W0-4`（发票查验）、`W0-5`（数据合规）、`W0-7`（多 Profile 实测）

**当前阶段**：Wave 1 主体已完成（后端底座 + 报销域 + API + 管理后台 + 权限模型 + 负面测试 + 发票查验接口 + Profile 生成器 + 备份工具）。阶段 1 尚缺：微信消息路由、OCR/ASR 解析流水线、备份异地存储与恢复演练收口。

---

## 3 不可违反的硬约束

1. **严禁读取或复制凭据值**：`~/.hermes/auth.json`、`config.yaml` 里的凭据、token、cookie、真实账号标识。报告中一律用占位符。**不要向项目所有者索要凭据，也不要在聊天中接收凭据。**
2. **严禁在本机新建、启动、停止任何真实 Hermes Profile**（W0-7 实验曾获一次性授权，不得顺延）。不得写入 `~/.hermes/` 下任何文件。
3. **不得使用 pip**，统一 uv（`~/.local/bin/uv`）；解释器由仓库根 `.python-version` 固定 3.13。
4. **不得新增第三方依赖**，除非在派发包中由 Master 显式批准并说明理由。当前 10 个直接依赖：fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2。
5. **默认 DSN 走本地 unix socket peer 认证**，任何文件里不得写入口令。
6. **不得为让测试通过而修改、删除或跳过已有测试断言**（可因行为改变而修正并说明理由）。
7. **受保护/禁改路径**：`AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`ORCHESTRATION-PROJECT.md`、`README.md`、`VERSION.txt`、`LICENSE.txt`、`templates/`、`scripts/`、`tests/`、`prompts/`、`.orchestration/`、`.env*`。`templates/PLAN.md` 必须留在原位置（`scripts/validate_kit.py` 的 REQUIRED 清单依赖该路径）。
8. **提交作者**：仓库级占位值 `Agent Orchestration <agent@localhost>`，项目所有者已明确不更换。
9. **子代理不得自批、自合并、自我验收**；**Master 不得虚假声明已派发**（本会话早期犯过一次，见第 11 节）。

---

## 4 环境事实

| 项 | 值 |
|---|---|
| 主机 | 2 vCPU / 1.9 GiB RAM / 约 3.0 GiB swap（zram + swapfile）/ 根分区 49 G |
| 内核 | `6.12.111+deb13-amd64`（2026-10-16 由 6.12.107 升级而来） |
| 发行版 | Debian GNU/Linux 13 (trixie) |
| Python | 3.13.5（仓库根 `.python-version` = `3.13`） |
| uv | 0.13.1（`~/.local/bin/uv`） |
| PostgreSQL | 17.11，集群 17/main，库 `ajaimgr_dev` / `ajaimgr_test`，角色 `agents` peer 认证 |
| 浏览器 | **已修好**：chromium 缺的 17 个图形库已装（libnspr4/libnss3/libatk*/libgbm1/libpango/libcairo2 等），实测可加载页面 |
| 出网 | 经 Privoxy 代理（<local-proxy>）；抓取用 `curl -sS -L --compressed -A '<UA>'`，**不加 --compressed 会拿到 gzip 字节** |
| venv 策略 | 所有 worktree 共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`）；提高并发上限时必须改回每 worktree 独立 |
| 并发上限 | **1**（登记在 `ORCHESTRATION-PROJECT.md`）。派发前先确认没有子代理在跑 |
| 本机定位 | **仅编排与开发机**；生产 Profile 主机另行准备（同类 Debian，内存更多、CPU 更快，规格待真实负载测试） |

---

## 5 当前状态快照

- 主分支 `main` 基线 = `d5b7ce6`（本文档提交之前的 head），工作区干净
- 测试：`uv run pytest app/tests -q -o addopts=""` → **564 passed，0 xfailed**（全仓已无 `pytest.mark.xfail`）
- 迁移链：`0001_org_core` → `0002_identity_rbac` → `0003_permission_catalog` → `0004_audit_events` → `0005_expense_core` → `0006_expense_claim_no_seq` → `0007_workflow_outbox` → `0008_intake_files` → `0009_perm_alignment` → `0010_invoice_verifications`
- 领域目录 12 个：`app/{identity,intake,expense,workflow,documents,inventory,integrations,audit,api,db,core,runtime}`
- 最近备份：`.orchestration/backups/state-20261016-085159.sqlite3`
- **活动 worktree**：`.worktrees/W1-25`（分支 `task/W1-25-backup-restore`，head `6b19cc4`，未合并）
- 无运行中的子代理

---

## 6 立即可做的下一步：W1-25 第 1 轮修复

W1-25（备份与恢复演练）已交回并被 Master 退回修复轮，**head `6b19cc481fe8d869e880097a1e67026eae0f2d9b`，分支 `task/W1-25-backup-restore`，worktree `.worktrees/W1-25` 已存在**。直接派发新执行实例到同一 worktree（追加提交，不要 amend）。

**缺陷 A（要紧）**：`ops/backup.py` 在必需组成部分缺失时**只打印 WARNING 并以退出码 0 成功结束**。实测：在 worktree 下跑（`--repo-root` 默认取当前目录），附件根与任务库都解析到不存在的位置，归档只含 `manifest.json` 与 `db/database.sql` 共 2 个成员，`backup_rc=0`。定时备份会静默产出空归档并记为成功。
→ 要求：storage root 或 orchestration dir 缺失时以**非零退出码**失败；仅在显式给出 `--allow-missing-<component>` 时才允许继续，并在 manifest 中标记缺失项。

**缺陷 B（次要但确实不一致）**：`--orchestration-dir` 的默认值与其自身帮助文本不符——帮助写「默认 `<repo-root>/.orchestration`」，实测传 `--repo-root ~/ajaimgr` 后仍解析为 `.worktrees/W1-25/.orchestration`（取自 cwd）。
→ 要求：统一按 `--repo-root` 解析。

**已复核通过、不得回退的部分**（写进派发包）：
- 6 文件全在允许路径内（`ops/`、`docs/`、`app/tests/ops/`）；`scripts/`、`migrations/`、`tests/`、`pyproject.toml`、`uv.lock` 与 `app/` 下产品代码零改动
- worktree 无残留 `.venv`、无 `-shm`/`-wal` 旁车，`.orchestration/` 只剩 `state.sqlite3`，无残留演练库
- 恢复工具对不完整归档**正确拒绝**（实测 `restore_rc=1`，逐项列出 `orchestration_sqlite` 与 `orchestration_config` 失败）；对归档字节翻转正确报 `archive_checksum` 失败且非零退出；恢复侧 13 项校验齐全

**派发后注意**：修完后重新跑真实演练（真实库 `ajaimgr_dev` + 真实任务库 + 真实附件），并复核 worktree 里不得残留 `.venv`（上次它误生成过 126 MB 的 `.venv` 并自行删除）。

---

## 7 未完成清单

### BLOCKED（等项目所有者输入）

| ID | 内容 | 需要什么 |
|---|---|---|
| W0-3 | 金蝶接入条件调研 | 金蝶产品/版本/许可/测试账套/API 文档；决定阶段 3 全部工作 |
| W0-6 | 脱敏业务样例与现有流程盘点 | 脱敏后的真实 Excel/Word/PDF 样例、报销规则、库存流程 |

### PLANNED（可直接派发）

| ID | 内容 | 关键点 |
|---|---|---|
| W1-22 | 人工辅助查验结果录入 | W1-20 的 `ManualAssistedProvider` 永不返回「查验通过」（正确），但 W0-4 §8 允许授权财务人员经官方平台核验后录入结果进入通过。**这条录入路径不存在，人工辅助现在是死的。** 须权限门控 + 记录 actor_id 与证据 |
| W1-23 | 报销单与发票关联（`expense_invoices`） | PLAN §8 列了但未建；现在查验记录挂不到报销单上 |
| W1-24 | 模型模块导入自足性 | 声明外键的模型模块未导入被引用表所在模块（如 `integrations/models.py` 的 FK 指向 `source_files`，定义在 `app/documents/models.py`），单独导入该模块时 `configure_mappers` 失败。当前靠 `migrations/env.py` 与 conftest 统一导入规避 |
| W1-26 | 受理解析流水线（OCR/ASR 可替换接口 + 人工录入兜底） | 阶段 1 要求「文字/语音/图片/文件接收与解析草稿」；现只有受理与草稿数据模型，无解析器。**OCR/ASR 供应商需所有者决定**，先做接口 + 兜底 |
| W1-16 | 迁移可复现性：`0003` 读活动代码而非冻结快照 | 全新库跑到 0003 即写入当前目录（现 20/32），0009 对全新库是 no-op。目录再变时 0003 行为会追溯改变 |

### IN_PROGRESS

| ID | 内容 |
|---|---|
| W1-25 | 备份与恢复演练 —— **第 1 轮修复，见第 6 节** |

### 阶段 1 尚未开工的两块

- **微信消息路由与统一输入格式**：依赖 W0-1 结论，且真实验收需要所有者授权在本机建 Profile
- **OCR/ASR 解析**：见 W1-26

---

## 8 已完成与已验收（34 条 VERIFIED，不要重做）

按波次：

- **准备**：TASK-0001 仓库骨架、0002 章程、0003 配置层、0004 规则包入库、0006 事实更正、0007 所有者决定记录、0008 开发环境、0009 解释器固定
- **调研**：W0-1 微信、W0-2 Profile 隔离、W0-4 发票查验渠道、W0-5 数据合规、W0-7 多 Profile 实测
- **Wave 1**：W1-1 后端骨架与迁移 → W1-2/3/4 身份与权限 → W1-5 审计（数据库层只增不改）→ W1-6/7 报销域与状态机 → W1-8 工作流与事务性 Outbox → W1-9 受理与文件服务 → W1-10 PDF/XLSX 导出 → TASK-0011 部署要求文档 → W1-11 API 层 → W1-12 最小管理后台 → W1-13 权限目录与 PLAN §9 对齐 → W1-14 端到端负面测试 → W1-17 输入校验修复 → W1-18 后台拒绝审计 → W1-19 渠道身份路由 → W1-20 发票查验 Provider 与腾讯云适配器 → W1-21 渠道身份拒绝审计 → W1-15 Profile 生成器与校验器

**值得知道的验收结论**：
- 越权矩阵 70 格逐格断言通过；主管不能审批自己的报销；状态机 49 种组合穷举
- 并发：两个真实连接抢 Outbox，`SKIP LOCKED` 语义正确
- 幂等：重复提交/审批/付款不产生第二条记录；发票查验 5 次只产生 1 行，第 6 次被当日限次拒绝
- 路径安全：7 种穿越输入（含符号链接逃逸）全拒；类型伪装双向拒；超限先拒后不落盘
- 渠道身份：6 种拒绝对外 1 种类型 1 种文案，全表搜索真实渠道 ID 零命中

---

## 9 已知缺陷、设计取舍与开放决策（最容易丢的一节）

### 9.1 已修复的缺陷（W1-14 报出，供追溯）

1. 非法数值金额返回 500 而非 422 → W1-17 修复（服务层先行校验 + IntegrityError 兜底映射）
2. `description` 无长度上限 → W1-17 修复（现 1–500 字符）
3. 超精度金额响应与库不一致（响应 1.234 / 库 1.23）→ W1-17 修复（入库前量化）
4. 后台页面拒绝访问不写审计 → W1-18 修复
5. 渠道身份路由完全未实现 → W1-19 实现

### 9.2 仍然存在的取舍与限制（不要当成缺陷重新修）

- **`authorized` scope 是占位实现**：`app/identity/authorization.py` 的 `visible_employee_ids` 对 `authorized` 只返回自身；显式授权表尚未实现。所有 PLAN §9 里写「按授权」的单元格因此**未落地为三元组**（见 `app/identity/permissions.py` 的逐行对照表）。不得假装已支持其语义。
- **单条越权返回 403 而非 404**，会泄露「该 ID 存在」。要防枚举应改 404——**这是待所有者决策的产品项**。
- **所有 `IntegrityError` 映射为 422/409**（`app/api/errors.py`）：真实服务端 bug 若以 IntegrityError 形式出现会被报成客户端错误。作为兜底接受，但降低了错误归因精度。
- **无效会话不写审计**：`AdminAuthRequired`（未认证流量）刻意不写业务审计表，避免对只增不改表的无界写入放大；针对未认证探测的安全监控应落在应用日志。
- **`sniff_mime` fail-closed**：扩展名与魔数不符一律拒绝（比「仅按魔数检测」更严），非常见扩展名也拒。偏严格但安全。
- **XLSX 单元格边界允许一次显式 `float()`**（openpyxl 不支持 Decimal）；数据库与 PDF 侧全程 Decimal。已加注释并有测试锁定（0.125 → 0.13 证明未走浮点）。
- **PDF 中文用 reportlab 内置 CID 字体 `STSong-Light`**，不依赖外部字体文件。
- **`resolve_principal` 在拒绝时会 commit 调用方会话**（与 `admin._record_denial` 同模式），调用方须把「拒绝」视为事务边界。
- **渠道身份拒绝审计的盐**：优先 `AJAIMGR_IDENTITY_HASH_SALT`，回落到内部令牌；两者都没有时**只记 reason 不记身份标识**（宁可少记也不写明文）。
- **后台创建的绑定会写 `verified_at`**（W1-19 修复）；升级前已存在的绑定 `verified_at` 为空，**需重新绑定才能被解析**。
- **`0003_permission_catalog` 读活动代码**，迁移不是可复现的冻结快照（见 W1-16）。
- **模型模块导入不自足**（见 W1-24）。
- **Outbox 只用应用侧时钟**（`datetime.now(UTC)`）判断 `available_at`；多机部署需 NTP 或改用数据库时钟。
- **`W1-8` 只交付 service 原语**，没有常驻 Worker 主循环；`process_event` 仅处理 `approval.requested`；单步流程。
- **API 层的身份来源仍是过渡方案**：`X-Employee-Id` 由调用方注入，服务端无法独立复核其来源。正确形态是工具代理调用 `resolve_principal` 后注入，或改为签名断言。
- **管理后台登录 = 内部令牌 + 操作员自报员工号**（浏览器路径没有渠道绑定可依据）；信任边界是服务器端内部令牌。**生产必须接入公司 SSO/账号体系**，已写入 `admin_auth.py` docstring。
- **微信通道**：W0-1 结论为「有条件 Go」，11 项待实测（多员工私聊、主动通知、限流、断线恢复等）。

### 9.3 需要项目所有者决策的事项

1. **金蝶**：产品/版本/许可/测试账套/API 文档（W0-3，阻塞阶段 3）
2. **脱敏业务样例**：真实单据样例与现有流程（W0-6）
3. **OCR/ASR 供应商**：决定 W1-26 的适配器方向（接口与人工兜底可先做）
4. **微信接入授权**：是否允许在本机建 Profile 做真实路由验收
5. **单条越权 403 vs 404**：防枚举取舍
6. **`description` 长度 1–500 字符**：PLAN 未规定，属引入的默认值
7. **生产 Profile 主机规格**：需真实活动负载测试（章程 O1）
8. **腾讯云发票查验的账号/费用/合规确认**：见 W0-4 §7 的 7 条待确认项
9. **生产备份必须存放于生产主机之外**：目前演练是同机的，章程成功标准尚未满足

---

## 10 操作手册（命令级）

### 派发一个任务的完整流程

```bash
cd ~/ajaimgr
# 1) 登记（只在首次）
python3 scripts/orchestrator.py task add --id <ID> --title "..." --description "..." \
  --paths <允许路径...> --depends <依赖> --acceptance "..." "..."
# 2) 正规路径（不得用 status 子命令跳状态，会绕过依赖与路径冲突检查）
python3 scripts/orchestrator.py task ready <ID>
python3 scripts/orchestrator.py task dispatch <ID>      # 获取路径锁
python3 scripts/orchestrator.py task status <ID> IN_PROGRESS --note "..."
python3 scripts/orchestrator.py task workspace <ID> --branch task/<ID>-<short> \
  --worktree .worktrees/<ID> --note "基线 main head <sha>"
git worktree add .worktrees/<ID> -b task/<ID>-<short>
# 3) 用 delegate_task 派发（背景模式），派发包必须自包含
```

**状态机**（`task status` 只接受这些转移）：
```
PLANNED → READY → DISPATCHED → IN_PROGRESS → REVIEW_READY → MASTER_REVIEW → APPROVED → MERGED → VERIFIED
退回修复：MASTER_REVIEW → IN_PROGRESS（第 1/2/3 轮，上限 3）
BLOCKED ↔ READY / IN_PROGRESS；CANCELED 为终态
```
`MERGED` 需要 `--merge-sha`；`APPROVED` 需要 `--head-sha`；**head 一变批准自动失效**。

### 合并门（每次合并后必跑）

```bash
git merge --no-ff task/<ID>-<short> -m "merge: <ID> <标题>（task/<ID>-<short>）"
MS=$(git rev-parse HEAD)
python3 scripts/orchestrator.py task status <ID> MERGED --merge-sha $MS --note "..."
python3 scripts/validate_kit.py --project-ready    # 必须 rc=0
python3 -m unittest discover -s tests              # 必须 rc=0
python3 scripts/orchestrator.py task status <ID> VERIFIED --note "..."
git worktree remove .worktrees/<ID> --force
git branch -d task/<ID>-<short>
python3 scripts/orchestrator.py backup
```

注意：合并门里**不要**跑 `uv run pytest app/tests`——它会清空共享测试库 `ajaimgr_test`，若同时有子代理在跑会互相干扰。

### 复核一个交付（Master 必做）

不要采信子代理的自述。至少要：
1. 查范围：`git show --name-status --format='' HEAD` 与受保护路径 diff
2. 自己跑关键命令并取**真实退出码**（不要用 `| head` 之类的管道，会吃掉退出码）
3. **自己写一个独立脚本复现核心断言**（这是本会话发现最多问题的环节）
4. 读关键实现文件，确认安全模型没有被放宽

### 其他

```bash
python3 scripts/orchestrator.py task list            # 台账
python3 scripts/orchestrator.py task show <ID>       # 单条详情（含验收标准）
python3 scripts/orchestrator.py task unlock <ID>     # 释放路径锁
python3 scripts/orchestrator.py backup               # 备份任务库
psql -d ajaimgr_test -c "\d <table>"                 # 看表结构
```

---

## 11 坑与教训（本会话真实踩过）

1. **虚假派发**：早期曾在台账把任务置为 DISPATCHED/IN_PROGRESS 却没真正启动子代理，汇报里写了「已在后台执行」。核实方法：`delegate_task(action='list')` 看有没有真的在跑。**派发后必须核实。**
2. **模型响应超时导致子代理中断**：本机 06:05 重启过一次（项目所有者做的系统升级 + 重启，非故障），吃掉了两轮执行。派发包过长会提高超时概率——**包要紧凑**。中断后先查 worktree 有无未提交改动，再决定是保留还是重置。
3. **我的验证脚本会往共享测试库写残留数据**，导致后续 pytest 的 teardown 外键失败。W1-14 已加固 conftest（对 21 张业务表 `TRUNCATE ... RESTART IDENTITY CASCADE` 并重播权限目录），但**你自己写的临时脚本仍要自己清理**（演练库用完就 drop）。
4. **测试库的角色/权限表会被 pytest 清理掉**，你的临时脚本若要建员工并授角色，需先 `seed_permission_catalog(session)`。
5. **默认 TMPDIR 在 `~/.hermes/cache/scratch` 下**——跑测试时若约束是「不得写 ~/.hermes」，要显式改 TMPDIR 或确认工具自身有安全回退。
6. **`uv run` 在 worktree 里会生成 126 MB 的 `.venv`**，除非显式 `export UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`。这个目录被 `.gitignore` 覆盖但仍占磁盘，用完要删。
7. **管道会吃掉退出码**：`cmd | head` 的 `$?` 是 `head` 的。要真退出码就重定向到文件再读。
8. **用 `task status` 直接跳状态会绕过依赖与路径冲突检查**，必须走 `task ready` → `task dispatch`。曾因此绕过一次，已回退重走。
9. **并发上限 1 是资源约束，锁模型管不了它**：路径不重叠的两个任务可以同时 dispatch，但在 2 vCPU / 1.9 GiB 上跑两个执行实例不划算。派发前先看有没有子代理在跑。
10. **`grep -vE '^dir/'` 这种带 `$` 的排除式很容易写错**（目录前缀加 `$` 只匹配整行等于该前缀）。范围检查用 `git show --name-status` 直接看更可靠。
11. **子代理可能"更新"既有测试**：W1-17 因行为改变更新了两个钉住缺陷现状的测试，这是正当的，但**必须逐字检查新断言是否仍在断言正确行为**，不能只看它说"已更新并说明理由"。
12. **子代理会主动纠正上一实例的假账**：W1-13 的对照表曾把不存在的权限标为"已实现"。派发重试时要求新实例**逐条核实**旧产物，而不是假定它是对的。

---

## 12 交接检查清单（下一任开局）

- [ ] 读 `AGENT-BOOTSTRAP.md`、`AGENTS.md`、`AGENT-ORCHESTRATION.md`、`ORCHESTRATION-PROJECT.md`、`templates/PLAN.md`
- [ ] 读本文档第 3、4、9 节（约束、环境、已知取舍）
- [ ] `cd ~/ajaimgr && git log --oneline -3 && git status --short`（最新提交应是本 HANDOVER.md 的提交，其父提交为 `d5b7ce6`；工作区应干净）
- [ ] `python3 scripts/orchestrator.py task list` 核对台账与本文第 5、7 节是否一致
- [ ] `delegate_task(action='list')` 确认没有遗留的运行中子代理
- [ ] 检查 `.worktrees/W1-25` 是否仍存在（应存在，head `6b19cc4`）
- [ ] 按第 6 节派发 W1-25 的修复轮
- [ ] 如需向所有者汇报：BLOCKED 两项（金蝶、脱敏样例）+ 第 9.3 节九条决策项
