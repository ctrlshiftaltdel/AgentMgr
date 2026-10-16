# W0-4 发票查验渠道调研与 Go/No-Go 评估

- 任务：W0-4（发票查验路径调研）
- 分支：task/W0-4-invoice-verification
- 输入：`templates/PLAN.md` §7（国家税务总局发票查验接入，重点 §7.1 可替换接口、§7.2 字段与状态）、`docs/research/W0-5-security-compliance.md`（字段分级与供应商条款审查清单）、`docs/DEPLOYMENT-REQUIREMENTS.md` §7（模型与数据边界）
- 范围：只产出本报告；不修改任何其它文件；不安装依赖；**不调用任何真实发票查验接口**；未申请、未持有、未使用任何 appId/appSecret/SecretId/SecretKey。
- 证据声明：本文两个渠道的全部技术细节均来自**本机 curl 实际抓取的公开网页**（命令、HTTP 状态码、字节数与关键字段行号见 §9）。凡抓取页面未列出的字段、错误码、价格、SLA、限频，本文一律标注「未提供/待确认」，**不编造**。
- 占位符声明：全文不含任何真实 appId、appSecret、税号或票号；示例值一律使用占位符（如 `appId=****`、`fpdm=150*****60`）。渠道 B 文章正文与示例代码中出现的 `appId`/`appSecret`/票号/税号字符串均为**该文章自带的示例占位串**，本文不复述、不采用。

> **⚠ 所有者决策更新（2026-10-16）**：本报告的渠道推荐顺序已被项目所有者决策**更新**——`authorized_provider` 的**首选**改为**百度智能云「增值税发票验真」**（适配器 `app/integrations/baidu_vat_verify.py`；渠道技术细节与实现说明见 `docs/research/W1-28-baidu-invoice-verification.md`）；**腾讯云** `VatInvoiceVerifyNew` 与**聚美智数** `invoice_validate_v2` **降为备用**（前者保留适配器实现、仅更正文首注释；后者仅文档留痕、不实现）；`manual_assisted` 兜底保留；**不新增渠道选择配置项或环境开关**。本报告历史正文不改写；其中「推荐/首选/备选」等表述一律以文末「§10 所有者决策更新（2026-10-16）」为准。

## 1. 背景与目的

- 项目需要对报销/采购环节取得的增值税发票做**真伪与票面一致性核验**，且核验结果要落库、可审计、可追溯（PLAN §5.2「票据附件、OCR 字段提取、发票查验状态」）。
- PLAN §7 明确规定：**不得把「有网页可查」当作「已有公开 API」**；全国增值税发票查验平台公开提供的是网页查验入口，当前未能确认面向任意企业直接调用的通用公开 API。因此 PLAN §7.1 要求先定义一个可替换的接口 `InvoiceVerificationProvider`，至少支持三类实现：
  1. `manual_assisted`：Agent/OCR 提取字段、去重并生成查验任务，授权财务人员通过官方平台完成查验并录入/导入结果、留存证据；
  2. `authorized_provider`：金蝶、经批准的税务/发票服务商或正式接入渠道，按其正式文档实施；
  3. `official_api`：只有在取得**可适用的官方 API 文档和合法授权**后才实现。
- PLAN §7.1 同时给出硬边界：**不通过绕过验证码、模拟登录或未授权爬虫来规避官方平台的访问控制**；没有可用接口时先落地人工辅助流程，**不得把 OCR 识别成功标记为「发票已验证」**。
- 本任务的目的，就是为 `authorized_provider` 一类筛选出可用的授权渠道，并逐项给出其能力、限制、合规与可审计性证据，供项目所有者决定是否接入、接入哪一个、以及以何顺序接入。
- **两个候选渠道**（由项目所有者提出）：
  - **渠道 A**：腾讯云「增值税发票核验（新版）」（官方产品，接口 `VatInvoiceVerifyNew`）。
  - **渠道 B**：「聚美智数」`invoice_validate_v2`（第三方厂商；本文证据仅为阿里云开发者社区的一篇**用户投稿文章**）。

## 2. 渠道 A 详述：腾讯云 增值税发票核验（新版）

- **来源 URL**：https://cloud.tencent.com/document/product/866/73674
- **抓取时间**：2026-10-16（本机 `curl`；HTTP 200，`size_download=54613` 字节，落盘 325265 字节；命令与校验见 §9）
- **页面标注最近更新时间**：`2026-10-13 03:09:51`（抓取页内「最近更新时间」字段）
- **性质**：**腾讯云官方产品文档**。页面层级为「文档中心 > 文字识别 > 服务端 API 文档 > 票据单据识别相关接口 > 增值税发票核验（新版）」。属于腾讯云「文字识别（OCR）」产品下的票据单据识别接口。
- **认证与调用方式**：腾讯云公共参数 + **签名方法 v3（TC3-HMAC-SHA256）**，凭证为 **SecretId / SecretKey**（密钥须严格保管，避免泄露）。签名方法文档：https://cloud.tencent.com/document/api/866/33519 （同次抓取，HTTP 200，`size_download=80663` 字节，页面标注最近更新 `2024-12-25`）。本页「输入参数」注明「完整公共参数列表见 公共请求参数」。

