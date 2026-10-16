# HANDOVER — 会话交接文档

- 生成时间：2026-10-16 11:30 CST（**主动暂停点：GitHub 转移前**）
- 主分支基线 head：`2cd271ae555a3574810ce003dc0cb7717f943607`（**这是本文档提交之前的 head**；本文档自身的提交位于其之上，是 main 的最新提交）
- 台账：45 条任务（38 VERIFIED / 2 BLOCKED / 3 PLANNED / 2 CANCELED / 0 IN_PROGRESS）
- 上一版交接于 `d5b7ce6` 之上（见 git 历史 `2b42170`）；本版由当前会话在「W1-23 收口后暂停、准备 GitHub 转移」时点刷新。**先读本文档，再读 AGENTS.md / AGENT-ORCHESTRATION.md / ORCHESTRATION-PROJECT.md / templates/PLAN.md。**

---

## 0 怎么用这份文档

1. 第 1–5 节是必读背景（角色、约束、环境、状态）。
2. **第 6 节是当前操作点**：项目处于**主动暂停**，所有者要把仓库转移到 GitHub——含转移前检查结论、默认约定与步骤。
3. 第 7 节是全部未完成项（**队列已冻结**），第 8 节是已完成项（**不要重做**）。
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
- 项目章程：`docs/PROJECT-CHARTER.md`（O3 已记录「百度为首选查验渠道」决策）
- 部署要求：`docs/DEPLOYMENT-REQUIREMENTS.md`
- 调研报告：`docs/research/W0-1`（微信）、`W0-2`（Profile 隔离）、`W0-4`（发票查验，§10 含所有者决策增补）、`W0-5`（数据合规）、`W0-7`（多 Profile 实测）、`W1-28`（百度查验适配器）

**当前阶段**：Wave 1 主体 + **发票查验完整链条**已完成（渠道 Provider：腾讯云 W1-20、百度 W1-28；人工辅助录入 W1-22；报销单挂账 W1-23；API 层与负面测试）。阶段 1 尚缺：微信消息路由、OCR/ASR 解析流水线、备份异地存储与恢复演练收口。当前为 **GitHub 转移前的主动暂停**（见 §6）。

---

## 3 不可违反的硬约束

1. **严禁读取或复制凭据值**：`~/.hermes/auth.json`、`config.yaml` 里的凭据、token、cookie、真实账号标识。报告中一律用占位符。**不要向项目所有者索要凭据，也不要在聊天中接收凭据。**
2. **严禁在本机新建、启动、停止任何真实 Hermes Profile**（W0-7 实验曾获一次性授权，不得顺延）。不得写入 `~/.hermes/` 下任何文件。
3. **不得使用 pip**，统一 uv（`~/.local/bin/uv`）；解释器由仓库根 `.python-version` 固定 3.13。
4. **不得新增第三方依赖**，除非在派发包中由 Master 显式批准并说明理由。当前 10 个直接依赖：fastapi、uvicorn[standard]、sqlalchemy、alembic、psycopg[binary]、pydantic、python-multipart、openpyxl、reportlab、jinja2。
5. **默认 DSN 走本地 unix socket peer 认证**，任何文件里不得写入口令。
6. **不得为让测试通过而修改、删除或跳过已有测试断言**（可因行为改变而修正并说明理由）。
7. **受保护/禁改路径**：`AGENTS.md`、`MASTER.md`、`AGENT-*.md`、`ORCHESTRATION-PROJECT.md`、`README.md`、`VERSION.txt`、`LICENSE.txt`、`templates/`、`scripts/`、`tests/`、`prompts/`、`.orchestration/`、`.env*`。`templates/PLAN.md` 必须留在原位置（`scripts/validate_kit.py` 的 REQUIRED 清单依赖该路径）。
8. **提交作者**：仓库级占位值 `Agent Orchestration <agent@localhost>`（历史中另有少量按任务的占位身份，均非真实个人信息），项目所有者已明确不更换。
9. **子代理不得自批、自合并、自我验收**；**Master 不得虚假声明已派发**（核实方法见第 11 节第 1 条）。
10. **暂停期（自 2026-10-16 11:21 起）**：不派发新任务；除所有者指示的 GitHub 转移相关动作外不变更 main。恢复开发须等所有者对 §6 末尾两项表态。

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
| 出网 | 经 Privoxy 代理（<local-proxy>）；抓取用 `curl -sS -L --compressed -A '<UA>'`，**不加 --compressed 会拿到 gzip 字节**；个别域名（如 ai.baidu.com）经代理 TLS 失败，**须直连 `--noproxy '*'`** |
| venv 策略 | 所有 worktree 共享主工作树 `.venv`（`UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`）；提高并发上限时必须改回每 worktree 独立 |
| 并发上限 | **1**（登记在 `ORCHESTRATION-PROJECT.md`）。派发前先确认没有子代理在跑 |
| 本机定位 | **仅编排与开发机**；生产 Profile 主机另行准备（同类 Debian，内存更多、CPU 更快，规格待真实负载测试） |

