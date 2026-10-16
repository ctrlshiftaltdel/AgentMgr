# 受理解析流水线：可替换接口与人工录入兜底（W1-26）

> 关联 Issue：W1-26（受理解析流水线）。实现依据：`../templates/PLAN.md` §4.1/§4.2、
> `PROJECT-CHARTER.md`、`research/W0-5-security-compliance.md` §3/§4.3、
> `research/W0-1-weixin-ilink.md` §6.3。
> 代码：`../app/intake/parsing.py`（纯逻辑）、`../app/intake/pipeline.py`（落库）。

## 1. 目标与范围

把 PLAN §4.2「统一资料接收流水线」的**解析环节**落成**供应商无关**的接口，并提供
**人工录入兜底**：文字 / 语音 / 图片 / 文档四类输入 → 解析草稿（`extraction_drafts`），
**不依赖任何外部服务也可工作**。

OCR/ASR 供应商尚未决定：本任务**不接任何真实供应商**，只做接口 + 兜底 + 草稿写入
机制；真实供应商接入留待后续 Issue。解析结果只进**草稿态**，人工确认是进入正式
记录的唯一路径。

## 2. 四类输入 → `input_type` 映射

模型（`../app/intake/models.py`）的 `IntakeJob.input_type` 由 CHECK 限定为五值
`text` / `image` / `file` / `voice` / `spreadsheet`。四类输入与五值的**显式映射**
如下（代码常量 `INPUT_KIND_MODEL_TYPES`、函数 `model_input_type`）：

| 四类输入（`InputKind`） | 载体 | `IntakeJob.input_type` |
|---|---|---|
| `text` 文字 | 微信文本等 | `text` |
| `voice` 语音 | 语音消息 | `voice` |
| `image` 图片 | 票据/收据照片 | `image` |
| `document` 文档 | PDF / Word / 文本文件 | `file` |
| `document` 文档 | Excel / CSV 表格 | `spreadsheet` |

反向映射用 `input_kind_for_model_type`：`file` 与 `spreadsheet` 同归 `document`；
未识别取值返回 `None`（调用方据此 fail closed，不臆断）。

## 3. 可替换接口

上层业务只依赖抽象接口 `IntakeParser`：

- `parser_name` / `parser_version`：解析器标识与版本（用于追溯）；
- `parser_kind`：`ParserKind`（人工 / OCR / ASR / 文档 / 组合）；
- `supported_input_kinds`：支持的输入类型集合；
- `parse(request) -> ParseResult`：只读解析，**不覆盖原始文件、不写正式业务表**。

实现：

| 实现 | 说明 |
|---|---|
| `ManualEntryParser` | **人工录入兜底**，不依赖外部服务；由授权人员逐字段录入，包装成与其它解析器同形的 `ParseResult` |
| `ExternalParser`（抽象基类） | 未来 OCR/ASR/文档供应商的基类，固定「凭据只从环境变量读取、缺失即 fail closed」的约束（本任务不实现真实子类） |
| `ParserRegistry` | 按输入类型解析到具体解析器（**替换点**）；`resolve` 返回 `None` 时上层回退到 `ManualEntryParser`，保证「无供应商也可工作」 |

`ParseResult` 的关键不变量：逐字段取值 `fields` 与逐字段置信度 `field_confidence`
**键集合完全一致**，置信度在 `[0.0, 1.0]`；失败时用机器可读 `error` 码表达，
**绝不放原文**。

## 4. 凭据策略（fail closed）

`ExternalParser` 子类用类属性 `credential_env_vars` 声明所需环境变量名：

- 构造时**只从环境变量读取**（可注入 `env` 供测试），任一缺失/空白即抛
  `MissingCredentialError`，错误信息**只提环境变量名、不含凭据值**；
- 凭据保存在私有属性中，**绝不写日志、绝不落库**；
- 供应商侧任意异常被收敛为 `error="provider_error"` 的失败结果，**不泄漏异常原文**。

与 `../app/integrations/baidu_vat_verify.py` 的凭据/fail-closed 模式一致。

