# W2 OCR/ASR 供应商选型报告与接入设计

> **状态：调研与设计，未接入。** 本文不调用任何真实 OCR/ASR 接口，不申请账号，不新增依赖，
> 不包含真实凭据、企业主体或票据信息。文中所有「推荐」「建议」均为**给所有者的决策输入**，
> 不是已实现能力。
>
> - 关联 Issue：#23（OCR/ASR 供应商选型报告与接入设计）
> - 代码基线：`app/intake/parsing.py`（`ExternalParser` / `ParserRegistry` / `ParseRequest` / `ParseResult`）、
>   `app/documents/storage.py`（`read_file`）、`app/documents/models.py`（`SourceFile`）
> - 既有设计：[../INTAKE-PARSING-PIPELINE.md](../INTAKE-PARSING-PIPELINE.md)
> - 同生态既有调研：[W1-28-baidu-invoice-verification.md](W1-28-baidu-invoice-verification.md)（发票验真，与本报告相互独立）
> - **查阅日期：2026-10-18**（本文全部外部来源，见 §10）

## 0. 一页结论

### 0.1 推荐组合

| 角色 | 供应商 | 产品 / 接口 | 选择理由（均有来源，见 §4） | 主要代价 |
|---|---|---|---|---|
| **OCR 主** | 腾讯云 | 通用票据识别（高级版）`RecognizeGeneralInvoice` | 单接口覆盖 14 大类标准报销票据，支持一页多票混贴、PDF 最多 30 页；业务错误码在文档中逐条列出；默认 5 次/秒对小团队足够 | **不返回逐字段置信度**（页面未提及），需要固定置信度 + 强制人工复核 |
| **OCR 备** | 阿里云 | `RecognizeInvoice`（增值税发票）+ `RecognizeMixedInvoices`（混贴）+ 各票种专用接口 | 唯一在官方文档中明确给出**逐字段置信度**（`keyProb` / `valueProb`）的候选，最贴合 `ParseResult.field_confidence` 契约；默认 10 QPS | 按票种拆多个接口，接入与路由成本最高；增值税发票单价 0.225 元/次起（月≤1 万次档） |
| **OCR 第三 / 私有化选项** | 百度智能云 | 智能财务票据识别 `multiple_invoice` / 增值税发票识别 `vat_invoice` | 企业认证免费额度最高（增值税发票 2,000 次/月），智能财务票据阶梯价 0.100 元/次起；官方提供私有化部署 | 图片上限 4M / 最长边 4096px（更紧）；`probability` 是**整票分类置信度**，不是逐字段置信度 |
| **ASR 主** | 腾讯云 | 一句话识别（≤60s）+ 录音文件识别（≤5h） | 短音频 23 种方言、词级时间戳；长音频最多 5 小时、支持句级分段（`ResTextFormat=3`） | 长音频为异步任务，需轮询/回调与 24 小时结果有效期管理 |
| **ASR 备** | 阿里云 | 录音文件识别极速版（≤2h） | 同步返回、句/词级时间戳、100MB；接入简单 | 仅商用版，不支持试用；>2h 需换普通版（未评估） |

**一句话**：OCR 先用**腾讯云单接口**把「能不能用」验证掉，再用**阿里云**补「逐字段置信度」；
若「数据不出内网」是硬约束，直接评估**百度私有化**，不要走公网 API。
ASR 与 OCR 同选腾讯云可复用一套签名与凭据管理。

### 0.2 继续只用人工兜底（`ManualEntryParser`）的适用条件

以下任一条成立时，**不应接入外部供应商**，继续用现有
[人工录入兜底](../INTAKE-PARSING-PIPELINE.md)：

1. **合规审查未完成**：未取得数据处理协议（DPA）、未完成个人信息保护影响评估（PIPIA）、
   未确认等保/审计要求（见 §7）。这是**阻塞条件**，不是「以后补」。
2. **月票据量低于人工可承受阈值**：建议以 **100 张/月**为观察线（**本报告建议值，非厂商数据**）；
   低于该量级时，接入、联调、对账与合规成本高于节省的人力。
3. **票种以非标/手写/严重折损为主**：三家厂商的准确率宣传均针对标准版式；非标票种的
   实际效果必须用自有样本验证（见 §4.3）。
4. **无预算/无实名主体/无稳定网络出口**：任一缺失都会让「接入」变成不可运维的依赖。
5. **试点期（建议前 1–2 个月）**：先以人工兜底跑通业务闭环，再用真实票据做供应商盲测。

### 0.3 本报告**没有**做的事

- 未注册/未登录任何云账号，未申请 API Key，未调用任何 OCR/ASR 接口；
- 未验证任何厂商的**实际识别效果**、**真实账单**、**SLA 赔付条款**、**数据处理协议正文**；
- 未新增依赖、未修改 `app/`、未新增迁移；
- 未对「腾讯云 / 阿里云 / 百度智能云」做商务询价。

因此，本文的「推荐」是**基于公开文档的适配度判断**，不是「实测最优」。

---

## 1. 目标与范围

### 1.1 目标

为 `app/intake/parsing.py` 的 `ExternalParser` 选择 OCR/ASR 供应商，并给出**可直接拆 Issue** 的
接入设计，使后续实现不必重新做选型。

### 1.2 范围

- 候选：百度智能云、腾讯云、阿里云（大陆区域）；
- OCR 维度：票据类型覆盖、字段返回形态、准确率口径、格式/大小限制、QPS/配额、计费、错误码、私有化；
- ASR 维度：音频格式/时长、方言、分段时间戳；
- 与 `ExternalParser` 的映射、风险与合规、后续 Issue 拆分。

### 1.3 非目标

- 不实现适配器、不写测试桩、不改 `ParseRequest` / `ParseResult` 契约；
- 不做发票验真（已由 W1-28 的百度/腾讯适配器覆盖）；
- 不做字段级业务规则（报销政策、税额校验规则）——这些属于后续产品决策。

---

## 2. 证据口径（先读这一节）

1. **只采信厂商官方文档**（`cloud.baidu.com` / `ai.baidu.com` / `cloud.tencent.com` /
   `help.aliyun.com` / `api.aliyun.com`）。第三方博客、云市场转述、媒体文章**不作为结论依据**。
2. 每一条外部结论在正文用 `[S#]` 标注，`[S#]` 的链接与**查阅日期 2026-10-18** 见 §10。
3. **价格、准确率、配额、字段名一律照录页面原文**；页面没有的，写「未验证」，
   不推测、不换算、不用「通常」「一般」补齐。
4. 厂商自述的「准确率 99%」**不是本项目验收标准**：口径（字段集合、样本分布、是否含低质量图）
   未公开，不可复现。验收必须用自有脱敏样本（见 §4.3、§8）。
5. 本文的「未验证」分两类：
   - **文档未载明**（页面没有该信息）；
   - **本文未实际调用验证**（文档有，但未做真实请求）。
   两类都会明确写出，不混用。

---

## 3. 候选与大陆服务区域、数据处理条款

### 3.1 接入点与区域