### 2.1 接口描述

- **接口请求域名**：`ocr.tencentcloudapi.com`
- **Action**：`VatInvoiceVerifyNew`（公共参数）
- **Version**：`2018-11-19`（公共参数）
- **Region**：可选公共参数
- **用途（页内原文要义）**：支持增值税发票的准确性核验，输入发票关键字段后，接口返回**真实的票面相关信息**，包括发票代码、发票号码、开票日期、金额、消费类型、购方名称、购方税号、销方名称、销方税号等多个常用字段。
- **支持票种**：增值税专用发票、增值税普通发票（含电子普通发票、卷式发票、通行费发票）、全电发票、机动车销售统一发票、货物运输业增值税专用发票、二手车销售统一发票、通用机打电子发票（**仅广东和浙江**）。
- **默认接口请求频率限制**：**20 次/秒**。

### 2.2 入参（页内「输入参数」表，逐字核对）

| 参数 | 必选 | 类型 | 页面描述要点 |
|---|---|---|---|
| `InvoiceNo` | 是 | String | 发票号码，**8 位、20 位（全电票）** |
| `InvoiceDate` | 是 | String | 开票日期，**不支持当天发票查询**，支持**五年以内**开具的发票，格式 `YYYY-MM-DD` |
| `InvoiceCode` | 否 | String | 发票代码（**10 或 12 位**），全电发票为空；**查验超过 5 次后当日无法再查** |
| `InvoiceKind` | 否 | String | 票种类型：`01` 增值税专用发票、`02` 货运运输业增值税专用发票、`03` 机动车销售统一发票、`04` 增值税普通发票、`08` 增值税电子专用发票(含全电)、`10` 增值税电子普通发票(含全电)、`11` 增值税普通发票(卷式)、`14` 增值税电子(通行费)发票、`15` 二手车销售统一发票、`16` 财务发票、`32` 深圳区块链发票（云南区块链因业务调整现已下线） |
| `CheckCode` | 否 | String | 校验码**后 6 位**；增值税普通发票、增值税电子普通发票、增值税普通发票(卷式)、增值税电子普通发票(通行费)、全电纸质发票（增值税普通发票）、财政票据时**必填**；区块链为 **5 位** |
| `Amount` | 否 | String | **不含税金额**：增值税专用发票、增值税电子专用发票、机动车销售统一发票、二手车销售统一发票、区块链发票、财政发票时必填；**全电发票为价税合计(含税金额)** |
| `RegionCode` | 否 | String | 地区编码，**通用机打电子发票时必填**；广东 `4400`，浙江 `3300` |
| `SellerTaxCode` | 否 | String | 销方税号，**通用机打电子发票必填**，区块链发票时必填 |
| `EnableCommonElectronic` | 否 | Boolean | 是否开启通用机打电子发票，**默认为关闭** |
| `EnableTodayInvoice` | 否 | Boolean | 是否允许查验**当日发票**，**默认值为 false**；打开后**仅支持查验已成功录入到税局中的当日发票**（发票从开具到录入税局需要一定时间更新验证） |

> 注意：`InvoiceKind` 未在页面上标为「必填」，但票种不同会改变 `Amount`/`CheckCode` 的语义，实际接入时须按票种显式传值。

### 2.3 出参（页内「输出参数」表）

顶层返回：

| 字段 | 类型 | 说明 |
|---|---|---|
| `Invoice` | `VatInvoice` | 增值税发票、购车发票、全电发票的基础要素字段信息 |
| `VehicleInvoiceInfo` | `VehicleInvoiceInfo` | 机动车销售统一发票详细字段 |
| `UsedVehicleInvoiceInfo` | `UsedVehicleInvoiceInfo` | 二手车销售统一发票详细字段 |
| `PassInvoiceInfoList` | `Array of PassInvoiceInfo` | 通行费发票详细字段 |
| `ElectronicTrainTicket` | `ElectronicTrainTicket` | 全电发票（铁路电子客票）详细字段 |
| `ElectronicAirTransport` | `ElectronicAirTransport` | 全电发票（航空运输电子客票行程单）详细字段 |
| `FinancialBill` | `FinancialBill` | 财政发票详细字段 |
| `RequestId` | String | 唯一请求 ID，每次请求返回；定位问题须提供 |

`Invoice`（`VatInvoice`）在页面「输出示例」中出现的字段（抓取所见，非穷举）：`AmountWithTax`、`AmountWithoutTax`、`BuyerAddressPhone`、`BuyerBankAccount`、`BuyerName`、`BuyerTaxCode`、`CheckCode`、`Code`、`Date`、`ElectronicType`、`HasSellerList`、**`IsAbandoned`（作废标志，示例值 `"N"`）**、`Items`（数组）、`MachineNo`、`Number`、`Remark`、**`RedLetterInvoiceMark`（红字发票标志，示例值 `false`）**、`IssuingTypeMark`（示例值 `1`）、`SellerAddressPhone` 等。

