# W1-28 百度智能云「增值税发票验真」渠道调研与适配实现说明

- 任务：W1-28（百度增值税发票验真适配器与渠道记录更新）
- 分支：task/W1-28-baidu-vat-verify
- 输入：项目所有者 2026-10-16 决策（原话见 §1）、`templates/PLAN.md` §7.1/§7.2、`docs/research/W0-4-invoice-verification.md`（W0-4，含文末「所有者决策更新（2026-10-16）」增补节）
- 范围：产出本报告 + 适配器 `app/integrations/baidu_vat_verify.py` + 桩测试 `app/tests/integrations/test_baidu_vat_verify.py`；**不调用任何真实发票查验接口**；未申请、未持有、未使用任何真实 API Key/Secret Key/access_token。
- 证据声明：本文渠道技术细节全部来自**本机 curl 实际抓取的公开网页**（命令、HTTP 状态码、字节数与关键字段行号见 §6）。凡抓取页面未列出的字段、错误码、价格、QPS、SLA，本文一律标注「未提供/待确认」，**不编造**。
- 占位符声明：全文不含任何真实凭据、税号或票号；示例一律使用占位符。百度官方文档示例中出现的 `access_token`/`client_id`/`client_secret` 串为**该文档自带的示例占位串**，本文不复述、不采用。

## 1. 背景与目的

- 所有者 2026-10-16 决策原话：

  > 「发票验真系统我想用 https://ai.baidu.com/ai-doc/OCR/cklbnrnwe 作为首选，之前发给你的两个作为备用，不需要设置选项，留在注释或说明文档中即可」

- 落地映射：
  - **首选**：百度智能云「增值税发票验真」（`authorized_provider`；接口 `POST https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice_verification`；适配器 `app.integrations.baidu_vat_verify.BaiduVatInvoiceVerifyProvider`）；
  - **备用 1**：腾讯云 `VatInvoiceVerifyNew`（已有适配器 `app.integrations.tencent_vat_verify` **保留**，仅更正文首注释；除注释外**不改变其行为**）；
  - **备用 2**：聚美智数 `invoice_validate_v2`（**仅文档备用、不实现**）；
  - **不新增任何渠道选择配置项或环境开关**（所有者明确）：渠道接入与否由服务层/部署层决定，备选信息只留在注释与本说明文档；
  - `manual_assisted` 兜底流程保留（PLAN §7.1；票面要素缺失、渠道异常或票种不在渠道覆盖范围时退化）。
- 本报告的目的：把百度渠道的接口/认证/入参/出参/结果码/计费与限制落成可核对的文字证据，并说明其与 PLAN §7.2 的字段与状态映射及待确认项，供后续接入与复核使用。

## 2. 渠道详述：百度智能云 增值税发票验真

- **来源 URL**：https://ai.baidu.com/ai-doc/OCR/cklbnrnwe
- **页面层级**：AI 开放平台 > 帮助文档 > 文字识别 > API 文档 > 财务票据文字识别 > 增值税发票验真
- **抓取时间**：2026-10-16（本机 `curl` **直连**，HTTP 200，`size_download=1002129` 字节，落盘 1002129 字节；命令与校验见 §6）
- **页面标注更新时间**：`2026-08-04`（抓取页内字段）
- **接口描述（页内原文要义）**：POST `https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice_verification`；`Content-Type: application/x-www-form-urlencoded`；`access_token` 置于 URL 参数；支持 **15 种**增值税发票的信息核验（增值税专票、电子专票、普票、电子普票、卷票、区块链发票（深圳地区）、全电发票（专票/普票）、通行费增值税电子普通发票、货物运输业增值税专用发票、机动车销售发票、二手车销售发票、电子发票（航空运输电子客票行程单）、电子发票（铁路电子客票）等），支持返回票面全部信息。
- **票种枚举**（页内「请求参数」`invoice_type` 可选值，共 **15** 个）：`special_vat_invoice`（专票）、`elec_special_vat_invoice`（电子专票）、`normal_invoice`（普票）、`elec_normal_invoice`（电子普票）、`roll_normal_invoice`（卷票）、`toll_elec_normal_invoice`（通行费电子普票）、`blockchain_invoice`（区块链，仅深圳）、`elec_invoice_special`（全电-专）、`elec_invoice_normal`（全电-普）、`special_freight_transport_invoice`（货运专票）、`motor_vehicle_invoice`（机动车/电子机动车）、`used_vehicle_invoice`（二手车/电子二手车）、`elec_flight_itinerary_invoice`（航空行程单）、`elec_train_ticket_invoice`（铁路电子客票）、`elec_toll_invoice`（全电-含通行费标识）。