| 供应商 | 文档给出的接入域名 | 大陆区域说明 | 备注 |
|---|---|---|---|
| 百度智能云 | `https://aip.baidubce.com/rest/2.0/ocr/v1/...` [S1][S2] | 域名属百度智能云；**具体机房/Region 未在接口页说明（未验证）** | 鉴权为 OAuth2.0 `access_token`（W1-28 已核对 [S23]） |
| 腾讯云 OCR | `ocr.tencentcloudapi.com` [S9] | 公共参数含 `Region`；计费文档明确「Region 选择境外地域（中国香港、中国澳门、中国台湾及新加坡等）」会按国际站计费 [S10] → **实现必须显式锁定大陆 Region**；具体哪些 Region 支持 OCR **未验证** | 不锁 Region 存在**数据出境与计费口径**双重风险 |
| 腾讯云 ASR | `asr.tencentcloudapi.com` [S12] | 同上，`Region` 为可选公共参数 | 长音频任务与结果均在服务端，需关注 24h 有效期 [S13] |
| 阿里云 OCR | `ocr-api`（OpenAPI 产品）[S16] | 已查阅的接口页**未标注 Region/端点**；具体区域支持情况**未验证** | 使用 AccessKey/RAM 授权 [S16] |
| 阿里云 ASR | `nls-gateway-cn-shanghai.aliyuncs.com`（示例）[S21] | 示例为上海；其他区域**未验证** | NLS Token + Appkey [S21] |

> **结论**：三家都可在大陆区域提供服务，但**只有腾讯云在文档中显式暴露了「Region 可切境外」的行为**。
> 接入腾讯云时，`Region` 必须作为**代码常量**写死为大陆区域并加测试，不能依赖 SDK 默认值。

### 3.2 数据处理条款（关键：多数为「未验证」）

| 供应商 | 官方文档中可核实的数据处理表述 | 结论 |
|---|---|---|
| 腾讯云 ASR | 「腾讯云语音识别针对于用户传输的音频是否仅供当次识别使用？是的，腾讯云语音识别针对于用户传入的语音内容仅供当次识别使用，不会进行保存。」[S14] | **可核实**：短音频不持久化（以该 FAQ 为准） |
| 腾讯云 ASR（长音频） | 「识别结果在服务端保存 24 小时」「TaskId 有效期为 24 小时」[S13] | **可核实**：结果有 24h 留存 |
| 腾讯云 OCR | API 文档未附数据处理条款；产品页仅链接《服务协议》[S11]，正文**本文未阅读（未验证）** | **未验证**：留存、是否用于训练、子处理者均未知 |
| 阿里云 OCR | `RecognizeInvoice` 页面未附数据处理条款 [S16]；产品计费页未涉及 [S19] | **未验证** |
| 阿里云 ASR | 极速版页面未附数据留存条款 [S21] | **未验证** |
| 百度智能云 OCR/ASR | 接口页与计费页均未附数据处理条款 [S1][S4][S5] | **未验证**；W1-28 对百度验真也已标注「合规材料未取得，审查未通过前不得接入」[S23] |

**硬结论**：除「腾讯云 ASR 短音频不保存」这一条外，**三家的 OCR 数据处理条款均未验证**。
在签署 DPA、确认留存/训练/转委托/跨境之前，**不得把真实票据或语音发给任何一家**（§7.1）。

---

## 4. OCR 对比

### 4.1 票据类型覆盖（重点：增值税发票、行程单、出租车票、定额发票）

| 票据类型 | 百度智能云 | 腾讯云 | 阿里云 |
|---|---|---|---|
| 增值税发票（专/普/电子/全电/卷票/区块链） | 增值税发票识别覆盖普票、专票、全电（专/普）、卷票、区块链 [S1]；智能财务票据也含增值税发票/卷票 [S2] | 通用票据（高级版）子票种含 `VatSpecialInvoice`、`VatCommonInvoice`、`VatElectronic*`、`VatElectronic*Full`（全电）、`VatInvoiceRoll`（卷票）、区块链、通行费 [S9] | `RecognizeInvoice` 支持增值税专用/普通/电子发票 [S16]；产品页另列卷票识别 [S18] |
| 机票/网约车行程单 | `air_ticket`（飞机行程单）、`taxi_online_ticket`（网约车行程单）[S2] | `AirTransport`、`ElectronicFlightTicketFull`（电子发票-机票行程单）、`OnlineTaxiItinerary` [S9] | `RecognizeAirItinerary`、`RecognizeRideHailingItinerary` [S18] |
| 出租车票 | `taxi_receipt` [S2] | `TaxiTicket`（Type=0）[S9] | `RecognizeTaxiInvoice` [S18] |
| 定额发票 | `quota_invoice`，返回发票代码/号码/金额/省/市等 7 个关键字段 [S2][S8] | `QuotaInvoice`（Type=1）[S9] | `RecognizeQuotaInvoice` [S18] |
| 混贴（一页多票） | 智能财务票据支持「多张不同种类票据在同一张图片上的混贴」，返回每张票位置/种类/结构化结果 [S2] | 支持「一页中单张、多张、类型票据的混合识别」，按 `MixedInvoiceItems[]` 返回 [S9] | 提供 `RecognizeMixedInvoices` 混贴发票识别 [S18] |
| PDF 多页 | 支持 `pdf_file` + 页码参数，**默认只识别第 1 页**；总页数上限**未验证** [S1][S2] | 支持 PDF 多页，**最多 30 页**（`EnableMultiplePage` 仅返回前 30 页）[S9] | 支持 PDF/OFD；页码参数 `page_no`，默认识别第 1 页 [S16] |
| OFD | 支持 `ofd_file` + 页码 [S1][S2] | 仅支持指定票种的 **OFD 第一页** [S9] | 支持 OFD [S16] |

**覆盖结论**：三类重点票据三家都覆盖；差异在**混贴与多页 PDF 的成熟度**：
腾讯云把「14 大类 + 混贴 + 30 页 PDF」放在**一个接口**里 [S9]；百度是「专用接口 + 混贴接口」两条路 [S1][S2]；
阿里是「按票种多接口 + 混贴接口」[S18]。对「员工一次上传多张票」的受理场景，腾讯云的接口形态最省路由代码。

### 4.2 字段返回形态与置信度（决定能否满足 `ParseResult`）

`ParseResult` 的硬约束是：`fields` 与 `field_confidence` **键集合完全一致**，每个字段都要有
`[0.0, 1.0]` 的置信度（见 [../../app/intake/parsing.py](../../app/intake/parsing.py) 第 252–306 行）。
因此「是否返回逐字段置信度」是**架构级差异**，不是锦上添花。

| 维度 | 百度 `vat_invoice` [S1] | 百度 `multiple_invoice` [S2] | 腾讯 `RecognizeGeneralInvoice` [S9] | 阿里 `RecognizeInvoice` [S16] |
|---|---|---|---|---|
| 顶层结构 | `log_id` / `words_result_num` / `words_result` | `log_id` / `words_result_num` / `words_result[]` | `MixedInvoiceItems[]` / `TotalPDFCount` / `RequestId` | `data`（结构化对象）+ 图像元信息 |
| 字段形态 | `words_result` 为**扁平 key-value**（`InvoiceCode`、`TotalAmount`…）；商品明细为 `Commodity*` 数组，元素含 `row`/`word` | 每张票一个对象：`probability`、`left/top/width/height`、`type`、`result{...}` | 每张票按**票种分组**：`SingleInvoiceInfos.VatSpecialInvoice{...}`、`.TaxiTicket{...}`；未命中票种为 `null` | `data` 下扁平字段（`invoiceCode`、`totalAmount`、`sellerTaxNumber`…）+ `invoiceDetails[]` |
| 逐字段置信度 | **未提供**（页面未提及） | **未提供**；`probability` 是**单张票据分类置信度**，不是字段置信度 | **未提供**（页面未提及） | **提供**：`prism_keyValueInfo[]` 含 `keyProb` / `valueProb`（页面标注为「字段名称置信度」「字段名称对应值的置信度」） |
| 字段坐标 | 未提供（页面未提及） | `left/top/width/height`（需 `location=true`） | `ItemPolygon`（需 `EnableItemPolygon=true`） | `prism_keyValueInfo` 含坐标信息 |
| 对 `field_confidence` 的可行映射 | 只能给固定值（如 0.5）或按票种/字段规则赋值 | 同左；可用 `probability` 作为**整票上限** | 同左（无置信度） | 可直接归一化 `keyProb`/`valueProb`；**但取值域未验证**（页面只写 int，未写 0–100 还是 0–1） |

