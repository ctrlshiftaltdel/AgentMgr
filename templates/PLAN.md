# 中国大陆企业：Hermes Agent 原生管理系统实施方案（修订版）

版本：v2.1（2026-10-17 修订：取消金蝶接入，对外账务交接改为模板驱动的 Excel 导出；远期自研财务/ERP）  
适用规模：20–100 名员工  
首期模块：报销  
后续优先模块：进销存/资产台账  
运行方式：每位员工独立 Hermes Profile 与独立进程；共享通讯接入和业务后端  
部署范围：中国大陆  

---

## 1. 项目定位

建设一套以 AI Agent 为主要工作入口的企业管理系统。系统不以传统网页菜单为中心，而是让员工通过自然语言、语音、图片、文件、表格和其他后续接入渠道发起业务；Agent 理解意图、收集信息、整理材料、调用受控业务工具，并在需要时生成标准化文档或表格。

系统主要解决：

- 公司过去依靠 Excel、Word、PDF、聊天记录等分散记录业务的问题；
- 报销、采购、库存、审批和通知缺少统一状态与审计的问题；
- 让员工 Agent 与主管/财务/仓储等角色的 Agent 能在受控工作流内协作；
- 在**不绑定特定第三方财务系统**的前提下，把 AI 对话、原始资料、业务流程与外部账务交接标准化（当前为模板驱动的 Excel 导出）；
- 后续增加企业微信、钉钉、飞书、邮件、网页、API 或文件导入等渠道时，不重写业务模块。

### 1.1 不变的强制原则

1. **每位员工一个 Hermes Profile、一个独立运行进程及独立配置/记忆/会话/技能上下文。** 不同员工不能共用同一 Profile。
2. 通讯渠道与 Agent 进程解耦。一个渠道适配器可以服务多个员工，但必须将每条消息路由到正确的员工 Profile。
3. 正式业务数据写入统一业务数据库；Agent 记忆只用于对话连续性，不能代替正式记录。
4. Hermes 不直接持有数据库管理员权限，不提供任意 SQL 或任意服务器命令作为普通业务工具。
5. 所有读取、写入、审批、导出、文件下载和外部同步都由后端执行权限验证并写入审计记录。
6. 模型可提取、解释、建议和生成草稿；金额计算、库存计算、会计校验、审批状态转换和付款状态必须由确定性程序/外部系统驱动。
7. 普通微信是首个渠道，不是系统唯一或永久绑定的渠道。
8. 中国大陆是首期唯一目标运行范围。数据位置、云端模型供应商、税务规则和外部接口按大陆使用条件设计。

## 2. 总体架构

```text
输入/输出渠道
微信 | 未来企业微信/钉钉/飞书 | Web | 邮件 | API | 文件导入
                |
                v
     Channel Adapters / 通讯适配层
        统一消息格式、重试、去重
                |
                v
   身份绑定与消息路由 Identity Router
   校验外部身份 -> 内部员工 -> Profile
                |
                v
     每员工独立 Hermes Profile 进程
     独立配置、会话、记忆、工作目录、工具授权
                |
                v
       受控工具代理 Agent Tool Proxy
       不信任模型自报身份；请求带受验证的上下文
                |
                v
         企业业务后端 API
  员工/权限 | 收件与解析 | 报销 | 工作流 | 进销存
  文档服务 | 财务导出与对账 | 发票查验适配器 | 审计/通知
           |                         |
           v                         v
      PostgreSQL              附件与文档存储
           |
           v
  事务性 Outbox / 后台任务 / 同步重试 / 审计
```

### 2.1 通讯适配层与 Profile 的分工

不要默认在每个员工 Profile 内分别登录同一个微信 Bot。首期采用共享的渠道接入/路由服务：

1. 渠道适配器收取消息并保留渠道事件 ID、外部用户 ID、会话 ID、时间和附件引用。
2. 身份服务验证该渠道用户是否已经绑定公司员工账号。
3. 路由服务确定目标员工 Profile，并把消息转给对应的本地 Hermes API/进程入口。
4. Agent 工具请求由代理附加经过验证的操作主体和请求上下文；不能依靠模型自行填入的 `employee_id`。
5. 回复和通知通过渠道适配层发回对应员工的已绑定身份。

每个 Profile 的 API 服务仅监听 localhost 或 Unix socket，不直接暴露公网。若官方接入模式不支持多员工路由，应由单独的适配/路由服务实现，不得假定 Hermes 原生就支持任意多租户路由。

### 2.2 每位员工的隔离边界

每个员工至少单独拥有：

- Hermes Profile 及运行进程；
- `config.yaml`、角色指令、技能清单及模型配置；
- 会话历史、记忆命名空间与状态数据；
- 工作目录和临时文件目录；
- 独立的内部 API 身份/短期调用凭据；
- 独立工具授权集合和访问审计上下文。