### 2.1 认证（OAuth2.0 → access_token）

- **方式**：百度 AI 开放平台 OAuth2.0；调用接口时在 URL 带 `access_token`（W1-28 抓取 token 文档 https://ai.baidu.com/ai-doc/REFERENCE/Ck3dwjhhu，HTTP 200，`size_download=871972` 字节；页面标注更新时间 `2025-01-20`）。
- **token 获取**：向 `https://aip.baidubce.com/oauth/2.0/token` 发送请求（文档「推荐使用 POST」），URL 参数：`grant_type=client_credentials`（固定）、`client_id`（应用 API Key）、`client_secret`（应用 Secret Key）。文档示例即把三者放在 URL 中、请求体为空。
- **token 有效期**：默认 **30 天**（`expires_in`，秒）。文档提示「线上环境 access_token 有过期时间，客户端可自行缓存，过期后重新获取」。
- **失败响应**：`{"error": "...", "error_description": "..."}`；鉴权错误码表：`invalid_client`+「unknown client id」= API Key 不正确；`invalid_client`+「Client authentication failed」= Secret Key 不正确。
- **安全提示（页内原文要义）**：请勿将 AK/SK 与 access_token 共享或硬编码；如发生泄露可在应用详情页更新 Secret Key（历史 token 立即失效）。
- **本仓库实现（对齐）**：凭据只从环境变量 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY` 读取；缺失即构造失败（fail closed），错误信息只含变量名、不含凭据值；token 在内存中按 `expires_in` 缓存（提前 60 秒视为过期，`expires_in` 缺失则不缓存、下次重新获取）；token 获取失败/超时/传输失败一律映射为「无法查验」，**不自动重发**。

### 2.2 入参（页内「请求参数」表，逐字核对）

| 参数 | 必选 | 类型 | 页面说明要点（逐字要义） |
|---|---|---|---|
| `invoice_code` | 是 | string | 发票代码。**全电发票（专用/普通/含通行费标识）、电子发票（机动车/二手车/铁路/航空）此参数可为空，其他类型均不可为空** |
| `invoice_num` | 是 | string | 发票号码 |
| `invoice_date` | 是 | string | 开票日期。格式 `YYYYMMDD`，例：`20210101` |
| `invoice_type` | 是 | string | 发票种类（15 个可选值见上；**无「可为空」例外**） |
| `check_code` | 是 | string | 校验码，填写**校验码后 6 位**。**电子专票、普票、电子普票、卷票、区块链、通行费电子普票必填；其他类型可为空** |
| `total_amount` | 是 | string | 发票金额。**专票、电子专票、区块链、机动车、电子发票（纸质机动车）、货运专票填不含税金额；二手车类填车价合计；全电（专/普）、铁路、航空、电子发票（机动车）、全电（含通行费标识）填价税合计金额；其他类型可为空** |

- **本适配器传参策略（不臆造）**：六个文档化参数始终传递；调用方未提供的条件字段以**空串**表达（文档原文「此参数可为空」），绝不编造取值；腾讯云特有的 `RegionCode`/`SellerTaxCode` 等参数**一律不传**；金额经定点格式化（如 `100.00`，不出现科学计数法）；`invoice_date` 以 `YYYYMMDD` 序列化（本仓库 `InvoiceFields.invoice_date` 为 `date`）。
- `invoice_type` 是唯一「必选且无『可为空』例外」者：调用方未提供票种时，适配器**本地 fail closed**（返回「无法查验」，不获取 token、不发起必然失败的请求）。

### 2.3 出参（页内「返回参数」表与示例）

顶层字段：`log_id`（uint64，唯一 log id，用于问题定位）、`words_result_num`（uint32）、`words_result`（object，识别结果）。`words_result` 内的通用字段（逐字）：

| 字段 | 类型 | 页面说明要点 |
|---|---|---|
| `VerifyResult` | string | 查验结果。**查验成功返回「0001」，查验失败返回对应查验结果错误码，详见末尾表格** |
| `VerifyMessage` | string | 查验结果信息。成功且发票为真返回「查验成功发票一致」 |
| `VerifyFrequency` | string | 查验次数（历史查验次数） |
| `InvalidSign` | string | 发票状态。**Y：已作废；H：已冲红；N：未作废；BH：部分红冲；QH：全额红冲** |
| `InvoiceType` | string | 发票种类（名称，如「增值税普通发票（电子）」） |
| `InvoiceCode` / `InvoiceNum` | string | 发票代码 / 发票号码 |
| `CheckCode` / `InvoiceDate` / `MachineCode` | string | 校验码 / 开票日期 / 机器编号 |

主表（专票、电子专票、普票、电子普票、卷票、通行费电子普票、货运专票、全电-含通行费标识）另有：`PurchaserName`、`PurchaserRegisterNum`（购方纳税人识别号）、`SellerName`、`SellerRegisterNum`（销售方纳税人识别号）、**`TotalAmount`（合计金额）**、**`TotalTax`（合计税额）**、**`AmountInFiguers`（价税合计-小写）**、`TollSign`、`CommodityName/CommodityAmount/CommodityTaxRate/CommodityTax` 等（「列数组」形式，含 `row`/`word`）。

分节字段（文档单列）：机动车（`Price` 不含税价格 / `Tax` 税额 / `PriceTaxLow` 价税合计 / `SalerCode` 等）、二手车（`TotalCarPrice` 车价合计 / `SalerCode` / `PurchaserCode` 等）、航空（`price_tax_low` / `total_tax` / `purchaser_register_num` / `seller_register_num` 等）、铁路（`commodity_amount` 发票金额 / `commodity_tax` 发票税额 / `fare` 票价 / `purchaser_register_num` / `seller_register_num` 等）。

> **诚实标注**：①本次抓取页面**未列出币种字段**，也未列出与腾讯云 `ElectronicType` 对应的「电子类型」代码字段；②文档示例中 `log_id` 出现两种位置：成功示例把 `log_id`/`words_result_num` 嵌在 `words_result` 内，字段表与失败示例则放在**顶层**——适配器两处都取（顶层优先）；③失败返回示例为顶层 `{"log_id", "error_code": 282162, "error_msg": "Unexpected query result exception", "VerifyResult": "0005", ...}`，即**失败响应可能同时带顶层 `error_code`（文档未给出该代码的映射表，属待确认）**。以上未列/歧义之处均按「不臆造」处理（见 §4）。

### 2.4 结果码（页内「查验结果码释义表」14 个，逐字核对 + 本项目映射）

| VerifyResult | 查验结果信息（VerifyMessage） | 描述 | **是否计费** | 本项目状态映射 |
|---|---|---|---|---|
| `0001` | 查验成功发票一致 | 查验成功发票一致 | 是 | 查验通过**候选**（仍须 `InvalidSign=N` + 票面一致性比对） |
| `0002` | 超过该张票当天查验次数 | 此发票今日查询次数已达上限（5次），请次日查询 | 否 | 无法查验 |
| `0005` | 请求不合法 | 发票入参格式有误，请核对后再查询 | 否 | 无法查验 |
| `0006` | 发票信息不一致 | 发票信息有误，请核对后再查询 | 是 | **查验不通过** |
| `0009` | 发票不存在 | 所查发票不存在 | 是 | 无法查验（归类理由见下） |
| `1005` | 查询发票不规范 | 信息有误，请核对后再查询 | 是 | 无法查验 |
| `1006` | 查验异常 | 发票信息有误，请核对后再查询 | 否 | 无法查验 |
| `1008` | 字段不能为空 | 发票请求参数不能为空 | 否 | 无法查验 |
| `1009` | 参数长度不正确 | 参数长度不符合规范，确认参数，再次查验 | 否 | 无法查验 |
| `1014` | 日期当天的不能查验 | 日期当天的不能查验，请隔天再查 | 否 | 无法查验 |
| `1015` | 超过5年的不能查验 | 超过5年的不能查验 | 否 | 无法查验 |
| `1020` | 没有查验权限 | 没有查验权限，请提交工单咨询 | 否 | 无法查验 |
| `1021` | 网络超时 | 税局维护升级，暂时无法查验，请提交工单咨询 | 否 | 无法查验 |
| `9999` | 查验失败 | 查验失败，业务出现异常，请提交工单咨询 | 否 | 无法查验 |

- **`0009`（发票不存在）归类理由与跨渠道一致性**：本项目把「票不存在」归入**无法查验**而非「查验不通过」，与腾讯云映射的既有决策（W1-20：`ResourceNotFound.NoInvoice` → `UNABLE_TO_VERIFY`）**保持一致**。理由：票不存在可由多种技术性/录入性原因造成（代码/号码/日期/金额任一要素录入有误、正当票据尚未同步税局、跨区数据延迟等），单独不足以在**本项目口径**下裁定「假票」；保持两渠道同归可避免「同一业务事实因渠道不同而得到不同状态」。若业务希望把“票不存在”直接否决（如退回报销单），需要**业务确认**后统一调整（含腾讯云侧）——见 §5。
- **未知结果码**：一律 fail closed 到「无法查验」（原样保留码值于 `error_code` 便于追查），**绝不**进入「查验通过」。
- **`InvalidSign` 生效规则**：`0001` 且 `InvalidSign ∈ {Y, H, BH, QH}` → **查验不通过**（`Y` 已作废 / `H` 已冲红 / `BH` 部分红冲 / `QH` 全额红冲；部分红冲与全额红冲均按否决处理，如业务有细分需求另行确认）；`InvalidSign` 缺失或出现未文档化取值 → **无法查验**（不臆断为正常）。
- **计费口径备注**：「是否计费」列按上表逐码照录。注意其与《计费概述》「调用失败不计费」的表述存在需要核对的边界（`0006`/`0009`/`1005` 在码表中标「是」）——精确账单口径**待确认**（见 §5）。

### 2.5 限制、配额与计费小结

- **当日发票**：不支持（`1014`「日期当天的不能查验，请隔天再查」）；与腾讯云一致，需「人工辅助兜底 + 次日渠道核验」的流程兜底（W0-4 §5 相同结论）。
- **时间范围**：仅 5 年以内（`1015`）。
- **当日限次**：同一张票当日查验上限 5 次（`0002`；与腾讯云 `InvoiceCode` 的「查验超过 5 次当日无法再查」表述一致）。
- **免费额度**（抓取《免费测试资源》页，HTTP 200，886397 字节）：**增值税发票验真：个人认证 20 次；企业认证 50 次**；用尽后可开通按量后付费或购买预付费资源包（按次计费）。
- **计费与 QPS**（抓取《计费概述》页，HTTP 200，873802 字节）：免费测试资源用完按「免费测试资源 > 专项资源包 > 共享资源包 > 按量后付费」顺序抵扣；开通付费后**保证单账户 10 QPS**（免费状态不保证并发），可购买 QPS 叠加包，单接口上限 100 QPS；欠费将停止超额调用。
- **单价与 SLA**：本次未取得「增值税发票验真」的**逐档单价**（财务票据专属价格页在抓取环境中未渲染正文）与 SLA 条款 → **待确认**。

## 3. 与 PLAN §7.2 的字段与状态对应

PLAN §7.2 要求保存的字段，在本渠道下的来源（「本项目记录」表示渠道不返回、由业务系统自身生成并落库）：

| PLAN §7.2 要求字段 | 百度渠道来源（`words_result`） | 备注 |
|---|---|---|
| 发票代码 | `InvoiceCode` | 全电等类型可为空 |
| 发票号码 | `InvoiceNum` | 8 位/20 位按票种 |
| 开票日期 | `InvoiceDate`（`YYYYMMDD`） | 入参同格式 |
| 销售方税号 | `SellerRegisterNum`（主表） | 机动车/二手车等分节用 `SalerCode` 等，**未并入通用槽位**（见 §5） |
| 购买方税号 | `PurchaserRegisterNum`（主表） | 分节命名不同，同上 |
| 金额 | `TotalAmount`（页面标注「合计金额」） | 映射 `amount_without_tax` |
| 税额 | `TotalTax`（页面标注「合计税额」） | 映射 `tax_amount`（腾讯云页未列此字段；本渠道**有**） |
| 价税合计 | `AmountInFiguers`（页面标注「价税合计（小写）」） | 映射 `amount_with_tax` |
| 币种（适用时） | **页面未列币种字段** | 保持 `None`，不臆造 |
| 票据类型 | 入参 `invoice_type`；输出 `InvoiceType`（名称） | 名称原样保留于证据 |
| 原件引用 | **本项目记录**（附件 `storage_key`/`sha256`） | 渠道不返回原件 |
| 重复检查结果 | **本项目记录**（业务去重） | `VerifyFrequency` 是「查验次数」而非重复标志，**不得**当作重复结果 |
| 查验渠道 | **本项目记录**：`baidu_cloud/vat_invoice_verification` | 记录 provider 标识 |
| 查验时间 | **本项目记录**（渠道不返回查验时间） | `InvoiceDate` 是开票日期 |
| 操作人 | **本项目记录**（`actor_id`） | 不得写模型名字/角色名 |
| 结果状态 | 见 §2.4 映射 | |
| 证据引用 | **本项目记录**：脱敏原始响应 + `log_id` | 证据原文按 A 类字段处理（W0-5 §3.1） |

**七种状态如何获得（映射）**：

| PLAN §7.2 状态 | 百度渠道判定依据 |
|---|---|
| 待识别 | OCR 尚未完成或未产出结构化字段（渠道无关） |
| 待人工确认 | 字段置信度低或关键字段缺失，需人工确认（渠道无关；`manual_assisted` 兜底） |
| 待查验 | 字段齐备、已去重，尚未调用渠道（渠道无关） |
| **查验通过** | `VerifyResult=0001` **且** `InvalidSign=N` **且** 票面要素与提交要素一致（金额按票种语义与 `TotalAmount`/`AmountInFiguers` 任一相等即一致） |
| **查验不通过** | `VerifyResult=0006`；或 `InvalidSign ∈ {Y, H, BH, QH}`；或票面一致性比对发现不一致（`face_mismatch:*`） |
| **无法查验/接口异常** | `0002`/`0005`/`0009`/`1005`/`1006`/`1008`/`1009`/`1014`/`1015`/`1020`/`1021`/`9999`；未知结果码；token 获取失败、超时、传输失败、响应不可解析、Http>=400 |
| 疑似重复 | **本项目去重逻辑**（渠道不提供重复判定） |

**硬性规则（对齐 PLAN §7.2）**：仅来自被认可渠道（`authorized_provider`/`official_api`）的结果才能进入「查验通过」；「无法查验」与「查验不通过」严格区分、互不覆盖；OCR 识别成功与模型自述一律不得置为「查验通过」。

## 4. 本项目适配器实现要点（app/integrations/baidu_vat_verify.py）

- `BaiduVatInvoiceVerifyProvider`：`provider_type=AUTHORIZED_PROVIDER`；`provider_name="baidu_cloud/vat_invoice_verification"`。
- **凭据**：环境变量 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY`（env-only）；缺失/空白即抛 `MissingCredentialError`（fail closed），错误信息只含变量名。
- **传输**：可注入 `transport`（`(url, headers, body, timeout) -> HttpResponse`）；生产默认 `httpx`（**惰性导入**，仅真实调用时加载；httpx 为既有传递依赖，未新增依赖）；测试一律注入桩，绝不触网。
- **请求**：POST 表单；`access_token` 在 URL；六参数按 §2.2 策略；金额定点；单次尝试（超时/失败不自动重发）。
- **结果处理**：`RESULT_CODE_TABLE`（14 码，含「是否计费」`billed` 标记与文档 `VerifyMessage` 原文）+ `map_result_code` / `is_result_code_billed`；未知码 fail closed；`0001` 分支仍执行 `InvalidSign` 与票面一致性校验。
- **错误码约定**（与腾讯云适配器一致）：渠道判定成功但业务否决（作废/红冲/票面不一致）时 `error_code=None`、缘由记入 `reason`；渠道结果码（如 `0006`）与本地失败码（`Timeout`/`TransportError`/`InvalidResponse`/`HTTP500` 等）写入 `error_code`。
- **桩测试**：`app/tests/integrations/test_baidu_vat_verify.py` 共 **54** 个用例，覆盖：入参裁剪不臆造、请求线路格式（表单/Token URL）、成功解析与票面映射、票面/金额不一致、`InvalidSign` 四类否决与未知取值 fail closed、14 码映射与计费标记、未知码 fail closed、token 获取/缓存/过期重取/失败映射、超时只尝试一次、凭据缺失 fail closed（错误信息不含凭据值）。
- **不新增渠道选择配置项/环境开关**（所有者决策）；腾讯云适配器除文首注释外行为不变；`manual_assisted` 兜底保留。