**关键提醒（易踩坑）**：

- 百度 `multiple_invoice` 的 `probability` 是「单张票据分类的置信度」[S2]，**不能**当作「金额字段的置信度」；
  若强行复用，会让「票种识别对了」被误读为「金额可信」。
- 阿里 `keyProb`/`valueProb` 的类型是 `int`，页面**未给出取值范围与含义**（是否 0–100、是否概率）→
  接入前必须用真实响应确认，否则归一化会写错（见 §9 第 6 项）。
- 腾讯云与百度的 `vat_invoice` 都没有逐字段置信度 → 若选它们，`field_confidence` 只能：
  (a) 统一给低于阈值的固定值（如 0.5），让所有单子都进 `needs_review`；
  (b) 按「字段是否为空 + 格式校验通过」给规则分（如非空 0.8、格式校验失败 0.4）。
  **(a) 更诚实**，也更符合「人工确认是唯一入口」的既有设计。

### 4.3 准确率口径（全部为厂商自述，不可作验收）

| 供应商 | 页面原文（照录） | 口径风险 |
|---|---|---|
| 百度 `vat_invoice` | 「其中五要素字段的识别准确率超过 99.9%；……四要素字段的识别准确率可达95%」（卷票 21 字段场景）[S1] | 「五要素」「四要素」的**字段集合未在页面定义（未验证）**；卷票 95% 明显低于普票/专票 |
| 百度 `multiple_invoice` | 页面未给出整体准确率（**未验证**）[S2] | 混贴场景无量化承诺 |
| 腾讯云票据 OCR | 「以增值税发票为例，发票代码、价税合计、合计金额、购买方识别号、开票日期等识别准确率达到99%以上」[S11] | 只列 5 个字段；**未说明样本来源、拍摄条件、是否含低质量图** |
| 阿里云票据凭证识别 | 「增值税发票识别……识别准确率超过99%」[S18] | 与同产品 `RecognizeInvoice` 页面「总体识别准确率可达 98%」[S16] **口径不一致**；以哪个为准**未验证** |

**结论**：

1. 三家数字**不可横向比较**（字段集合、样本、分母都不同），更**不可写进验收标准**。
2. 验收必须自建**脱敏样本集**（建议 ≥100 张/票种，覆盖拍照、扫描、折损、混贴），
   以「字段级 exact-match」计算，而不是引用厂商数字。
3. 对报销最敏感的字段是**金额、税额、发票号、发票代码、开票日期、购销方税号**；
   建议验收门槛（**本报告建议值，需所有者确认**）：
   - 金额/税额：≥98% exact-match，且**不允许「金额错但置信度≥阈值」**；
   - 发票号/代码/日期：≥99%；
   - 税号：≥98%（并按统一社会信用代码校验位二次校验）。

### 4.4 图片 / PDF / OFD 格式与大小限制

| 项目 | 百度 `vat_invoice` [S1] | 百度 `multiple_invoice` [S2] | 腾讯 `RecognizeGeneralInvoice` [S9] | 阿里 `RecognizeInvoice` [S16] |
|---|---|---|---|---|
| 图片格式 | jpg / jpeg / png / bmp | jpg / jpeg / png / bmp | PNG / JPG / JPEG / PDF（暂不支持 GIF） | PNG / JPG / JPEG / BMP / GIF / TIFF / WebP / PDF / OFD |
| 大小上限 | base64 + urlencode 后 **≤ 8M** | base64 + urlencode 后 **≤ 4M** | base64 后 **≤ 10M**；下载 ≤3 秒 | 二进制 **≤ 10MB** |
| 像素限制 | 最短边 ≥15px，最长边 ≤4096px | 最短边 ≥15px，最长边 ≤4096px | 单边 20–10000px | 长宽 >15px 且 <8192px；长宽比 <50 |
| URL 限制 | ≤1024 字节；需关闭防盗链 | ≤1024 字节；需关闭防盗链 | `ImageUrl` 与 `ImageBase64` 二选一，都传时只用 URL | `Url` 与 `body` 二选一 |
| PDF / OFD | `pdf_file` / `ofd_file` + 页码；默认第 1 页；页数上限未验证 | 同左 | PDF ≤30 页；OFD 仅指定票种第一页 | PDF/OFD + `page_no`；页数上限未验证 |

**工程含义**：

- 微信图片常见 3–8MB，**百度 `multiple_invoice` 的 4M 上限最先撞墙**；需要在上传前压缩/缩放。
- 百度 4096px 上限对「一张 A4 拍多张票」的混贴场景偏紧；腾讯 10000px、阿里 8192px 更宽松。
- 三家都要求 base64 或公网 URL；本项目**不应**把 `SourceFile` 直接暴露为公网 URL，
  应使用 base64 上传（或临时签名 URL，需额外实现，且**不得长期公开**）。

### 4.5 QPS 与免费配额

| 供应商 | 默认 QPS | 免费额度（页面原文） | 扩容 |
|---|---|---|---|
| 百度 | 付费后**保证单账户 10 QPS**，单个接口最大可购 **100 QPS**；**免费状态不保证并发** [S4] | 增值税发票识别：个人 1,000 次/月、企业 2,000 次/月；智能财务票据：个人 200 次、企业 500 次（**一次性，非每月**）[S3] | QPS 叠加包 [S4] |
| 腾讯云 | 通用票据（高级版）**5 次/秒** [S9] | 通用文字/卡证/票据等共享 **1,000 次/月**，每月 1 日发放、当月有效 [S10] | 购买资源包 / 后付费；QPS 提升未在 OCR 文档中说明（**未验证**） |
| 阿里云 | 每个 API **默认 10 QPS**；可购买 QPS 叠加包 [S19]；`RecognizeInvoice` 的 OpenAPI 文档标注「请求速率为 10/1(s)」[S17] | 识别类 API **每接口 200 次/月**；核验类每账号累计 50 次 [S19]（英文页同述：每接口 200 次/月 [S22]） | QPS 叠加包 [S19] |

**容量判断（建议，非厂商承诺）**：

- 小团队月票据量 <1,000 张时，三家免费额度都够试点；腾讯 1,000/月是**共享池**，
  混用通用文字会挤占票据额度。
- 5 QPS ≈ 18,000 次/小时的理论上限，对本项目**不是瓶颈**；真正的瓶颈是**并发上传与人工复核**。
- 百度「免费不保证并发」意味着**不能把免费额度当生产容量**。

### 4.6 计费方式与已知单价

| 供应商 | 抵扣顺序 | 已知单价（页面原文） | 备注 |
|---|---|---|---|
| 百度 | 免费测试资源 > 专项资源包 > 共享资源包 > 按量后付费 [S4] | 智能财务票据识别阶梯价示例：0–2 万次 **0.100 元/次**；2–5 万 **0.095**；5–10 万 **0.080**；10–20 万 **0.070** [S4]。增值税发票识别单价**未取得（未验证）** | 预付费包 1 年有效；按小时扣费 [S4] |
| 腾讯云 | 免费资源包 > 付费资源包 > 后付费 [S10] | 通用票据（高级版）**单价未取得（未验证）**；计费页只给模式与免费额度 | **部分调用失败的错误码也计费** [S10]，对账时必须按错误码拆分 |
| 阿里云 | 免费额度 > 专用资源包 > 共享资源包 > 按量后付费 [S19] | 增值税发票：月≤1 万 **0.225 元/次**；1–10 万 0.09；10–50 万 0.054；50–100 万 0.045；>100 万 0.036。混贴票证同档 0.225 起 [S20] | 资源包 1 年有效 [S19]；单价显著高于百度 |

