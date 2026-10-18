# 百度智能财务票据识别（`multiple_invoice`）真实联调认知

> **状态：已接入并已真实联调。** 适配器 `app/integrations/baidu_invoice_ocr.py` 已随 #35
> （PR #36）合入 `main`，并按真实响应修正于 #37（PR #38）；2026-10-18 在 `main` head
> `fd4d197` 上，用**真实凭据 + 20 份真实票据**（14 份 PDF、6 份 OFD）经
> `BaiduInvoiceOcrParser` 完成端到端联调，**20/20 成功**。
>
> 本文记录的是**从真实响应中学到的事实**、与选型报告不一致之处、以及仍未验证项。
>
> - 关联 Issue：#35（接入）、#37（按真实响应修正）、#39（本文）
> - 代码：`app/integrations/baidu_invoice_ocr.py`（`BaiduInvoiceOcrClient` / `BaiduInvoiceOcrParser`）
> - 上游：选型报告 [W2-ocr-asr-vendor-selection.md](W2-ocr-asr-vendor-selection.md)、
>   契约 [../INTAKE-PARSING-PIPELINE.md](../INTAKE-PARSING-PIPELINE.md)
> - **证据口径：本文只用聚合统计。** 不含任何票面字段值、凭据值、企业主体或机器标识。

---

## 0. 一页结论

1. **百度已由所有者拍板为 OCR 主用**（替代选型报告原先推荐的腾讯云主 / 阿里云备）。
   拍板依据是既有账号与免费额度，不是报告里的适配度排序；报告 §0.1 的排序因此**不再作为
   执行依据**，仅保留为备选信息。
2. **「百度没有逐字段置信度」是错的**——选型报告 §4.2 的这条结论已被真实响应推翻。
   开 `probability=true` 后，字段项确实带置信度，但形态是 **dict**，不是标量（见 §2）。
3. **票种之间的字段能力差异很大**：本次样本里增值税发票可取到 10 个归一化字段，网约车
   电子发票只有 4 个，**没有**发票代码/号码、购方与税额。「人工确认必填项」必须按票种
   分别设计，不能共用一套。
4. **置信度中位数 0.912，但 19.4% 的字段低于默认阈值 0.75** → 自动过阈值的比例没有想象
   中高，人工复核量是常态而非例外。
5. **本次联调没有验证准确率**（样本无标注真值，未做字段级 exact-match）。「能用」与
   「准」是两件事，验收仍需要自建脱敏样本集（见 §9）。

---

## 1. 接口事实（已由真实调用核实）

| 项 | 值 |
|---|---|
| 鉴权 | `POST https://aip.baidubce.com/oauth/2.0/token`，`grant_type=client_credentials` + `client_id` / `client_secret` → `access_token`（按 `expires_in` 缓存，提前 60 秒过期；`110`/`111` 刷新一次后重试） |
| 识别 | `POST https://aip.baidubce.com/rest/2.0/ocr/v1/multiple_invoice`，`access_token` 放 **URL 参数** |
| Content-Type | `application/x-www-form-urlencoded` |
| 入参 | `image` / `pdf_file` / `ofd_file`（base64 → urlencode）；本适配器**不实现** `url` 模式（不把外链交给第三方抓取） |
| 可选参数 | 本模块固定传 `probability=true`；`location` / `verify_parameter` 不传（不臆造） |
| 大小上限 | base64+urlencode 后 **≤ 4,000,000 字节**（文档只写「4M」未说明进制，取十进制下界，宁可本地早拒） |
| 图片像素 | 最短边 ≥15px、最长边 ≤4096px（用标准库解析 PNG/JPEG/BMP 头；解析不了则跳过该检查并记 warning） |
| 页码 | PDF/OFD 可用 `pdf_file_num` / `ofd_file_num`（默认第 1 页）；**图片传页码即拒绝** |
| 超时 | 10 秒，超时即失败，**不自动重发** |
| 解析器标识 | `parser_name = baidu_cloud/multiple_invoice`，`parser_version = 1` |

---

## 2. 真实响应结构与三处偏差（#37 的由来）

实现最初按官方文档与选型报告写成，真实响应暴露三处偏差——**这是本文最有价值的部分**：

### 2.1 字段置信度是 dict，不是标量

```jsonc
// 真实形态（结构示意，值为占位）
{
  "log_id": 0,
  "words_result": [
    {
      "probability": { "average": 0.0, "min": 0.0, "variance": 0.0 },  // 票种分类置信度
      "type": "vat_invoice",
      "result": {
        "InvoiceCode": [ { "word": "", "probability": { "average": 0.0, "min": 0.0 } } ]
      }
    }
  ]
}
```

- `words_result[].probability` = **票种分类置信度**（也曾是 dict 形态），**只允许**用于
  `invoice_type` 这一个字段，绝不能当金额/税号的置信度；