> **诚实标注**：本次抓取的该页输出部分**未列出独立的「税额」字段，也未列出「币种」字段**。PLAN §7.2 需要的「税额/币种」在本渠道下需以真实响应或完整字段表核对（见 §8）。页面输出的 `IsAbandoned`、`RedLetterInvoiceMark`、`HasSellerList`、`ElectronicType`、`IssuingTypeMark` 为票面状态/结构标志。

### 2.4 错误码（页内「6. 错误码」）

抓取页内列出的错误码（公开文档内容，非编造）：`FailedOperation.ArrearsError`、`FailedOperation.CountLimitError`、`FailedOperation.InvoiceMismatch`、`FailedOperation.UnKnowError`、`FailedOperation.UnOpenError`、`ResourceNotFound.NoAreaCode`、`ResourceNotFound.NoInvoice`、`ResourceNotFound.NotSupportCurrentInvoiceQuery`（不支持当天发票查询）、`ResourceUnavailable.InArrears`（账号已欠费）、`ResourceUnavailable.ResourcePackageRunOut`、`ResourceUnavailable.TaxNetworkError`。

### 2.5 限制小结

- **当日发票**：默认**不支持**（`InvoiceDate` 明确「不支持当天发票查询」）；`EnableTodayInvoice` 打开后**仅**支持已成功录入税局的当日发票——即「当日可查」是**有条件**的，取决于税局数据更新时延。
- **时间范围**：支持五年以内开具的发票。
- **查验次数**：`InvoiceCode` 说明「查验超过 5 次后当日无法再查」（同一张票当日重复查验有限次）。
- **限频**：默认 20 次/秒。
- **票种覆盖**：通用机打电子发票仅广东、浙江；深圳区块链发票可用，云南区块链已下线。

## 3. 渠道 B 详述：聚美智数 invoice_validate_v2（第三方厂商）

- **来源 URL**：https://developer.aliyun.com/article/1707407
- **抓取时间**：2026-10-16（本机 `curl`；HTTP 200，`size_download=18818` 字节，落盘 91238 字节；命令与校验见 §9）
- **文章标题（页内）**：电子税务平台 Api 接口-发票查验-发票验真-发票核验-发票查验真伪
- **文章日期（页内）**：2026-01-20
- **⚠ 性质与证据强度（关键）**：这是**阿里云开发者社区的一篇用户投稿文章**，**不是阿里云自有产品**。页尾版权声明原文要义：**「本文内容由阿里云实名注册用户自发贡献，版权归原作者所有，阿里云开发者社区不拥有其著作权，亦不承担相应法律责任」**。文中 Java 示例的注释显示服务厂商为「**聚美智数**」，`appId`/`appSecret` 由聚美智数分配。因此本渠道的全部信息**证据等级仅为「第三方厂商经由社区投稿文章的单方描述」**，未经腾讯云式官方文档背书，也未经本项目实测。

### 3.1 接口与签名

- **productCode**：固定值 `invoice_validate_v2`（文章注释「// 固定值」）。
- **签名算法**：`sign = sha256(appId + appSecret + timestamp)`，参数 `appId`、`timestamp`、`sign`。
- **apiUrl**：**文章未给出**（示例代码中 `String apiUrl = ""`，为空字符串）。
- **请求方式（文章示例）**：`HttpUtil.postForm(apiUrl, null, params)`（表单 POST）。

### 3.2 入参（文章「输入参数」表，逐字核对）

| 名称 | 类型 | 必须 | 说明 |
|---|---|---|---|
| `appId` | String | 是 | 用户唯一标识（聚美智数分配） |
| `timestamp` | Long | 是 | 当前时间戳 |
| `sign` | String | 是 | 签名，`sha256(appId + appSecret + timestamp)` |
| `fpdm` | String | 否 | 发票代码，**非全电发票必填** |
| `fphm` | String | 是 | 发票号码 |
| `kprq` | String | 是 | 开票日期，格式 `YYYYMMDD` |
| `jym` | String | 否 | 校验码**后 6 位**；增值税普通发票、增值税电子普通发票、增值税普通发票（卷式）、增值税电子普通发票（通行费）必填 |
| `je` | String | 否 | 增值税专用发票、增值税电子专用发票、机动车销售统一发票输入**不含税金额**；二手车销售统一发票输入**车价合计**；全电发票输入**价税合计** |

### 3.3 出参（文章「响应示例」，以增值税专用发票为例）

顶层：`code`（返回码，示例 `200`）、`msg`（示例「成功」）、`taskNo`（**本次唯一请求号**）、`charge`（**计费标志**，示例 `true`）、`data{…}`。