目录权限至少限于该 Profile 对应的服务身份。不能仅靠提示词或文件路径约定隔离文件。部署 Agent 进程时采用非 root 身份；由平台 Agent 评估 systemd 独立服务账户、Linux namespace 或容器隔离。共享路由服务只拥有路由所需权限，不能随意读取所有 Profile 的记忆。

20–100 个常驻进程需要在阶段 0 进行负载测试。若内存成本过高，可优化模型调用与进程资源，但不能未经用户批准把架构改成所有员工共用同一个 Profile。

## 3. 面向中国大陆的技术选型

| 层 | 推荐选型 | 原则 |
|---|---|---|
| Agent | Hermes Agent Profile | 固定经过验证的版本；逐人配置与运行 |
| 微信 | Hermes Weixin/iLink 适配器 + 自建身份路由 | 先验证真实多员工场景、主动通知与账号限制 |
| 未来渠道 | 独立 Channel Adapter 插件 | 渠道变化不能影响报销/库存域服务 |
| 业务 API | Python + FastAPI | 模块化单体，按领域划分接口 |
| 数据校验 | Pydantic 等 schema 校验 | 所有模型输出与外部输入先做结构校验 |
| ORM/迁移 | SQLAlchemy + Alembic | 数据库结构变更可审查、可回滚/前滚 |
| 生产数据库 | PostgreSQL | 适合多进程并发、事务、约束和后续行级安全 |
| 文件/表格解析 | 独立解析流水线 | 解析结果先进入草稿区，不直接成为正式账务记录 |
| 文件存储 | 初期受控本地存储；后续可迁移到兼容 S3 的对象存储 | 与数据库分别备份，元数据保存在数据库 |
| 后台任务 | PostgreSQL Transactional Outbox + Worker | 通知/同步失败可重试，不产生重复业务操作 |
| 文档生成 | DOCX/XLSX/PDF 模板服务 | 模板版本可追踪，正式文档保留来源记录 |
| Web 管理界面 | React + TypeScript 或轻量管理前端 | 主要用于审核、查询、权限、配置和审计，不强迫日常工作都在 UI 完成 |
| 服务管理 | Linux systemd；之后按隔离与部署需要再决定容器 | 开发早期不要求先引入 Docker |

**数据库建议：**本地单元测试可以使用 SQLite，但由于目标是 20–100 人、每人独立进程、共享业务数据，并且后续涉及库存和财务，生产环境建议从第一阶段直接使用 PostgreSQL，避免把 SQLite 迁移作为正式上线前的额外风险。

### 3.1 中国大陆数据处理要求

- 优先把业务数据库、附件、备份和运行日志部署在中国大陆可控环境。
- 云端模型默认选择能够明确提供大陆服务区域和数据处理条款的供应商/接口；如果考虑境外模型或境外处理，先进行数据合规评估，不把跨境传输视为默认许可。
- 按任务需要最小化传给模型的字段。对身份证号、银行卡号、个人联系方式及票据中不必要的敏感字段进行遮蔽或去标识化。
- 与模型及外部服务供应商确认数据用途、留存期限、是否用于训练、访问控制、处理区域和删除机制。
- 建立数据分类、授权、保留/删除、事件响应和审计机制。具体合规义务由公司结合实际业务和专业法律意见确认。

## 4. Agent 原生的工作方式

### 4.1 支持的输入

- 微信文本、语音消息、图片和文件；
- 发票、收据、合同、送货单、采购单等图片/PDF；
- Excel/CSV 台账与清单；
- Word/PDF 说明或已有业务记录；
- Web 页面上传、内部 API 和未来接入的其他通讯渠道；
- 定时任务及业务事件生成的通知。

### 4.2 统一资料接收流水线

```text
接收原始资料
   -> 身份/权限验证
   -> 文件类型、大小、安全扫描与去重
   -> OCR/语音转写/表格或文档解析
   -> 形成结构化草稿 + 字段置信度 + 原件引用
   -> Agent 向用户追问或要求确认
   -> 后端校验业务规则
   -> 用户/授权人员提交或审批
   -> 写入正式业务表并生成审计事件
```

原始文件不得被解析结果覆盖。每个提取值都应能追溯到来源文件、页码/区域（可用时）、解析器版本和确认人。低置信度或关键字段缺失时，应要求人工确认。

### 4.3 输出

- 普通对话答复和业务状态查询；
- DOCX/PDF 报销单、审批单、通知、凭证或汇总报告；
- XLSX/CSV 明细、库存盘点表和财务汇总；
- 定时消息、待办提醒和异常告警；
- 受权网页查看链接或管理后台记录；
- 后续可扩展语音播报、企业知识库检索和其他渠道推送。

