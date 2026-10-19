# Track D 首个真实模板：导出模板结构落库与映射设计（Issue #43）

> 关联 Issue：**#43**（`docs: Track D 首个真实模板——导出模板结构落库与映射设计`）。
> 上游依据：[AgentMgr_AiNativeERP_Integration_Plan.md](AgentMgr_AiNativeERP_Integration_Plan.md)
> §0.2（对外账务交接改为模板驱动 Excel 导出）与 §7（Track D 导出契约、校验、manifest）。
> 只读参考代码：[`../app/expense/models.py`](../app/expense/models.py)、
> [`../app/expense/invoices.py`](../app/expense/invoices.py)、
> [`../app/expense/service.py`](../app/expense/service.py)、
> [`../app/expense/money.py`](../app/expense/money.py)、
> [`../app/integrations/models.py`](../app/integrations/models.py)、
> [`../app/documents/models.py`](../app/documents/models.py)、
> [`../app/documents/storage.py`](../app/documents/storage.py)、
> [`../app/documents/export.py`](../app/documents/export.py)、
> [`../app/db/models/organization.py`](../app/db/models/organization.py)、
> [`../app/identity/permissions.py`](../app/identity/permissions.py)。

**本文件只产文档，不实现导出代码。** Track D 的机制实现（loader / validator /
mapping engine / manifest 落库）与首个真实模板导出验收是**下游 Issue**，本任务不
触碰 `app/`、`scripts/`、`tests/`、`migrations/`。

---

## 0 结论摘要

1. 所有者提供的模板文件（下称 `<template-file>`，本地只读，**不入库**）是一个
   **14 工作表**的凭证导入模板包：7 张**输入表**（`简易日记账` / `日记账` /
   `发票记录` / `横向自定义` / `发票多行式` / `发票多行式二` / `先借后贷`）+ 1 张
   工具的**中转表**（`录入数据`）+ 1 张标准格式凭证列集（`PZ`）+ 3 张基础资料表
   （`科目数据` / `核算项目` / `核算分类`）+ 1 张辅助表（`模式凭证`）+ 1 张说明页
   （`使用说明`）。
2. 四个候选布局中，**推荐默认目标布局为 `录入数据`（31 列，一行 = 一条会计分录）
   ——「待所有者确认」**；`PZ`（44 列）为备选（直接对齐「标准格式凭证 / DBF」通道）；
   `发票记录` 与 `日记账` 不推荐作为本系统报销闭环的默认目标（理由见 §3.2）。
3. 映射设计的最大信息缺口**不在模板侧，而在本系统侧**：本系统**没有科目表、
   没有银行/资金账户、没有销售方（往来单位）名称字段、没有保留期实现**。因此
   「科目代码、贷方资金科目、辅助核算往来单位」三类值必须由所有者给出映射口径，
   否则导出只能 fail closed（§4.3、§9）。
4. 模板本身的结构事实（14 表、表头行、列清单、必填性线索、示例行结构）由 §8 的
   内嵌命令可重跑复核，逐项输出见附录 A；纯结构 sidecar 见
   [`export-templates/template-structure.json`](export-templates/template-structure.json)。
5. **更新（2026-10-19，Issue #71）**：所有者改用**新模板**
   《凭证导入模板-20261019》——目标布局改为 `凭证模版` 单表 **25 列**
   （`voucher_template`），取代上面第 2 条的 `录入数据` 31 列（该布局保留为
   历史/回退）。新模板结构见 §2.4、逐列映射见 §5.6、两布局对照见 §5.7；纯结构
   sidecar 见
   [`export-templates/voucher-template-structure.json`](export-templates/voucher-template-structure.json)。

**演示数据的处理**：模板自带示例行中的往来单位、公司名与人名一律**不复制**到本
文件与 sidecar；本文只记录**结构**（表头文本、列位置、行位置、被填充的列集合）。
本文出现的科目代码类示例（如 `100201`、`560101`）均为模板自带的基础资料示例，
不含任何真实业务数据。

---

## 1 范围、证据口径与非目标

### 1.1 范围

- 把模板结构（工作表、表头行、列清单、必填性线索、示例行结构）落库为可复核文档；
- 给出「报销单 / 明细行 / 挂账发票 → 模板列」的逐列映射设计（四种候选布局）；
- 给出校验规则、manifest 草案、需所有者拍板的决策清单。

### 1.2 证据口径

| 结论类型 | 证据 |
|---|---|
| 模板结构 | §8 内嵌命令 + 附录 A 真实输出 + [`export-templates/template-structure.json`](export-templates/template-structure.json) |
| 模板用途 | `使用说明` 工作表原文（第一至第七步操作说明），本文只引用短语 |
| 本系统字段 | 只读代码符号（本文一律写 `文件路径` + `符号名`，不写行号以免漂移） |
| 金额口径 | [`../app/integrations/baidu_invoice_ocr.py`](../app/integrations/baidu_invoice_ocr.py) `_VAT_ALIASES` 与 [`research/W2-35-baidu-multiple-invoice-ocr.md`](research/W2-35-baidu-multiple-invoice-ocr.md) §「金额口径」 |
| 未知 / 未实现 | 明确标注「缺口」或「需所有者决策」，**不臆测** |

### 1.3 非目标

- 不实现导出（不写 loader / validator / writer / 端点 / 迁移）；
- 不引入依赖（`pyproject.toml` / `uv.lock` 不动）；
- 不把模板文件、真实业务数据、凭据或本机信息写入仓库；
- 不代替所有者做产品决策（§9 全部为待拍板项）。

---

## 2 模板总览（14 个工作表）

### 2.0 模板指纹

| 项 | 值 | 来源 |
|---|---|---|
| 文件（占位） | `<template-file>`（所有者本地文件，不入库） | 所有者提供 |
| 大小 | 161974 字节 | 内嵌命令输出 |
| SHA-256 | `1a235166285ee31255c899ce57e09eedd1e7f6641cd6fd3b5e931471ecb2718c` | 内嵌命令输出 |
| 工作表数 | 14 | 内嵌命令输出 |
| 共享字符串数 | 989 | 内嵌命令输出 |
| 工作簿命名区域 | `_xlnm._FilterDatabase`（`local_sheet_id=4` → `横向自定义!#REF!`，已失效）；`_xlnm._FilterDatabase`（`local_sheet_id=8` → `录入数据!$A$1:$AE$8`） | 内嵌命令输出 |

按上游 §7.2 的模板标识约定（`template_id` + `template_version` + SHA-256），建议把
上表 SHA-256 作为**首个 `template_version = 1` 的指纹**；模板任何列或口径变化必须
升版，不改历史版本（§六、§7.2）。

### 2.1 工作表清单

| # | 工作表 | 角色 | 表头行 | 数据起始行 | 列数 | 非空行数 | extent |
|---|---|---|---|---|---|---|---|
| 1 | `使用说明` | 说明页（无表结构） | 无 | 无 | — | 24（分布到第 34 行） | `A1:V34` |
| 2 | `简易日记账` | 输入表：一行 → 2 分录 | 1 | 2 | 15 | 3（2 行数据） | `A1:O5` |
| 3 | `日记账` | 输入表：一行 → 2 分录 | 1 | 2 | 29 | 5（4 行数据） | `A1:AC21` |
| 4 | `发票记录` | 输入表：一行 → 3 分录 | 1 | 2 | 32 | 4（3 行数据） | `A1:AF31` |
| 5 | `横向自定义` | 输入表：键值转置（无表头行） | 无 | 无 | — | 11 | `A1:H29984` |
| 6 | `发票多行式` | 输入表：一行 → 3 分录 | 3 | 4 | 16 | 6（3 行数据） | `A1:Q33` |
| 7 | `发票多行式二` | 输入表：一行 → 3 分录 | 1 | 2 | 20 | 7（6 行数据） | `A1:V31` |
| 8 | `先借后贷` | 输入表：一行 → 2 分录 | 1 | 2 | 13 | 7（6 行数据） | `A1:T31` |
| 9 | `录入数据` | 工具**中转表**：一行 = 一条分录 | 1 | 2 | 31 | 7（6 行分录） | `A1:AE18` |
| 10 | `PZ` | 标准格式凭证表（F\* 列集） | 1 | 2 | 44 | 19（18 行分录） | `A1:AR19` |
| 11 | `科目数据` | 基础资料：会计科目（供工具校验/映射） | 1 | 2 | 15 | 202（201 行科目） | `A1:O202` |
| 12 | `核算项目` | 基础资料：辅助核算项目 | 1 | 2 | 9（表头占 9 列，第 10 列无表头） | 16（15 行项目） | `A1:J76` |
| 13 | `模式凭证` | 辅助：按摘要关键词生成固定分录 | 1 | 2 | 17 | 19（18 行模板行） | `A1:Q19` |
| 14 | `核算分类` | 基础资料：辅助核算类别字典 | 1 | 2 | 4 | 6（5 行类别） | `A1:D6` |

「列数」= 表头行非空单元格数；`使用说明` 与 `横向自定义` 无表头行（见 §2.2 与 §8
的 `header_row` 覆盖策略）。

### 2.2 逐工作表结构

表内符号：`※` = 表头文本自带「非必录 / 非必填 / 非必须 / 无需手填」线索（**文本
线索，非权威规则**）；「列清单」用模板表头原文（`_x000D_`、CR、LF、TAB 归一化为
一个空格）。

#### 2.2.1 `使用说明`（#1）

- **用途**：模板包的使用说明与七步操作流程；说明适用范围（两版模板分别适配的
  第三方产品族）、各输入表的定位、工具按钮流程、「录入数据」的必填集合、以及
  「生成 dbf 标准格式凭证文件」的步骤。
- **结构**：无表头行、无列结构；24 个非空单元格分布在 1–34 行，其中 A 列为流程
  文本，`N2` 为「跟随位置」语义说明（0/1/2 跟随第 N 条分录，为空视为 0）；
  10 个合并区域（如 `A15:M15`、`N2:V2`）。
- **必填性**：本表不定义必填性；但对 `录入数据` 明确写出**必填集合**：
  凭证字、凭证号、日期、摘要、科目代码、借方金额、贷方金额；其余「可以让程序
  检查补充」（原文短语）。
- **示例行结构**：不适用（非数据表）。
- **备注**：原文明确「各表中有填写参考例子（**注意例子内容不一定正确**）」——因此
  本文与 sidecar **不把演示数据的取值当作口径依据**。

#### 2.2.2 `简易日记账`（#2）

- **用途**：资金方向的两分录输入表（收款/付款流水、现金银行日记账等）；每行记录
  转 2 行分录。