`data` 字段（抓取所见）：`fplx`（发票类型，注释「详见发票类型说明」）、`times`（查验次数，第几次）、`xfsbh`（销方识别号）、`gfmc`（购方名称）、`gmfyhzh`（购买方银行账号）、`xhqdBz`（**清单标志**：Y 清单 / N 非清单）、`dkXsfmc`（代开销售方名称）、`dq`（地区）、`tspzDm`（**特殊票种代码**，含 02 农产品收购、08 成品油、06 通行费可抵扣等一长串代码）、`xsfdzdh`（销售方地址电话）、`se`（税额）、`kprq`（开票日期）、`bz`（备注）、`kjlx`（开具类型）、**`fpztDm`（发票状态代码，示例 `"0"`）**、`sbbh`（设备编号）、`gfsbh`（购方识别号）、`fpdm`（发票代码）、`fphm`（发票号码）、`jym`（校验码）、`dkXsfsbh`（代开销售方识别号）、`gmfdzdh`（购买方地址电话）、`xsfyhzh`（销售方银行账号）、`jshj`（价税合计）、`jshjcn`（价税合计中文大写）、`je`（金额）、`xfmc`（销方名称）、`txfbz`（通行费标识，Y 可抵扣 / N 不可抵扣）、**`hwxx[]`（货物信息：`ggxh` 规格型号、`jldw` 计量单位、`dj` 单价、`mxxh` 明细序号、`se` 税额、`ysse` 原始税额、`mc` 名称、`sl` 数量、`je` 金额、`slv` 税率、`ysslv` 原始税率、`spbm` 商品编码）**。

### 3.4 声称能力（文章自述，未经本项目验证）

- **支持票种**：增值税专用发票、普通发票、机动车销售发票、二手车销售发票、通行费发票、航空运输电子客票行程单、铁路电子客票、财政发票、区块链发票、通用电子发票。
- **时间范围**：可查验**最近 5 年**内开具的发票；**当日开具的发票当日可进行查验**。
- **缺失信息（文章未提供）**：`apiUrl`、**完整错误码表**（文中仅出现 `code=200` 示例，注释写「详见 code 返回码说明」但未给出表）、**SLA**、**限频**、**计费单价**（仅有 `charge` 布尔标志）、**数据存储与留存说明**。

## 4. 对照表

| 维度 | 渠道 A：腾讯云 `VatInvoiceVerifyNew` | 渠道 B：聚美智数 `invoice_validate_v2` |
|---|---|---|
| **性质** | **官方产品**（腾讯云文字识别产品下的公开 API） | **第三方厂商**；证据仅为**阿里云开发者社区用户投稿文章**（页尾声明阿里云不拥有著作权、不承担法律责任） |
| **证据来源与等级** | 腾讯云官方文档（页面最近更新 2026-10-13）；抓取 2026-10-16 | 社区投稿文章（2026-01-20）；抓取 2026-10-16；**单方描述，未经官方背书** |
| **认证方式** | 腾讯云公共参数 + 签名方法 v3（TC3-HMAC-SHA256），**SecretId/SecretKey** | `appId`/`timestamp`/`sign`，`sign = sha256(appId + appSecret + timestamp)`（appId/appSecret 由厂商分配） |
| **接口地址** | `ocr.tencentcloudapi.com`（Action/Version 明确） | **未提供**（文章示例 `apiUrl = ""`） |
| **必填字段差异** | 必填：`InvoiceNo`、`InvoiceDate`；其余按票种条件必填（`CheckCode`/`Amount`/`RegionCode`/`SellerTaxCode`）；`InvoiceKind` 页内未标必填 | 必填：`appId`、`timestamp`、`sign`、`fphm`、`kprq`；`fpdm` 非全电必填；`jym`/`je` 按票种条件必填 |
| **当日发票支持** | 默认**不支持**；`EnableTodayInvoice` 打开后**仅**支持已录入税局的当日发票（有条件） | 文章自述**当日开具当日可查验**（未经验证） |
| **时间范围** | 五年以内 | 最近 5 年（自述） |
| **限频** | 文档明确：**20 次/秒**；且同一票「查验超过 5 次后当日无法再查」 | **未提供** |
| **错误码可得性** | 文档列出 11 个错误码（含 `NotSupportCurrentInvoiceQuery`、`InvoiceMismatch`、`TaxNetworkError`、欠费/资源包等） | **未提供错误码表**（仅 `code=200` 示例） |
| **SLA 与计费透明度** | 属腾讯云正式产品，计费/配额/欠费错误码在文档中可见（`ResourceUnavailable.InArrears` 等）；具体单价与 SLA 需按腾讯云产品定价/协议确认 | **计费单价、SLA 均未提供**；仅有 `charge` 布尔标志 |
| **输出：作废/红字标志** | **有**：`IsAbandoned`（作废）、`RedLetterInvoiceMark`（红字） | **无**明确的作废/红字布尔标志；仅有 `fpztDm` 发票状态代码（示例 `"0"`，**代码含义文章未定义**） |
| **输出：货物明细** | `Invoice.Items[]`（示例为空数组；结构未在本页详列） | **有**：`hwxx[]`（含名称、规格型号、计量单位、数量、单价、金额、税率、税额、商品编码） |
| **输出：发票状态码** | 以 `IsAbandoned`/`RedLetterInvoiceMark`/`IssuingTypeMark` 等标志表达；**无**单一「发票状态代码」字段 | **有** `fpztDm`（发票状态代码），但**码表未给出** |
| **可审计性与追责对象** | 追责对象为**腾讯云**（正式产品、有合同/条款、有 `RequestId`、有公开错误码与定价） | 追责对象为**聚美智数（第三方厂商）**；且经由社区文章描述，阿里云**明确不承担法律责任**；`taskNo` 可作请求号，但无公开条款支撑 |