### 4.4 Agent 之间的协作

员工 Agent 不直接随意调用主管 Agent 或财务 Agent 的内部会话。Agent 间协作通过业务工作流和任务路由服务完成：

1. 员工 Agent 创建业务草稿并请求提交。
2. 后端创建审批任务，按部门、额度和规则确定审批人。
3. 路由服务向审批人的个人 Profile/已绑定渠道发送带任务编号的通知。
4. 主管 Agent 读取授权范围内的任务内容，提示主管批准、拒绝或要求补充资料。
5. 后端验证实际操作人的权限与工作流当前状态后执行转换。
6. 结果写入审计和通知队列，再反馈给申请人。

Agent 间可共享的是授权业务记录与任务信息，不是各自的私人记忆目录。通知不等于审批，模型口头声称成功也不等于后端已提交成功。

## 5. 业务模块边界

### 5.1 平台公共模块

- 公司、部门、员工、主管关系；
- 外部渠道身份绑定/解绑；
- Hermes Profile 生命周期及状态监控；
- 角色、权限、数据范围；
- 文件收件与解析；
- 统一业务 API 和工具代理；
- 工作流、任务、通知和重试；
- 文档模板与生成服务；
- 操作审计、备份和恢复；
- 财务导出、对账与发票查验框架。

### 5.2 报销模块（第一期）

- 报销草稿、费用明细、费用类型、币种、成本中心；
- 票据附件、OCR 字段提取、发票查验状态；
- 费用政策、金额校验、重复申请/重复票据检查；
- 提交、撤回、审批、退回补件、拒绝；
- 财务复核、付款状态记录、报销单 PDF 与明细 XLSX；
- 为财务导出与对账提供稳定的数据投影（见 §6）。

### 5.3 进销存与资产台账（第二期）

- 商品/SKU、单位、分类、条码（可选）；
- 供应商、客户、仓库、库位；
- 采购申请、采购订单、采购入库、采购退货；
- 销售订单、销售出库、销售退货；
- 库存移动、库存余额、批次/序列号（仅在业务需要时）；
- 库存盘点、差异调整、低库存提醒；
- 资产领用、归还和责任人记录（固定资产折旧等可后续单独规划）；
- 采购/销售/库存相关审批、文档和导入导出。

库存不能只依靠 Agent 记忆或 Excel 文件维护。每次入库、出库、退货和盘点调整都必须形成正式库存流水。若外部财务/库存系统也管理库存，阶段 0 必须明确哪个系统是某类库存记录的唯一权威来源，禁止两边各自独立维护相同库存余额。

### 5.4 财务导出与对账（第三期）

核心原则是**不对接金蝶或任何特定第三方财务系统，也不自行重复开发完整会计引擎**。内部系统负责对话入口、原始资料、审批和业务协同；对外账务交接采用**模板驱动的 Excel 导出**，由外部财务系统导入。导出是只读投影：不生成会计分录、不自动过账、不写外部系统。

分模块推进：

- 导出模板契约：`template_id` + 版本 + 文件 SHA-256、列映射、期间/币种/单据号口径、合计行规则；
- 导出范围：单张报销单与按期间批量，粒度由模板区分；
- 导出前校验与对账：行数、笔数、金额合计（到分）与数据库一致，不一致 fail closed，不产出文件；
- 每次导出生成 manifest（期间、模板版本、记录集合、行数、金额合计、文件哈希、导出人与时间）；
- 导出审计、下载鉴权与保留期；
- 历史数据与期初余额迁移、内部账务能力（科目、凭证、总账）**不在第三期范围**，属远期计划。

每个财务领域必须在决策记录中指定唯一权威源：内部系统是业务流程权威源；导出文件被外部系统导入后，账务权威源为外部系统。禁止同一期间「导出导入」与「内部记账」两份账并存。

## 6. 财务导出设计（模板驱动）

系统不对接特定第三方财务系统。对外交接通过按模板生成的 `.xlsx` 完成；模板由项目所有者提供，模板未提供前**不实现具体列，也不猜测目标系统格式**。

### 6.1 模板契约

每个模板声明：

- `template_id` 与 `template_version`：任何列或口径变化必须升版，历史版本只读；
- 模板文件 SHA-256：与版本一起记录，保证可复现；
- 列映射：内部字段 → 列、列序、表头、数据起始行、日期与金额格式；
- 口径：期间定义、币种、单据号规则、借贷方向（如适用）、合计行规则；
- 必填列与允许为空的条件。

模板文件存放于仓库外的受控目录；仓库内只保留映射定义、合成样例与校验用例。