- **结构**：表头行 1，数据起始行 2，15 列（A–O），extent `A1:O5`，非空 3 行。
- **列清单**：A 凭证字 (非必录)※ · B 序号 · C 凭证日期 交易日期 · D 摘 要 ·
  E 借方(收款金额） · F 贷方（付款金额） · G 资金科目代码 (与金额方向相同) ·
  H 资金科目名称 (非必填）※ · I 辅助核算名称A 跟随资金科目(非必填）※ ·
  J 对方科目代码 (与金额方向相反) · K 对方科目名称 (非必填）※ ·
  L 辅助核算名称B 跟随对方科目(非必填）※ · M 数据检测情况 · N 匹配模板编号 ·
  O 匹配模板名称
- **示例行结构**：首行数据被填充的列集合 = `A B C D F G H J K N O`（本演示行是
  付款行：`F` 有值而 `E` 为空，`I`/`L`/`M` 为空）。
- **必填性**：`A` 标注非必录、`H`/`K` 非必填、`I`/`L` 非必填；其余未标注 →
  权威必填集合未声明（**待确认**）。
- **备注**：无合并、无数据验证、无公式。

#### 2.2.3 `日记账`（#3）

- **用途**：资金方向两分录输入表的加宽版（含数量/单价/外币/业务编号列）。
- **结构**：表头行 1，数据起始行 2，29 列（A–AC），extent `A1:AC21`，非空 5 行。
- **列清单**：A 凭证字 (非必录)※ · B 序号 · C 凭证日期 交易日期 · D 摘要 ·
  E 借方金额 · F 贷方金额 · G 数量跟随位置 · H 数量 · I 数量单位 · J 单价 ·
  K 资金科目代码 (与金额方向相同) · L 资金科目名称A (非必填）※ ·
  M 辅助核算名称A1 （非必须）※ · N 辅助核算名称A2 （非必须）※ ·
  O 对方科目代码 (与金额方向相反) · P 对方科目名称B (非必填）※ ·
  Q 辅助核算名称B1 （非必须）※ · R 辅助核算名称B2 （非必须）※ ·
  S 外币跟随位置 · T 币别代码 · U 币别名称 · V 外币原值 · W 汇率 · X 附件数 ·
  Y 业务编号位置 · Z 业务编号 · AA 数据检测情况 · AB 匹配模板编号 ·
  AC 匹配模板名称
- **示例行结构**：首行数据被填充的列集合 = `A B C D E F H K L M O P X`。
- **必填性**：`A` 非必录、`L`–`R` 分别标注非必填/非必须；其余未标注（**待确认**）。
- **备注**：2 个数据验证（`G2:G4` 列表、`Y1:Y4` 整数）；无合并、无公式。
  `G`/`S`/`Y` 为「跟随位置」控制列（指向本记录的第 N 条分录）。

#### 2.2.4 `发票记录`（#4）

- **用途**：按份处理进项/销项发票，一行 → 3 行分录（借方费用 / 税费 / 贷方）；
  表内显式给出「不含税金额 / 税额 / 价税合计」三列。
- **结构**：表头行 1，数据起始行 2，32 列（A–AF），extent `A1:AF31`，非空 4 行。
- **列清单**：A 凭证字 (非必录)※ · B 凭证号 · C 凭证日期 · D 摘 要 ·
  E 数量跟随位置 · F 数量单位 (非必填）※ · G 数量 (非必填）※ ·
  H 单价 (非必填）※ · I 不含税金额 · J 税额 · K 价税合计 ·
  L 借方 科目代码A · M 借方 科目名称A (非必填）※ ·
  N 辅助核算名称A1 (非必填）※ · O 辅助核算名称A2 (非必填）※ ·
  P 税费 科目代码B · Q 税费科 目名称B · R 贷方 科目代码C ·
  S 贷方 科目名称C (非必填）※ · T 辅助核算名称C1 (非必填）※ ·
  U 辅助核算名称C2 (非必填）※ · V 外币跟随位置 · W 币别代码 · X 币别名称 ·
  Y 外币原值 · Z 汇率 · AA 附件数 · AB 业务编号位置 · AC 业务编号 ·
  AD 数据检测情况 · AE 模板号（无需手填）※ · AF 模板名称（无需手填）※
- **示例行结构**：首行数据被填充的列集合 = `A B C D G H I J K L M P Q R S AA`。
- **必填性**：`A` 非必录；`F`–`H`/`M`–`O`/`S`–`U` 非必填；`AE`/`AF` 为工具列；
  其余未标注（**待确认**）。
- **备注**：2 个数据验证（`E2:E31` 与 `AC2:AC31` 列表、`AB1:AB12` 整数）。
  该布局把「贷方科目」固定为单一 C 列（工具语义为应付/应收），与报销付款场景的
  贷方语义不同（见 §3.2）。

#### 2.2.5 `横向自定义`（#5）

- **用途**：给「无明细行、科目固定」的场景做横向上的科目/核算项目自定义。
- **结构**：**无表头行**（键值转置布局）；列 A 为行标签（`F1`…`F5` 等键），
  E–H 为横向科目槽位：第 1 行 = 「科目代码」标签，第 2 行 = 科目代码值，
  第 3 行 = 「科目名称」标签，第 4 行 = 科目名称值，第 5 行 = 「科目方向」标签，
  第 6 行 = 方向值（`借方`/`贷方`）。extent 声明 `A1:H29984`，实际非空 11 行。
- **列清单**：无表头行 → 不适用（键值结构，见上）。
- **示例行结构**：非空行 1–11；`E`–`H` 四槽在 1–6 行成对出现标签/取值。
- **必填性**：未声明（**待确认**）。
- **备注**：1 个数据验证（`E6:H6` 列表：科目方向）；`local_sheet_id=4` 的命名
  区域 `横向自定义!#REF!` **已失效**（模板残留）。

#### 2.2.6 `发票多行式`（#6）

- **用途**：按发票**每行明细**入账；第 2 行先选票种并填写应收/应付、税费科目等
  配置，第 3 行起为明细表头。
- **结构**：表头行 **3**（`A`–`P`，16 列），数据起始行 **4**，extent `A1:Q33`，
  非空 6 行；第 1 行为配置项名称行，第 2 行为配置值行（`A` 为票种、`B` 为凭证日期、
  `C`/`D` 为应收应付科目、`E` 为「对方公司名称存放位置」、`F`/`G` 为税费科目）。
- **列清单**：A 发票代码 · B 发票号码 · C 全电号码 · D 对方公司名称 ·
  E 单位 (非必填）※ · F 数量 (非必填）※ · G 单价 (非必填）※ · H 不含税金额 ·
  I 税额 · J 价税合计 · K 摘要 · L 科目代码A · M 科目名称A (非必填）※ ·
  N 辅助核算名称A1 (非必填）※ · O 辅助核算名称A2 (非必填）※ · P 数据检测情况
- **示例行结构**：首个数据行（第 4 行）被填充的列集合 = `B D F G H I J K L M`
  （`A` 发票代码、`C` 全电号码留空）。
- **必填性**：`E`–`G`/`M`–`O` 非必填；其余未标注（**待确认**）。第 2 行配置项
  （票种/科目）在语义上是必填，但**未在表头标注**。
- **备注**：1 个数据验证（`A2` 票种列表）。**表头不在第 1 行**——机读实现必须按
  固定行号定位，不能假设「首行即表头」（见 §8 覆盖策略）。

#### 2.2.7 `发票多行式二`（#7）

- **用途**：与 `发票多行式` 同类，但表头归一到第 1 行、列集更宽（借贷方科目与
  辅助核算直接给列）。
- **结构**：表头行 1，数据起始行 2，20 列（A–T），extent `A1:V31`，非空 7 行。
- **列清单**：A 发票代码 · B 发票号码 · C 全量发票号码 · D 摘 要 ·
  E 数量单位 (非必填）※ · F 数量 (非必填）※ · G 单价 (非必填）※ ·
  H 不含税金额 · I 税额 · J 价税合计 · K 借方跟随明细 科目代码A ·
  L 借方跟随明细 科目名称A (非必填）※ · M 跟随明细辅助核算名称A (非必填）※ ·
  N 税费 科目代码B · O 税费科 目名称B · P 税费科目核算名称B (非必填）※ ·
  Q 贷方应付 科目代码C · R 贷方应付 科目名称C (非必填）※ ·
  S 辅助核算名称C1 (非必填）※ · T 数据检测情况
- **示例行结构**：首行数据被填充的列集合 = `A B D F G H I J K L N O Q R`。
- **必填性**：`E`–`G`/`L`/`M`/`P`/`R`/`S` 非必填；其余未标注（**待确认**）。
- **备注**：无数据验证、无合并、无公式。

#### 2.2.8 `先借后贷`（#8）

- **用途**：一行给出一对（借方跟随明细 + 贷方）科目与单一金额的两分录输入表，
  适合「先多行借方、后多行贷方」之外的简单配对场景。
- **结构**：表头行 1，数据起始行 2，**13 个表头单元格**（A–L 共 12 个，另加 T 列
  一个同名表头），extent `A1:T31`，非空 7 行。
- **列清单**：A 凭证字 · B 凭证号 · C 凭证日期 · D 摘 要 · E 金额 ·
  F 借方跟随明细 科目代码A · G 借方跟随明细 科目名称A (非必填）※ ·
  H 跟随明细辅助核算名称A (非必填）※ · I 贷方 科目代码B ·
  J 贷方 科目名称B (非必填）※ · K 辅助核算名称B (非必填）※ · L 数据检测情况 ·
  T 数据检测情况
- **示例行结构**：首行数据被填充的列集合 = `A B C D E F G I J`。
- **必填性**：`G`/`H`/`J`/`K` 非必填；其余未标注（**待确认**）。
- **备注**：`L` 与 `T` 两列表头**同名**（`数据检测情况`），属模板复制残留 →
  机读实现**必须按列号定位，不能按表头名定位**。

#### 2.2.9 `录入数据`（#9）

- **用途**：模板自带工具的**中转表**（原文短语「重要的中转表」）：其他输入表经工具
  转换后写入本表，也可手动填写；本表再生成标准格式凭证文件。**一行 = 一条会计分录**。
- **结构**：表头行 1，数据起始行 2，31 列（A–AE），extent `A1:AE18`，非空 7 行
  （6 行分录）；工作簿命名区域 `录入数据!$A$1:$AE$8` 表明工具的自动筛选/处理区为
  `A1:AE8`。
- **列清单**：A 凭证字 · B 凭证号 · C 凭证日期 · D 凭证摘要 · E 科目代码 ·
  F 科目名称 · G 余额方向 D/C(自动补充) · H 币别代码 · I 币别名称 · J 原币金额 ·
  K 汇率 · L 数量 · M 单价 · N 借方金额 · O 贷方金额 · P 制单人 ·
  Q 辅助核算类别A · R 项目代码A · S 核算项目名称A · T 辅助核算类别B ·
  U 项目代码B · V 核算项目名称B · W 辅助核算类别C · X 项目代码C ·
  Y 核算项目名称C · Z 辅助核算类别D · AA 项目代码D · AB 核算项目名称D ·
  AC 业务编号 · AD 附件数 · AE 检测错误情况
- **示例行结构**：首行数据被填充的列集合 = `A B C D E F H I J K L M N S`
  （该演示行是借方分录：`N` 有值、`O` 为空，仅用了第 1 组辅助核算名称 `S`）。
- **必填性**：`使用说明` 对本表明确写出必填集合 = 凭证字、凭证号、日期、摘要、
  科目代码、**借方金额、贷方金额**；`F`/`G`/`H`–`M`/`P`–`AB`/`AC`–`AD` 由工具
  检查或按需补充。表头未使用「非必录」标注（唯一无 ※ 的表之一）。
- **备注**：无数据验证、无合并、无公式；`G` 列由工具「自动补充」。

#### 2.2.10 `PZ`（#10）

- **用途**：与第三方系统「标准格式凭证文件」等价的**列集定义**（F\* 列名族），
  一行 = 一条分录，含期间、分录序号、借贷方向与四组辅助核算。
- **结构**：表头行 1（英文列名），数据起始行 2，44 列（A–AR），extent `A1:AR19`，
  非空 19 行（18 行分录）。
- **列清单**：A FDATE · B FPERIOD · C FGROUP · D FNUM · E FENTRYID · F FEXP ·
  G FACCTID · H FCLSNAME1 · I FOBJID1 · J FOBJNAME1 · K FCLSNAME2 · L FOBJID2 ·
  M FOBJNAME2 · N FCLSNAME3 · O FOBJID3 · P FOBJNAME3 · Q FCLSNAME4 · R FOBJID4 ·
  S FOBJNAME4 · T FTRANSID · U FCYID · V FEXCHRATE · W FDC · X FFCYAMT ·
  Y FQTY · Z FPRICE · AA FDEBIT · AB FCREDIT · AC FSETTLCODE · AD FSETTLENO ·
  AE FPREPARE · AF FPAY · AG FCASH · AH FPOSTER · AI FCHECKER · AJ FATTCHMENT ·
  AK FPOSTED · AL FMODULE · AM FDELETED · AN FSERIALNO · AO FTRANSDATE ·
  AP FCASHFLOW · AQ FUNITNAME · AR FREFERENCE
- **示例行结构**：首个数据行被填充的列集合 =
  `A B C D E F G U V W X Y Z AA AB AE AJ AK AM`。
- **必填性**：未在任何表头标注（**待确认**）。仅 `使用说明` 提示「日期、金额有误的
  请分列或替换转换正确」，未给必填集合。
- **备注（重要观察）**：演示数据中 `W`（`FDC` 借贷方向）与
  `AA`/`AB`（`FDEBIT`/`FCREDIT`）**不一致**——多数行 `FDC='D'` 而金额落在
  `FCREDIT`，另有一行 `FDC='C'` 而金额落在 `FDEBIT`。因此 `FDC` **不能**照抄；
  实现必须由同一处规则同时生成 `FDC` 与 `FDEBIT`/`FCREDIT`。
  `PZ` 表内**不含** `科目数据`/`核算分类`——科目与辅助核算类别需由工具侧另行装载。

#### 2.2.11 `科目数据`（#11）

- **用途**：基础资料表：会计科目及其属性（余额方向、外币/往来/部门/职员/数量核算
  标志等），供工具在转换时校验与解析科目代码。
- **结构**：表头行 1，数据起始行 2，15 列（A–O），extent `A1:O202`，非空 202 行
  （201 行科目）。
- **列清单**：A 科目代码 · B 科目名称 · C 助记码 · D 科目类别 · E 是否存货科目 ·
  F 余额方向 · G 外币核算 · H 期末调汇 · I 往来单位核算 · J 部门核算 ·
  K 职员核算 · L 其他核算项目 · M 往来业务核算 · N 数量金额核算 · O 数量单位
- **示例行结构**：首个数据行被填充的列集合 = `A B C F`（其余列留空表示「无该属性」）。
- **必填性**：未标注（**待确认**，推测 `A`/`B` 为必需）。
- **备注**：**这是本系统最可能复用的模板资源**：若未来在本系统内维护科目映射，
  可导出此表装入模板，避免工具侧科目缺失导致导入被拒（见 §9 决策 D2）。

#### 2.2.12 `核算项目`（#12）

- **用途**：基础资料表：辅助核算项目（往来单位/部门等）及其对应的费用/成本科目。
- **结构**：表头行 1（9 列 A–I），但**第 10 列 `J` 无表头**且被填充（`类别|代码|名称`
  拼接形式）；extent `A1:J76`，非空 16 行（15 行项目）。
- **列清单**：A 核算分类 · B 代码 · C 核算名称 · D 费用、成本科目代码-A ·
  E 费用、成本科目名称-A · F 费用、成本科目代码-B · G 费用、成本科目名称-B ·
  H 费用、成本科目代码-C · I 费用、成本科目名称-C
- **示例行结构**：首个数据行被填充的列集合 = `A B C J`。
- **必填性**：未标注（**待确认**；`A`/`B`/`C` 从示例看为必需）。
- **备注**：表头只有 9 列而 extent 到 `J` → 机读实现应按**列号**取值并在 `J` 上
  按无表头列处理，不要假设「表头列数 = 数据列数」。

#### 2.2.13 `模式凭证`（#13）

- **用途**：辅助：按摘要关键词匹配，生成固定科目/比例/金额的多行分录模板。
- **结构**：表头行 1，数据起始行 2，17 列（A–Q），extent `A1:Q19`，非空 19 行
  （18 行模板行）；`F` 列为**公式填充**（该表公式单元格共 18 个），同一模板的多行
  共享同一模板编号。
- **列清单**：A 顺序号 · B 摘要等于条件 · C 摘要包含条件 · D 摘要不包含条件 ·
  E 备用 · F 模板编号 · G 模板名称 · H 科目代码 · I 科目名称 ·
  J 辅助核算明细名称 · K 借方金额比例 · L 贷方金额比例 · M 借方金额 ·
  N 贷方金额 · O 匹配新摘要（非必须）※ · P 匹配币别代码 · Q 匹配汇率
- **示例行结构**：首个数据行被填充的列集合 = `A C F G H I K M O`（该行是模板首行：
  有匹配条件，无比例/金额则留空表示沿用下行）。
- **必填性**：`O` 非必须；其余未标注（**待确认**）。
- **备注**：含公式 → 若目标布局依赖本表，导出必须写入**值**而非公式（
  `app/documents/export.py` 现有 XLSX 渲染也只写值）。

#### 2.2.14 `核算分类`（#14）

- **用途**：辅助核算类别字典（`FClsID` → `FClsName`），供 `PZ` 的
  `FCLSNAME*` 列取值。
- **结构**：表头行 1，数据起始行 2，4 列（A–D），extent `A1:D6`，非空 6 行
  （5 行类别）。
- **列清单**：A FClsID · B FClsName · C FQtyAux · D FObjTableName
- **示例行结构**：首个数据行被填充的列集合 = `A B C`。
- **必填性**：未标注（**待确认**）。
- **备注**：字典示例含「往来单位 / 部门 / 职员 / 工程」四类与一个 `*` 通配；
  `D`（`FObjTableName`）仅「工程」行有值 → 机读时按行取值。

### 2.3 模板侧的结构性风险（机读实现必须处理的差异）

| # | 事实 | 影响与对策 |
|---|---|---|
| R1 | `发票多行式` 的表头在第 **3** 行 | 不得假设「首行即表头」；表头行必须按模板版本显式登记 |
| R2 | `使用说明`、`横向自定义` 无表头行 | 同上；这两张表在实现中应只作说明/可选输入，不作为机器写入目标 |
| R3 | `先借后贷` 的 `L` 与 `T` 列**表头同名** | 列定位必须用列号/列索引，不能用表头名匹配 |
| R4 | `核算项目` 表头 9 列而 extent 到 `J`（第 10 列无表头） | 同上 |
| R5 | `PZ` 演示数据的 `FDC` 与 `FDEBIT`/`FCREDIT` 不一致 | 方向列必须与金额列同源生成；禁止照抄模板示例行 |
| R6 | `模式凭证` 的 `F` 列为公式 | 只写值，不写公式（沿用现有 XLSX 渲染的取值方式） |
| R7 | `横向自定义` extent 到 `H29984`（格式化的空行）、且命名区域指向 `#REF!` | 不得用 extent 推断数据区长度；有效行以「有非空单元格」为准 |
| R8 | `使用说明` 原文声明示例「内容不一定正确」 | 演示数据不得作为口径依据；结构与口径分别取证 |

### 2.4 新模板《凭证导入模板-20261019》结构（Issue #71，2026-10-19）

> 所有者 2026-10-19 决定改用**新模板**（见 §9「D1 更新」与 Issue #43 评论）。
> 新模板是与 §2.1–§2.2 的旧模板**不同的文件**；旧模板的 14 表结构保留为历史记录。

- **提取方式**：仍用 §8.1 的内嵌脚本（stdlib，只输出结构），对新模板运行**无需**
  新增 `HEADER_ROW_OVERRIDES`（三张表首行即表头行）。
- **纯结构 sidecar**：
  [`export-templates/voucher-template-structure.json`](export-templates/voucher-template-structure.json)
  （**仅结构、零数据值**）。
- **工作表（3 张）**：

| # | 工作表 | 角色 | 表头行 | 数据起始行 | 列数 | extent | 非空行 |
|---|---|---|---|---|---|---|---|
| 1 | `凭证模版` | **导入目标表**（一行 = 一条分录） | 1 | 2 | 25（A–Y） | `A1:Y1` | 1（仅表头，无示例数据行） |
| 2 | `科目数据` | **备查表，不会被导入** | 1 | 2 | 6（A–F） | `A1:F234` | 234 |
| 3 | `辅助核算项目数据` | **备查表，不会被导入** | 1 | 2 | 3（A–C） | `A1:C24` | 24 |

- **`凭证模版` 列清单**（A–Y，按列序；表头文本为模板原文）：

  | 列 | 表头 | 列 | 表头 |
  |---|---|---|---|
  | A | 日期 | N | 项目 |
  | B | 凭证字 | O | 部门 |
  | C | 凭证号 | P | 存货 |
  | D | 附件数 | Q | 自定义辅助核算类别 |
  | E | 分录序号 | R | 自定义辅助核算编码 |
  | F | 摘要 | S | 自定义辅助核算类别1 |
  | G | 科目代码 | T | 自定义辅助核算编码1 |
  | H | 科目名称 | U | 数量 |
  | I | 借方金额 | V | 单价 |
  | J | 贷方金额 | W | 原币金额 |
  | K | 客户 | X | 币别 |
  | L | 供应商 | Y | 汇率 |
  | M | 职员 | | |

- **备查表的表头文本**（模板原文；两张表均为「该列是备查数据、不会被导入」的
  提示文本，**不含**任何科目/核算项目取值）：`科目数据` 六列均为
  `科目备查数据,不会被导入。`；`辅助核算项目数据` 三列均为
  `辅助核算项目备查数据,不会被导入。`。sidecar 只登记**列位置与表头文本**，
  不登记任何行取值。
- **结构性风险**：本表无示例数据行（`non_empty_rows = 1`），故「示例行结构」不可
  作为口径依据；所有列处置由 §5.6 与配置显式给定。

---

## 3 候选导入布局对比与推荐

### 3.1 对比

| 维度 | `PZ` | `发票记录` | `录入数据` | `日记账` |
|---|---|---|---|---|
| 行语义 | 一行 = 一条分录 | 一行 = 一份发票（→ 3 分录） | 一行 = 一条分录 | 一行 = 一组资金/对方科目（→ 2 分录） |
| 列数 | 44 | 32 | 31 | 29 |
| 表头行 | 1 | 1 | 1 | 1 |
| 在模板中的角色（据 `使用说明`） | 「标准格式凭证文件」等价列集（`使用说明` 第七步） | 输入表（按份汇总） | 工具的**中转表**（`使用说明` 第四步） | 输入表（资金流水） |
| 借贷表达 | `FDC` 方向 + `FDEBIT`/`FCREDIT` | 单一 `金额` 语义列 + 借/税费/贷三组科目列 | `借方金额`/`贷方金额` 分列 | `借方金额`/`贷方金额` 分列（资金方向） |
| 税额承载 | 无税额列 → 拆成独立分录 | **有** `不含税金额`/`税额`/`价税合计` 三列 | 无税额列 → 拆成独立分录 | 无税额列 |
| 期间字段 | `FPERIOD`（会计期间，月） | 无（仅日期） | 无 | 无 |
| 分录序号 | `FENTRYID`（凭证内行序） | 无 | 无（行序即分录序） | 无 |
| 科目引用 | 1 个 `FACCTID` + 4 组（类别/代码/名称）辅助核算 | 3 组科目列（借/税费/贷）各带名称与辅助核算 | 1 个 `科目代码` + 名称 + 4 组（类别/代码/名称）辅助核算 | 资金/对方两组科目 + 各 1–2 个辅助核算名称 |
| 制单人 | `FPREPARE` | 无 | `制单人` | 无 |
| 附件数 | `FATTCHMENT` | `附件数` | `附件数` | `附件数` |
| 业务编号 | 无直接列（`FSERIALNO`/`FREFERENCE` 语义未确认） | `业务编号` | `业务编号` | `业务编号` |
| 是否需外部基础资料 | 需要（科目与辅助核算类别由工具侧装载） | 不需要 | 建议提供 `科目数据`（工具校验用） | 不需要 |
| 模板自带示例量 | 18 行分录 | 3 行 | 6 行分录 | 4 行 |
| 与报销闭环数据形态的距离 | 中：需自建期间/分录序号/方向列 | 中：贷方语义固定为应收/应付，与报销付款不符 | **低：与「一明细行 → 一分录」几乎同构** | 高：资金方向语义 + 无税额列 |
| 主要风险 | 44 列中含大量工具/冗余列；演示行方向列不自洽（R5）；期间字段口径需拍板 | 贷方科目语义与报销不符；无期间字段；无制单人/业务编号以外的主键线索 | 依赖科目/核算项目基础资料才能被工具完整消化 | 无法承载进项税拆分；与报销单头信息（制单人/附件数）错配 |

### 3.2 推荐：`录入数据`（一行 = 一条分录）——**待所有者确认**

理由（按权重）：

1. **与本系统数据形态同构**：`expense_lines`（明细行）天然是「一条分录」的粒度，
   借贷金额分列，不需要把三组科目折叠/展开（`发票记录` 需要按票折叠），也不需要
   自造期间与分录序号（`PZ` 需要）。
2. **最少隐含变换**：一行写一行，`借方金额`/`贷方金额` 显式分列 → 借贷平衡可在
   写出前用 `Decimal` 逐凭证校验；`PZ` 需要额外维护 `FDC` 与 `FENTRYID`（且模板
   演示行这两列不自洽，见 R5）。
3. **与工具链位置一致**：`使用说明` 第四步说明其他输入表都要先转成 `录入数据`，
   再由工具生成标准格式凭证文件；把本系统定位成「直接产出中转表」可绕过工具侧
   的输入表转换语义，减少一次口径翻译。
4. **可直接复用模板自带的科目校验资源**：`科目数据` / `核算项目` 两张基础资料表
   可一并装载（或由本系统导出），使工具侧科目与辅助核算可解析。

**备选与不推荐**：

- 备选 `PZ`：当目标系统直接吃「标准格式凭证 / DBF」而**不经**工具中转，或必须
  携带 `FPERIOD`/`FENTRYID`/4 组辅助核算时选它；代价是自建期间与方向列规则。
- 不推荐 `发票记录`：其贷方列语义是应收/应付（发票视角），而报销闭环的贷方是
  资金或往来（付款视角）；且它是「按份汇总」粒度，与明细行不一一对应。
- 不推荐 `日记账`：无税额列，无法承载进项税拆分；表头以资金科目为主语，与报销
  单的费用科目为主语相反。

**该推荐必须由所有者确认**（§9 决策 D1）：本系统**不能**从模板推断所有者实际
使用的目标财务系统与导入通道，而通道决定了目标布局。

### 3.3 本任务四候选之外、模板实际存在的其它布局

`简易日记账`（15 列，两分录）、`横向自定义`（键值转置）、`发票多行式`（表头在
第 3 行）、`发票多行式二`（20 列）、`先借后贷`（13 列 + 重复表头）。这五张表都是
**输入表**，其中 `横向自定义`/`发票多行式`/`先借后贷` 存在 R1/R2/R3 类结构差异，
故未纳入默认候选；若所有者指定其中之一，映射口径需按 §2.2 对应小节单独设计。

---

## 4 数据源字段清单（真实列，只读核对）

### 4.1 报销域 —— [`../app/expense/models.py`](../app/expense/models.py)

> **命名更正**：Issue #43 写作 `expense_claim_lines`，代码中的实际表名是
> **`expense_lines`**（`ExpenseLine.__tablename__`）。本文一律使用真实表名。

| 表（类） | 可用列（导出相关） |
|---|---|
| `expense_claims`（`ExpenseClaim`） | `id`、`claim_no`（全局唯一，格式 `EC-{nextval:08d}`，见 `app/expense/service.py` `_next_claim_no`/`CLAIM_NO_SEQUENCE`）、`employee_id`、`currency`（默认 `'CNY'`）、`total_amount`（`NUMERIC(18,2)`，= Σ 明细 `amount`）、`status`（`draft`/`submitted`/`withdrawn`/`approved`/`finance_reviewed`/`paid`/`rejected`）、`submitted_at` |
| `expense_lines`（`ExpenseLine`） | `claim_id`、`expense_date`（`Date`）、`category_id`、`description`（长度 `[1, 500]`，常量 `DESCRIPTION_MIN_LENGTH`/`DESCRIPTION_MAX_LENGTH`）、`amount`（**价税合计**，见 §4.4）、`tax_amount`（默认 0）、`cost_center_id`（可空） |
| `expense_categories`（`ExpenseCategory`） | `(company_id, code)` 唯一、`name`、`status` —— **`code` 是本系统自定义编码（测试中形如 `CAT-TRV`），不是会计科目代码** |
| `cost_centers`（`CostCenter`） | `(company_id, code)` 唯一、`name`、`status` |
| `reimbursement_payments`（`ReimbursementPayment`） | `claim_id`、`amount`（`> 0`）、`currency`、`payment_ref`（可空）、`status`（`pending`/`paid`/`failed`）、`paid_at`。状态机 `finance_reviewed --pay--> paid`（`paid` 为终态），故**一张报销单在实践中只有一条付款记录** |
| `expense_invoices`（`ExpenseInvoice`） | `claim_id`、`source_file_id`（可空）、`invoice_fields_json`（票面快照：`invoice_code`/`invoice_no`/`invoice_date`(ISO 字符串)/`invoice_kind`/`amount`(**价税合计**，字符串定点)；**不含税号**）、`verification_status`（七状态副本）、`duplicate_key`（票面身份单向 sha256 摘要，`(claim_id, duplicate_key)` 唯一）、`suspected_duplicate` |

### 4.2 查验域 —— [`../app/integrations/models.py`](../app/integrations/models.py)

| 表（类） | 可用列（导出相关） |
|---|---|
| `invoice_verifications`（`InvoiceVerification`） | `invoice_code`、`invoice_no`、`invoice_date`（`Date`）、`amount_without_tax`、`tax_amount`、`amount_with_tax`（三者均可空，`NUMERIC(18,2)`）、`currency`、`invoice_type`、`status`（七状态，`STATUS_VALUES`）、`duplicate_key`（唯一）、`duplicate_check_result`、`provider_type`/`provider_name`、`verified_at`、`verified_by`、`source_file_id`。**受权限保护（B 类敏感，禁止入日志/审计/提示词）**：`seller_tax_code`、`buyer_tax_code` |

七状态（`STATUS_VALUES`）：`pending_identification`、`pending_manual_confirmation`、
`pending_verification`、`verified_pass`、`verified_fail`、`unable_to_verify`、
`suspected_duplicate`。

### 4.3 组织、文件与审计

| 表（类） | 可用列 | 导出相关性与**缺口** |
|---|---|---|
| `employees`（[`../app/db/models/organization.py`](../app/db/models/organization.py)） | `company_id`、`department_id`（可空）、`manager_id`、`employee_no`、`status` | **无姓名字段**——`app/api/claims.py` `_render_context` 明确以 `employee_no` 作为员工标识；摘要/制单人只能取 `employee_no` |
| `departments` | `company_id`、`parent_id`、`name` | 可作为「辅助核算：部门」的**名称**来源；**无会计侧部门编码**（`dept.code` 不存在）→ 若目标要求部门代码，需所有者提供映射 |
| `companies` | `name`、`country_code`、`base_currency`、`status` | 币别口径来源（`base_currency`，默认 `CNY`） |
| `source_files`（[`../app/documents/models.py`](../app/documents/models.py)） | `owner_id`、`storage_key`、`sha256`、`mime_type`、`size_bytes`、`original_name`、`scan_status` | 附件计数来源（配合 `expense_invoices.source_file_id`） |
| `audit_events`（[`../app/audit/models.py`](../app/audit/models.py)） | `actor_id`、`action`、`resource_type`、`resource_id`、`request_id`、`details`、`created_at` | 导出行为审计的落点（动作名建议见 §7.4） |
| **缺口：销售方/往来单位名称** | — | `invoice_verifications` **不持久化** `seller_name`（仅有 `seller_tax_code`）；`expense_invoices.invoice_fields_json` 也**不含**销售方名称。OCR 结果里的 `seller_name` 只存在于 `extraction_drafts.extracted_json`（草稿、未确认）→ **本系统当前没有权威的往来单位名称来源**（§9 决策 D8） |
| **缺口：科目表 / 银行资金账户** | — | 本系统**没有**会计科目表、没有银行账户或资金科目模型 → 费用科目代码、贷方资金科目都必须由所有者以映射口径提供（§9 决策 D2 / D4） |

### 4.4 金额与日期口径（最关键的一处）

| 口径 | 本系统字段 | 依据 |
|---|---|---|
| 价税合计（报销金额） | `expense_lines.amount`；合计 `expense_claims.total_amount`（= Σ `amount`，由 `app/expense/money.py` `sum_amounts` 重算） | `app/integrations/baidu_invoice_ocr.py` `_VAT_ALIASES`：`amount` **只取** `AmountInFiguers`（价税合计），绝不回退不含税合计；[`research/W2-35-baidu-multiple-invoice-ocr.md`](research/W2-35-baidu-multiple-invoice-ocr.md)「金额口径」表 |
| 税额 | `expense_lines.tax_amount`（默认 0，`>= 0`） | `app/expense/models.py` `ExpenseLine.tax_amount` |
| 不含税 | **派生**：`amount - tax_amount` | 无独立列；必须校验 `tax_amount <= amount`（数据库 CHECK 只保证二者各自 `>= 0`，**不保证** `tax <= amount`） |
| 票面三口径（发票维度） | `invoice_verifications.amount_without_tax` / `tax_amount` / `amount_with_tax`（**可空**） | `app/integrations/models.py`；「无法查验」时渠道不返回票面，三列可空 → **不得臆造金额** |
| 挂账票面金额 | `expense_invoices.invoice_fields_json["amount"]`（字符串定点，价税合计） | `app/expense/invoices.py` `_invoice_fields_snapshot` |
| 小数位与舍入 | 一律 2 位、`ROUND_HALF_UP`，`float` 被拒绝 | `app/expense/money.py`（`MONEY_EXPONENT`、`quantize`、`to_decimal`、`MAX_AMOUNT`） |
| 日期 | `expense_lines.expense_date`（`Date`）、`reimbursement_payments.paid_at`（`timestamptz`）、`invoice_verifications.invoice_date`（`Date`） | 模板侧 `凭证日期` 的基准日取值属决策 D5 |
| 日期单元格格式 | 模板演示数据为 **Excel 序列号**（1900 日期系统；例如 `45301` ↔ `2024-01-10`，可由 `datetime.date(1899,12,30) + timedelta(days=n)` 验证） | 内嵌命令输出与模板示例行；实现应写**日期型单元格**（显示 `YYYY-MM-DD`）而非裸数字，并在模板版本登记该约定 |

---

## 5 逐列映射设计

映射列中的「处置」取值：**直出**（无需变换）、**派生**（由本系统计算）、
**映射**（需外部映射表/常量，见 §9 决策）、**留空**（不写入）、**常量**。

### 5.1 目标布局 `录入数据`（推荐默认，31 列）

一行 = 一条分录；一张报销单 = 一张凭证。分录构成：

```text
每张报销单（凭证）
├── 借方分录 × N   （每个 expense_lines 行一条：费用科目）
├── 借方分录 × 0..1（Σ tax_amount > 0 且决策 D3 要求拆税时：应交税费-进项税额）
└── 贷方分录 × 1   （贷方科目 + 应付/已付金额）
```

| 列 | 表头 | 处置 | 取值来源与规则 |
|---|---|---|---|
| A | 凭证字 | 常量 | 无对应列；由决策 D5 确定（如「记」），同一凭证同一值 |
| B | 凭证号 | 派生 | 期间内按（首个 `expense_lines.expense_date`、`expense_claims.id`）升序从 1 编号；同一 `claim_id` 全部分录同号 |
| C | 凭证日期 | 派生 | `expense_lines.expense_date`，或按决策 D5 取 `reimbursement_payments.paid_at` 的日期部分；写日期型单元格 |
| D | 凭证摘要 | 派生 | 决策 D6 的拼接模板，输入 `expense_claims.claim_no` + `employees.employee_no` + `expense_lines.description`（≤ 500 字符），按 §6.4 转义 |
| E | 科目代码 | 映射 | 借方=费用科目（`expense_categories.code` → 会计科目代码，决策 D2）；税费行=决策 D3；贷方行=决策 D4。**解析失败即 fail closed** |
| F | 科目名称 | 映射 | 借方=`expense_categories.name`；税费/贷方名称随映射表。非必填但建议填（模板演示行对费用行填写了名称） |
| G | 余额方向 | 留空 | 模板标注「自动补充」（工具列） |
| H | 币别代码 | 映射 | `expense_claims.currency`（默认 `CNY`）→ 模板币别代码（示例用 `RMB`）。口径归入决策 D1 |
| I | 币别名称 | 映射 | 同上（示例「人民币」）。非必填 |
| J | 原币金额 | 派生 | 本币场景 = 该分录金额（与 N/O 同口径） |
| K | 汇率 | 常量 | `1`（跨币种导出不在本期范围） |
| L | 数量 | 留空 | 无数据源（`expense_lines` 无数量列） |
| M | 单价 | 留空 | 同上 |
| N | 借方金额 | 派生 | 借方分录：不含税口径 `amount - tax_amount`（决策 D3=a）或含税口径 `amount`（决策 D3=b）；税费分录为 `tax_amount`；贷方分录留空 |
| O | 贷方金额 | 派生 | 贷方分录 = `reimbursement_payments.amount`（若无付款记录则按决策 D3/D4 的口径取 `expense_claims.total_amount`）；借方分录留空 |
| P | 制单人 | 决策 | 报销人 `employees.employee_no` 或导出人（决策 D12.5） |
| Q | 辅助核算类别A | 留空/映射 | 辅助核算类别字符串（模板 `核算分类.FClsName` 口径）→ 决策 D8 |
| R | 项目代码A | 留空/映射 | 部门代码：**当前无来源**（`departments` 无编码列）→ 决策 D8 |
| S | 核算项目名称A | 派生 | 部门名称 `departments.name`（经 `employees.department_id`）；无部门则留空 |
| T–V | 辅助核算类别/项目代码/名称 B | 留空 | 无第二组数据源（往来单位名称缺失，§4.3 缺口） |
| W–AB | 辅助核算槽 C、D | 留空 | 同上 |
| AC | 业务编号 | 派生 | `expense_claims.claim_no`（业务单号，与 B 列的工具凭证号不同） |
| AD | 附件数 | 派生 | 该报销单 `expense_invoices` 行数（或 `source_file_id` 非空计数，口径见决策 D12.1） |
| AE | 检测错误情况 | 留空 | 本系统在写出前自行校验（§6）；不依赖工具列回填 |

### 5.2 备选布局 `PZ`（44 列）

| 列 | 表头 | 处置 | 取值来源与规则 |
|---|---|---|---|
| A | FDATE | 派生 | 凭证日期（Excel 序列号或日期型；与 C 列同源） |
| B | FPERIOD | 派生 | 会计期间月 = 凭证日期所在月（1–12） |
| C | FGROUP | 常量 | 凭证字（决策 D5） |
| D | FNUM | 派生 | 凭证号（同 §5.1 B） |
| E | FENTRYID | 派生 | 凭证内分录序号，从 0 起；模板演示行为 `0`/`1` |
| F | FEXP | 派生 | 摘要（同 §5.1 D） |
| G | FACCTID | 映射 | 科目代码（同 §5.1 E） |
| H/I/J | FCLSNAME1/FOBJID1/FOBJNAME1 | 留空/映射 | 第 1 组辅助核算（类别/代码/名称）→ 决策 D8；代码需 `核算项目` 表口径 |
| K–S | FCLSNAME2–4 / FOBJID2–4 / FOBJNAME2–4 | 留空 | 无 2–4 组数据源 |
| T | FTRANSID | 留空 | 外币业务标识，语义未确认 |
| U | FCYID | 映射 | 币别代码（同上 H） |
| V | FEXCHRATE | 常量 | `1` |
| W | FDC | 派生 | `D`/`C`，**必须与本行 FDEBIT/FCREDIT 同源生成**（模板演示行不一致，见 R5） |
| X | FFCYAMT | 派生 | 原币金额 |
| Y/Z | FQTY/FPRICE | 留空 | 无数量单价数据源 |
| AA/AB | FDEBIT/FCREDIT | 派生 | 借方/贷方本币金额（不含税或含税口径取决 D4） |
| AC/AD | FSETTLCODE/FSETTLENO | 留空/可选 | 结算方式无数据源；结算号可考虑 `reimbursement_payments.payment_ref`（决策 D12.2） |
| AE | FPREPARE | 决策 | 制单人（同 §5.1 P） |
| AF–AI | FPAY/FCASH/FPOSTER/FCHECKER | 留空 | 出纳/现金/过账/审核字，无数据源 |
| AJ | FATTCHMENT | 派生 | 附件数（同 §5.1 AD） |
| AK | FPOSTED | 常量 | `0`（未过账；本系统不产生过账状态） |
| AL | FMODULE | 留空 | 模块标记，语义未确认 |
| AM | FDELETED | 常量 | `0` |
| AN–AR | FSERIALNO/FTRANSDATE/FCASHFLOW/FUNITNAME/FREFERENCE | 留空 | 语义未确认；`FREFERENCE` 可考虑承载 `claim_no`（决策 D12.2） |

### 5.3 对照布局 `发票记录`（32 列）

一行 = 一条「明细行」映射（本系统按 `expense_lines` 逐行填写，但注意该表语义为
「按份发票汇总」，逐行填写属**重载**用法，需所有者认可）。

| 列 | 表头 | 处置 | 取值来源与规则 |
|---|---|---|---|
| A | 凭证字 (非必录) | 常量 | 决策 D5 |
| B | 凭证号 | 派生 | 同 §5.1 B |
| C | 凭证日期 | 派生 | 同 §5.1 C |
| D | 摘 要 | 派生 | 同 §5.1 D |
| E | 数量跟随位置 | 留空 | 数量不导出 |
| F/G/H | 数量单位/数量/单价 | 留空 | 无数据源 |
| I | 不含税金额 | 派生 | `amount - tax_amount` |
| J | 税额 | 直出 | `expense_lines.tax_amount`（**0 时不产生税费分录**） |
| K | 价税合计 | 直出 | `expense_lines.amount` |
| L | 借方 科目代码A | 映射 | 费用科目（决策 D2） |
| M | 借方 科目名称A | 映射 | `expense_categories.name` |
| N/O | 辅助核算名称A1/A2 | 派生/留空 | 部门名称（决策 D8）/无 |
| P | 税费 科目代码B | 映射 | 进项税科目（决策 D3）；`J=0` 时留空 |
| Q | 税费科 目名称B | 映射 | 同上 |
| R | 贷方 科目代码C | 映射 | 贷方科目（决策 D4）。**该布局的贷方语义为应收/应付，与报销付款不完全对应** |
| S | 贷方 科目名称C | 映射 | 同上 |
| T/U | 辅助核算名称C1/C2 | 留空 | 无数据源 |
| V–Z | 外币跟随位置/币别代码/币别名称/外币原值/汇率 | 留空 | 本币导出 |
| AA | 附件数 | 派生 | 同 §5.1 AD |
| AB | 业务编号位置 | 留空 | 工具控制列 |
| AC | 业务编号 | 派生 | `claim_no` |
| AD | 数据检测情况 | 留空 | 工具列 |
| AE/AF | 模板号/模板名称（无需手填） | 留空 | 工具列 |

### 5.4 对照布局 `日记账`（29 列）

一行 = 一条「资金 ↔ 对方」配对（→ 2 分录）。本系统没有资金科目与资金单据，
故只在「按报销明细逐行 + 贷方固定为资金科目」的简化用法下可映射。

| 列 | 表头 | 处置 | 取值来源与规则 |
|---|---|---|---|
| A | 凭证字 (非必录) | 常量 | 决策 D5 |
| B | 序号 | 派生 | 记录序号（1 起） |
| C | 凭证日期 交易日期 | 派生 | 同 §5.1 C |
| D | 摘要 | 派生 | 同 §5.1 D |
| E | 借方金额 | 派生 | 与 F 互斥（本系统的费用方在借方） |
| F | 贷方金额 | 派生 | 付款金额（`reimbursement_payments.amount`） |
| G | 数量跟随位置 | 留空 | — |
| H/I/J | 数量/数量单位/单价 | 留空 | 无数据源 |
| K | 资金科目代码 | 映射 | 贷方资金科目（决策 D4） |
| L | 资金科目名称A | 映射 | 同上 |
| M/N | 辅助核算名称A1/A2 | 留空 | 资金侧无辅助核算数据源 |
| O | 对方科目代码 | 映射 | 费用科目（决策 D2）/税费科目（决策 D3） |
| P | 对方科目名称B | 映射 | 同上名称 |
| Q/R | 辅助核算名称B1/B2 | 派生/留空 | 部门名称/无 |
| S–W | 外币跟随位置/币别代码/币别名称/外币原值/汇率 | 留空 | 本币导出 |
| X | 附件数 | 派生 | 同 §5.1 AD |
| Y | 业务编号位置 | 留空 | 工具控制列 |
| Z | 业务编号 | 派生 | `claim_no` |
| AA–AC | 数据检测情况/匹配模板编号/匹配模板名称 | 留空 | 工具列 |

**结论**：`日记账` **无法承载进项税拆分**（无税额列），且其行粒度是「一笔资金
收付」。若所有者要求拆税，则本布局不适用。

### 5.5 四种布局共用的派生规则（必须集中实现，不得各表各写一份）

| 规则 | 定义 |
|---|---|
| 凭证粒度 | 一张报销单 = 一张凭证（`claim_id` 决定凭证边界） |
| 凭证号 | 期间内按（首个 `expense_date`、`claim_id`）升序从 1 编号；导出集合确定后编号可复现 |
| 期间 | 由导出请求给定（`period`，形如 `YYYY-MM`）；`PZ.FPERIOD` 取其月号 |
| 分录顺序 | 借方费用分录（按 `expense_lines.id`）→ 税费分录（若有）→ 贷方分录。顺序必须固定，否则字节不可复现 |
| 借贷平衡 | 同一凭证 Σ 借方 = Σ 贷方（`Decimal` 到分） |
| 金额口径 | 价税合计 / 税额 / 不含税 的取值与拆分见 §4.4、决策 D3 |
| 日期 | 写日期型单元格；序列号口径在模板版本中登记（§4.4） |
| 摘要 | 决策 D6 的模板拼接；长度上限 500（`DESCRIPTION_MAX_LENGTH`）；按 §6.4 转义 |
| 科目解析 | 任何科目代码解析失败 → fail closed（不产文件） |
| 辅助核算 | 仅「部门名称」有权威来源；其余留空（决策 D8） |
| 工具列 | 一律留空，由工具回填或不回填（`数据检测情况`/`余额方向`/`模板号` 等） |

### 5.6 目标布局 `voucher_template`（新模板「凭证模版」，25 列，Issue #71）

> 所有者 2026-10-19 决定改用新模板（§9「D1 更新」）；本布局为示例默认，旧布局
> §5.1 保留为历史/回退。**按列号定位**（§2.3 R3/R4）：列序由
> [`export-templates/voucher-template-structure.json`](export-templates/voucher-template-structure.json)
> 的 `凭证模版` 列序固定。

行语义与 §5.1 相同：一行 = 一条分录；一张报销单 = 一张凭证。分录构成、顺序、金额
口径、科目解析、借贷平衡、摘要规则**全部沿用 §5.5**（集中实现，不另写一份）。

| 列 | 表头 | 处置 | 取值来源与规则 |
|---|---|---|---|
| A | 日期 | 派生 | 基准日口径与旧布局一致：`expense_date` 取该明细行费用发生日期；`paid_at` 取付款日期（`scope.date_basis`）。写日期型单元格 |
| B | 凭证字 | 常量 | `[voucher].voucher_word`（如「记」） |
| C | 凭证号 | 派生/留空 | `[voucher].number_mode`：`sequential`（默认）= 期间内按（基准日、`claim_id`）升序从 `number_start`（默认 1）连续编号，同一报销单全部分录同号；`blank` = 留空（由会计系统导入时分配）。**注意**：跨期续号等真实行为由会计系统决定，导入时按实调整 |
| D | 附件数 | 派生 | 既有口径 `[invoice_policy].attachment_count_basis`（`invoices` / `files`），与旧布局 `AD` 列同源 |
| E | 分录序号 | 派生 | 该分录在**本凭证内**的 1 基序号（借方费用按 `line_id` → 税费 → 贷方）。**实现口径**：新模板无示例数据行可依据，取 1 起 |
| F | 摘要 | 派生 | `[summary].template` 渲染（占位符白名单见 §9.1），长度上限 `max_length`；按 §6.4 转义 |
| G | 科目代码 | 映射 | 借方=费用科目（`[accounts]`，D2）；税费行=`[tax].account_code`（D3）；贷方行=`[credit]`（D4）。**解析失败即 fail closed** |
| H | 科目名称 | 映射 | 随 G 的映射表；未命中回退 `expense_categories.name` |
| I | 借方金额 | 派生 | 借方分录 = 不含税（拆税）或含税（不拆）口径；税费分录 = `tax_amount`；贷方分录留空 |
| J | 贷方金额 | 派生 | 贷方分录 = 付款金额（无付款记录时取报销单合计）；借方分录留空 |
| K | 客户 | 配置/留空 | `[auxiliary.named_columns].客户`：`blank`（安全默认）/ `department` / `literal` |
| L | 供应商 | 配置/留空 | 同上。**本系统无权威来源**（§4.3 缺口）→ 示例 `blank`，可按需 `literal` |
| M | 职员 | 配置/留空 | 同上（无权威来源） |
| N | 项目 | 配置/留空 | 同上（无权威来源） |
| O | 部门 | 配置/派生 | `[auxiliary.named_columns].部门 = { source = "department" }` → `departments.name`（经 `employees.department_id`）；税费/贷方分录无部门 → 留空 |
| P | 存货 | 配置/留空 | 同上（无权威来源） |
| Q | 自定义辅助核算类别 | 配置/留空 | 同上（无权威来源） |
| R | 自定义辅助核算编码 | 配置/留空 | 同上 |
| S | 自定义辅助核算类别1 | 配置/留空 | 同上 |
| T | 自定义辅助核算编码1 | 配置/留空 | 同上 |
| U | 数量 | 留空 | **安全默认**：`expense_lines` 无数量列 |
| V | 单价 | 留空 | **安全默认**：同上 |
| W | 原币金额 | 派生 | 本币场景 = 该分录金额（与 I/J 同口径） |
| X | 币别 | 映射 | `[currency].codes`（本系统币别 → 模板币别代码）；未登记 → `currency_unmapped` fail closed |
| Y | 汇率 | 常量 | `[currency].rate`（本币导出固定为 1） |

**未使用**：旧布局的「制单人 / 业务编号 / 余额方向 / 币别名称 / 辅助核算槽 A–D」在
本布局**没有对应列**（25 列不含制单人列）；`[voucher].preparer` 与
`[auxiliary].mode` 对本布局不生效。

**命名辅助核算列的取值口径**（`[auxiliary.named_columns]`，键 = 表头名）：
`blank`（留空）/ `department`（取分录所属部门名称）/ `literal`（固定文本，需给
`value`）。未登记的列一律留空；非法取值在配置装载期 fail closed。

### 5.7 两布局对照（示例默认与回退）

| 维度 | `entry_rows`（§5.1，旧模板 `录入数据`） | `voucher_template`（§5.6，新模板 `凭证模版`） |
|---|---|---|
| 列数 / 定位 | 31（A–AE） | 25（A–Y） |
| 状态 | 历史 / 回退 | **示例默认**（Issue #71） |
| 制单人 | 有（`preparer` 配置） | **无此列** |
| 业务编号 | 有（`claim_no`） | **无此列** |
| 辅助核算 | 类别/代码/名称 A–D 槽（`[auxiliary].mode`） | 命名列（`[auxiliary].named_columns`） |
| 凭证号 | `[voucher].number_mode` 同上 | 同上 |
| 校验 / 注入防护 / manifest / 存储 / 审计 | 全部复用 | 全部复用（同一实现） |

---

## 6 校验规则设计

### 6.1 恒等式（逐凭证 + 全量）

| # | 规则 | 不通过 |
|---|---|---|
| E1 | 逐凭证 Σ 借方金额 = Σ 贷方金额（`Decimal`，2 位） | fail closed |
| E2 | 全量 Σ 借方 = 全量 Σ 贷方 | fail closed |
| E3 | 逐明细行：不含税 + 税额 = 价税合计，即 `(amount - tax_amount) + tax_amount == amount`；且 `tax_amount <= amount`（**数据库 CHECK 不保证此式**） | fail closed |
| E4 | 逐报销单：`expense_claims.total_amount == Σ expense_lines.amount` | fail closed（不在导出时自愈） |
| E5 | 有付款记录时：`reimbursement_payments.amount == expense_claims.total_amount`（状态机只允许一次 `pay`，但服务层**未断言**金额相等） | fail closed（防差额被静默吞掉） |
| E6 | 发票维度：若 `invoice_verifications.amount_without_tax` / `amount_with_tax` 均非空，则 `amount_without_tax + tax_amount == amount_with_tax` | fail closed（票面三口径不自洽） |
| E7 | 挂账票面金额与明细金额的对照：`expense_invoices.invoice_fields_json["amount"]` 之和与所属明细行的价税合计 —— **当前无明细↔发票的一对一外键**（挂账只到报销单粒度） | 只能做报销单级对照；不对应即 fail closed（决策 D12.3 是否要求引入） |

### 6.2 对账（行数与金额）

| # | 规则 |
|---|---|
| C1 | 行数：`导出分录行数 == Σ(每张凭证的借方分录数 + 税费分录数 + 1 条贷方分录)`，逐凭证与全量双重核对 |
| C2 | 笔数：`凭证数 == 期间内被选中报销单数`；`明细行数 == 期间内被选中 expense_lines 行数` |
| C3 | 金额：`Σ 价税合计 == expense_claims.total_amount` 之和；`Σ 借方本币金额 == Σ 价税合计`（含税口径）或 `== Σ 不含税`（拆税口径） |
| C4 | 期间包含性：所有被选中行的 `expense_date`（或按 D5 的基准日）落在请求期间内；**跨期数据不得进入导出** |
| C5 | 集合一致性：manifest 的 `claim_ids` 与文件内出现的业务编号集合**完全一致**（无遗漏、无多余） |

### 6.3 fail closed 条件清单（任一命中即**不产出文件**）

1. 借贷不平衡（E1/E2）。
2. 价税合计 ≠ 不含税 + 税额（E3），或 `tax_amount > amount`。
3. 报销单合计与明细合计不符（E4），或付款金额与合计不符（E5）。
4. 票面三口径不自洽（E6）。
5. 行数/笔数/金额任一对账失败（C1–C3）。
6. 期间包含性失败（C4），包含「请求期间内无任何合格记录」。
7. 任一科目代码（费用/税费/贷方）**无法解析**（映射表缺失或为空）。
8. 任一挂账发票的 `verification_status == 'verified_fail'`；或挂账行存在
   `suspected_duplicate=True` 且所有者未给处置口径（决策 D12.4）。
9. 存在 `amount - tax_amount < 0`、`amount <= 0`、超 `MAX_AMOUNT`、非 2 位小数等
   越界值。
10. 模板缺失、模板 SHA-256 与登记的 `template_version` 不符，或目标工作表不存在
    → **禁止静默降级为默认格式**（上游 §7.6）。
11. 导出人无导出权限（§7.4；权限口径见决策 D10）。
12. 摘要/文本单元格含未转义的危险前缀（§6.4）。

失败行为：返回明确错误码 + 命中规则清单，**不写任何文件、不写 manifest**，
并写一条拒绝审计（沿用 `app/expense/service.py` 的 `_record_deny` 模式与
`app/api/errors.py` 的错误映射风格）。

### 6.4 单元格注入与格式

- 文本值以 `=`、`+`、`-`、`@` 开头时必须转义（前缀单引号或前置空格），
  否则会变成公式或外部引用（上游 §7.4）。**摘要、科目名称、核算项目名称**都要过这道闸。
- 只产 `.xlsx`，不产 CSV，不使用宏（上游 §7.4）。
- UTF-8；金额写数值单元格（2 位小数）、日期写日期单元格；整数型列（`序号`/`附件数`/
  `FENTRYID` 等）写整数，避免被工具按文本拒绝。
- 单文件上限沿用 `app/documents/storage.py` 的 `DEFAULT_MAX_BYTES`（20 MiB）。

### 6.5 幂等与可复现

- 同期间 + 同模板版本重复导出：manifest 除 `export_id`/`exported_at` 外**逐字段一致**；
- **仅当模板与内容不含时间戳时承诺文件字节一致**（上游 §7.3）；因此导出文件中
  **不得**写入导出时间戳（若确需，写入 manifest 而非文件）；
- 分录排序固定（§5.5）是字节可复现的前提。

---

## 7 manifest 设计草案

### 7.1 字段（JSON 结构，值均为占位/示意）

```json
{
  "manifest_version": 1,
  "export_id": "<uuid4>",
  "scope": {
    "company_id": 0,
    "period": "YYYY-MM",
    "date_basis": "paid_at | expense_date",
    "claim_ids": [0],
    "claim_nos": ["EC-00000000"],
    "invoice_duplicate_keys": ["<sha256-hex>"]
  },
  "template": {
    "template_id": "<id>",
    "template_version": 1,
    "sha256": "<64-hex>",
    "worksheet": "录入数据",
    "layout": "entry_rows"
  },
  "rows": {
    "voucher_count": 0,
    "entry_count": 0,
    "expense_line_count": 0,
    "invoice_count": 0
  },
  "amounts": {
    "gross_total": "0.00",
    "net_total": "0.00",
    "tax_total": "0.00",
    "debit_total": "0.00",
    "credit_total": "0.00"
  },
  "file": {
    "name": "expense-export-<period>.xlsx",
    "sha256": "<64-hex>",
    "size_bytes": 0,
    "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "storage_key": "<aa>/<bb>/<64-hex>"
  },
  "checks": ["debit_credit_balance", "gross_equals_net_plus_tax", "row_count",
             "amount_total", "period_containment", "claim_total_matches",
             "payment_matches_total", "account_codes_resolved",
             "invoice_status_allowed", "template_fingerprint"],
  "exported_by": {"employee_id": 0, "employee_no": "E0000"},
  "exported_at": "<ISO-8601 with timezone>"
}
```

设计约束：

- **期间、模板版本与 SHA-256、记录 ID 集合、行数、金额合计、文件哈希、导出人、
  时间**八项覆盖上游 §7.3 的 manifest 要求；`checks` 记录本条 manifest 对应的
  校验项（全绿才落 manifest，因此它是「校验通过清单」而非错误清单）。
- **不含敏感值**：不写税号、不写发票号/代码明文（票面身份只用 `duplicate_key`
  单向摘要，与 `expense_invoices` / `invoice_verifications` 同口径）、不写员工姓名
  （员工表本无姓名列，用 `employee_no`）、不写摘要文本。
- **金额一律字符串定点**（`Decimal` 十进制字符串），与
  `app/expense/invoices.py` 的快照序列化方式一致，禁止浮点。

### 7.2 与 [`../app/documents/storage.py`](../app/documents/storage.py) 的复用点

| 上游 §7.2 要求 | 复用点（现有符号） | 复用方式 |
|---|---|---|
| 存储 | `store_file(root, owner_id=..., content=..., declared_name=...)` → `StoredFile(storage_key, sha256, size_bytes, mime_type, deduped)` | 导出文件落在同一内容寻址存储根（`AJAIMGR_STORAGE_ROOT`，默认 `var/storage`）；`storage_key = sha[:2]/sha[2:4]/sha` 直接写入 manifest、同时落一行 `source_files` 元数据（与附件同一约定）。`owner_id` 用导出人 |
| 大小限制 | `DEFAULT_MAX_BYTES`（20 MiB） | 现有 20 MiB 上限对导出文件同样适用（超出则 fail closed） |
| 类型校验 | `sniff_mime(content, declared_name)` → XLSX 走 ZIP 魔数 + 声明扩展名（`XLSX_MIME`） | 写出后自检一次：`store_file` 已在落盘前做魔数校验，`.xlsx` 声明与 ZIP 内容不匹配会被拒 |
| 哈希校验 | `store_file` 返回值里的 `sha256`；读取时 `verify_file(root, storage_key, expected_sha256)` | manifest 的 `file.sha256` 即该值；下载前后可用 `verify_file` 复核未被篡改 |
| 路径安全 | `resolve_within(base, *parts)` | 通过 `read_file(root, storage_key)` 间接复用；导出实现不得自行拼接路径 |
| 下载鉴权 | `app/api/claims.py` 导出端点的既有模式：**先授权、后渲染**（`_authorize_read` + `_unit_of_work`） | 财务导出端点应保持同一顺序；授权判定复用 `app/identity/authorization.py` + `app/identity/permissions.py` |
| 审计 | `audit_events`（只增不改，DB 触发器强制），模式见 `app/expense/service.py` `_record_success`/`_record_deny`；上游 Track B 已规划动作名 `finance.export.period` | 导出成功 → `finance.export.period`（`details` 只放非敏感聚合值：`period`、`template_version`、`entry_count`、金额合计、`file_sha256`）；拒绝 → 对应 `*.deny` |
| 保留期 | **无实现**（仓库当前不存在保留期常量、清理任务或过期字段；`source_files` 也没有 `expires_at`） | **缺口**：上游 §7.2 要求「复用保留期机制」，但该机制尚未存在 → 需在实现任务或决策中明确（§9 D11） |

### 7.3 manifest 与报告的呈现

- manifest 随导出文件一并落存储（或其 JSON 作为 `source_files` 之外的一条元数据行），
  下载时一并提供，便于对账复核；
- 审计里只放 manifest 的**摘要字段**（`export_id`、`file.sha256`、行数与金额合计），
  不放 `claim_ids` 全量列表（避免审计日志被业务 ID 淹没）；
- manifest 本身**不含**导出文件的字节，只含其哈希与指针。

---

## 8 结构提取命令（可重跑）与真实输出

### 8.1 运行方式

模板文件由所有者本地提供（`<template-file>`，**不入库**）。在仓库根执行：

> **Issue #71（2026-10-19）**：同一条命令也用于**新模板**《凭证导入模板-20261019》
> 的结构提取（产出 `export-templates/voucher-template-structure.json`），**无需**
> 修改 `HEADER_ROW_OVERRIDES`（新模板三张表首行即表头行）。见 §2.4。

```bash
python3 - "<template-file>" <<'PY'
#!/usr/bin/env python3
"""Extract the *structure* of an .xlsx voucher-import template (stdlib only).

Usage (embedded in docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md Sec.8):
    python3 - "<template-file>" [--json <out.json>] <<'PY' ... PY

The script uses ``zipfile`` + ``xml.etree`` only (no third-party dependency)
and prints, for every worksheet in workbook order:

* sheet order and name, sheet extent (``dimension``), the number of rows that
  contain at least one non-empty cell, and the number of non-empty cells in
  the first three rows (evidence for the header-row choice);
* the header row and the data start row;
* the header text of every column, with ``_x000D_`` / CR / LF / TAB collapsed
  to a single space, plus an ``optional_hint`` flag taken from the header text
  itself (``非必录`` / ``非必填`` / ``非必须`` / ``无需手填``) -- a textual hint,
  not an authoritative rule.

Facts only: no data row is emitted, no sheet is classified, and the JSON
output contains structure exclusively.

Header-row policy (an explicit, reviewable assumption, printed with every
sheet): the default header row is 1; three sheets whose first row is not a
column-header row are overridden in ``HEADER_ROW_OVERRIDES`` below.  The
printed non-empty-cell counts of the first three rows let a reviewer confirm
each override against the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICEREL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: Sheets whose first row is not a column-header row.
#: ``None`` = no header row (instruction page / transposed key-value layout).
HEADER_ROW_OVERRIDES: dict[str, int | None] = {
    "使用说明": None,
    "横向自定义": None,
    "发票多行式": 3,
}

OPTIONAL_MARKERS = ("非必录", "非必填", "非必须", "无需手填")


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def column_letters(cell_ref: str) -> str:
    letters = ""
    for char in cell_ref:
        if char.isalpha():
            letters += char
        else:
            break
    return letters


def normalize(text: str) -> str:
    """Collapse CR/LF/TAB and the ``_x000D_`` escape into single spaces."""
    text = text.replace("_x000D_", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [
        "".join(node.text or "" for node in item.iter(M + "t"))
        for item in root.findall(M + "si")
    ]


def sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target_of = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall(PKGREL + "Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.find(M + "sheets").findall(M + "sheet"):
        name = sheet.get("name") or ""
        target = (target_of.get(sheet.get(OFFICEREL + "id")) or "").lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append((name, target))
    return sheets


def cell_text(cell: ET.Element, strings: list[str]) -> str | None:
    kind = cell.get("t")
    value = cell.find(M + "v")
    if kind == "s" and value is not None:
        return strings[int(value.text)]
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(M + "t"))
    if value is not None:
        return value.text
    return None


def read_sheet(archive: zipfile.ZipFile, target: str, strings: list[str]) -> dict:
    root = ET.fromstring(archive.read(target))
    dimension = root.find(M + "dimension")
    rows: dict[int, dict[str, tuple[str, str]]] = {}
    formula_cells = 0
    for row in root.iter(M + "row"):
        cells: dict[str, tuple[str, str]] = {}
        for cell in row.findall(M + "c"):
            reference = cell.get("r") or ""
            if cell.find(M + "f") is not None:
                formula_cells += 1
            text = cell_text(cell, strings)
            if text is None or str(text).strip() == "":
                continue
            cells[column_letters(reference)] = (str(text), cell.get("t") or "n")
        if cells:
            rows[int(row.get("r") or 0)] = cells
    merged = root.find(M + "mergeCells")
    validations = root.find(M + "dataValidations")
    return {
        "extent": dimension.get("ref") if dimension is not None else None,
        "rows": rows,
        "formula_cells": formula_cells,
        "merged_cells": [
            item.get("ref") for item in merged.findall(M + "mergeCell") if merged is not None
        ]
        if merged is not None
        else [],
        "data_validations": [
            {"sqref": item.get("sqref"), "type": item.get("type")}
            for item in (validations.findall(M + "dataValidation") if validations is not None else [])
        ],
    }


def defined_names(archive: zipfile.ZipFile) -> list[dict]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    container = workbook.find(M + "definedNames")
    if container is None:
        return []
    return [
        {
            "name": node.get("name") or "",
            "local_sheet_id": node.get("localSheetId"),
            "refers_to": (node.text or "").strip(),
        }
        for node in container.findall(M + "definedName")
    ]


def structure_of(path: Path) -> dict:
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        names = defined_names(archive)
        sheets = []
        for order, (name, target) in enumerate(sheet_targets(archive), start=1):
            sheet = read_sheet(archive, target, strings)
            rows = sheet["rows"]
            row_numbers = sorted(rows)
            header_row = HEADER_ROW_OVERRIDES.get(name, 1)
            columns: list[dict] = []
            if header_row is not None:
                for ref, (text, _kind) in rows.get(header_row, {}).items():
                    header = normalize(text)
                    columns.append(
                        {
                            "column": ref,
                            "header": header,
                            "optional_hint": any(
                                marker in header for marker in OPTIONAL_MARKERS
                            ),
                        }
                    )
                columns.sort(key=lambda item: column_index(item["column"]))
            counters = []
            for number in (1, 2, 3):
                cells = rows.get(number, {})
                counters.append(
                    {
                        "row": number,
                        "non_empty_cells": len(cells),
                        "numeric_cells": sum(1 for _v, kind in cells.values() if kind == "n"),
                    }
                )
            # Example-row *shape* only: which columns the first data row fills.
            # No cell value is ever emitted.
            first_data: dict | None = None
            if header_row is not None:
                data_row = header_row + 1
                populated = sorted(rows.get(data_row, {}), key=column_index)
                first_data = {"row": data_row, "populated_columns": populated}
            sheets.append(
                {
                    "order": order,
                    "name": name,
                    "extent": sheet["extent"],
                    "non_empty_rows": len(row_numbers),
                    "first_row": row_numbers[0] if row_numbers else None,
                    "last_row": row_numbers[-1] if row_numbers else None,
                    "first_three_rows": counters,
                    "header_row": header_row,
                    "header_row_is_override": name in HEADER_ROW_OVERRIDES,
                    "data_start_row": None if header_row is None else header_row + 1,
                    "first_data_row": first_data,
                    "column_count": len(columns),
                    "formula_cells": sheet["formula_cells"],
                    "merged_cells": sheet["merged_cells"],
                    "data_validations": sheet["data_validations"],
                    "columns": columns,
                }
            )
    return {
        "template": {
            "path_placeholder": "<template-file>",
            "sha256": sha256(data).hexdigest(),
            "size_bytes": len(data),
            "worksheet_count": len(sheets),
            "shared_strings": len(strings),
            "defined_names": names,
        },
        "extraction": {
            "tool": "python3 (stdlib zipfile + xml.etree)",
            "script": "embedded heredoc, docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md Sec.8",
            "header_row_policy": (
                "default 1; explicit overrides for non-tabular first rows"
            ),
            "header_row_overrides": HEADER_ROW_OVERRIDES,
            "header_text_normalization": "_x000D_ / CR / LF / TAB -> single space",
            "optional_hint": "textual hint from the header text; not authoritative",
        },
        "worksheets": sheets,
    }


def print_report(result: dict) -> None:
    template = result["template"]
    print(f"file: {template['path_placeholder']}")
    print(f"sha256: {template['sha256']}")
    print(f"size_bytes: {template['size_bytes']}")
    print(f"worksheets: {template['worksheet_count']}")
    for name in template["defined_names"]:
        print(
            f"defined_name: {name['name']} local_sheet_id={name['local_sheet_id']} "
            f"refers_to={name['refers_to']}"
        )
    for sheet in result["worksheets"]:
        override = " [override]" if sheet["header_row_is_override"] else ""
        print()
        print(
            f"[{sheet['order']}] {sheet['name']}  extent={sheet['extent']}  "
            f"non_empty_rows={sheet['non_empty_rows']} "
            f"(rows {sheet['first_row']}..{sheet['last_row']})"
        )
        counted = " | ".join(
            f"r{row['row']}:{row['non_empty_cells']}cells/{row['numeric_cells']}num"
            for row in sheet["first_three_rows"]
        )
        print(f"    first-three-rows: {counted}")
        print(
            f"    header_row={sheet['header_row']}{override}  "
            f"data_start_row={sheet['data_start_row']}  "
            f"columns={sheet['column_count']}"
        )
        if sheet["first_data_row"] is not None:
            filled = " ".join(sheet["first_data_row"]["populated_columns"])
            print(
                f"    first_data_row={sheet['first_data_row']['row']} "
                f"populated_columns: {filled}"
            )
        print(
            f"    formula_cells={sheet['formula_cells']}  "
            f"merged_cells={len(sheet['merged_cells'])}  "
            f"data_validations={len(sheet['data_validations'])}"
        )
        for column in sheet["columns"]:
            hint = " [optional*]" if column["optional_hint"] else ""
            print(f"      {column['column']}: {column['header']}{hint}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    result = structure_of(args.template)
    print_report(result)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\njson written: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
```

生成纯结构 sidecar（本文档同目录的
[`export-templates/template-structure.json`](export-templates/template-structure.json)
即由该命令产出）：

```bash
python3 - "<template-file>" --json docs/export-templates/template-structure.json <<'PY'
#!/usr/bin/env python3
"""Extract the *structure* of an .xlsx voucher-import template (stdlib only).

Usage (embedded in docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md Sec.8):
    python3 - "<template-file>" [--json <out.json>] <<'PY' ... PY

The script uses ``zipfile`` + ``xml.etree`` only (no third-party dependency)
and prints, for every worksheet in workbook order:

* sheet order and name, sheet extent (``dimension``), the number of rows that
  contain at least one non-empty cell, and the number of non-empty cells in
  the first three rows (evidence for the header-row choice);
* the header row and the data start row;
* the header text of every column, with ``_x000D_`` / CR / LF / TAB collapsed
  to a single space, plus an ``optional_hint`` flag taken from the header text
  itself (``非必录`` / ``非必填`` / ``非必须`` / ``无需手填``) -- a textual hint,
  not an authoritative rule.

Facts only: no data row is emitted, no sheet is classified, and the JSON
output contains structure exclusively.

Header-row policy (an explicit, reviewable assumption, printed with every
sheet): the default header row is 1; three sheets whose first row is not a
column-header row are overridden in ``HEADER_ROW_OVERRIDES`` below.  The
printed non-empty-cell counts of the first three rows let a reviewer confirm
each override against the file.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
PKGREL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
OFFICEREL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

#: Sheets whose first row is not a column-header row.
#: ``None`` = no header row (instruction page / transposed key-value layout).
HEADER_ROW_OVERRIDES: dict[str, int | None] = {
    "使用说明": None,
    "横向自定义": None,
    "发票多行式": 3,
}

OPTIONAL_MARKERS = ("非必录", "非必填", "非必须", "无需手填")


def column_index(letters: str) -> int:
    index = 0
    for char in letters:
        index = index * 26 + (ord(char.upper()) - 64)
    return index


def column_letters(cell_ref: str) -> str:
    letters = ""
    for char in cell_ref:
        if char.isalpha():
            letters += char
        else:
            break
    return letters


def normalize(text: str) -> str:
    """Collapse CR/LF/TAB and the ``_x000D_`` escape into single spaces."""
    text = text.replace("_x000D_", " ")
    text = re.sub(r"[\r\n\t]+", " ", text)
    return re.sub(r" {2,}", " ", text).strip()


def shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(raw)
    return [
        "".join(node.text or "" for node in item.iter(M + "t"))
        for item in root.findall(M + "si")
    ]


def sheet_targets(archive: zipfile.ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target_of = {
        rel.get("Id"): rel.get("Target")
        for rel in rels.findall(PKGREL + "Relationship")
    }
    sheets: list[tuple[str, str]] = []
    for sheet in workbook.find(M + "sheets").findall(M + "sheet"):
        name = sheet.get("name") or ""
        target = (target_of.get(sheet.get(OFFICEREL + "id")) or "").lstrip("/")
        if not target.startswith("xl/"):
            target = "xl/" + target
        sheets.append((name, target))
    return sheets


def cell_text(cell: ET.Element, strings: list[str]) -> str | None:
    kind = cell.get("t")
    value = cell.find(M + "v")
    if kind == "s" and value is not None:
        return strings[int(value.text)]
    if kind == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(M + "t"))
    if value is not None:
        return value.text
    return None


def read_sheet(archive: zipfile.ZipFile, target: str, strings: list[str]) -> dict:
    root = ET.fromstring(archive.read(target))
    dimension = root.find(M + "dimension")
    rows: dict[int, dict[str, tuple[str, str]]] = {}
    formula_cells = 0
    for row in root.iter(M + "row"):
        cells: dict[str, tuple[str, str]] = {}
        for cell in row.findall(M + "c"):
            reference = cell.get("r") or ""
            if cell.find(M + "f") is not None:
                formula_cells += 1
            text = cell_text(cell, strings)
            if text is None or str(text).strip() == "":
                continue
            cells[column_letters(reference)] = (str(text), cell.get("t") or "n")
        if cells:
            rows[int(row.get("r") or 0)] = cells
    merged = root.find(M + "mergeCells")
    validations = root.find(M + "dataValidations")
    return {
        "extent": dimension.get("ref") if dimension is not None else None,
        "rows": rows,
        "formula_cells": formula_cells,
        "merged_cells": [
            item.get("ref") for item in merged.findall(M + "mergeCell") if merged is not None
        ]
        if merged is not None
        else [],
        "data_validations": [
            {"sqref": item.get("sqref"), "type": item.get("type")}
            for item in (validations.findall(M + "dataValidation") if validations is not None else [])
        ],
    }


def defined_names(archive: zipfile.ZipFile) -> list[dict]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    container = workbook.find(M + "definedNames")
    if container is None:
        return []
    return [
        {
            "name": node.get("name") or "",
            "local_sheet_id": node.get("localSheetId"),
            "refers_to": (node.text or "").strip(),
        }
        for node in container.findall(M + "definedName")
    ]


def structure_of(path: Path) -> dict:
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        strings = shared_strings(archive)
        names = defined_names(archive)
        sheets = []
        for order, (name, target) in enumerate(sheet_targets(archive), start=1):
            sheet = read_sheet(archive, target, strings)
            rows = sheet["rows"]
            row_numbers = sorted(rows)
            header_row = HEADER_ROW_OVERRIDES.get(name, 1)
            columns: list[dict] = []
            if header_row is not None:
                for ref, (text, _kind) in rows.get(header_row, {}).items():
                    header = normalize(text)
                    columns.append(
                        {
                            "column": ref,
                            "header": header,
                            "optional_hint": any(
                                marker in header for marker in OPTIONAL_MARKERS
                            ),
                        }
                    )
                columns.sort(key=lambda item: column_index(item["column"]))
            counters = []
            for number in (1, 2, 3):
                cells = rows.get(number, {})
                counters.append(
                    {
                        "row": number,
                        "non_empty_cells": len(cells),
                        "numeric_cells": sum(1 for _v, kind in cells.values() if kind == "n"),
                    }
                )
            # Example-row *shape* only: which columns the first data row fills.
            # No cell value is ever emitted.
            first_data: dict | None = None
            if header_row is not None:
                data_row = header_row + 1
                populated = sorted(rows.get(data_row, {}), key=column_index)
                first_data = {"row": data_row, "populated_columns": populated}
            sheets.append(
                {
                    "order": order,
                    "name": name,
                    "extent": sheet["extent"],
                    "non_empty_rows": len(row_numbers),
                    "first_row": row_numbers[0] if row_numbers else None,
                    "last_row": row_numbers[-1] if row_numbers else None,
                    "first_three_rows": counters,
                    "header_row": header_row,
                    "header_row_is_override": name in HEADER_ROW_OVERRIDES,
                    "data_start_row": None if header_row is None else header_row + 1,
                    "first_data_row": first_data,
                    "column_count": len(columns),
                    "formula_cells": sheet["formula_cells"],
                    "merged_cells": sheet["merged_cells"],
                    "data_validations": sheet["data_validations"],
                    "columns": columns,
                }
            )
    return {
        "template": {
            "path_placeholder": "<template-file>",
            "sha256": sha256(data).hexdigest(),
            "size_bytes": len(data),
            "worksheet_count": len(sheets),
            "shared_strings": len(strings),
            "defined_names": names,
        },
        "extraction": {
            "tool": "python3 (stdlib zipfile + xml.etree)",
            "script": "embedded heredoc, docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md Sec.8",
            "header_row_policy": (
                "default 1; explicit overrides for non-tabular first rows"
            ),
            "header_row_overrides": HEADER_ROW_OVERRIDES,
            "header_text_normalization": "_x000D_ / CR / LF / TAB -> single space",
            "optional_hint": "textual hint from the header text; not authoritative",
        },
        "worksheets": sheets,
    }


def print_report(result: dict) -> None:
    template = result["template"]
    print(f"file: {template['path_placeholder']}")
    print(f"sha256: {template['sha256']}")
    print(f"size_bytes: {template['size_bytes']}")
    print(f"worksheets: {template['worksheet_count']}")
    for name in template["defined_names"]:
        print(
            f"defined_name: {name['name']} local_sheet_id={name['local_sheet_id']} "
            f"refers_to={name['refers_to']}"
        )
    for sheet in result["worksheets"]:
        override = " [override]" if sheet["header_row_is_override"] else ""
        print()
        print(
            f"[{sheet['order']}] {sheet['name']}  extent={sheet['extent']}  "
            f"non_empty_rows={sheet['non_empty_rows']} "
            f"(rows {sheet['first_row']}..{sheet['last_row']})"
        )
        counted = " | ".join(
            f"r{row['row']}:{row['non_empty_cells']}cells/{row['numeric_cells']}num"
            for row in sheet["first_three_rows"]
        )
        print(f"    first-three-rows: {counted}")
        print(
            f"    header_row={sheet['header_row']}{override}  "
            f"data_start_row={sheet['data_start_row']}  "
            f"columns={sheet['column_count']}"
        )
        if sheet["first_data_row"] is not None:
            filled = " ".join(sheet["first_data_row"]["populated_columns"])
            print(
                f"    first_data_row={sheet['first_data_row']['row']} "
                f"populated_columns: {filled}"
            )
        print(
            f"    formula_cells={sheet['formula_cells']}  "
            f"merged_cells={len(sheet['merged_cells'])}  "
            f"data_validations={len(sheet['data_validations'])}"
        )
        for column in sheet["columns"]:
            hint = " [optional*]" if column["optional_hint"] else ""
            print(f"      {column['column']}: {column['header']}{hint}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("template", type=Path)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)

    result = structure_of(args.template)
    print_report(result)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\njson written: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY
```

**验证方式（评审者可独立复跑比对）**：

1. 运行 §8.1 第一条命令，输出应与附录 A **逐字一致**（含 SHA-256）；
2. 运行第二条命令后用 `git diff --stat docs/export-templates/template-structure.json`
   检查 sidecar 是否与仓库内版本一致；
3. 脚本只读模板的**结构**：不输出任何数据行的取值，`first_data_row` 只给出
   **被填充的列集合**（示例行结构），因此输出与 sidecar 都不含业务数据。

### 8.2 脚本的显式假设（可复核）

| 假设 | 内容 | 复核方式 |
|---|---|---|
| 表头行 | 默认第 1 行；`使用说明` 与 `横向自定义` 无表头行、`发票多行式` 为第 3 行（`HEADER_ROW_OVERRIDES`） | 输出中每表的 `first-three-rows` 计数与 `[override]` 标记 |
| 表头文本归一化 | `_x000D_` / CR / LF / TAB 折叠为单个空格 | 输出中的列清单 |
| 必填性线索 | 表头文本含「非必录/非必填/非必须/无需手填」→ `[optional*]`（**文本线索，非权威规则**） | 输出中的 `[optional*]` 标记；权威必填集合见 §2.2.9 与 `使用说明` |
| 数据区长度 | 以「有非空单元格的行」为准，不用 extent（`横向自定义` 的 extent 到 `H29984`） | 输出中的 `non_empty_rows` 与 `first_row..last_row` |
| 无数据输出 | 脚本从不输出单元格取值（表头文本除外） | 阅读脚本 `print_report` |

---

## 9 决策清单（需所有者拍板）

> **决策状态（2026-10-18 更新，Issue #53）**：D1 与 D2–D8 **均已由所有者收口**，
> 见 Issue #43 的两条决策评论：
> 「决策记录：D1 已由所有者决定」与「决策记录（所有者 2026-10-18 补充）：D2–D8
> 全部设计为**可配置**」；配置载体另见「决策记录补充二：配置采用**文件方式**」。
>
> **D1 更新（2026-10-19，Issue #71）**：所有者确认**改用新模板**——以
> 《凭证导入模板-20261019》的 `凭证模版` 单表 **25 列**作为 Track D 导出的目标格式
> （取代此前的 `录入数据` 31 列），依据见 Issue #43 评论「D1 更新（所有者
> 2026-10-19）：**改用新模板布局**」。落地见 §2.4（新模板结构）、§5.6（逐列映射）、
> §5.7（两布局对照）与 §9.2（实现口径）；旧 `录入数据` 布局保留为历史/回退。
>
> | 决策 | 状态 | 收口口径 |
> |---|---|---|
> | D1 目标财务系统与导入布局 | **已决策（2026-10-19 更新）** | **改用新模板**：`voucher_template`（`凭证模版` 单表 25 列）；实现按 §5.6 + §6 + §7。旧 §5.1 `录入数据` 31 列保留为历史/回退布局（仍受支持） |
> | D2 费用科目代码口径 | **已决策「可配置化」** | 导出配置文件内的「`expense_categories.code` → 会计科目代码」映射表（可选兜底科目） |
> | D3 进项税处理 | **已决策「可配置化」** | 配置文件开关：不拆 / 全拆 / 按费用类别拆；**示例给安全默认「不拆」** |
> | D4 贷方科目与日期基准 | **已决策「可配置化」** | 配置文件：单一口径或按报销单状态区分；日期基准与期间口径成对配置 |
> | D5 期间、凭证字与日期基准 | **已决策「可配置化」** | 期间由导出请求给定（`YYYY-MM`）；凭证字、日期基准、制人口径均可配置 |
> | D6 摘要文案模板 | **已决策「可配置化」** | 配置文件内的模板字符串（占位符白名单校验）+ 长度上限 |
> | D7 模板文件是否进仓库 | **已决策** | **不进仓库**：模板为运行时装载的受控产物，仓库只放映射与结构 sidecar |
> | D8 辅助核算输出口径 | **已决策「可配置化」** | 配置文件开关：关闭 / 仅部门 / 部门+往来（往来无权威来源，由配置给固定值） |
>
> D9–D12 的处置见本文末「实现口径澄清（Issue #53）」。
>
> 以下逐项选项与影响**保留原文**，作为决策依据的历史记录。

每项给出选项与影响。~~**本节全部为待决策项，本任务不替所有者选择**~~（已按上表
收口）；除 D1 与 D7 外，其余选项都会改变导出内容（因而影响校验与验收），故在实现
任务前必须收口。

### D1 目标财务系统与导入布局（**最高优先**）

| 选项 | 影响 |
|---|---|
| (a) `录入数据`（本文推荐；一行 = 一条分录，需工具再生成标准格式凭证文件） | 最少变换、与明细行同构；需所有者确认实际导入链路确实经过工具 |
| (b) `PZ`（44 列标准格式列集） | 直接对齐标准格式凭证/DBF 通道；需自建期间、分录序号、借贷方向列；列冗余多 |
| (c) `发票记录` / `日记账` / 其它输入表 | 需要重载模板的既有语义（发票视角/资金视角），并接受贷款方科目口径与报销不符 |

### D2 费用科目代码口径（网约车/餐饮等）

| 选项 | 影响 |
|---|---|
| (a) 所有者提供「`expense_categories.code` → 会计科目代码」映射表（本系统内配置） | 最精确；需新增配置载体与维护责任 |
| (b) 要求 `expense_categories.code` 直接使用会计科目代码 | 无需映射表；但现有系统内该类编码是自定义的（非科目代码），需重新约定编码规范并可能迁移数据 |
| (c) 全部映射到单一费用科目 | 实现最简；丧失费用明细的会计意义，通常不可接受 |

### D3 进项税处理

| 选项 | 影响 |
|---|---|
| (a) 拆出「应交税费-进项税额」借方分录（`Σ tax_amount > 0` 时） | 会计正确；需税费科目代码、需校验 E3；`日记账` 布局不支持 |
| (b) 不拆，全额计入费用科目 | 实现最简；税额信息丢失，目标系统无法抵扣 |
| (c) 仅对增值税专用发票拆税，普票/网约车票不拆 | 最贴近税务处理；需要能按票种判定（`invoice_verifications.invoice_type` 存在但取值集合未固定） |

### D4 贷方科目口径

| 选项 | 影响 |
|---|---|
| (a) 资金科目（银行存款/现金）+ 凭证日期取 `paid_at` | 与「已付款才导出」一致；需所有者提供资金科目代码；本系统无银行账户模型 |
| (b) 往来/挂账科目（如其他应付款-员工）+ 凭证日期取 `expense_date` | 与费用发生时点一致；贷方科目仍需所有者提供 |
| (c) 按报销单状态区分（未付款用 (b)，已付款用 (a)） | 最贴近现实；导出的凭证集合随时间变化，需明确「导出时点」语义与幂等边界 |

### D5 期间、凭证字与日期基准

| 选项 | 影响 |
|---|---|
| (a) 期间 = 付款月，凭证字固定「记」，日期 = `paid_at` | 与 D4(a) 配套；跨月付款会改变期间归属 |
| (b) 期间 = 费用发生月，凭证字固定「记」，日期 = `expense_date` | 与费用归属一致；可能与资金流水期间不符 |
| (c) 凭证字按业务类型区分（如付款用「付」、其余用「记」） | 需所有者给出凭证字集合与适用规则 |

### D6 摘要文案模板

| 选项 | 影响 |
|---|---|
| (a) `{claim_no} {employee_no} {description}` | 可追溯到单据与人员；长度需截断到 500 内 |
| (b) 仅 `{description}` | 最简洁；失去单据号，无法反查 |
| (c) `{描述}-{员工号}` 或按类别拼接（如 `差旅费-{employee_no}`） | 更易人工阅读；需要类别 → 中文名的稳定口径 |

### D7 模板文件是否进仓库

| 选项 | 影响 |
|---|---|
| (a) 不进仓库（本文采用；模板置于仓库外受控目录，仓库只放映射与 sidecar） | 符合上游 §7.2 与 O6；需保证模板分发与版本登记流程 |
| (b) 进仓库（如 `templates/` 下） | 评审与 CI 可直接校验结构；但第三方模板的**许可与再分发**未确认，且会把工具痕迹公开进仓库 → 需所有者明确授权 |

### D8 辅助核算输出口径

| 选项 | 影响 |
|---|---|
| (a) 不输出辅助核算（全部留空） | 实现最简；部门维度丢失 |
| (b) 仅输出「部门名称」（`departments.name`） | 有权威来源；但目标系统通常要求「类别 + 代码 + 名称」，代码缺失可能被拒 |
| (c) 输出部门 + 往来单位 | 需要新增往来单位数据源（§4.3 缺口：销售方名称未持久化）与部门编码口径 → 需另立任务 |

### D9 导出对象范围与粒度

| 选项 | 影响 |
|---|---|
| (a) 按期间批量，仅 `paid` 状态 | 边界清晰、与付款口径一致；未付款报销单不出账 |
| (b) 按期间批量，`finance_reviewed` + `paid` | 覆盖已复核未付款；贷方语义需按 D4(c) 区分 |
| (c) 单张报销单导出 | 上游 §7.2 要求支持「单张」与「按期间」两种且**同一模板不得混用**两种粒度 → 需两个模板标识 |

### D10 导出所需权限与角色

| 选项 | 影响 |
|---|---|
| (a) 复用现有读取权限（`expense_claim/read/company` (4) / `read/authorized` (5)）+ 审计 | 零新增权限编号；但「读」权限隐含「可导出」可能超出预期 |
| (b) 新增独立导出权限（如 `finance/export/company`） | 语义清晰、可单独授权；需在 `app/identity/permissions.py` 追加**新的权限编号**（编号发布后不得复用）并做角色矩阵/迁移 |
| (c) 仅财务主管可导出（现有权限组合 + 显式名单） | 最少权限面；批量导出会变慢（人工在场） |

### D11 保留期与清理

| 选项 | 影响 |
|---|---|
| (a) 与附件同口径：不设自动清理，由运维手工处置 | 零实现；存储长期增长 |
| (b) 新增保留期（如导出文件 N 天后可删）并在 `source_files` 增加过期字段 | 需迁移与清理任务（涉及 `app/`+`migrations/`，须单独立 Issue） |
| (c) 导出文件不落盘，仅流式返回并只保留 manifest | 无留存风险；但失去「哈希可复核 + 重复下载一致」的能力 |

### D12 附加待确认（本任务新发现，建议与 D1 一并确认）

- **D12.1 附件数口径**：`附件数` 取 `expense_invoices` 行数，还是 `source_file_id` 非空计数？
- **D12.2 业务编号口径**：`业务编号` 取 `claim_no`；`PZ.FREFERENCE` / `FSETTLENO`
  是否可用于承载 `payment_ref`（付款参考号）？
- **D12.3 明细 ↔ 发票对应关系**：挂账只到报销单粒度（`expense_invoices.claim_id`），
  无法做「明细行 ↔ 发票」级对账。若所有者要求逐票核对，需新增关联（数据模型变更，
  须单独立 Issue）。
- **D12.4 `verified_fail` / `suspected_duplicate` 挂账行的导出处置**：整单排除、
  仅排除该票，还是允许导出并在 manifest 标注？
- **D12.5 制单人取值口径（`录入数据.P` / `PZ.FPREPARE`）**：取报销人
  `employees.employee_no`（业务事实）、取导出人（操作事实），还是取固定文案？
  取导出人会使同一批数据的导出产物随操作者变化，从而破坏「同期同模板可复现」。

### 9.1 实现口径澄清（Issue #53）

实现（`app/export/`、`config/ajaimgr.example.toml`）对文档未完全确定之处作出的
**可配置化 + 显式默认**决定，逐项记录如下（均为「配置项或显式默认」，不写死）：

| # | 项 | 实现口径 |
|---|---|---|
| D9 | 导出对象范围与粒度 | 本期实现**按期间批量**；对象状态集合由配置 `[scope].statuses` 给定（示例安全默认 `["paid"]`）。单张导出不在本期（§10 后续任务）。 |
| D10 | 导出权限 | 复用现有 RBAC 三元组，**不新增权限编号**（新增编号涉及迁移，另立任务）：配置 `[permissions]` 给出 `(resource, action, scopes)`，命中任一即放行（示例：`expense_claim/read/company` 或 `read/authorized`）。拒绝写拒绝审计。 |
| D11 | 保留期与清理 | **未实现**（仓库当前不存在保留期机制，见 §7.2）；导出文件与 manifest 落同一内容寻址存储根，清理沿用附件口径，由运维手工处置。另立任务。 |
| D12.1 | 附件数口径 | 配置 `[invoice_policy].attachment_count_basis`：`invoices`（挂账发票行数，默认）/ `files`（原件引用非空计数）。 |
| D12.2 | 业务编号口径 | `录入数据.AC`（业务编号）取 `expense_claims.claim_no`；本期无 `PZ` 布局，故 `FREFERENCE`/`FSETTLENO` 不涉及。 |
| D12.3 | 明细 ↔ 发票对应 | 本期**只做报销单级**对照（E7：挂账票面金额之和 vs 明细价税合计之和）；逐票核对需数据模型变更，另立任务。 |
| D12.4 | 异常票处置 | 配置 `[invoice_policy].on_verified_fail` / `on_suspected_duplicate`：`fail`（安全默认，整单不导出）/ `allow`。 |
| D12.5 | 制单人取值 | 配置 `[voucher].preparer`：`claimant`（报销人工号，**安全默认**，保持字节可复现）/ `exporter`（导出人工号；会破坏「同期同模板可复现」，已在示例中注明）/ `literal`（固定文案）。 |
| — | 期间包含性（C4） | `date_basis = "expense_date"` 时要求该单**全部**明细日期落在请求期间内（跨期整单不导出）；`date_basis = "paid_at"` 时按付款日期判定。 |
| — | 币别未登记 | 新增显式 fail closed：`currency_unmapped`（不臆造币别代码）。 |
| — | 单元格注入转义 | 文本以 `=`/`+`/`-`/`@` 开头时前缀单引号（§6.4），并在写出前用纵深防御闸门 `guard_no_unescaped` 复核（fail closed #12）。 |
| — | xlsx 字节可复现 | `openpyxl` 默认写入 `docProps/core.xml` 的 `created`/`modified` 时间戳与 zip 条目本地时间戳；实现把两者固定为常量，因此导出文件**不含导出时间戳**、字节可复现（§6.5）。时间只进 manifest 的 `exported_at`。 |
| — | manifest 与文件落库 | 导出文件与 manifest 各经 `app/documents/storage.store_file` 落内容寻址存储，并各写一行 `source_files` 元数据（§7.2）；`source_files.scan_status` 沿用默认值。 |

### 9.2 实现口径澄清（Issue #71：新模板布局）

`voucher_template` 布局（§5.6）对文档未完全确定之处作出的**可配置化 + 显式默认**
决定（与 §9.1 同口径，均为配置项或显式默认，不写死）：

| # | 项 | 实现口径 |
|---|---|---|
| 凭证号模式 | 新模板惯例「记-N」跨期连续 | 配置 `[voucher].number_mode`：`sequential`（**安全默认**，从 `number_start` 连续编号，同一报销单同号）/ `blank`（留空，由会计系统分配）。**跨期续号等真实行为由会计系统决定，本系统无法推断**；首次导入按实调整 `number_start`（示例注释已注明）。 |
| 分录序号 | 新模板无示例数据行可依据 | 取该分录在**本凭证内**的 1 基序号（`1,2,3…`）；分录顺序沿用 §5.5 的确定性顺序（借方费用 → 税费 → 贷方）。 |
| 命名辅助核算列 | 仅「部门」有权威来源 | 配置 `[auxiliary].named_columns`（键 = 表头名）：`blank`（**安全默认**）/ `department`（`departments.name`）/ `literal`（固定文本）。客户/供应商/职员/项目/存货/自定义辅助核算在本系统**无权威来源** → 示例一律 `blank`，可按需改 `literal`。非法取值在配置装载期 fail closed。 |
| 数量 / 单价 | `expense_lines` 无数量、单价列 | **安全默认留空**（不臆造）。 |
| 原币金额 / 币别 / 汇率 | 本币导出 | W = 该分录金额；X = `[currency].codes` 映射（未登记 → `currency_unmapped`）；Y = `[currency].rate`（固定 1）。 |
| 制单人 | 新模板 25 列**不含**制单人列 | `[voucher].preparer` 对本布局不生效（仅旧布局使用）。真实凭证列表「制单人 = 会计」属会计系统侧口径，需导入后由对方处理。 |
| 模板装载 | 新模板为另一文件 | SHA-256 绑定 + 工作表存在 + **表头/列数逐列一致**（25 列，按列号定位）→ fail closed 语义不变；未登记布局（如 `pz`）仍 `unsupported_layout`。 |
| 复用 | — | 校验（§6 E1–E7/C1–C5/fail closed）、单元格注入防护（§6.4）、manifest（§7.1）、存储（§7.2）、审计**全部复用既有实现**，不重写。 |

---

## 10 未完成项与后续任务

| # | 后续任务 | 依赖 | 说明 |
|---|---|---|---|
| 1 | **决策收口**：所有者对 §9 逐项拍板（至少 D1–D7） | 本文件 | 实现前必须收口，否则导出内容不可验收 |
| 2 | Track D 导出机制实现（模板 loader + 映射引擎 + 校验 + manifest + 端点 + 迁移） | 1 | 涉及 `app/`、`migrations/`、`tests/`；须单独立 Issue 并声明路径 |
| 3 | 首个真实模板导出验收（合成数据 + 真实模板端到端、篡改金额 fail closed、manifest 复现） | 2 | 上游 §7.6 的验收标准逐条执行 |
| 4 | 科目与辅助核算映射载体的选型与落地（配置表 vs 代码常量 vs 外部文件） | D2/D8 | 决定映射是否可运维 |
| 5 | 往来单位（销售方）名称持久化（OCR/查验结果 → 业务表） | D8 | 当前唯一来源是不可信的草稿 `extraction_drafts.extracted_json` |
| 6 | 保留期机制（附件与导出文件）与导出权限编号 | D10/D11 | 涉及权限矩阵与迁移 |

**本任务明确不做**：任何 `app/` / `scripts/` / `tests/` / `migrations/` 改动；
模板文件入库；依赖变更。

---

## 11 与 Issue #43 验收标准的对照

| 验收标准 | 本文对应位置 |
|---|---|
| 新文档 `docs/TRACK-D-EXPORT-TEMPLATE-MAPPING.md`：模板总览（14 表、用途、表头行、列清单、必填性、示例行结构） | §2（含 §2.1 总表、§2.2 逐表、§2.3 结构风险） |
| 四个候选布局对比 + 应用场景 + 推荐默认目标（标注待确认） | §3（§3.1 对比、§3.2 推荐与备选、§3.3 其它布局） |
| 逐列映射（`expense_claims`/`expense_lines`/`expense_invoices`/`invoice_verifications` 真实列；含金额口径、日期格式、凭证字与期间、摘要规则、辅助核算处置） | §4（字段与口径）+ §5（四布局逐列映射）+ §5.5（共用派生规则） |
| 校验规则（借贷平衡、价税合计 = 不含税 + 税额、行数与金额合计核对、fail closed 清单） | §6（E1–E7、C1–C5、fail closed 12 条、注入与格式、幂等） |
| manifest 草案（期间、模板版本与 SHA-256、记录 ID 集合、行数、金额合计、文件哈希、导出人、时间）+ 与 `app/documents/storage.py` 的复用点 | §7（§7.1 字段、§7.2 复用点表含保留期缺口） |
| 决策清单（目标系统/布局、费用科目、进项税、贷方科目、凭证字与期间、摘要、模板是否入库） | §9（D1–D7 逐项 2–3 选项 + 影响；另附 D8–D12 新发现） |
| 结构与模板实际内容逐项一致；PR 附可复核提取过程与真实输出；提取命令内嵌 | §8（运行方式 + 显式假设）+ 附录 A（真实输出）+ `export-templates/template-structure.json` |
| 不提交模板文件、真实业务数据、凭据或本机信息；`privacy-scan` 通过 | §0「演示数据的处理」；全部路径与模板文件用 `<template-file>` 占位；sidecar 只含结构 |

---

## 附录 A 结构提取命令的真实输出

以下为 §8.1 第一条命令在 `<template-file>` 上的真实输出（未删减；表头文本为模板
原文，数据行取值从不输出）：

```text
file: <template-file>
sha256: 1a235166285ee31255c899ce57e09eedd1e7f6641cd6fd3b5e931471ecb2718c
size_bytes: 161974
worksheets: 14
defined_name: _xlnm._FilterDatabase local_sheet_id=4 refers_to=横向自定义!#REF!
defined_name: _xlnm._FilterDatabase local_sheet_id=8 refers_to=录入数据!$A$1:$AE$8

[1] 使用说明  extent=A1:V34  non_empty_rows=24 (rows 1..34)
    first-three-rows: r1:1cells/0num | r2:2cells/0num | r3:1cells/0num
    header_row=None [override]  data_start_row=None  columns=0
    formula_cells=0  merged_cells=10  data_validations=0

[2] 简易日记账  extent=A1:O5  non_empty_rows=3 (rows 1..3)
    first-three-rows: r1:15cells/0num | r2:11cells/3num | r3:11cells/3num
    header_row=1  data_start_row=2  columns=15
    first_data_row=2 populated_columns: A B C D F G H J K N O
    formula_cells=0  merged_cells=0  data_validations=0
      A: 凭证字 (非必录) [optional*]
      B: 序号
      C: 凭证日期 交易日期
      D: 摘 要
      E: 借方(收款金额）
      F: 贷方（付款金额）
      G: 资金科目代码 (与金额方向相同)
      H: 资金科目名称 (非必填） [optional*]
      I: 辅助核算名称A 跟随资金科目(非必填） [optional*]
      J: 对方科目代码 (与金额方向相反)
      K: 对方科目名称 (非必填） [optional*]
      L: 辅助核算名称B 跟随对方科目(非必填） [optional*]
      M: 数据检测情况
      N: 匹配模板编号
      O: 匹配模板名称

[3] 日记账  extent=A1:AC21  non_empty_rows=5 (rows 1..5)
    first-three-rows: r1:29cells/0num | r2:13cells/6num | r3:13cells/5num
    header_row=1  data_start_row=2  columns=29
    first_data_row=2 populated_columns: A B C D E F H K L M O P X
    formula_cells=0  merged_cells=0  data_validations=2
      A: 凭证字 (非必录) [optional*]
      B: 序号
      C: 凭证日期 交易日期
      D: 摘要
      E: 借方金额
      F: 贷方金额
      G: 数量跟随位置
      H: 数量
      I: 数量单位
      J: 单价
      K: 资金科目代码 (与金额方向相同)
      L: 资金科目名称A (非必填） [optional*]
      M: 辅助核算名称A1 （非必须） [optional*]
      N: 辅助核算名称A2 （非必须） [optional*]
      O: 对方科目代码 (与金额方向相反)
      P: 对方科目名称B (非必填） [optional*]
      Q: 辅助核算名称B1 （非必须） [optional*]
      R: 辅助核算名称B2 （非必须） [optional*]
      S: 外币跟随位置
      T: 币别代码
      U: 币别名称
      V: 外币原值
      W: 汇率
      X: 附件数
      Y: 业务编号位置
      Z: 业务编号
      AA: 数据检测情况
      AB: 匹配模板编号
      AC: 匹配模板名称

[4] 发票记录  extent=A1:AF31  non_empty_rows=4 (rows 1..4)
    first-three-rows: r1:32cells/0num | r2:16cells/8num | r3:15cells/9num
    header_row=1  data_start_row=2  columns=32
    first_data_row=2 populated_columns: A B C D G H I J K L M P Q R S AA
    formula_cells=0  merged_cells=0  data_validations=2
      A: 凭证字 (非必录) [optional*]
      B: 凭证号
      C: 凭证日期
      D: 摘 要
      E: 数量跟随位置
      F: 数量单位 (非必填） [optional*]
      G: 数量 (非必填） [optional*]
      H: 单价 (非必填） [optional*]
      I: 不含税金额
      J: 税额
      K: 价税合计
      L: 借方 科目代码A
      M: 借方 科目名称A (非必填） [optional*]
      N: 辅助核算名称A1 (非必填） [optional*]
      O: 辅助核算名称A2 (非必填） [optional*]
      P: 税费 科目代码B
      Q: 税费科 目名称B
      R: 贷方 科目代码C
      S: 贷方 科目名称C (非必填） [optional*]
      T: 辅助核算名称C1 (非必填） [optional*]
      U: 辅助核算名称C2 (非必填） [optional*]
      V: 外币跟随位置
      W: 币别代码
      X: 币别名称
      Y: 外币原值
      Z: 汇率
      AA: 附件数
      AB: 业务编号位置
      AC: 业务编号
      AD: 数据检测情况
      AE: 模板号（无需手填） [optional*]
      AF: 模板名称（无需手填） [optional*]

[5] 横向自定义  extent=A1:H29984  non_empty_rows=11 (rows 1..11)
    first-three-rows: r1:5cells/0num | r2:5cells/0num | r3:5cells/0num
    header_row=None [override]  data_start_row=None  columns=0
    formula_cells=0  merged_cells=0  data_validations=1

[6] 发票多行式  extent=A1:Q33  non_empty_rows=6 (rows 1..6)
    first-three-rows: r1:7cells/0num | r2:5cells/1num | r3:16cells/0num
    header_row=3 [override]  data_start_row=4  columns=16
    first_data_row=4 populated_columns: B D F G H I J K L M
    formula_cells=0  merged_cells=0  data_validations=1
      A: 发票代码
      B: 发票号码
      C: 全电号码
      D: 对方公司名称
      E: 单位 (非必填） [optional*]
      F: 数量 (非必填） [optional*]
      G: 单价 (非必填） [optional*]
      H: 不含税金额
      I: 税额
      J: 价税合计
      K: 摘要
      L: 科目代码A
      M: 科目名称A (非必填） [optional*]
      N: 辅助核算名称A1 (非必填） [optional*]
      O: 辅助核算名称A2 (非必填） [optional*]
      P: 数据检测情况

[7] 发票多行式二  extent=A1:V31  non_empty_rows=7 (rows 1..7)
    first-three-rows: r1:20cells/0num | r2:14cells/5num | r3:14cells/5num
    header_row=1  data_start_row=2  columns=20
    first_data_row=2 populated_columns: A B D F G H I J K L N O Q R
    formula_cells=0  merged_cells=0  data_validations=0
      A: 发票代码
      B: 发票号码
      C: 全量发票号码
      D: 摘 要
      E: 数量单位 (非必填） [optional*]
      F: 数量 (非必填） [optional*]
      G: 单价 (非必填） [optional*]
      H: 不含税金额
      I: 税额
      J: 价税合计
      K: 借方跟随明细 科目代码A
      L: 借方跟随明细 科目名称A (非必填） [optional*]
      M: 跟随明细辅助核算名称A (非必填） [optional*]
      N: 税费 科目代码B
      O: 税费科 目名称B
      P: 税费科目核算名称B (非必填） [optional*]
      Q: 贷方应付 科目代码C
      R: 贷方应付 科目名称C (非必填） [optional*]
      S: 辅助核算名称C1 (非必填） [optional*]
      T: 数据检测情况

[8] 先借后贷  extent=A1:T31  non_empty_rows=7 (rows 1..7)
    first-three-rows: r1:13cells/0num | r2:9cells/3num | r3:9cells/3num
    header_row=1  data_start_row=2  columns=13
    first_data_row=2 populated_columns: A B C D E F G I J
    formula_cells=0  merged_cells=0  data_validations=0
      A: 凭证字
      B: 凭证号
      C: 凭证日期
      D: 摘 要
      E: 金额
      F: 借方跟随明细 科目代码A
      G: 借方跟随明细 科目名称A (非必填） [optional*]
      H: 跟随明细辅助核算名称A (非必填） [optional*]
      I: 贷方 科目代码B
      J: 贷方 科目名称B (非必填） [optional*]
      K: 辅助核算名称B (非必填） [optional*]
      L: 数据检测情况
      T: 数据检测情况

[9] 录入数据  extent=A1:AE18  non_empty_rows=7 (rows 1..7)
    first-three-rows: r1:31cells/0num | r2:14cells/7num | r3:13cells/7num
    header_row=1  data_start_row=2  columns=31
    first_data_row=2 populated_columns: A B C D E F H I J K L M N S
    formula_cells=0  merged_cells=0  data_validations=0
      A: 凭证字
      B: 凭证号
      C: 凭证日期
      D: 凭证摘要
      E: 科目代码
      F: 科目名称
      G: 余额方向 D/C(自动补充)
      H: 币别代码
      I: 币别名称
      J: 原币金额
      K: 汇率
      L: 数量
      M: 单价
      N: 借方金额
      O: 贷方金额
      P: 制单人
      Q: 辅助核算类别A
      R: 项目代码A
      S: 核算项目名称A
      T: 辅助核算类别B
      U: 项目代码B
      V: 核算项目名称B
      W: 辅助核算类别C
      X: 项目代码C
      Y: 核算项目名称C
      Z: 辅助核算类别D
      AA: 项目代码D
      AB: 核算项目名称D
      AC: 业务编号
      AD: 附件数
      AE: 检测错误情况

[10] PZ  extent=A1:AR19  non_empty_rows=19 (rows 1..19)
    first-three-rows: r1:44cells/0num | r2:19cells/11num | r3:19cells/11num
    header_row=1  data_start_row=2  columns=44
    first_data_row=2 populated_columns: A B C D E F G U V W X Y Z AA AB AE AJ AK AM
    formula_cells=0  merged_cells=0  data_validations=0
      A: FDATE
      B: FPERIOD
      C: FGROUP
      D: FNUM
      E: FENTRYID
      F: FEXP
      G: FACCTID
      H: FCLSNAME1
      I: FOBJID1
      J: FOBJNAME1
      K: FCLSNAME2
      L: FOBJID2
      M: FOBJNAME2
      N: FCLSNAME3
      O: FOBJID3
      P: FOBJNAME3
      Q: FCLSNAME4
      R: FOBJID4
      S: FOBJNAME4
      T: FTRANSID
      U: FCYID
      V: FEXCHRATE
      W: FDC
      X: FFCYAMT
      Y: FQTY
      Z: FPRICE
      AA: FDEBIT
      AB: FCREDIT
      AC: FSETTLCODE
      AD: FSETTLENO
      AE: FPREPARE
      AF: FPAY
      AG: FCASH
      AH: FPOSTER
      AI: FCHECKER
      AJ: FATTCHMENT
      AK: FPOSTED
      AL: FMODULE
      AM: FDELETED
      AN: FSERIALNO
      AO: FTRANSDATE
      AP: FCASHFLOW
      AQ: FUNITNAME
      AR: FREFERENCE

[11] 科目数据  extent=A1:O202  non_empty_rows=202 (rows 1..202)
    first-three-rows: r1:15cells/0num | r2:4cells/0num | r3:4cells/0num
    header_row=1  data_start_row=2  columns=15
    first_data_row=2 populated_columns: A B C F
    formula_cells=0  merged_cells=0  data_validations=0
      A: 科目代码
      B: 科目名称
      C: 助记码
      D: 科目类别
      E: 是否存货科目
      F: 余额方向
      G: 外币核算
      H: 期末调汇
      I: 往来单位核算
      J: 部门核算
      K: 职员核算
      L: 其他核算项目
      M: 往来业务核算
      N: 数量金额核算
      O: 数量单位

[12] 核算项目  extent=A1:J76  non_empty_rows=16 (rows 1..16)
    first-three-rows: r1:9cells/0num | r2:4cells/0num | r3:4cells/0num
    header_row=1  data_start_row=2  columns=9
    first_data_row=2 populated_columns: A B C J
    formula_cells=0  merged_cells=0  data_validations=0
      A: 核算分类
      B: 代码
      C: 核算名称
      D: 费用、成本科目代码-A
      E: 费用、成本科目名称-A
      F: 费用、成本科目代码-B
      G: 费用、成本科目名称-B
      H: 费用、成本科目代码-C
      I: 费用、成本科目名称-C

[13] 模式凭证  extent=A1:Q19  non_empty_rows=19 (rows 1..19)
    first-three-rows: r1:17cells/0num | r2:9cells/3num | r3:6cells/2num
    header_row=1  data_start_row=2  columns=17
    first_data_row=2 populated_columns: A C F G H I K M O
    formula_cells=18  merged_cells=0  data_validations=0
      A: 顺序号
      B: 摘要等于条件
      C: 摘要包含条件
      D: 摘要不包含条件
      E: 备用
      F: 模板编号
      G: 模板名称
      H: 科目代码
      I: 科目名称
      J: 辅助核算明细名称
      K: 借方金额比例
      L: 贷方金额比例
      M: 借方金额
      N: 贷方金额
      O: 匹配新摘要（非必须） [optional*]
      P: 匹配币别代码
      Q: 匹配汇率

[14] 核算分类  extent=A1:D6  non_empty_rows=6 (rows 1..6)
    first-three-rows: r1:4cells/0num | r2:3cells/1num | r3:3cells/1num
    header_row=1  data_start_row=2  columns=4
    first_data_row=2 populated_columns: A B C
    formula_cells=0  merged_cells=0  data_validations=0
      A: FClsID
      B: FClsName
      C: FQtyAux
      D: FObjTableName
```

---

## 附录 B sidecar 说明

### B.1 旧模板 sidecar

[`export-templates/template-structure.json`](export-templates/template-structure.json)
为纯结构 sidecar，由 §8.1 第二条命令生成，包含：

- `template`：`path_placeholder`（恒为 `<template-file>`）、`sha256`、`size_bytes`、
  `worksheet_count`、`shared_strings`、`defined_names`；
- `extraction`：工具、表头策略与覆盖表、归一化规则、必填性线索的定义；
- `worksheets[]`：每表的 `order`/`name`/`extent`/`non_empty_rows`/`first_three_rows`/
  `header_row`(+`override`)/`data_start_row`/`column_count`/`columns[]`
  （`column`/`header`/`optional_hint`）/`first_data_row.populated_columns`/
  `formula_cells`/`merged_cells`/`data_validations`。

**不含**任何数据行取值、真实业务数据、凭据或本机路径。

### B.2 新模板 sidecar（Issue #71）

[`export-templates/voucher-template-structure.json`](export-templates/voucher-template-structure.json)
为**新模板**《凭证导入模板-20261019》的纯结构 sidecar，由**同一条** §8.1 命令
（无需新增 `HEADER_ROW_OVERRIDES`）生成，字段结构与 B.1 完全一致。

**关键约束**：新模板的 `科目数据` / `辅助核算项目数据` 两张**备查表含真实科目
数据**，因此 sidecar 对这两张表**只登记表头文本与存在性/列位置**，**严禁**登记任何
数据取值；提取脚本本身从不输出数据行取值（§8.2「无数据输出」）。sidecar 中的两张
备查表的 `columns[].header` 是模板自带的「备查数据,不会被导入。」提示文本，不含任何
科目/核算项目取值。