## 5. 结论与建议

- **推荐腾讯云 `VatInvoiceVerifyNew` 作为 `authorized_provider` 的「首选实现」。** 理由：官方产品文档、接口/版本/域名/入参/出参/限频/错误码齐全，认证与计费透明，追责对象明确，且返回 `IsAbandoned`/`RedLetterInvoiceMark` 等状态标志，可直接支撑 PLAN §7.2 的「查验通过 / 查验不通过」判定与作废/红字识别。其唯一显著短板是**默认不支持当日发票**（`EnableTodayInvoice` 仅为有条件放行），这与「报销单常在开票当日提交」的业务节奏存在摩擦，需以流程兜底（见下）。
- **聚美智数 `invoice_validate_v2` 建议作为「备选适配器」，不作首选。** 其突出卖点是**当日可查**与**含货物明细 `hwxx[]`**，但证据等级低（社区投稿文章）、`apiUrl`/错误码/SLA/限频/计费单价均缺失、追责对象为第三方且无官方条款支撑。若确需当日查验能力，须先取得聚美智数的**正式接口文档、SLA、错误码表与数据处理协议**，方可评估启用。
- **两者必须置于同一个 `InvoiceVerificationProvider` 接口之后**（PLAN §7.1），实现类分别如 `TencentVatVerifyNewProvider`、`JumeiInvoiceValidateV2Provider`、`ManualAssistedProvider`；上层业务只依赖接口，切换渠道不影响业务域（与 W0-1 §5「渠道适配层」同一思路）。
- **`official_api` 目前标记为「不可用」。** PLAN §7 已明确：当前未能确认面向任意企业直接调用的通用公开官方 API；在没有取得可适用官方文档与合法授权前，不实现 `official_api`。**不得**以网页抓取、模拟登录、绕过验证码等方式替代。
- **`manual_assisted` 作为兜底保留。** 当票面要素缺失、渠道限流/异常、或票种不在渠道覆盖范围（如非广东/浙江的通用机打电子发票）时，退化为人工辅助：生成查验任务、由授权财务人员经官方平台查验、录入结果并留存证据。**任何情况下都不得把 OCR 识别成功当作「查验通过」。**
- **降级/回退顺序建议**：腾讯云（首选）→ 聚美智数（备选，仅在取得正式文档与合规确认后）→ 人工辅助（兜底）。

## 6. 数据合规评估

- **发送给第三方云厂商的字段**（以渠道 A 的入参为准，渠道 B 类似）：发票号码、发票代码、开票日期、金额、校验码后 6 位、销方税号（部分票种）、地区编码。这些都是**发票关键字段**，其中**发票号码/代码、购销方税号**在 W0-5 的字段分级中属**敏感字段**。
- **必要性**：这些字段是**完成核验所必需**的最小集合——渠道需要它们与税局票面比对才能返回真伪与票面信息。**因此「把发票关键字段发送给第三方云厂商」是使用 `authorized_provider` 的固有前提**，无法通过字段最小化完全消除；能做的只是「按票种只传该票种必需的字段」，不多传（例如全电票不传 `InvoiceCode`，非相关票种不传 `Amount`）。
- **最小化与脱敏要求**：
  - 仅传输核验所必需的字段，**不额外附带**员工姓名、身份证、银行卡、报销事由等无关个人信息；
  - 发送前对字段做**按票种裁剪**；日志与审计中**只记状态、结果码、请求号（`RequestId`/`taskNo`）与脱敏后的字段**，不落票号/税号原文（对齐 W0-5 §3.2 日志禁止项与 `docs/DEPLOYMENT-REQUIREMENTS.md` §7「日志禁止项：凭据与 token、完整渠道用户 ID、附件原文、OCR 原文全文、A 类字段原文」）。