### 6.2 生成与校验

- 金额一律用 `Decimal` 计算并输出到分，不使用浮点；
- 导出前对账：行数、笔数、金额合计与数据库一致；任一不符 fail closed，不产出文件；
- 重复单据号、跨期记录、超出授权范围的记录不得进入导出；
- 输出 `.xlsx`（无宏），以 `=`、`+`、`-`、`@` 开头的文本必须转义，防止单元格注入；
- 每次导出记录 manifest 与审计事件；同期间 + 同模板版本重复导出，manifest 可复现；
- 导出不改变任何业务状态，不触发外部调用。

### 6.3 禁止事项

- 不自动上传、不自动过账、不写入任何外部系统数据库；
- 不把模型生成内容未经结构校验写入导出文件；
- 不因模板缺失而静默降级为默认格式或猜测列名；
- 不在日志、Trace 或错误响应中输出完整票据内容、账号或个人敏感字段。

## 7. 国家税务总局发票查验接入

全国增值税发票查验平台目前公开提供网页查验入口。当前查阅到的官方公开材料未能确认一个可面向任意企业直接调用的通用公开 API，因此不能把“有网页可查”当作“已有公开 API”。

### 7.1 先做可替换的查验接口

统一定义 `InvoiceVerificationProvider`，至少有以下实现类型：

1. `manual_assisted`：Agent/OCR 提取发票字段、去重并生成查验任务，授权财务人员通过官方平台完成查验，再录入/导入结果并留存证据；
2. `authorized_provider`：如果经批准的税务/发票服务商或正式接入渠道确实提供适用的授权接口，按其正式文档实施；
3. `official_api`：只有在取得可适用的官方 API 文档和合法授权后才实现。

不通过绕过验证码、模拟登录或未授权爬虫来规避官方平台的访问控制。若没有可用接口，先落地人工辅助流程，不得把 OCR 识别成功标记为“发票已验证”。

### 7.2 发票数据字段与状态

保存发票代码/号码（按票种结构）、开票日期、销售方/购买方税号（受权限保护）、金额、税额、价税合计、币种（适用时）、票据类型、原件引用、重复检查结果、查验渠道、查验时间、操作人、结果状态和证据引用。

状态至少区分：`待识别`、`待人工确认`、`待查验`、`查验通过`、`查验不通过`、`无法查验/接口异常`、`疑似重复`。只有来自被认可查验渠道的结果才能进入“查验通过”。

## 8. 数据库初步设计

正式业务数据库采用 PostgreSQL；文件原件与业务元数据分开保存。以下为首期核心表建议，可随 API 契约调整，但必须由 Master Agent 统一审定，不允许各 Agent 各自建立冲突 schema。

### 身份、Profile 与权限

- `companies(id, name, country_code, base_currency, status)`
- `departments(id, company_id, parent_id, name)`
- `employees(id, company_id, department_id, manager_id, employee_no, status)`
- `employee_channel_bindings(id, employee_id, channel_type, external_user_id, external_account_id, verified_at, status)`
- `agent_profiles(id, employee_id, profile_name, runtime_status, config_version, status)`
- `roles(id, name)`、`permissions(id, resource, action, scope)`
- `employee_roles(employee_id, role_id)`、`role_permissions(role_id, permission_id)`
- `agent_profile_services(profile_id, service_name, service_identity_ref, status)`

### 工作流、收件和文件

- `intake_jobs(id, submitted_by, channel_type, input_type, status, received_at)`
- `source_files(id, owner_id, storage_key, sha256, mime_type, size_bytes, scan_status)`
- `extraction_drafts(id, intake_job_id, business_type, extracted_json, confidence_json, review_status)`
- `workflow_definitions(id, company_id, workflow_type, version, status)`
- `workflow_instances(id, definition_id, business_type, business_id, status)`
- `approval_steps(id, instance_id, step_no, approver_id, status, due_at)`
- `workflow_events(id, instance_id, actor_id, action, payload_ref, created_at)`
- `outbox_events(id, event_type, aggregate_id, idempotency_key, published_at)`
- `notifications(id, recipient_id, channel_type, delivery_status, attempt_count, last_error)`
- `documents(id, document_type, template_version, source_refs, storage_key, sha256, created_by)`
- `audit_events(id, actor_id, action, resource_type, resource_id, request_id, created_at)`

### 报销

- `expense_claims(id, claim_no, employee_id, currency, total_amount, status, submitted_at)`
- `expense_lines(id, claim_id, expense_date, category_id, description, amount, tax_amount, cost_center_id)`
- `expense_invoices(id, claim_id, source_file_id, invoice_fields_json, verification_status, duplicate_key)`
- `expense_policies(id, company_id, policy_type, version, rule_json, status)`
- `reimbursement_payments(id, claim_id, amount, currency, payment_ref, status, paid_at)`