**成本量级（仅按公开单价、按 1,000 张/月估算，**不含腾讯**）**：

- 百度智能财务票据：1,000 × 0.100 ≈ **100 元/月**（若全走增值税发票专用接口，单价未验证，无法估算）；
- 阿里增值税发票：1,000 × 0.225 ≈ **225 元/月**（混贴同价）；
- 腾讯：单价未验证 → **无法估算**，这是选型的**未闭合项**（§9 第 7 项）。

> 注意：以上是**文档公开档位**的算术，不含资源包折扣、失败重试、图片压缩/多页计费规则；
> 实际账单必须以后付费账单为准。

### 4.7 错误码与失败可诊断性

| 供应商 | 文档化程度 | 可用错误码（原文示例） |
|---|---|---|
| 腾讯云 | **最好**：通用票据接口页直接列出业务错误码 [S9] | `FailedOperation.DownLoadError`、`FailedOperation.ImageBlur`、`FailedOperation.ImageDecodeFailed`、`FailedOperation.OcrFailed`、`FailedOperation.UnKnowError`、`FailedOperation.UnOpenError`、`InvalidParameterValue.InvalidParameterValueLimit`、`LimitExceeded.TooLargeFileError`、`ResourceUnavailable.InArrears`、`ResourceUnavailable.ResourcePackageRunOut`、`ResourcesSoldOut.ChargeStatusException` |
| 阿里云 | 中等：返回 `Code` / `Message`，错误码清单在错误中心 [S16]；清单正文**未阅读（未验证）** | 示例 `noPermission`（页面示例值）[S16] |
| 百度 | **弱**：`vat_invoice` / `multiple_invoice` 页面**未列错误码** [S1][S2]；需另查通用错误码页（**未验证**） | 未验证 |

**工程含义**：腾讯云的错误码可以直接映射为「可重试 / 不可重试 / 需人工」三类；
阿里需要补错误中心清单；百度需要先找到通用错误码页，否则只能按 HTTP 状态码粗分。

### 4.8 私有化 / 专有云

| 供应商 | 结论 | 依据 |
|---|---|---|
| 百度智能云 | **支持**：OCR 私有化部署，容器化软件包，可部署本地服务器/私有云，CPU/GPU，单机/多机/集群，70+ 模型 | [S7] |
| 腾讯云 | **未验证**：本次查阅的 OCR 计费/接口/产品页均未提及私有化 | [S9][S10][S11] |
| 阿里云 | **未验证**：本次查阅的 OCR 计费/票据页均未提及私有化 | [S18][S19] |

> 若「票据不出内网」是硬约束，**百度是唯一有公开私有化文档的候选**；腾讯/阿里需商务确认，
> 不能假设「大厂都有」.
> 但私有化的代价是：授权按 QPS/期限、需要鉴权服务器与运维，**远超本项目当前阶段**（[S7] 的部署形态）。

---

## 5. ASR 单列对比

语音输入在受理场景通常是**微信短语音（≤60s）**，但也可能出现会议/通话长录音。因此拆成
「短音频」与「长音频」两档看。

| 维度 | 百度短语音识别标准版 [S5][S6] | 腾讯一句话识别 [S12] | 腾讯录音文件识别 [S13] | 阿里录音文件识别极速版 [S21] |
|---|---|---|---|---|
| 音频时长 | **≤60 秒** | **≤60 秒** | **≤5 小时**（URL）；本地文件 ≤5MB | **≤2 小时**；>2h 用普通版 |
| 大小限制 | 页面未提及（**未验证**） | **≤3MB**（Base64 后） | URL **≤1GB**；本地 **≤5MB** | **≤100MB** |
| 支持格式 | pcm、wav、amr、m4a [S5][S6] | wav、pcm、ogg-opus、speex、silk、mp3、m4a、aac、amr [S12] | wav、mp3、m4a、flv、mp4、wma、3gp、amr、aac、ogg-opus、flac [S13] | MP4、AAC、MP3、OPUS、WAV [S21] |
| 采样率/声道 | 16000 / 8000（8k 仅普通话），16bit，单声道 [S5] | 按引擎（8k/16k）[S12] | 按引擎 [S13] | 8000 / 16000 [S21] |
| 方言 | 中文普通话、英语、粤语、四川话 [S5] | 23 种方言（上海、四川、武汉、贵阳、昆明、西安、郑州、太原、兰州、银川、西宁、南京、合肥、南昌、长沙、苏州、杭州、济南、天津、石家庄、黑龙江、吉林、辽宁）+ 多语种 [S12] | 模型支持 31 种方言（`16k_zh_en_2.0` 等）[S13] | 控制台配置方言模型；**具体方言名单未验证**（页面指向另一页）[S21] |
| 分段时间戳 | **页面未提及（未验证）**；返回 `result` 文本，未见时间戳字段 [S6] | **词级时间戳**：`WordList[]` 含 `Word`/`StartTime`/`EndTime`（需 `WordInfo=1/2`）[S12] | **句级/词级**：`ResultDetail`，`ResTextFormat=3` 时按标点分段（字幕场景）[S13] | **句级 + 词级**：`sentences.begin_time` / `end_time`；`words[]` 含 `begin_time`/`end_time`（需 `enable_word_level_result`）[S21] |
| 调用方式 | 同步 HTTP [S5] | 同步 HTTP [S12] | **异步任务**（创建任务 → 轮询/回调）[S13] | 同步 HTTP [S21] |
| QPS | 页面未提及（**未验证**） | **30 次/秒** [S12] | **20 次/秒** [S13] | 页面未提及；仅链接并发说明（**未验证**）[S21] |
| 结果有效期 | 不适用（同步） | 不适用（同步） | **24 小时** [S13] | 页面未提及（**未验证**） |

**建议**：

- **短语音**（微信语音条）：腾讯一句话识别，60s/3MB 覆盖绝大多数场景，23 方言 + 词级时间戳，
  30 QPS 足够；且与 OCR 同供应商，可复用 TC3 签名与凭据管理。
- **长语音**：腾讯录音文件识别（5h/1GB），异步任务 + 24h 结果有效期需要额外的任务表/轮询，
  **不建议在第一个 ASR Issue 里做**；先只做 ≤60s。
- **备选**：阿里极速版（2h 同步）适合「一次性长录音、要求同步返回」的场景；但仅商用版、
  不支持试用 [S21]，试点成本更高。
- **百度短语音**：页面未给出时间戳能力（未验证），若后续要做「语音定位到原文位置」，
  需先确认；否则只适合「转成纯文本再抽字段」。

---

## 6. 与 `ExternalParser` 的映射设计

本节只做**设计**，不写实现。所有命名均为**提议**，落地前需所有者确认。

### 6.1 凭据环境变量命名

沿用仓库既有约定（`AJAIMGR_` 前缀 + 供应商 + 用途），不新增配置文件、不落库：

| 供应商 | 环境变量（提议） | 依据 |
|---|---|---|
| 腾讯云 | `AJAIMGR_TENCENT_SECRET_ID` / `AJAIMGR_TENCENT_SECRET_KEY` | 与 `app/integrations/tencent_vat_verify.py` 既有命名一致 |
| 百度智能云 | `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY` | 与 `app/integrations/baidu_vat_verify.py` 既有命名一致 [S23] |
| 阿里云 | `AJAIMGR_ALIYUN_ACCESS_KEY_ID` / `AJAIMGR_ALIYUN_ACCESS_KEY_SECRET` | 与阿里云 AccessKey 语义一致（提议） |
| 腾讯云区域 | `AJAIMGR_TENCENT_OCR_REGION`（**建议默认写死大陆区域，不暴露为可配置项**） | §3.1 的出境风险 |