- **字段分级引用（W0-5 §3.1）**：
  - 发票号码与代码 → **B 类（必须脱敏后才可入模型）**：保留可核验的少量位、其余打码；查验所需原文**仅在后端处理，不进模型**。
  - 公司税号 → **B 类**：保留前若干位、其余打码或替换为内部代号。
  - 发票查验凭证内容 → **A 类（禁止进入模型提示词与日志）**：原文只存受控附件存储，模型/日志只见状态与引用 ID。
  - 开票日期、金额 → **C 类（可入模型，仍按任务最小化）**。
- **出境与供应商审查**：W0-5 §2.2 与 PLAN §3.1 确立「首期仅中国大陆、数据默认不出境」。腾讯云为**大陆主体**、渠道 B 的厂商亦为大陆主体，但**接入前仍须按 W0-5 §6 的九项审查清单取得书面证据**（数据用途、留存、是否用于训练、访问控制、处理区域、删除机制、子处理者、泄露通知、DPA 可得性），**审查未通过即不得接入**，不得以营销页面或口头承诺替代。
- **结论**：本报告不替代法律意见；「是否接受把发票关键字段发送给腾讯云（大陆主体）」属**需项目所有者决定**的合规取舍（见 §7）。

## 7. 待项目所有者确认项

以下各项**由项目所有者决定，本文不代为选择**：

1. **公司是否已有腾讯云账号并开通「文字识别」下的增值税发票核验（新版）服务？** 若需新开通，账号归属、实名主体与费用承担如何安排？
2. **预计调用量与费用承受度**：每月大致查验票数、可接受的单次调用价格与月度预算上限？（腾讯云为按量计费，渠道 B 单价未公开。）
3. **是否接受把发票关键字段（票号、代码、开票日期、金额、购销方税号）发送给腾讯云（大陆主体）？** 这是使用 `authorized_provider` 的前提，需与 §6 的合规判断一并确认。
4. **是否同时需要聚美智数作为备选适配器？** 若需要，是否接受「先取得其正式接口文档、SLA、错误码表与数据处理协议后再评估」的前置条件？
5. **「当日发票」这一能力的优先级**：业务是否强依赖开票当日即可查验？若是，是否接受先走「人工辅助兜底 + 次日渠道核验」的过渡流程，而非立即启用证据较弱的第三方？
6. **凭证注入方式**：SecretId/SecretKey（或 appId/appSecret）**必须经环境变量或密钥存储注入，不得写入仓库、日志或模型提示词**；请确认使用的密钥存储方案（Profile 私有目录 / 系统密钥环 / 部署层注入）。
7. **`InvoiceVerificationProvider` 的接口契约与持久化字段**：是否由 Master 统一审定（PLAN §8 要求 schema 由 Master 统一审定，不允许各 Agent 各自建立冲突 schema）？

## 8. 与 PLAN §7.2 的字段与状态对应

PLAN §7.2 要求保存的字段，在本渠道下的来源（「本项目记录」表示渠道不返回、由业务系统自身生成并落库）：

| PLAN §7.2 要求字段 | 渠道 A（腾讯云）来源 | 渠道 B（聚美智数）来源 | 备注 |
|---|---|---|---|
| 发票代码 | `Invoice.Code` | `data.fpdm` | 按票种结构；全电票代码可为空 |
| 发票号码 | `Invoice.Number` | `data.fphm` | 按票种结构（8 位/20 位） |
| 开票日期 | `Invoice.Date` | `data.kprq` | A 入参 `InvoiceDate` 为 `YYYY-MM-DD`；B 入参 `kprq` 为 `YYYYMMDD` |
| 销售方税号 | `Invoice.SellerTaxCode` | `data.xfsbh` | 受权限保护 |
| 购买方税号 | `Invoice.BuyerTaxCode` | `data.gfsbh` | 受权限保护 |
| 金额 | `Invoice.AmountWithoutTax` | `data.je` | 不含税金额 |
| 税额 | **本页输出未列出独立字段，待以真实响应/完整字段表核对** | `data.se`（票面税额）；明细 `hwxx[].se` | **A 渠道此项须核实**，勿臆造字段名 |
| 价税合计 | `Invoice.AmountWithTax` | `data.jshj` | |
| 币种（适用时） | **本页未列币种字段** | **文章未列币种字段** | 两者均未提供；默认人民币，需按真实响应确认 |
| 票据类型 | 入参 `InvoiceKind`（票种代码）；输出 `Invoice` 含 `ElectronicType`/`IssuingTypeMark` | `data.fplx`（发票类型） | 类型判定以入参票种 + 输出类型字段共同确定 |
| 原件引用 | **本项目记录**（附件 `storage_key`/`sha256`） | **本项目记录** | 渠道不返回原件 |
| 重复检查结果 | **本项目记录**（业务去重；渠道不提供重复判定） | **本项目记录**（`data.times` 是「查验次数」而非重复标志，不可当重复结果） | 见 §8 状态「疑似重复」 |
| 查验渠道 | **本项目记录**：`tencent_cloud/vat_invoice_verify_new` | **本项目记录**：`jumei/invoice_validate_v2` | 记录 provider 标识 |
| 查验时间 | **本项目记录**（渠道不返回查验时间） | **本项目记录**（`kprq` 是开票日期，非查验时间） | |
| 操作人 | **本项目记录**（`actor_id`，关联实际人员） | **本项目记录** | 不得写模型名字/角色名 |
| 结果状态 | 见下表映射 | 见下表映射 | |
| 证据引用 | **本项目记录**：保存脱敏后的原始响应 + `RequestId` | **本项目记录**：保存脱敏后的原始响应 + `taskNo` | 证据原文按 A 类字段处理（W0-5 §3.1） |