## 5. 待确认项（不阻塞本次交付；接入启用前逐项确认）

1. **百度智能云账号与开通**：账号归属/实名主体；是否需要控制台开通「文字识别」下该接口的付费/权限（存在 `1020 没有查验权限` 结果码，提示权限可能需单独申请）。
2. **费用**：免费额度（个人 20 次 / 企业 50 次）之外的按量后付费与资源包**单价**未取得；月调用量与预算上限待所有者确认；「是否计费」列与《计费概述》「调用失败不计费」的精确关系待以账单核对。
3. **合规**：向百度智能云（大陆主体）发送发票关键字段（票号/代码/开票日期/金额/校验码/购销方税号）的取舍；需按 W0-5 §6 九项审查清单取得书面材料（本次抓取页面未含数据处理条款）。**审查未通过前不得接入**。
4. **业务口径待确认**：`0009 发票不存在`（及腾讯云 `ResourceNotFound.NoInvoice`）是否需要从「无法查验」改判为「查验不通过」；`0005`/`1005` 的归类是否与业务一致；`BH`（部分红冲）是否与 `QH`（全额红冲）同等否决。
5. **未文档化取值**：`InvalidSign` 除 `Y/H/N/BH/QH` 外的取值、失败返回示例中顶层 `error_code`（如 `282162`）的含义与码表——百度侧文档补充后核对；适配器当前对二者均 fail closed。
6. **按票种金额比对**：机动车/二手车/铁路/航空分节的金额字段（`Price`/`PriceTaxLow`/`TotalCarPrice`/`commodity_amount`/`price_tax_low` 等）未并入通用票面槽位；是否需要按票种语义扩展比对，待业务确认。
7. **字段结构核对**：区块链电子发票、全电发票（专/普）的返回字段是否与主表同构——文档未单列，建议接入联调时以真实响应核对；`log_id` 的两种位置已在适配器内兼容。
8. **QPS/SLA**：本接口未列出专属 QPS；《计费概述》给出付费 10 QPS（可购至 100 QPS）、免费不保证并发；SLA 条款待确认。
9. **凭证管理**：`AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY` 的密钥存储方案（部署层注入/密钥环）待定；**不得**写入仓库、日志或模型提示词。