### 进销存

- `products(id, sku, barcode, name, unit_id, category_id, status)`
- `warehouses(id, code, name, status)`
- `suppliers(id, code, name, status)`、`customers(id, code, name, status)`
- `purchase_orders(id, order_no, supplier_id, status, total_amount)`
- `purchase_receipts(id, receipt_no, purchase_order_id, status, received_at)`
- `sales_orders(id, order_no, customer_id, status, total_amount)`
- `delivery_orders(id, delivery_no, sales_order_id, status, delivered_at)`
- `stock_movements(id, product_id, warehouse_id, movement_type, quantity, source_type, source_id, created_at)`
- `stock_balances(product_id, warehouse_id, quantity, updated_at)`（由事务维护或从流水重算）
- `stocktakes(id, warehouse_id, status, created_by, approved_by)`

### 外部集成与导出

- `export_templates(id, template_id, version, file_sha256, status, created_at)`
- `export_runs(id, template_id, period_start, period_end, status, row_count, amount_total, manifest_json, file_id, created_at, created_by)`
- `integration_connections(id, provider, product_line, environment, status, secret_ref)` —— 仅为未来适配器预留，当前不实现具体外部系统连接
- `external_record_mappings(id, connection_id, internal_type, internal_id, external_id, sync_status)` —— 预留；当前导出以 manifest 记录记录集合
- `invoice_verifications(id, source_file_id, provider_type, provider_ref, status, verified_at, verified_by, evidence_ref)`

### 数据完整性要求

- 关键业务号、外部映射和幂等键设唯一约束。
- 金额采用固定精度 decimal，不用浮点数保存财务金额。
- 库存变动必须记录不可随意覆盖的流水，库存余额与流水可校验。
- 审批、导出/同步和通知通过事务及 Outbox 保证状态一致性。
- 对附件实施文件类型/大小限制、恶意文件扫描、授权下载和散列校验。
- 文件路径不能由用户输入直接拼接；外部 API 返回内容必须视为不可信数据。

## 9. 权限模型

同时采用 RBAC（角色权限）与资源/数据范围控制。后端对每次调用执行授权，Profile 本身并不是访问其他业务数据的许可。

| 能力 | 普通员工 | 主管 | 财务经办 | 财务主管 | 仓储/采购 | 系统管理员 | 审计人员 |
|---|---|---|---|---|---|---|---|
| 发起报销 | 自己 | 自己 | 按授权 | 按授权 | 自己/授权 | 默认不授予 | 否 |
| 查看报销 | 自己 | 负责团队 | 授权范围 | 公司范围 | 不默认授予 | 默认不授予 | 只读授权范围 |
| 审批报销 | 否 | 负责团队且非本人 | 按规则限制 | 按授权且非本人 | 否 | 否 | 否 |
| 发票查验/财务复核 | 否 | 按授权查看状态 | 是 | 是 | 否 | 否 | 只读 |
| 过账/付款类操作 | 否 | 否 | 仅明确授权步骤 | 按财务制度授权 | 否 | 否 | 否 |
| 采购/入库/出库 | 按授权申请 | 按审批职责 | 按需查看 | 按需查看 | 按岗位执行 | 默认不授予 | 只读 |
| 查看公司财务数据 | 否 | 按团队授权的汇总 | 业务所需范围 | 授权公司范围 | 否 | 默认不授予 | 只读授权范围 |
| 管理角色与身份绑定 | 否 | 否 | 否 | 可有限授权 | 否 | 是 | 否 |
| 审计日志 | 否 | 按授权 | 按授权 | 是 | 按授权 | 受控 | 是 |

强制规则：

- 默认拒绝，最小权限；角色调整和身份绑定变更均需审计。
- 管理员不因管理服务器或用户而自动获得财务批准权。
- 员工不能审批自己的报销；高风险付款实行职责分离。
- Agent 不能通过向其他 Agent 发消息来提升权限。
- 不向一般业务 Agent 开放任意 SQL、任意 shell 或整台服务器的文件访问。
- Profile 目录只允许该服务身份及受控运维账号访问；共享公司文件通过带权限验证的文件 API 读取。
- 重要操作需二次确认或独立审批；审批记录关联实际人员而非模型名字。

## 10. 分阶段实施与验收

### 阶段 0：技术验证与架构冻结（阻塞性前置阶段）

并行调查：