**七种状态如何获得（映射）**：

| PLAN §7.2 状态 | 渠道 A 判定依据 | 渠道 B 判定依据 |
|---|---|---|
| **待识别** | OCR 尚未完成或未产出结构化字段（渠道无关） | 同左 |
| **待人工确认** | OCR 字段置信度低或关键字段缺失，需人工确认后方可进入查验 | 同左 |
| **待查验** | 字段齐备、已去重，尚未调用渠道 | 同左 |
| **查验通过** | 渠道返回成功且票面要素与提取字段**一致**（无 `InvoiceMismatch`，返回 `Invoice` 且关键字段匹配） | `code=200` 且 `data` 字段与提取字段一致 |
| **查验不通过** | `FailedOperation.InvoiceMismatch`（票面不一致）；或返回票面为**作废**（`IsAbandoned`）/红字（`RedLetterInvoiceMark`）等需否决的情形（具体策略由业务定义） | `code` 非 200（**错误码表缺失，具体码含义待厂商文档确认**） |
| **无法查验/接口异常** | `ResourceUnavailable.TaxNetworkError`、`ResourceUnavailable.InArrears`、`ResourceUnavailable.ResourcePackageRunOut`、`FailedOperation.UnKnowError`、`FailedOperation.CountLimitError`、`ResourceNotFound.NoInvoice`、`ResourceNotFound.NoAreaCode`、`ResourceNotFound.NotSupportCurrentInvoiceQuery`（当日票） | 网络异常/超时；或返回未知 `code`（码表缺失）；`apiUrl` 未知导致的接入异常 |
| **疑似重复** | **本项目去重逻辑**（渠道不提供重复判定；`InvoiceCode` 的「查验超过 5 次」是限次而非重复标志） | **本项目去重逻辑**（`data.times` 为查验次数，非重复结果） |

**硬性规则（对齐 PLAN §7.2 与 §7.1）**：

- **仅来自被认可渠道的结果才能进入「查验通过」**：即 `authorized_provider`（腾讯云首选 / 聚美智数备选）或 `manual_assisted`（授权财务人员经官方平台核验并留存证据）产出的结果。**OCR 识别成功、模型自述「看起来是真的」、或未授权的网页抓取结果，一律不得置为「查验通过」。**
- 「疑似重复」由**业务去重**产生，不依赖渠道；渠道返回的 `times`/「查验次数」**不得**当作重复标志。
- 「无法查验/接口异常」与「查验不通过」必须严格区分：前者是可重试/可降级的技术性失败，后者是**票面核验结论为不一致**，不得互相覆盖。

## 9. 抓取证据与自检

**抓取命令与返回（本机 `curl 8.14.1`，出网经 Privoxy 代理）：**

```bash
UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'

# 渠道 A
curl -sS -L --compressed -A "$UA" -o A.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://cloud.tencent.com/document/product/866/73674'
# → HTTP=200 SIZE=54613 CT=text/html; charset=utf-8 URL=https://cloud.tencent.com/document/product/866/73674
#   落盘 325265 字节

# 渠道 A 签名方法 v3
curl -sS -L --compressed -A "$UA" -o C.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://cloud.tencent.com/document/api/866/33519'
# → HTTP=200 SIZE=80663 CT=text/html; charset=utf-8 URL=https://cloud.tencent.com/document/api/866/33519
#   落盘 490703 字节

# 渠道 B
curl -sS -L --compressed -A "$UA" -o B.html \
  -w 'HTTP=%{http_code} SIZE=%{size_download} CT=%{content_type} URL=%{url_effective}\n' \
  'https://developer.aliyun.com/article/1707407'
# → HTTP=200 SIZE=18818 CT=text/html;charset=UTF-8 URL=https://developer.aliyun.com/article/1707407
#   落盘 91238 字节
```

**实际抓取到的关键字段（`grep -o -n` 于原始 HTML 的行号，证明字段确在页面中）：**