---

## 5 当前状态快照

- 主分支 `main` head = `2cd271a`（W1-23 合并提交），工作区干净；**无活动 worktree、无任务分支**（仅 `main`）
- 测试：`uv run pytest app/tests -q -o addopts=""` → **675 passed，0 xfail**（全仓无 `pytest.mark.xfail`）
- 迁移链：`0001_org_core` → `0002_identity_rbac` → `0003_permission_catalog` → `0004_audit_events` → `0005_expense_core` → `0006_expense_claim_no_seq` → `0007_workflow_outbox` → `0008_intake_files` → `0009_perm_alignment` → `0010_invoice_verifications` → `0011_expense_invoices`
- 领域目录 12 个：`app/{identity,intake,expense,workflow,documents,inventory,integrations,audit,api,db,core,runtime}`
- 最近备份：`.orchestration/backups/state-20261016-112144.sqlite3`（W1-23 合并后）
- 无运行中的子代理；历史共 81 个提交
- GitHub 远端：**尚未配置**（转移前状态，见 §6）

---

## 6 当前操作点：GitHub 转移（主动暂停中）

**所有者指示（2026-10-16）**：本阶段在途工作完成后暂停，准备把项目转移到 GitHub。在途工作已于 W1-23 收口后全部完成；**队列冻结**（W1-24 / W1-16 / W1-26 未派发）。

**转移前检查已完成（只读，结论）**：

- 无远端配置（干净起点）；分支仅 `main`；工作区干净
- 160 个追踪文件、无大件（最大 `uv.lock` 244KB）；**无任何敏感文件名**；内容扫描无真实凭据（仅命中 `ops/_common.py` 扫描器自身的假密钥标记常量）
- 历史作者均为占位身份；`.orchestration/`、`.worktrees/`、`.venv/` 已被 `.gitignore` 排除，不会进入远端

**默认约定（待所有者确认）**：

- 仓库建议**私有**；
- `.orchestration/`（任务台账/治理状态）与 `.worktrees/` **不随迁**（属本机治理状态；如需连台账一起推由所有者决定）；
- 推送认证由所有者侧配置（SSH / gh），**Master 不接触任何令牌/密码**。

**转移步骤（所有者创建仓库并配好认证后执行）**：

```bash
git remote add origin <repo-url>
git push -u origin main
```

- 不要 force push；不要改写历史（81 个提交全部保留）；
- 推送后核对：远端 main == 本地 main；远端文件树中无 `.orchestration/`、`.worktrees/`、`.venv/`。

**恢复开发前需要所有者表态的两项**：

1. 转移后是否改协作策略（远端 PR 流程 vs 保持本机编排 + 远端只读备份）；
2. 冻结的队列（W1-24 / W1-16 / W1-26）何时继续。

---

## 7 未完成清单

### BLOCKED（等项目所有者输入）

| ID | 内容 | 需要什么 |
|---|---|---|
| W0-3 | 金蝶接入条件调研 | 金蝶产品/版本/许可/测试账套/API 文档；决定阶段 3 全部工作 |
| W0-6 | 脱敏业务样例与现有流程盘点 | 脱敏后的真实 Excel/Word/PDF 样例、报销规则、库存流程 |

### PLANNED（冻结，恢复后可直接派发）

| ID | 内容 | 关键点 |
|---|---|---|
| W1-24 | 模型模块导入自足性 | 声明外键的模型模块未导入被引用表所在模块（如 `integrations/models.py` 的 FK 指向 `source_files`，定义在 `app/documents/models.py`），单独导入该模块时 `configure_mappers` 失败。当前靠 `migrations/env.py` 与 conftest 统一导入规避。（W1-23 后 `app/expense/models.py` 也会拉入 `integrations.models`，仍未根治） |
| W1-26 | 受理解析流水线（OCR/ASR 可替换接口 + 人工录入兜底） | 阶段 1 要求「文字/语音/图片/文件接收与解析草稿」；现只有受理与草稿数据模型，无解析器。**OCR/ASR 供应商需所有者决定**，先做接口 + 兜底 |
| W1-16 | 迁移可复现性：`0003` 读活动代码而非冻结快照 | 全新库跑到 0003 即写入当前目录（现 20/32），0009 对全新库是 no-op。目录再变时 0003 行为会追溯改变 |