1. 微信 iLink：多员工私聊、稳定外部 ID、主动通知、文件/语音消息、限流和断线恢复；确认普通群聊限制及替代路径。
2. Hermes Profile：建立多个 Profile，分别运行进程和配置；测试内部 API 路由、凭据隔离和服务恢复。
3. 负载：以 20 个 Profile 起步，模拟 100 个用户的消息并发和进程常驻内存；决定单机配置和资源限制。
4. 财务导出：确认首个导出模板、字段口径（期间/币种/单据号）、目标财务系统的导入要求及唯一权威数据源。
5. 发票查验：确认可用官方路径、授权服务商接口或人工辅助流程；不默认存在通用公开 API。
6. 数据与文档盘点：收集脱敏后的常见 Excel/Word/PDF 样例，列出当前记录字段、模板与审批规则。
7. 大陆数据策略：确认服务部署区域、模型供应商条款、日志和备份位置、敏感数据边界。

验收：形成 ADR（架构决策记录）、接口清单、数据分类与威胁模型、Profile 路由原型、微信 Go/No-Go 报告、财务导出与发票接入结论。若个人微信通道不满足稳定性/合规/主动通知需求，不阻断业务架构，但必须先改接入方案再进入生产上线。

### 阶段 1：Agent 工作底座 + 报销 MVP

功能：

- 员工、部门、角色和渠道身份绑定；
- 每员工独立 Hermes Profile 进程及独立目录；
- 微信消息路由和统一输入格式；
- 文字/语音/图片/文件接收与解析草稿；
- 报销创建、明细、票据附件、提交、撤回、审批、退回补件和状态查询；
- 审批任务路由及消息通知；
- PDF 报销单和 XLSX 明细导出；
- 最小化 Web 管理后台；
- 审计、备份和恢复。

验收：

- 至少 3 个测试员工走通完整报销流程；
- 20 个 Profile 同时运行测试通过，具备向 100 个用户扩容的测试数据；
- 跨员工会话、记忆、文件和业务记录访问测试全部通过；
- 重复消息不会重复创建报销单/审批任务；
- 金额、日期、必填字段异常会被拒绝或要求确认；
- 权限拒绝、状态变化和文件导出均可审计；
- 数据库及附件恢复测试通过。

### 阶段 2：进销存/企业资产模块

功能：主数据、供应商/客户、采购申请与入库、销售订单与出库、退货、库存流水、盘点、库存差异、库存预警、领用/归还记录和库存报表。提供从旧 Excel 导入产品与期初库存的受控工具，保留导入来源和确认记录。

验收：

- 入库、出库、退货和盘点均产生流水；
- 库存余额与流水汇总一致；
- 无权限员工不能修改仓库数据；
- 重复导入/重复业务消息不会产生重复库存变动；
- Excel 导入有预览、校验、错误报告和确认步骤；
- 库存与外部系统若存在重叠，由阶段 0 确定唯一权威源并测试对账。

### 阶段 3：财务导出、对账与发票查验

功能：导出模板契约与装载、字段映射、单张与按期间导出、导出前对账、manifest 与审计、下载鉴权与保留期，以及发票查验工作流。

验收：

- 至少完成一个真实模板（由所有者提供）的端到端导出与导入验证；
- 导出行数、笔数、金额合计与数据库一致；不一致时不产出文件；
- 同一期间 + 同模板版本重复导出的 manifest 可复现；
- 导出文件具备单元格注入防护、下载鉴权与保留期；
- 导出不改变任何业务状态，不写外部系统、不自动过账；
- 发票 OCR 与正式查验状态明确分离；
- 未确认的发票接口使用人工辅助流程；
- 模板版本、映射定义与导出记录可追溯。

### 阶段 4：财务能力扩展与自动化（远期自研方向）

在导出与对账稳定后，按业务需要评估在本系统内建设财务能力的可行性：应收应付、收付款申请、银行流水导入与对账、费用与预算汇总、报表与月度报告。**是否自研会计引擎、科目表、凭证与总账，须由所有者单独立项决策**；启用内部账务前必须完成单一权威源迁移，禁止与外部导入并行双账。

验收：财务数据有唯一权威来源；导出与导入记录可相互追溯；重复导出不重复记账；异常可重试并人工补偿；月报数据可追溯到源记录；付款动作在本系统外执行，并有明确授权、复核和外部回执。

### 阶段 5：更多渠道、知识检索和生产加固

增加企业微信、钉钉、飞书、邮件、Web 上传或 API 渠道。每个渠道只需实现标准化消息接入、身份绑定与回执接口。完善服务监控、容量扩展、密钥轮换、权限穿透测试、事件应急和恢复演练。

验收：新增渠道不会改变业务表和工作流语义；相同业务身份跨渠道仍使用正确权限；未绑定渠道身份默认拒绝；渠道失败不会破坏已提交业务状态。

## 11. Agent 并行任务分派

