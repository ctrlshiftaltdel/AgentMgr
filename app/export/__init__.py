"""Track D 导出机制（服务层）。

把已付款（可配置状态集合）的报销数据按**单一配置文件** + **运行时装载的模板**
投影为可导入的 xlsx + manifest。目标布局由 ``template.layout`` 决定：

* ``entry_rows`` —— 旧模板 ``录入数据``（31 列，历史/回退布局）；
* ``voucher_template`` —— 新模板 ``凭证模版``（25 列，Issue #71，示例默认）。

模块边界：

* :mod:`app.export.config` —— 单文件 TOML 配置装载（``tomllib``，fail closed）；
* :mod:`app.export.template` —— 模板装载与 SHA-256/表头结构校验（不入库，按布局）；
* :mod:`app.export.mapping` —— §5.1/§5.5 映射引擎（纯逻辑，金额用 ``Decimal``）；
* :mod:`app.export.voucher_layout` —— §5.6 新布局（``凭证模版`` 25 列）按列号展开；
* :mod:`app.export.validation` —— §6 校验（E1–E7 / C1–C5 / fail closed）；
* :mod:`app.export.writer` —— 单元格注入防护 + 字节可复现的 xlsx 渲染（按布局）；
* :mod:`app.export.manifest` —— §7.1 manifest 构建；
* :mod:`app.export.service` —— 服务层入口（只读投影 + 存储 + 审计 + 幂等）。

**本模块不提供 HTTP 端点**（另立任务），也不引入任何新依赖。

子模块按需导入（``from app.export import config``）；包 ``__init__`` 不导入
:mod:`app.export.service`，因此纯逻辑子模块可脱离数据库独立测试。
"""