约束（与 `ExternalParser` 现有实现一致，见 [../../app/intake/parsing.py](../../app/intake/parsing.py) 第 460–521 行）：

- `credential_env_vars` 只放**变量名**；构造时任一缺失/空白 → `MissingCredentialError`（fail closed）；
- 错误信息只提变量名，**不含值**；凭据存私有属性，**不写日志、不落库、不进提示词**；
- 不在仓库、`.env*`、测试夹具中出现任何真实值；测试用 `env={...}` 注入假值。

### 6.2 `_parse` 的入参、出参与「源文件读取」缺口

**现状**：`ParseRequest` 只有 `source_file_id`（整数）与 `metadata`，**没有文件字节**；
`ExternalParser._parse(request, credentials)` 拿不到图片/音频内容。这是接入前必须解决的**契约缺口**。

**不建议**的做法：

- 在 `parse()` 里直接开数据库会话查 `source_files` —— 让解析器依赖 ORM/事务，破坏
  「纯逻辑层可单测、无数据库也可跑」的既有设计（见模块 docstring 第 39–40 行）。
- 把 base64 塞进 `ParseRequest.metadata` —— 违反「`metadata` 不得放附件原文」的注释约束，
  且会让请求对象携带敏感大字段、易被日志打印。

**建议**：适配器构造时注入一个**只读的源文件读取器**（依赖注入，不新增依赖）：

```python
# 设计示意（不是实现）
class SourceReader(Protocol):
    def read(self, source_file_id: int) -> tuple[bytes, str, int]:
        """返回 (content, mime_type, size_bytes)；不存在/不可读时抛 SourceUnavailable。"""

class TencentGeneralInvoiceParser(ExternalParser):
    credential_env_vars = ("AJAIMGR_TENCENT_SECRET_ID", "AJAIMGR_TENCENT_SECRET_KEY")

    def __init__(self, *, source_reader: SourceReader, env=None, transport=None) -> None:
        super().__init__(env=env)
        self._source_reader = source_reader
        self._transport = transport or _httpx_transport  # 惰性导入 httpx，与百度适配器一致

    def _parse(self, request, credentials) -> ParseResult:
        content, mime, size = self._source_reader.read(request.source_file_id)
        # 1) 预检：mime / size / 图片像素（尽量本地做，避免白花一次调用）
        # 2) 调用供应商（单次尝试，超时不自动重发）
        # 3) 归一化字段 + 置信度 → ParseResult
```

生产实现可直接包装 `app.documents.storage.read_file(root, storage_key)` 与
`SourceFile.mime_type/size_bytes/scan_status`（见 `app/documents/storage.py` 第 377 行、
`app/documents/models.py` 第 44–67 行）；测试注入内存桩，**绝不触网**。

**预检（必须在发请求前做，fail closed）**：

| 检查 | 失败处理 | 理由 |
|---|---|---|
| `source_file_id` 存在且可读 | 抛 `SourceUnavailable` → `provider_error` | 追溯链断裂 |
| `SourceFile.scan_status == 'clean'` | 拒绝外发（`provider_error` 或专用本地失败码） | 未扫描的文件可能是恶意内容；不得送给第三方 |
| `mime_type` 在该接口支持列表内 | `UnsupportedInputError`（不消耗配额） | 避免无效调用与计费 |
| `size_bytes` ≤ 供应商上限（注意 base64 膨胀约 4/3） | 本地拒绝或先压缩 | 百度和腾讯的上限不同 |
| 像素/长宽比（若可读） | 本地缩放/拒绝 | 百度 4096px、阿里 8192px |

### 6.3 失败码如何收敛为 `provider_error`

`ExternalParser.parse` 的现有行为（第 498–515 行）：

- `UnsupportedInputError` / `MissingCredentialError`（均为 `ParserError`）→ **原样抛出**；
- 其它任何异常 → `ParseResult.failed(error="provider_error")`，**不泄漏异常原文**。

因此适配器内部应这样分层：

| 供应商情形 | 适配器内部处理 | 对外结果 |
|---|---|---|
| 凭据缺失 | 构造期 `MissingCredentialError` | 构造失败（fail closed），不进入 `parse` |
| 输入类型不支持 | `UnsupportedInputError` | 原样抛出（上层可回退人工） |
| 文件不存在/未扫描/超限 | 抛普通 `Exception` 子类（**不要**继承 `ParserError`） | `provider_error` |
| HTTP 401/403（鉴权失败） | 抛普通异常；日志只记状态码 | `provider_error` |
| HTTP 429 / 欠费 / 资源包耗尽 | 抛普通异常；日志记供应商错误码 | `provider_error` |
| 超时 / 连接失败 | 抛普通异常；**不自动重试** | `provider_error` |
| HTTP 200 但业务码失败（如图片模糊） | 抛普通异常；日志记业务码 | `provider_error` |
| 响应结构不可解析 | 抛普通异常 | `provider_error` |
| 供应商返回空字段 | **不要**抛错；返回 `fields={}` | 由 `decide_draft` 判 `needs_review`/`failed` |

**为什么不把供应商错误码放进 `ParseResult.error`**：

- `ParseResult.error` 是**机器可读的单一失败码**，`decide_draft` 只判断 `ok/not ok`；
  塞入 `tencent.ImageBlur` 这类值会让上层耦合供应商。
- 现有契约没有 `metadata` 字段承载错误详情；若要保留可诊断性，应写**审计/日志的非敏感字段**
  （`provider`、`provider_code`、`provider_request_id`），**不写原文**。
- 若未来确实需要区分「可重试/不可重试」，应新增 Issue 扩展 `ParseResult`，
  **不要**在本期偷偷改语义。

### 6.4 字段归一化（建议的统一键）

`ParseResult.fields` 是自由 `Mapping`，但下游（草稿、复核、报销）需要稳定键。建议先定**最小字段集**：

| 统一键 | 含义 | 腾讯（`VatSpecialInvoice` 等）[S9] | 阿里 `data` [S16] | 百度 `words_result` [S1] |
|---|---|---|---|---|
| `invoice_code` | 发票代码 | `Code` | `invoiceCode` | `InvoiceCode` |
| `invoice_number` | 发票号码 | `Number` | `invoiceNumber` | `InvoiceNum` |
| `invoice_date` | 开票日期（ISO `YYYY-MM-DD`） | `Date` | `invoiceDate` | `InvoiceDate` |
| `invoice_type` | 票种（统一枚举，见下） | `SubType` / `TypeDescription` | `invoiceType`（中文） | `InvoiceType`（中文/枚举） |
| `amount` | **价税合计**（报销金额） | `Total` | `totalAmount` | `AmountInFiguers` |
| `amount_without_tax` | 不含税金额 | `PretaxAmount` | `invoiceAmountPreTax` | `TotalAmount` |
| `tax_amount` | 税额 | `Tax` | `invoiceTax` | `TotalTax` |
| `buyer_name` | 购买方名称 | `Buyer` | `purchaserName` | `PurchaserName` |
| `buyer_tax_id` | 购买方税号 | `BuyerTaxID` | `purchaserTaxNumber` | `PurchaserRegisterNum` |
| `seller_name` | 销售方名称 | `Seller` | `sellerName` | `SellerName` |
| `seller_tax_id` | 销售方税号 | `SellerTaxID` | `sellerTaxNumber` | `SellerRegisterNum` |
| `check_code` | 校验码 | `CheckCode` | `checkCode` | `CheckCode` |
| `passenger_name` | 乘车/乘机人 | 火车票/行程单分节字段（需按 `SubType` 取） | 对应票种接口字段（未逐一核对，**未验证**） | `PassengerName`（旅客运输类） |

**金额语义必须先定，再写代码**：