### 阶段 1 尚未开工的两块

- **微信消息路由与统一输入格式**：依赖 W0-1 结论，且真实验收需要所有者授权在本机建 Profile
- **OCR/ASR 解析**：见 W1-26

### W1-25 已验收但遗留

- **跨主机/异地恢复演练未做**（生产验收必需项，章程成功标准之一）；同机恢复演练已通过（13/13 项校验）；`--allow-missing-*` 误用边界见 `docs/BACKUP-RESTORE.md` §10

---

## 8 已完成与已验收（38 条 VERIFIED，不要重做）

- **准备**：TASK-0001 仓库骨架、0002 章程、0003 配置层、0004 规则包入库、0006 事实更正、0007 所有者决定记录、0008 开发环境、0009 解释器固定
- **调研**：W0-1 微信、W0-2 Profile 隔离、W0-4 发票查验渠道、W0-5 数据合规、W0-7 多 Profile 实测
- **Wave 1**：W1-1 后端骨架与迁移 → W1-2/3/4 身份与权限 → W1-5 审计（数据库层只增不改）→ W1-6/7 报销域与状态机 → W1-8 工作流与事务性 Outbox → W1-9 受理与文件服务 → W1-10 PDF/XLSX 导出 → TASK-0011 部署要求文档 → W1-11 API 层 → W1-12 最小管理后台 → W1-13 权限目录与 PLAN §9 对齐 → W1-14 端到端负面测试 → W1-17 输入校验修复 → W1-18 后台拒绝审计 → W1-19 渠道身份路由 → W1-20 发票查验 Provider 与腾讯云适配器 → W1-21 渠道身份拒绝审计 → W1-15 Profile 生成器与校验器
- **本会话增量（全经 Master 独立复核后合并）**：
  - **W1-25** 备份与恢复演练——第 1 轮修复收口（缺失必需组件非零退出 rc=2、仅 `--allow-missing-*` 放行、默认路径按 `--repo-root` 解析；真实演练 备份 rc=0 / 恢复 13/13 / 篡改 rc=1）
  - **W1-28** 百度增值税发票验真适配器（`provider_name=baidu_cloud/vat_invoice_verification`；**所有者决策：百度为首选渠道**，腾讯云与聚美智数降为备用，无渠道选择开关；凭据 env-only fail closed）
  - **W1-22** 人工辅助查验结果录入（`POST /invoice-verifications/manual`；权限门控 + 证据必填 + 无明文审计；含第 1 轮修复 F1：既有行被采纳为通过时清空被取代的 `error_code`/`reason`）
  - **W1-23** 报销单与发票关联 `expense_invoices`（0011 迁移；`verification_status` 只来源于查验记录、调用方不可指定；同单唯一约束 + 跨单 `suspected_duplicate` 双标记与查询；`submit()` 挂账校验：verified_fail/未知状态阻断，pending 放行且不改写）

**值得知道的验收结论（累计）**：

- 越权矩阵 70 格逐格断言通过；主管不能审批自己的报销；状态机 49 种组合穷举
- 并发：两个真实连接抢 Outbox，`SKIP LOCKED` 语义正确
- 幂等：重复提交/审批/付款不产生第二条记录；发票查验 5 次只产生 1 行，第 6 次被当日限次拒绝
- 路径安全：7 种穿越输入（含符号链接逃逸）全拒；类型伪装双向拒；超限先拒后不落盘
- 渠道身份：6 种拒绝对外 1 种类型 1 种文案，全表搜索真实渠道 ID 零命中
- W1-23 独立探针 32/32（含 verified_fail 阻提交 → 翻转 verified_pass 后放行且 live 同步；拒绝审计独立连接读回）；W1-22 探针 15/15；W1-28 探针 15/15（含百度文档 sha256 逐字节比对）

---

## 9 已知缺陷、设计取舍与开放决策（最容易丢的一节）

### 9.1 已修复的缺陷（供追溯）