## 5. 草稿裁决

`decide_draft(result, required_fields=..., confidence_threshold=...)`：

| 情形 | `IntakeJob.status` | 草稿 `review_status` |
|---|---|---|
| 字段齐全且置信度达标 | `parsed` | `pending` |
| 存在低置信度字段（`< 阈值`） | `needs_review` | `pending` |
| 关键字段缺失（不存在或值为空） | `needs_review` | `pending` |
| 解析失败 | `failed`（不建草稿，转人工录入兜底） | 不适用 |

- 默认置信度阈值：`DEFAULT_CONFIDENCE_THRESHOLD = 0.75`（保守默认，可覆盖）；
- 关键字段：`BUSINESS_REQUIRED_FIELDS`（当前仅登记 `expense_claim` → `amount`、
  `expense_date`，属**保守默认**，不是最终业务规则，可覆盖）；
- **草稿 `review_status` 恒初始为 `pending`**：本层绝不自动确认，更不写正式业务
  字段；低置信度/关键字段缺失只影响 `status`（是否 `needs_review`），不影响
  「必须人工确认」这一事实。

## 6. 追溯口径（来源文件 / 解析器版本 / 确认人）

按 Issue 授权评论「优先考虑无需结构变更的方案」，本任务**不新增迁移**，追溯由
**既有列 + 既有审计**承载：

| 追溯对象 | 承载方式 |
|---|---|
| 来源文件 | `ExtractionDraft.source_file_id`（原件引用；**原件不被覆盖**） |
| 逐字段取值 | `ExtractionDraft.extracted_json`（键 = 字段名） |
| 逐字段置信度 | `ExtractionDraft.confidence_json`（键集合与 `extracted_json` 一致） |
| 解析器版本 | `extraction_draft.created` 审计事件 `details.parser_name` / `parser_version` / `source_file_id` / `field_names` |
| 确认人 | `extraction_draft.confirmed` / `rejected` 审计事件 `actor_id`（已验证的人员员工 ID） |

选择「无结构变更」的理由：追溯所需的解析器版本与确认人天然属于**追加式审计**
语义（只增不改、以 `actor_id` 关联实际人员），已有 `app.audit` 机制可完整承载；
新增列会带来迁移与回滚成本而无额外收益。若后续需要「按解析器版本批量检索草稿」，
再评估结构化列（本 Issue 的受限迁移授权允许 `extraction_drafts` / `intake_jobs`
新增可空列）。

`app/intake/pipeline.py` 只写草稿区与审计，**绝不 import 或写任何正式业务表**，
**绝不 commit**（事务边界由服务层控制，保证草稿 + 状态 + 审计同事务）。

## 7. 日志与脱敏

- `safe_log_summary(result)`：可写日志的**安全摘要**，只含解析器标识/版本、输入
  类型、原件引用、字段名与计数、失败码——**不含字段取值或原文**；
- `parse_provenance(result)`：可写审计的非敏感追溯信息，只含字段名与标识；
- 结论：**附件原文与识别全文不进日志、不进审计**（对齐 W0-5 §3.1/§3.2）。

## 8. 未来接入 OCR/ASR 的扩展点

1. 新增 `ExternalParser` 子类：声明 `credential_env_vars`、实现 `_parse`；
2. 在 `ParserRegistry` 登记（同一类型后登记者覆盖，显式替换）；
3. 未登记类型的输入自动回退到 `ManualEntryParser`；
4. 无需改动裁决、草稿写入与追溯逻辑——它们只依赖 `ParseResult`。

## 9. 未决事项与风险

- **关键字段口径**（`BUSINESS_REQUIRED_FIELDS`）与**置信度阈值**是保守默认，最终
  取值属产品决策，需在后续任务中确认；
- 真实 OCR/ASR 供应商未接入，`ExternalParser` 尚无产品实现；供应商确定后需按其
  响应结构实现 `_parse` 并补齐测试；
- 本层只到「草稿 + 确认状态」，**正式业务记录的写入路径不在本任务范围**（后续
  Issue），且必须经后端校验与授权。