- `amount` 统一取**价税合计**（报销口径），`amount_without_tax` + `tax_amount` 单独保留；
- 三家的「金额」字段语义不同：百度 `TotalAmount` 是「合计金额」（不含税）、`AmountInFiguers` 是
  「价税合计」[S1]；腾讯 `Total` 与 `PretaxAmount`/`Tax` 分列 [S9]；阿里 `totalAmount` 与
  `invoiceAmountPreTax`/`invoiceTax` 分列 [S16]；
- 若把「不含税」当「价税合计」写入 `amount`，会造成**系统性少报**，且人工复核很难发现——
  这是本设计里**最高风险的单点**（§7.6）。

**票种枚举**：不要把供应商的中文/英文枚举直接落库；建一个内部枚举（如 `vat_special`、
`vat_normal`、`vat_electronic`、`vat_full_electronic`、`train_ticket`、`air_itinerary`、
`taxi_receipt`、`quota_invoice`、`other`），在适配器内做映射，未知值 → `other` + 强制复核。

### 6.5 置信度归一化

| 供应商能力 | 归一化规则（提议） |
|---|---|
| 阿里 `keyProb`/`valueProb` | 先确认真实取值域；若为 0–100，则 `/100`；若为 0–1，直接用。**取值域未验证，必须先做一次真实响应核对** [S16] |
| 腾讯（无逐字段） | 统一 `DEFAULT_EXTERNAL_CONFIDENCE = 0.5`（< 默认阈值 0.75）→ 所有单子 `needs_review`；这是**有意保守**，不是缺陷 |
| 百度 `multiple_invoice` 的 `probability` | 只作为**整票上限**：`field_confidence[f] = min(probability, 0.5)`；**不得**把票种置信度当字段置信度 [S2] |
| 百度 `vat_invoice` | 同腾讯：统一 0.5 |
| 字段为空/格式校验失败 | 该字段置信度 0.0（且值为空时 `decide_draft` 会计为缺失） |

`ParseResult.__post_init__` 会强制 `fields` 与 `field_confidence` 键集合一致、
置信度在 `[0,1]`（第 288–306 行）——适配器**必须**为每个返回字段赋值，不能只给「有把握的字段」。

### 6.6 必须人工复核的字段（无论置信度多高）

现有设计已保证「草稿恒 `pending`、人工确认是唯一入口」；在此基础上，建议把以下字段标为
**必审**（即使置信度 1.0 也不允许「快速通过」）：

1. `amount`、`tax_amount`、`amount_without_tax`（金额类，错一位就是钱）；
2. `invoice_code`、`invoice_number`、`invoice_date`、`check_code`（票据身份）；
3. `buyer_tax_id`、`seller_tax_id`（税号，且做统一社会信用代码校验位）；
4. `invoice_type`（票种决定后续规则）；
5. 火车票/行程单的 `passenger_name` 及身份证号（**PII，最敏感**，见 §7.3）；
6. 任何 `confidence < 0.75` 的字段（现有 `decide_draft` 已覆盖）。

实现方式（提议）：在 `BUSINESS_REQUIRED_FIELDS` 之外新增「必审字段」集合，或在
`decide_draft` 的调用方传入更高的 `confidence_threshold` 并按字段覆盖；
**本期不改 `decide_draft` 签名**，先在适配器层把必审字段的置信度压到阈值以下，
用既有机制达成「必审」，避免扩散改动。

### 6.7 传输、重试与审计

| 项 | 建议 | 理由 |
|---|---|---|
| HTTP 客户端 | 复用 `httpx`（既有传递依赖，惰性导入），**不新增供应商 SDK** | 与百度适配器一致；避免 `tencentcloud-sdk-python` / `aliyun-sdk` 新依赖 |
| 签名 | 腾讯 TC3-HMAC-SHA256、阿里 ACS3 用标准库 `hmac/hashlib` 实现 | 不新增依赖；但签名实现需单测（已知风险） |
| 超时 | 单次 10s（提议） | OCR 通常 <2s；长尾不应拖住受理 |
| 重试 | **超时不重试**；5xx 最多 1 次重试（需预算上限） | OCR 按次计费，重试会翻倍成本；超时重试可能产生重复扣费 |
| 幂等 | 以 `source_file_id + parser_version` 做本地去重，避免重复解析同一文件 | 供应商无幂等键 |
| 审计 | 记录 `provider`、`provider_code`、`provider_request_id`（腾讯 `RequestId` / 百度 `log_id` / 阿里 `RequestId`）、`parser_version`、`source_file_id`、字段名清单 | 可追溯且不含原文（对齐 [W0-5](W0-5-security-compliance.md) §3.1/§3.2） |
| 日志 | **禁止**打印 base64、识别全文、字段值、税号、身份证号 | 既有 `safe_log_summary` 已定调 |
| 文件 | 只读 `SourceFile`，**绝不覆盖原件**；解析结果只进 `extraction_drafts` | 模块硬边界第 1/2 条 |

### 6.8 供应商适配器的命名与注册

| 类（提议） | `parser_kind` | `supported_input_kinds` | `credential_env_vars` |
|---|---|---|---|
| `TencentGeneralInvoiceParser` | `OCR` | `{IMAGE, DOCUMENT}` | `AJAIMGR_TENCENT_SECRET_ID`、`AJAIMGR_TENCENT_SECRET_KEY` |
| `AliyunInvoiceParser` | `OCR` | `{IMAGE, DOCUMENT}` | `AJAIMGR_ALIYUN_ACCESS_KEY_ID`、`AJAIMGR_ALIYUN_ACCESS_KEY_SECRET` |
| `TencentSentenceAsrParser` | `ASR` | `{VOICE}` | 同上（腾讯） |
| `ManualEntryParser`（既有） | `MANUAL_ENTRY` | 全部四类 | 无 |

注册顺序很关键：`ParserRegistry.register` 是「**同一类型后登记者覆盖**」（第 536–539 行）。
因此：

- 生产装配：先 `ManualEntryParser`，再外部解析器 → 外部优先；
- 故障回退：**不要**在构造期把外部解析器注册进去再指望自动回退；`resolve` 返回的是
  「已登记的外部解析器」，调用失败时**上层**必须显式回退到人工录入（`ParseResult.error` →
  `failed` → 转人工），这是现有设计的语义。
- 灰度：建议用**部署层开关**（是否装配外部解析器）而不是在代码里加 `if provider == ...`；
  所有者此前已明确「不新增渠道选择配置项」（W1-28 §1），OCR 接入应沿用同一原则。

---

## 7. 风险与合规

### 7.1 数据出境（高）

- 腾讯云文档明确 `Region` 可选境外地域并按国际站计费 [S10] → **必须锁大陆 Region**，
  且加一条「Region 不得来自环境变量」的单测。
- 三家是否使用境外子处理者/是否跨境传输：**官方 API 文档未载明（未验证）**。
- 处置：签署 DPA 前不发真实票据；在 DPA 中明确**存储地点、留存期限、是否用于模型训练、转委托**。

### 7.2 数据留存（中高）

- 腾讯 ASR 短音频「仅供当次识别使用，不会保存」[S14]；长音频结果保存 24h [S13]。
- OCR 的留存期限：三家均**未验证**。
- 处置：假设「会被留存」做最坏设计；在 DPA 中约定删除机制；本地不保存供应商原始响应（或加密 + 限期删除）。

### 7.3 PII（高）

票据中的 PII 远超「发票」本身：

- 火车票/机票行程单：**姓名 + 身份证号**（最敏感）；
- 增值税发票：购销方名称、税号、地址电话、开户行及账号；
- 出租车票/定额发票：一般无 PII，但可能有车牌。

处置建议：

1. **分级**：含身份证号的票据（火车票/行程单）默认**不送外部 OCR**，走人工录入；
   确需识别时，先裁剪/遮盖身份证号区域，或只上传不含 PII 的票面区域（可行性需实测）。