Master Agent 负责架构决策、接口契约、任务依赖、合并审查及最终验收。各 Execution Agent 在收到任务后必须提交：变更文件、接口变更、测试命令及输出、已知限制、迁移脚本和回滚/补偿说明。

### Wave 0（并行调研）

| ID | Agent | 交付物 |
|---|---|---|
| W0-1 | WeChat/Platform Agent | iLink 多用户/媒体/主动通知/限流测试与 Go/No-Go 报告 |
| W0-2 | Hermes Runtime Agent | 多 Profile 独立进程、独立目录和 API 路由原型；20/100 用户资源测试 |
| W0-3 | Finance Export Agent | 首个导出模板与字段口径、目标系统导入要求、对账规则草案 |
| W0-4 | Tax Invoice Agent | 官方发票查验路径、授权服务商/API 可行性及人工辅助方案 |
| W0-5 | Security/Data Agent | 大陆数据处理边界、威胁模型、权限/文件隔离设计 |
| W0-6 | Business Analyst Agent | 脱敏 Excel/Word/PDF 样例、报销规则、库存流程和模板目录 |

Master Agent 在 Wave 1 前审定：统一身份模型、消息事件 schema、Profile 路由协议、权限模型、业务对象 ownership、数据库 schema v1 和接口错误码。

### Wave 1（基础底座与报销，可按接口并行）

| ID | Agent | 任务 | 依赖 |
|---|---|---|---|
| W1-1 | Backend Agent | FastAPI、PostgreSQL、Alembic、组织/员工/审计基础模块 | W0 设计审定 |
| W1-2 | Security Agent | RBAC、数据范围、调用主体绑定、文件权限和拒绝访问测试 | W0-5、API 契约 |
| W1-3 | Runtime Agent | Profile provisioning、独立进程/服务、配置注入、健康检查、消息路由 | W0-1、W0-2 |
| W1-4 | Intake/Document Agent | 文件接收、散列、OCR/ASR 解析草稿、模板文档生成 | W1-1、W1-2 |
| W1-5 | Expense Agent | 报销域模型、状态机、附件关联、费用规则与 API | W1-1、W1-2 |
| W1-6 | Workflow Agent | 审批任务、负责人路由、通知 Outbox、重试和幂等 | W1-1、W1-2 |
| W1-7 | Admin UI Agent | 低依赖管理后台：员工/权限/报销/审计查看和修改 | API 契约冻结后 |
| W1-8 | QA Agent | 身份隔离、越权、重复提交、状态机和端到端测试 | 各模块测试接口 |

W1-5 与 W1-6 可并行，但必须共用统一状态机与审批事件契约。W1-4 不得直接将解析结果写入正式报销/账务字段。

### Wave 2（进销存）

| ID | Agent | 任务 | 依赖 |
|---|---|---|---|
| W2-1 | Inventory Agent | 商品、仓库、库存流水、余额和盘点模型/API | 数据模型审定 |
| W2-2 | Procurement/Sales Agent | 采购、入库、销售、出库、退货流程 | W2-1 的接口契约 |
| W2-3 | Import/Report Agent | Excel/CSV 期初数据导入、预览校验、库存报表 | W2-1、W2-2 |
| W2-4 | Workflow Agent | 采购/库存相关审批规则与通知 | W1 工作流基础 |
| W2-5 | QA Agent | 库存并发、重复消息、流水一致性和权限测试 | W2-1–W2-4 |

### Wave 3（财务导出与税务接口，可并行但先统一账务归属）

| ID | Agent | 任务 | 依赖 |
|---|---|---|---|
| W3-1 | Export Adapter Agent | 模板装载、字段映射、xlsx 生成与单元格注入防护 | W0-3、统一接口契约 |
| W3-2 | Invoice Agent | 发票字段 schema、重复检查、查验状态机和人工辅助 UI/工作流 | W0-4、W1-4 |
| W3-3 | Finance Projection Agent | 报销/采购/库存数据到导出模板的业务映射与期间口径 | 账务归属决策、W1/W2 |
| W3-4 | Reconciliation Agent | 对账校验、manifest、失败重试和补偿工具 | W3-1、W3-3 |
| W3-5 | QA/Security Agent | 模板版本、重放/重复、金额精度、注入与权限测试 | W3 模块接口 |

### Wave 4（发布加固）

| ID | Agent | 任务 |
|---|---|---|
| W4-1 | Security Agent | 威胁建模复核、跨用户访问、文件恶意内容、密钥和提示注入测试 |
| W4-2 | Operations Agent | 备份恢复、日志告警、服务重启、升级和回滚演练 |
| W4-3 | QA Agent | 报销/库存/导出/发票全链路测试 |
| W4-4 | Documentation Agent | 用户操作、部署、恢复、故障处理及管理员手册 |
| W4-5 | Master Agent | 检查验收证据与阻塞项，决定是否发布 |