- `result.<Field>[0].probability` = **dict** `{"average": float, "min": float}`，取值 0–1。
  原实现按标量解析 → 全部被丢弃、静默回退 0.5（**会造成「看起来有置信度、实际全是兜底值」**）。

**修正后的取值口径**：优先取 `min`（保守），缺失时回退 `average`，都没有才回退 `0.5`
（低于默认阈值 0.75 → 必然进人工复核）。标量形态仍兼容（`>1` 视为 0–100 归一）。

### 2.2 `taxi_online_ticket` 用 snake_case，不能复用 `taxi_receipt` 的别名

| 归一化键 | `vat_invoice`（PascalCase） | `taxi_online_ticket`（snake_case） |
|---|---|---|
| `invoice_date` | `InvoiceDate` | `application_date` |
| `amount` | `AmountInFiguers` | `total_fare` |
| `seller_name` | `SellerName` | `service_provider` |

票种字段名**必须以真实响应为准**；同名不同大小写是两套世界，复用别名会静默取不到值。

### 2.3 `result.<Field>` 是单元素数组，需先解包再取 `word`

原实现把整个 dict/数组 `str()` 成字段值 → 字段值是脏字符串。修正后：展开单元素数组，
再取 `word`（缺失时取 `value`）。

---

## 3. 字段映射与金额口径

归一化输出只有 10 个键，且 **`fields` 与 `field_confidence` 键集合严格一致**
（`ParseResult` 的硬约束，缺失即抛错）：

`invoice_type` / `invoice_code` / `invoice_no` / `invoice_date` / `amount` / `tax_amount` /
`buyer_name` / `buyer_tax_id` / `seller_name` / `seller_tax_id`

**金额口径（最容易系统性出错的一处）**：

| 百度字段 | 含义 | 本项目取用 |
|---|---|---|
| `AmountInFiguers` | **价税合计** | 报销金额 `amount` **只取这个** |
| `TotalAmount` | **不含税**合计 | **不取**（取错会系统性少报） |

- 金额统一为 2 位小数字符串（`Decimal` + `ROUND_HALF_UP`），避免浮点与序列化问题；
- 日期统一 ISO `YYYY-MM-DD`（支持 `2019年08月28日` 形态）；
- **4 类只分类不识别**：`limit_invoice` / `shopping_receipt` / `pos_invoice` / `others`
  —— `result` 为空，只产出 `invoice_type`，**不伪造**金额等字段。

---

## 4. 票种能力矩阵（来自本次 20 份样本）

| 票种 | 样本数 | 实际取到的归一化字段 |
|---|---|---|
| `vat_invoice` | 14 | 10 个全字段（含发票代码/号码、购方、税额） |
| `taxi_online_ticket` | 6 | **4 个**：`invoice_type` / `amount` / `invoice_date` / `seller_name` |

按载体分布：OFD 6 份全部识别为增值税发票；PDF 14 份中 8 份增值税、6 份网约车
——接口的自动票种分类在本次样本上有效。

**对下游的直接影响**：网约车票没有发票代码/号码与购方信息，「人工确认必填项」若按增值税
发票设计（要求发票号必填），网约车票会被全部卡住。该设计属于后续 Issue，本文只记事实。

**一页多票**：`words_result` 多于一张时，当前只解析**金额最大**的一张（并列取第一张），
因为下游一次只接收一张票的草稿；**不伪造** `invoice_count` 之类下游不认识的字段。
一页多票的拆分是后续 Issue。

---

## 5. 置信度口径与真实分布

**取值口径**：`min` → `average` → `0.5`（见 §2.1）。非有限值与负数丢弃。

**本次联调的真实分布**（144 个非票种字段的置信度，已排除 `invoice_type` 的分类置信度）：

| 指标 | 值 |
|---|---|
| 样本字段数 | 144（归一化字段合计 164，含 20 个 `invoice_type`） |
| min | 0.29 |
| 中位数 | 0.912 |
| max | 0.973 |
| < 0.75（默认阈值） | 28 个（19.4%） |

**读法**：不要按「高置信度即可自动入账」做设计。近五分之一字段会落到阈值以下走人工复核，
这与「人工确认是进入正式记录的唯一路径」的既有设计是一致的——**阈值以下不是故障**。

---

## 6. 预检 fail closed（发请求前，连 token 都不取）

顺序：扫描状态 → MIME → 空内容 → 页码 → 编码后大小 → 图片像素。拒绝原因只写日志
（常量，不含文件内容）：

1. `scan_status != 'clean'`（未过毒扫的原件不得送第三方）；
2. MIME 不在允许列表（jpg/jpeg/png/bmp 与 PDF/OFD）；
3. 空内容；
4. 图片却指定页码、或页码非法（非 ≥1 整数）；
5. base64+urlencode 后超过 4,000,000 字节；
6. 图片像素越界（**图片尺寸解析不了时跳过该检查并记 warning，不阻断**）。

---

## 7. 凭据、传输与失败收敛