2. **最小化**：只上传票据主体，不上传整页含其他信息的截图。
3. **不留存**：本地不落供应商原始响应；审计只留字段名与请求 ID。
4. **PIPIA**：处理身份证号属于敏感个人信息，接入前应完成个人信息保护影响评估（**建议，需法务确认**）。

### 7.4 发票号/税号的商业敏感性（中）

- 发票代码/号码、税号可用于查验、比对、甚至推断交易关系；属于**商业敏感**而非严格 PII。
- 处置：日志/审计/错误信息中一律不出现；草稿表中按现有权限控制访问；导出需审计。

### 7.5 审计与证据（中）

- 需保留：`provider`、`provider_request_id`、`parser_name/version`、`source_file_id`、
  调用时间、字段名清单、失败码；**不保留**原文/字段值（对齐 W0-5）。
- 若业务要求「留存原始响应作为证据」，应单独建加密证据表 + 保留期限 + 访问审计，
  **不要**塞进 `extraction_drafts` 或日志。

### 7.6 识别错误的「静默失败」（高）

- 最大风险不是「识别失败」，而是**识别错但看起来对**：金额少一位、税号错一位、日期年份错。
- 置信度不能完全防住（尤其腾讯/百度无逐字段置信度）。
- 处置（多层）：
  1. 金额/税额做**算术校验**：`amount_without_tax + tax_amount ≈ amount`（允许分位误差）；
  2. 税号做**校验位**校验；
  3. 发票代码/号码做**长度/字符集**校验；
  4. 日期做**合理区间**校验（如不得晚于受理日、不得早于 5 年前）；
  5. 与 W1-28 的**发票验真**串联：OCR 只产出草稿，验真通过才可能进入「查验通过」——
     **OCR 成功 ≠ 发票为真**（W1-28 §3 硬性规则 [S23]）。

### 7.7 供应商锁定与变更（中）

- 字段名、错误码、计费模式均绑定供应商；适配器层已做归一化，但**票种枚举**与**金额语义**
  仍可能随供应商差异而漂移。
- 处置：契约测试（固定响应样本 → 固定 `ParseResult`）；供应商版本变更时跑回归；
  保留「切回人工兜底」的部署开关。

### 7.8 成本失控（中）

- 失败码也可能计费（腾讯 [S10]）；重试、PDF 多页、混贴可能改变计费次数（**具体规则未验证**）。
- 处置：月度预算告警；按 `provider_code` 对账；对同一 `source_file_id` 做本地幂等；
  设「单日调用上限」硬闸。

### 7.9 账号与实名（低，但阻塞）

- 三家均需实名认证/开通服务；企业认证才享有更高免费额度（百度 [S3]）。
- 处置：由所有者决定主体与账号归属；**不得**用个人账号处理公司票据（合规与离职风险）。

---

## 8. 后续实现 Issue 拆分建议

> 原则：**先合规、再骨架、再单供应商、再验收、最后备选**。每个 Issue 都应能独立回滚，
> 且默认「不触网」的测试可跑。

| # | 建议标题 | 范围 | 依赖 | 验收要点 |
|---|---|---|---|---|
| 1 | `chore(intake): OCR 接入前置合规审查（DPA/PIPIA/Region）` | 与三家中的**主选**签署 DPA；确认留存/训练/转委托；锁定大陆 Region；完成 PIPIA | 所有者/法务 | 书面材料归档；未完成则**禁止**任何真实调用 |
| 2 | `feat(intake): ExternalParser 源文件读取器与预检` | `SourceReader` 协议 + `storage.read_file` 包装；`scan_status='clean'`、mime、size、像素预检；单测不触网 | #1 可并行（不触网） | 未扫描/超限/不支持类型均在**发请求前**失败；无新依赖 |
| 3 | `feat(intake): 腾讯云通用票据识别适配器（桩传输）` | `TencentGeneralInvoiceParser`：TC3 签名、单次调用、超时、错误码→`provider_error`、字段归一化、固定置信度 0.5 | #2 | 桩响应 → 固定 `ParseResult`；凭据缺失 fail closed；不打印原文 |
| 4 | `feat(intake): 字段口径与置信度归一化` | 统一键、票种枚举、金额语义（价税合计）、ISO 日期、必审字段降置信度 | #3 | 金额/税额/日期映射单测；不含税 vs 价税合计有专门用例 |
| 5 | `test(intake): OCR 脱敏样本集与验收脚本` | ≥100 张/票种的脱敏样本（含拍照/扫描/折损/混贴）；字段级 exact-match 报表 | #3 #4 | 金额 ≥98%、票号/日期 ≥99%、税号 ≥98%（建议值，需确认）；报告可复现 |
| 6 | `feat(intake): OCR 配额、预算与告警` | 调用计数、日上限、月度预算、欠费/429 处理、按错误码对账 | #3 | 超限 fail closed；告警可观测；不重试超时 |
| 7 | `feat(intake): 腾讯云一句话识别适配器（≤60s）` | `TencentSentenceAsrParser`：60s/3MB 预检、词级时间戳、文本→字段（可选） | #2 | 音频超限本地拒绝；时间戳可选；不落原文 |
| 8 | `feat(intake): 阿里云 OCR 备选适配器（按需）` | `RecognizeInvoice` + 混贴；`keyProb/valueProb` 归一化（先确认真实取值域） | #4 #5 | 仅在主选验收不达标时启动；契约测试与腾讯同形 |
| 9 | `docs(intake): 接入运行手册与回滚` | 开通/配额/密钥轮换/故障回退人工/对账步骤 | #1–#6 | 值班可按手册在 10 分钟内回退人工 |

**不建议**在第一个 Issue 里同时做 OCR + ASR + 两家供应商；也不建议先做「自动确认」——
现有设计明确「人工确认是进入正式记录的唯一路径」，接入后仍应保持。

---

## 9. 未验证清单（接入前必须逐项关闭）

> 这些不是「待办事项」的同义词，而是**本文不能替你做的判断**。未关闭前，任何「已选型完成」
> 的表述都不成立。

1. **数据处理条款正文**：三家 OCR 的留存、是否用于训练、子处理者、跨境传输——均未验证（§3.2）。
2. **腾讯云 OCR 单价**：计费页只给模式与免费额度，未取得通用票据（高级版）单价（§4.6）。
3. **百度增值税发票识别单价**：未取得（只有智能财务票据的阶梯价示例）（§4.6）。
4. **Region 支持矩阵**：腾讯/阿里哪些大陆 Region 支持对应 OCR/ASR 接口——未验证（§3.1）。
5. **阿里 `keyProb`/`valueProb` 取值域**：页面只写 `int`，未写 0–100 还是 0–1（§4.2）。
6. **百度「五要素/四要素」定义**：页面未定义字段集合（§4.3）。
7. **腾讯/阿里私有化**：文档未提及，需商务确认（§4.8）。
8. **PDF 多页计费规则**：腾讯 30 页是否按 1 次计费、百度按页是否重复计费——未验证（§4.6）。
9. **失败码计费清单**：腾讯「部分失败错误码计费」的具体清单未逐条核对（§4.6）。
10. **百度 OCR 错误码**：`vat_invoice` / `multiple_invoice` 页面未列，通用错误码页未核实（§4.7）。
11. **阿里 OCR 错误码清单**：错误中心正文未阅读（§4.7）。
12. **ASR 结果留存**：阿里极速版未载明；百度未载明（§5）。
13. **方言名单（阿里）**：页面指向另一页，本文未核实（§5）。
14. **真实识别效果**：本文未调用任何接口，所有准确率均为厂商自述（§4.3）。
15. **SLA / 赔付**：三家的 OCR/ASR SLA 条款均未阅读（§7.5）。