### 并行执行规则

- 所有共享数据模型先由 Master Agent 审定，禁止不同 Agent 各自定义冲突的 schema。
- 接口契约未冻结前，可以调研/写测试，不得提前合并依赖该契约的实现。
- 数据库迁移和测试必须与功能代码同一任务交付。
- 财务、库存、身份和文件模块必须有负面测试：越权、非法状态、重复调用、并发冲突和失败恢复。
- 不允许只凭 Agent 的“已完成”文字通过验收；必须有可复现的测试结果或实测记录。
- 任何高风险阻塞由 Master Agent 明确决定解决方式，不能以提示词修改代替真实权限控制。

## 12. 全局发布验收清单

### Profile 与通道

- [ ] 每位员工 Profile/进程/配置/记忆/文件工作目录隔离。
- [ ] 不同员工的消息被稳定映射到正确的 Profile。
- [ ] 未绑定或已解绑的渠道身份默认拒绝。
- [ ] 重复事件、断线和重试不会重复创建业务记录。
- [ ] 20 个 Profile 并发与 100 用户负载测试有结果和容量结论。
- [ ] 微信群限制和备用沟通方式有正式说明。

### 输入输出与工作流

- [ ] 文本、语音、图片、PDF、Word、Excel 的支持范围有明确测试。
- [ ] 解析草稿能追溯原件，低置信度字段需要确认。
- [ ] 正式数据变更通过后端 API 完成并有审计。
- [ ] 文档模板有版本与源数据关联。
- [ ] Agent 间审批任务不能越权读取其他 Profile 记忆。

### 报销与库存

- [ ] 报销申请、审批、补件、撤回和通知端到端通过。
- [ ] 主管不能批准自己的报销。
- [ ] 库存流水、余额和盘点差异可相互核对。
- [ ] 所有导入支持预览、验证、幂等和错误报告。

### 财务导出与发票

- [ ] 首个导出模板经所有者确认，`template_id`、版本与 SHA-256 已记录。
- [ ] 导出行数、笔数与金额合计与数据库一致；不一致时不产出文件。
- [ ] 同一期间 + 同模板版本重复导出，manifest 可复现。
- [ ] 导出文件具备单元格注入防护、下载鉴权与保留期。
- [ ] OCR 识别结果不会被误当成正式发票查验结果。
- [ ] 无官方/授权 API 时采用人工辅助路径，不绕过验证码或访问控制。
- [ ] 正式账务的权威系统、授权人与复核规则已书面确定；本系统不执行付款。

### 安全与运行

- [ ] 数据库和附件备份存放于生产主机之外。
- [ ] 已完成真实恢复演练。
- [ ] 模型供应商的数据用途/处理区域/留存条款已审查。
- [ ] Profile 进程以非 root 身份运行，文件访问范围经过测试。
- [ ] API 密钥不进入代码仓库、普通日志或模型提示词。
- [ ] 关键状态变化、权限变更、导出和外部调用均可审计。

## 13. 给 Master Agent 的启动指令

从 Wave 0 开始，不要一次性生成所有业务代码。先并行执行 W0-1 至 W0-6，收集真实微信接入结果、Hermes Profile 多进程验证、财务导出模板与字段口径、发票查验路径和脱敏业务样例。

Master Agent 随后统一审定：

1. 渠道事件和身份路由 schema；
2. 每员工 Profile 服务的创建、停止、监控与隔离方式；
3. 员工/角色/资源范围权限模型；
4. 核心数据库 schema 与 API 契约；
5. 内部系统、外部财务系统（导出导入）与发票查验服务的数据权威边界；
6. 首期报销状态机及验收测试。

审定通过后再启动 Wave 1。每个 Wave 结束时必须进行集成测试、代码审查、权限测试和实际验收；通过的模块才成为下一 Wave 的稳定依赖。

项目优先级依次为：身份与权限正确性、业务数据一致性、工作流可靠性、外部接口可追溯性，然后才是交互体验和功能数量。最终目标是让 Hermes 成为企业日常工作的 AI 操作入口，而不是让模型直接成为数据库管理员、会计系统或付款执行者。

---

## 参考资料

- Hermes Agent Profiles: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/profiles.md
- Hermes Agent Weixin 接入: https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/messaging/weixin.md
- 国家税务总局全国增值税发票查验平台: https://inv-veri.chinatax.gov.cn/
- 《中华人民共和国个人信息保护法》: https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm
- 《网络数据安全管理条例》: https://app.www.gov.cn/govdata/gov/202409/30/520076/article.html