## 6. 抓取证据与自检

**环境说明**：本机经 Privoxy（<local-proxy>）访问 `ai.baidu.com` 会 TLS 失败，故下列抓取均使用**直连**（`curl --noproxy '*'`）；实测 HTTP 200。

**抓取命令与返回（本机 `curl`，2026-10-16）：**

```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# ① 渠道主文档（本任务核心证据）
curl --noproxy '*' -sS -L --compressed -A "$UA" -o v1.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://ai.baidu.com/ai-doc/OCR/cklbnrnwe'
# → HTTP=200 SIZE=1002129 CT=text/html; charset=utf-8 URL=https://ai.baidu.com/ai-doc/OCR/cklbnrnwe
#   落盘 1002129 字节

# ② Access Token 获取（OAuth2.0 细节）
curl --noproxy '*' -sS -L --compressed -A "$UA" -o token.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://ai.baidu.com/ai-doc/REFERENCE/Ck3dwjhhu'
# → HTTP=200 SIZE=871972 CT=text/html; charset=utf-8 URL=https://ai.baidu.com/ai-doc/REFERENCE/Ck3dwjhhu

# ③ 计费概述
curl --noproxy '*' -sS -L --compressed -A "$UA" -o billing.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://ai.baidu.com/ai-doc/OCR/Jk3h7xtsd'
# → HTTP=200 SIZE=873802 CT=text/html; charset=utf-8 URL=https://ai.baidu.com/ai-doc/OCR/Jk3h7xtsd

# ④ 免费测试资源
curl --noproxy '*' -sS -L --compressed -A "$UA" -o freetier.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://ai.baidu.com/ai-doc/OCR/fk3h7xu7h'
# → HTTP=200 SIZE=886397 CT=text/html; charset=utf-8 URL=https://ai.baidu.com/ai-doc/OCR/fk3h7xu7h

# ⑤ 产品价格（智能文档分析平台价格页；未含『增值税发票验真』单价）
curl --noproxy '*' -sS -L --compressed -A "$UA" -o price.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://ai.baidu.com/ai-doc/OCR/tlrzzplc1'
# → HTTP=200 SIZE=888669 CT=text/html; charset=utf-8 URL=https://ai.baidu.com/ai-doc/OCR/tlrzzplc1

# ⑥ 百度智能云价格页（财务票据正文未渲染，仅作核对记录）
curl --noproxy '*' -sS -L --compressed -A "$UA" -o cloudprice.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://cloud.baidu.com/doc/OCR/s/9k3h7xuv6'
# → HTTP=200 SIZE=236896 CT=text/html; charset=utf-8 URL=https://cloud.baidu.com/doc/OCR/s/9k3h7xuv6
```