---

## 10. 来源清单（官方文档，查阅日期 2026-10-18）

| 编号 | 供应商 | 文档 | 链接 | 本文用途 |
|---|---|---|---|---|
| S1 | 百度智能云 | 增值税发票识别 | https://cloud.baidu.com/doc/OCR/s/nk3h7xy2t | 票种、字段、8M/4096px、准确率、PDF/OFD |
| S2 | 百度智能云 | 智能财务票据识别 | https://ai.baidu.com/ai-doc/OCR/7ktb8md0j | 13 类票据、混贴、`probability`/`location`、4M |
| S3 | 百度智能云 | 文字识别 免费测试资源 | https://ai.baidu.com/ai-doc/OCR/fk3h7xu7h | 各接口免费额度 |
| S4 | 百度智能云 | 文字识别 计费概述 | https://ai.baidu.com/ai-doc/OCR/Jk3h7xtsd | 抵扣顺序、QPS、阶梯价示例、欠费 |
| S5 | 百度智能云 | 短语音识别 简介 | https://ai.baidu.com/ai-doc/SPEECH/qlcirqhz0 | 60s、格式、方言 |
| S6 | 百度智能云 | 短语音识别 API | https://cloud.baidu.com/doc/qianfan-api/s/mm7viqkmz | 请求方式、格式、返回 `result` |
| S7 | 百度智能云 | OCR 私有化部署 产品介绍 | http://market.cloud.baidu.com/doc/OCR/s/9kio79qfv | 私有化能力与部署形态 |
| S8 | 百度智能云 | 文字识别 能力简介 | https://ai.baidu.com/ai-doc/OCR/Dkibizx1z | 票据能力清单、定额发票 7 字段 |
| S9 | 腾讯云 | 通用票据识别（高级版） | https://cloud.tencent.com/document/product/866/90802 | 14 类票种、30 页 PDF、10M、5 QPS、错误码、返回结构 |
| S10 | 腾讯云 | 文字识别 计费概述 | https://cloud.tencent.com/document/product/866/17619 | 免费 1,000 次/月、抵扣顺序、失败码计费、境外 Region |
| S11 | 腾讯云 | 票据单据识别 产品页 | https://cloud.tencent.com/product/invoiceocr | 99% 准确率表述、混贴/PDF 能力 |
| S12 | 腾讯云 | 一句话识别 | https://cloud.tencent.com/document/product/1093/35646 | 60s/3MB、23 方言、词级时间戳、30 QPS |
| S13 | 腾讯云 | 录音文件识别请求 | https://cloud.tencent.com/document/product/1093/37823 | 5h/1GB、异步、`ResultDetail`、24h、20 QPS |
| S14 | 腾讯云 | 语音识别常见问题（PDF） | https://main.qcloudimg.com/raw/document/product/pdf/1093_35801_cn.pdf | 语音不保存、方言、时长 |
| S15 | 腾讯云 | 票据单据识别 产品简介 | https://cloud.tencent.com/document/product/866/37495 | 14 大类、OFD 范围 |
| S16 | 阿里云 | RecognizeInvoice 增值税发票识别 | https://help.aliyun.com/document_detail/442267.html | 字段、格式、10MB/8192px、`prism_keyValueInfo` 置信度 |
| S17 | 阿里云 | RecognizeInvoice OpenAPI 文档 | https://error-center.aliyun.com/document/ocr-api/2021-07-07/RecognizeInvoice | 流控 10 次/秒 |
| S18 | 阿里云 | 票据凭证识别 产品功能 | https://help.aliyun.com/zh/ocr/product-overview/ticket-and-invoice-recognition-1 | 近 20 种票据、混贴、99% 表述、API 入口 |
| S19 | 阿里云 | 文字识别 产品计费 | https://help.aliyun.com/zh/ocr/product-overview/product-billing/ | 抵扣顺序、200 次/月/接口、10 QPS、QPS 叠加包 |
| S20 | 阿里云 | 文字识别 按量付费价格 | https://help.aliyun.com/knowledge_detail/295347.html | 增值税发票 0.225 起、混贴 0.225 起 |
| S21 | 阿里云 | 录音文件识别极速版 | https://help.aliyun.com/zh/isi/developer-reference/sdk-reference-9 | 2h/100MB、句/词时间戳、商用版 |
| S22 | 阿里云 | OCR Free quotas | https://help.aliyun.com/document_detail/295346.html | 每接口 200 次/月（英文页，交叉核对 S19） |
| S23 | 百度智能云 | 增值税发票验真（W1-28 既有调研） | https://ai.baidu.com/ai-doc/OCR/cklbnrnwe | 合规未通过不得接入的既有结论；同生态凭据模式 |
| S24 | 腾讯云 | 文字识别 计费错误码说明 | https://cloud.tencent.com/document/product/866/45470 | 仅作为「失败计费」的进一步核对入口（正文未阅读，**未验证**） |

> 说明：S24 仅列链接供后续核对，本文**未**据其得出结论。

---

## 附录 A 决策矩阵（权重为建议值，非事实）

评分 1–5；权重合计 100。**该矩阵用于暴露取舍，不是客观打分**，所有者可改权重。

| 维度 | 权重 | 腾讯云 | 阿里云 | 百度 |
|---|---|---|---|---|
| 票据覆盖与混贴/多页 | 25 | 5 | 5 | 4 |
| 字段形态与逐字段置信度 | 20 | 3 | 5 | 2 |
| 接入成本（接口数量/签名） | 15 | 5 | 2 | 4 |
| 配额与单价 | 15 | 4 | 3 | 5 |
| 合规/私有化可选性 | 15 | 3 | 3 | 4 |
| 错误码与可诊断性 | 10 | 5 | 4 | 2 |
| **加权总分（/500）** | 100 | **415** | **385** | **355** |

**读法**：

- 腾讯赢在「一个接口 + 明确错误码 + 足够配额」，适合**先跑通**；
- 阿里赢在「逐字段置信度」，但接口多、单价高，适合**主选验收不达标时补位**；
- 百度赢在「单价/免费额度/私有化」，但图片限制更紧、置信度形态最弱、错误码最少；
  若**私有化**成为硬约束，百度应直接升为主选（此时矩阵权重需重排）。
- 三家分差不大（415/385/355），说明**没有压倒性最优**；真正的决定因素是 §7 的合规与 §9 的未验证项。

---

## 附录 B 与既有 `ExternalParser` 契约的差异清单（供评审）

| 契约点 | 现状 | 接入时需要什么 | 是否改契约 |
|---|---|---|---|
| 源文件字节 | `ParseRequest` 只有 `source_file_id` | 注入 `SourceReader`（构造参数） | **否**（推荐） |
| 逐字段置信度 | `ParseResult` 强制每字段有值 | 供应商无置信度时给固定 0.5 | **否** |
| 失败码 | 只有 `provider_error` | 供应商错误码写日志/审计，不写 `error` | **否** |
| 失败详情 | `ParseResult` 无 `metadata` | 审计侧记 `provider_code`/`request_id` | **否**（本期） |
| 必审字段 | `decide_draft` 只看缺失/低置信度 | 适配器把必审字段置信度压到阈值下 | **否**（本期） |
| 供应商选择 | `ParserRegistry` 后登记者覆盖 | 部署层决定是否装配；不加运行时开关 | **否** |
| 异步 ASR | `parse` 是同步返回 | 长音频需任务表/轮询——**超出本期** | **是**（未来 Issue） |

> 结论：**OCR 可以在不改 `parsing.py` 契约的前提下接入**；ASR 长音频若要做，需要新增
> 异步任务模型，属于独立 Issue，不应与 OCR 混在一起。