- 渠道 A：`VatInvoiceVerifyNew`（行 554）、`ocr.tencentcloudapi.com`（行 545）、`InvoiceKind`（行 611）、`CheckCode`（行 617）、`RegionCode`（行 629）、`SellerTaxCode`（行 635）、`EnableTodayInvoice`（行 647）、`IsAbandoned`（行 733）、`RedLetterInvoiceMark`（行 738）、`20次/秒`（行 547）、`最近更新时间：2026-10-13`（行 545）。
- 渠道 A 签名页：`TC3-HMAC-SHA256`、`SecretId`、`SecretKey`、`最近更新时间：2024-12-25`（均命中）。
- 渠道 B：`聚美智数`（行 336）、`invoice_validate_v2`（行 348）、`fpdm`（行 293）、`fphm`（行 294）、`kprq`（行 287）、`jym`（行 295）、`fpztDm`（行 290）、`hwxx`（行命中）、`sha256`（行 259）、`taskNo`（行 272）、`charge`（行 273）、`实名注册用户…版权归原作者…不拥有其著作权`（行 169）。

**自检声明：**

- 本文两个渠道的技术内容**均经本机实际抓取核对**，非抄录他人转述；未调用任何真实查验接口，未使用任何真实凭证。
- 文中未出现任何真实 appId/appSecret/税号/票号；示例一律占位符。
- 渠道 B 正文中出现的 `appId`/`appSecret`/票号/税号字符串为该文章自带的**示例占位串**，本文未采用。

## 10. 所有者决策更新（2026-10-16）

> 本节为**增补节**（历史正文不改写）。所有者 2026-10-16 决策后，本报告 §5 的渠道推荐顺序与相关表述一律以本节为准。

- **决策原话**：

  > 「发票验真系统我想用 https://ai.baidu.com/ai-doc/OCR/cklbnrnwe 作为首选，之前发给你的两个作为备用，不需要设置选项，留在注释或说明文档中即可」

- **落地映射**：
  - **首选**：百度智能云「增值税发票验真」（`POST https://aip.baidubce.com/rest/2.0/ocr/v1/vat_invoice_verification`；页面更新时间 2026-08-04；支持 15 种票种）。适配器 `app/integrations/baidu_vat_verify.BaiduVatInvoiceVerifyProvider`（`provider_name=baidu_cloud/vat_invoice_verification`）；调研、映射与实现说明见 `docs/research/W1-28-baidu-invoice-verification.md`。
  - **备用 1**：腾讯云 `VatInvoiceVerifyNew`（`app/integrations/tencent_vat_verify` **保留为备用实现**；除文首注释外**不改变其行为**，可随时启用）。
  - **备用 2**：聚美智数 `invoice_validate_v2`（**仅文档备用、不实现**；若启用须先取得正式接口文档、SLA、错误码表与数据处理协议）。
  - **不做事先可选项**：**不新增**任何渠道选择配置项或环境开关（所有者明确「不需要设置选项」；备选信息只留在注释与说明文档中）。
  - **兜底**：`manual_assisted` 人工辅助流程保留（票面要素缺失、渠道异常或票种不在渠道覆盖范围时退化；**OCR 识别成功不得当作「查验通过」**）。
- **§7 待确认项逐条重述状态**（历史条目原文见 §7，此处只更新状态）：
  1. **腾讯云账号/开通**：降为备用；转为「备用启用前提」——仅当百度渠道不可用或经所有者批准切换时再逐项确认。
  2. **调用量与费用承受度**：仍待所有者确认；**新增百度侧**：免费额度（个人认证 20 次 / 企业认证 50 次；抓取证据见 W1-28 §6）、按量后付费/资源包预算与单价待明确。
  3. **数据发送给云厂商的合规取舍**：腾讯云项转为备用前提；**新增百度侧**：向百度智能云（大陆主体）发送发票关键字段（票号/代码/开票日期/金额/校验码/购销方税号）仍须所有者确认，并按 W0-5 §6 九项审查取得书面材料（**审查未通过前不得接入**）。
  4. **是否同时需要聚美智数**：仍为备用（仅文档、未实现）；启用前须先取得其正式接口文档、SLA、错误码表与数据处理协议。
  5. **「当日发票」优先级**：问题保留；**更新**为百度侧限制——当日发票不支持（结果码 `1014`），超 5 年不支持（`1015`）；流程兜底（人工辅助 + 次日渠道核验）不变。
  6. **凭证注入方式**：**更新**为百度侧变量名 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY`（env-only、fail closed、不落仓库/日志/模型提示词）；密钥存储方案（部署层注入/密钥环）待确认。
  7. **接口契约与持久化字段由 Master 审定**：已按 W1-20/W1-28 落地（`InvoiceVerificationProvider` 契约不变；百度适配器实现 `authorized_provider`）。
- **新增（百度侧）待确认项**：账号/实名/开通与权限（`1020 没有查验权限`）；费用与计费口径（「是否计费」列与《计费概述》「调用失败不计费」的关系）；QPS/SLA（主文档未列本接口 QPS）；合规审查材料；`0009` 等结果码的业务口径（当前与腾讯云保持一致归「无法查验」）；`InvalidSign` 未文档化取值与失败响应顶层 `error_code`（当前均 fail closed）。逐项细节见 `docs/research/W1-28-baidu-invoice-verification.md` §5。