**落盘校验（`wc -c` 与 `sha256sum`）：**

| 文件 | 字节数（=size_download） | sha256（前 16 位） |
|---|---|---|
| v1.html（渠道主文档） | 1002129 | `f175a2b201f3a065` |
| token.html | 871972 | `133cef60294af252` |
| billing.html | 873802 | `2ab8a9897603de75` |
| freetier.html | 886397 | `a948a1e4ee017ae6` |
| price.html | 888669 | `042dbdf2f3a5502a` |
| cloudprice.html | 236896 | `41693d2e7c8172c5` |

**关键字段行号（`grep -o -n` 于原始 HTML 的首次命中行号，证明字段确在页面中）：**

- **v1.html（主文档）**：`2026-08-04`（行 1770）、`支持 15 种`（行 1771）、`https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice_verification`（行 1780）、`invoice_code`（行 1825）、`invoice_num`（行 1832）、`invoice_date`（行 1839）、`格式YYYYMMDD`（行 1843）、`invoice_type`（行 1846）、`check_code`（行 1853）、`total_amount`（行 1860）、`log_id`（行 2351）、`words_result`（行 2357）、`VerifyResult`（行 2369）、`VerifyMessage`（行 2375）、`VerifyFrequency`（行 2381）、`InvalidSign`（行 2387）、`是否计费`（行 3528）、`0001`（行 3533）、`查验成功发票一致`（行 3534）、`9999`（行 3539）、`0002`（行 3545）、`0005`（行 3551）、`0006`（行 3557）、`0009`（行 3563）、`发票不存在`（行 3564）、`所查发票不存在`（行 3565）、`1014`（行 3593）、`1020`（行 3605）、`1021`（行 3611）。
- **token.html**：`2025-01-20`（行 1770）、`oauth/2.0/token`（行 1817）、`grant_type`（行 1819）、`client_id`（行 1820）、`client_secret`（行 1821）、`expires_in`/`有效期30天`（行 2318）、`invalid_client`（行 2335）。
- **billing.html**：`2026-06-05`（行 1770）、`10 QPS`（行 1857）、`不保证并发`（行 1875）、`保证10次并发`（行 1881）。
- **freetier.html**：`2026-09-11`（行 1770）、`增值税发票验真`（行 2116，正文单元格）、`个人认证：20次`（行 2117）、`企业认证：50次`（行 2118）。
- **price.html**：`2026-09-10`（行 1770）。

**自检声明：**

- 本文渠道内容均经本机实际抓取核对（直连，HTTP 200），非抄录他人转述；未调用任何真实查验接口，未使用任何真实凭据。
- 文中未出现任何真实 API Key/Secret Key/access_token/税号/票号；示例一律占位符。
- 抓取到的原始 HTML 落盘于仓库外临时目录（不随提交入库）；如复核需要可按本节命令原样重抓比对 sha256。