- **凭据**：只从环境变量读，fail closed（缺一个即构造失败）。优先名 `BDOCR_API_KEY` /
  `BDOCR_SECRET_KEY`，兼容回退 `AJAIMGR_BAIDU_API_KEY` / `AJAIMGR_BAIDU_SECRET_KEY`。
  凭据值**不进结果、不进日志**；`log_id` 只写 debug 日志，不进 `ParseResult.fields`。
- **传输层可注入**：默认 `httpx`（惰性导入）；签名是
  `(url, headers, body, timeout) -> HttpResponse`。本次联调在**没有 httpx 的环境**里用标准库
  urllib 实现同签名传输跑完全量 20 份——证明传输层与业务逻辑确实解耦，环境缺网络库不构成
  阻塞（但生产环境仍应按 `pyproject.toml` 安装 `httpx`）。
- **失败收敛**：预检失败、传输异常、4xx、业务错误码、响应形态异常，统一产出
  `error='provider_error'`，**不泄漏**供应商错误码与响应原文给上层；错误码仅内部记日志
  （`110/111` token、`17/18/19` 配额、`216631` 尺寸、`282000` 服务，未知一律
  `provider_error`）。

---

## 8. 本次联调证据（聚合口径，2026-10-18，head `fd4d197`）

| 项 | 结果 |
|---|---|
| 样本 | 20 份真实票据（PDF 14 / OFD 6） |
| 成功率 | **20 / 20**，失败 0 |
| 票种分布 | `vat_invoice` 14、`taxi_online_ticket` 6 |
| 字段覆盖 | `amount` / `invoice_date` / `invoice_type` / `seller_name` 各 20；`invoice_code` / `invoice_no` / `buyer_name` / `buyer_tax_id` / `seller_tax_id` / `tax_amount` 各 14 |
| 每票字段数 | min 4 / 中位 10 / max 10，合计 164 |
| 契约不变量 | **0 违规**（`fields` 与 `field_confidence` 键集合一致、置信度值域 [0,1]、金额两位小数、日期 ISO） |

**注意**：这是「端到端跑通」的证据，**不是准确率证据**（见 §9 第 1 项）。

---

## 9. 仍未验证 / 未覆盖（不要当成已完成）

1. **准确率未验证**：20 份样本没有标注真值，未做字段级 exact-match。验收仍需按选型报告
   §4.3 的建议自建**脱敏**样本集（≥100 张/票种，覆盖拍照、扫描、折损、混贴）。
2. **样本覆盖面窄**：本次只有增值税发票与网约车票两类；行程单、出租车票、定额发票、
   火车票、机票等票种**从未在真实响应中出现过**，其字段别名是按文档推定的。
3. **合规条款未关闭**：数据处理协议（DPA）、PIPIA、留存与是否用于训练——仍按选型报告
   §9 第 1 项**未验证**，这是合规阻塞项，不是技术项。
4. **配额与计费未验证**：免费额度、超额度后的行为、PDF 多页是否重复计费，均未实测。
5. **OFD 只见过增值税发票**：OFD 承载其他票种时的表现未知。
6. **未注册进 `ParserRegistry`**：当前适配器可用但未接入自动路由，端到端「解析 → 草稿 →
   人工确认」链路尚未打通（后续 Issue）。
7. **一页多票拆分未实现**（见 §4）。

---

## 10. 隐私与安全边界（操作级，不是口号）

联调时实际执行的约束，后续重跑必须照做：

- **凭据**：只在环境变量/本地 `.env` 中存在；`.env` 必须被 Git 忽略（已入 `.gitignore`，
  各机另用 `.git/info/exclude` 兜底）；**不打印、不回显、不写进日志与 Issue**。
- **票据**：真实样本只在临时目录解压，用完即删；**不进仓库、不进 `git status`、不进 CI**。
- **输出**：脚本只打印**聚合统计**（计数、分布、分位数）；票面字段值一律不打印、不落盘。
- **日志**：`log_id` 只进 debug；异常原文、凭据、响应体**不进结果、不进日志**。
- **失败**：统一 `provider_error`，不把供应商错误码与响应原文泄漏给上层或用户。

---

## 11. 复现方式（不含任何敏感值）

```
# 1) 在仓库根目录放置 .env（BDOCR_API_KEY / BDOCR_SECRET_KEY），并确认已被忽略
#    git check-ignore -v .env    → 必须命中
# 2) 用受控样本集（自有的、可留存或可删除的）在仓库外临时目录解压
# 3) 注入 SourceReader 与（可选）标准库传输，构造 BaiduInvoiceOcrParser 后逐个 parse
# 4) 只收集聚合统计；结束后删除临时目录
```

单元测试不需要真实凭据：`app/tests/integrations/test_baidu_invoice_ocr.py`（54 例）用桩传输
覆盖 token 缓存/刷新、预检拒绝、字段归一化、置信度形态、失败收敛等分支。