1. 非法数值金额返回 500 而非 422 → W1-17 修复（服务层先行校验 + IntegrityError 兜底映射）
2. `description` 无长度上限 → W1-17 修复（现 1–500 字符）
3. 超精度金额响应与库不一致（响应 1.234 / 库 1.23）→ W1-17 修复（入库前量化）
4. 后台页面拒绝访问不写审计 → W1-18 修复
5. 渠道身份路由完全未实现 → W1-19 实现
6. 备份工具在必需组件缺失时**静默成功**（空归档 rc=0）→ W1-25 第 1 轮修复（rc=2 fail-closed）
7. W1-22 人工录入采纳既有行时行内状态自相矛盾（`verified_pass` 行残留被取代的 `error_code`/`reason`）→ 第 1 轮修复（清空 + 置 `manual_verification_recorded`）

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
- **`expense_invoices`（W1-23）**：`suspected_duplicate` 为 Master 授权新增列（PLAN 列集之外，承载验收第 2 条）；`attach_invoice` 不校验 `source_file_id` 是否存在（FK 兜底，与既有服务同风格）；同单重复的并发竞态以 `IntegrityError` 失败（唯一约束兜底，可重试）；**尚无 HTTP 端点**（域层完成，app/api 未接）。
- **`invoice_verifications` 不在 conftest 的 TRUNCATE 清单**：由各模块（W1-20/22/23）自清理，套件跑完残留实测为 0；如需更稳可补清单（未做，留待决定）。
- **W1-22 人工录入**：缺证据不写拒绝审计（按规格「不落任何状态变更」字面执行）；对 `verified_fail` 行的重复录入冲突用 409 `invalid_transition`；证据为调用方声明（审计存证，渠道侧不参与）。

### 9.3 需要项目所有者决策的事项

1. **金蝶**：产品/版本/许可/测试账套/API 文档（W0-3，阻塞阶段 3）
2. **脱敏业务样例**：真实单据样例与现有流程（W0-6）
3. **OCR/ASR 供应商**：决定 W1-26 的适配器方向（接口与人工兜底可先做）
4. **微信接入授权**：是否允许在本机建 Profile 做真实路由验收
5. **单条越权 403 vs 404**：防枚举取舍
6. **`description` 长度 1–500 字符**：PLAN 未规定，属引入的默认值
7. **生产 Profile 主机规格**：需真实活动负载测试（章程 O1）
8. **查验渠道正式开通**：百度账号/费用/合规确认（首选已落地为适配器，正式开通尚待办；腾讯云/聚美智数为备用）
9. **生产备份必须存放于生产主机之外**：目前演练是同机的，章程成功标准尚未满足
10. **GitHub 转移默认约定**：私有仓库 / `.orchestration/` 与 `.worktrees/` 不随迁 / 转移后的协作策略（见 §6）

---

## 10 操作手册（命令级）

### 派发一个任务的完整流程

```bash
cd ~/ajaimgr
# 1) 登记（只在首次；注意 --acceptance 是重复标志，逐条写，登记后用 task show 核对条数）
python3 scripts/orchestrator.py task add --id <ID> --title "..." --description "..." \
  --paths <允许路径...> --depends <依赖> --acceptance "..." "..."
# 2) 正规路径（不得用 status 子命令跳状态，会绕过依赖与路径冲突检查）
python3 scripts/orchestrator.py task ready <ID>
python3 scripts/orchestrator.py task dispatch <ID>      # 获取路径锁
python3 scripts/orchestrator.py task status <ID> IN_PROGRESS --note "..."
python3 scripts/orchestrator.py task workspace <ID> --branch task/<ID>-<short> \
  --worktree .worktrees/<ID> --note "基线 main head <sha>"
git worktree add .worktrees/<ID> -b task/<ID>-<short>
# 3) 用 delegate_task 派发（背景模式），派发包必须自包含、紧凑；派发后核对真的在跑
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
3. **自己写一个独立脚本复现核心断言**（这是历次发现最多问题的环节）；探针要**指向被测 worktree 的代码路径**（未合并模块在 main 里不存在）
4. 读关键实现文件，确认安全模型没有被放宽；子代理"更新"既有测试的，逐字检查新断言强度

### 其他

```bash
python3 scripts/orchestrator.py task list            # 台账
python3 scripts/orchestrator.py task show <ID>       # 单条详情（含验收标准）
python3 scripts/orchestrator.py task unlock <ID>     # 释放路径锁
python3 scripts/orchestrator.py backup               # 备份任务库
psql -d ajaimgr_test -c "\d <table>"                 # 看表结构
```

---

## 11 坑与教训

1. **虚假派发**：早期曾在台账把任务置为 DISPATCHED/IN_PROGRESS 却没真正启动子代理，汇报里写了「已在后台执行」。核实方法：`delegate_task(action='list')` 看有没有真的在跑。**派发后必须核实。**
2. **模型响应超时导致子代理中断**：本机 06:05 重启过一次（所有者做的系统升级 + 重启，非故障），吃掉了两轮执行。派发包过长会提高超时概率——**包要紧凑**。中断后先查 worktree 有无未提交改动，再决定是保留还是重置。
3. **验证脚本会往共享测试库写残留数据**，导致后续 pytest 的 teardown 外键失败。conftest 已对 22 张业务表 `TRUNCATE ... RESTART IDENTITY CASCADE` 并重播权限目录，但**你自己写的临时脚本仍要自己清理**（演练库用完就 drop），并在结束时用独立连接复核残留为 0。
4. **测试库的角色/权限表会被 pytest 清理掉**，临时脚本若要建员工并授角色，需先 `seed_permission_catalog(session)`。
5. **默认 TMPDIR 在 `~/.hermes/cache/scratch` 下**——跑测试时若约束是「不得写 ~/.hermes」，要显式改 TMPDIR 或确认工具自身有安全回退。
6. **`uv run` 在 worktree 里会生成 126 MB 的 `.venv`**，除非显式 `export UV_PROJECT_ENVIRONMENT=~/ajaimgr/.venv`。这个目录被 `.gitignore` 覆盖但仍占磁盘，用完要删。
7. **管道会吃掉退出码**：`cmd | head` 的 `$?` 是 `head` 的。要真退出码就重定向到文件再读。
8. **用 `task status` 直接跳状态会绕过依赖与路径冲突检查**，必须走 `task ready` → `task dispatch`。
9. **并发上限 1 是资源约束，锁模型管不了它**：路径不重叠的两个任务可以同时 dispatch，但在 2 vCPU / 1.9 GiB 上跑两个执行实例不划算。派发前先看有没有子代理在跑。
10. **`grep -vE '^dir/'` 这种带 `$` 的排除式很容易写错**。范围检查用 `git show --name-status` 直接看更可靠。
11. **子代理可能"更新"既有测试**：因行为改变更新钉住缺陷现状的测试是正当的，但**必须逐字检查新断言是否仍在断言正确行为**。W1-23 对 `test_migration_0010_roundtrip` 的适配（改显式 revision + 动态 head）经逐行检查确认强度未降。
12. **子代理会主动纠正上一实例的假账**：W1-13 的对照表曾把不存在的权限标为"已实现"。派发重试时要求新实例**逐条核实**旧产物。
13. **`task add --acceptance` 是重复标志，CLI 只保留最后一条**：W1-27 曾因一次写 6 条只留 1 条而取消重登记（W1-28）。登记后用 `task show` 核对验收条数。
14. **复核探针必须指向被测 worktree 的代码**：未合并的模块（如当时未合并的 `app/expense/invoices.py`）在 main 里不存在，探针 `sys.path` 指错会直接 ImportError。
15. **新增业务表要同步 conftest 的 `BUSINESS_TABLES` 清单**（W1-23 已加入 `expense_invoices`）；`invoice_verifications` 仍未入列（靠模块自清理），新增涉及它的任务要留意清理责任。

---

## 12 恢复/交接检查清单（下一任开局）

- [ ] 读 `AGENT-BOOTSTRAP.md`、`AGENTS.md`、`AGENT-ORCHESTRATION.md`、`ORCHESTRATION-PROJECT.md`、`templates/PLAN.md`
- [ ] 读本文档第 3、4、6、9 节（约束、环境、暂停点、已知取舍）
- [ ] `cd ~/ajaimgr && git log --oneline -3 && git status --short`（最新提交应是本 HANDOVER.md 的提交，其父提交为 `2cd271a`；工作区应干净；分支仅 `main`）
- [ ] `python3 scripts/orchestrator.py task list` 核对台账与本文第 5、7 节是否一致（45 条：38 VERIFIED / 2 BLOCKED / 3 PLANNED / 2 CANCELED）
- [ ] `delegate_task(action='list')` 确认没有遗留的运行中子代理
- [ ] 若继续 GitHub 转移：按 §6 核对远端与推送结果（私有、无 `.orchestration/`/`.worktrees/` 泄漏）
- [ ] 若恢复开发：先取得所有者对 §6 末尾两项（协作策略、队列恢复）的表态，再从 W1-24 / W1-16 / W1-26 继续；BLOCKED 两项（W0-3、W0-6）与 §9.3 决策项在汇报中列出
