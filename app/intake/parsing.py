"""受理解析流水线：可替换的解析接口、人工录入兜底与草稿裁决（W1-26）。

本模块把 ``templates/PLAN.md`` §4.2「统一资料接收流水线」的**解析环节**落成
**供应商无关**的纯逻辑层：

* **四类输入**（``InputKind``）：文字（``text``）/ 语音（``voice``）/ 图片
  （``image``）/ 文档（``document``）。它们到 ``IntakeJob.input_type`` 的五值
  ``CHECK`` 集合（``text``/``image``/``file``/``voice``/``spreadsheet``）有**显式
  映射**（见 ``INPUT_KIND_MODEL_TYPES`` 与 ``model_input_type``）——文档类输入按
  是否为表格映射到 ``spreadsheet``，否则为 ``file``。
* **可替换接口**（``IntakeParser``）：上层业务只依赖本接口；OCR/ASR 供应商尚未
  决定，故本任务**不接任何真实供应商**，只定义接口。未来接入的
  ``ExternalParser`` 基类强制**凭据只从环境变量读取、缺失即构造失败（fail
  closed）**，与 ``app.integrations.baidu_vat_verify`` 同一模式。
* **人工录入兜底**（``ManualEntryParser``）：完全不依赖任何外部服务，由授权人员
  逐字段录入，保证「无供应商也可工作」。
* **状态裁决**（``decide_draft``）：低置信度或关键字段缺失时产出
  ``IntakeJob.status = 'needs_review'``，否则 ``'parsed'``；解析失败为
  ``'failed'``。``ExtractionDraft.review_status`` 恒为 ``'pending'``——**人工确认
  是进入正式记录的唯一路径**，本层绝不自动确认、绝不写正式业务表。
* **可追溯**（``parse_provenance`` / ``safe_log_summary``）：来源文件经
  ``source_file_id`` 承载，解析器版本与确认人经既有 ``app.audit`` 审计承载
  （见 ``app.intake.pipeline``）；**日志与审计只记录字段名与标识，绝不记录附件
  原文或识别全文**。

硬边界（对齐 PLAN §4.2、``docs/research/W0-5-security-compliance.md`` §3.1/§3.2、
``docs/research/W0-1-weixin-ilink.md`` §6.3）：

1. **原始文件不得被解析结果覆盖**：本模块只读 ``source_file_id`` 引用，不写文件。
2. **解析结果只进草稿态**：任何来源都不得直接写正式业务字段（报销/账务）。
3. **非文字输入必须携带原件引用**：图片/语音/文档的 ``ParseRequest`` 若缺
   ``source_file_id`` 直接 ``ValueError``（fail closed），保证「每项提取值可追溯到
   来源文件」成立。
4. **凭据 fail closed**：解析器接口的任何凭据只从环境变量读取，缺失即失败，错误
   信息只提环境变量名、**不含凭据值**。
5. **不记录原文**：本模块不写任何含附件原文或识别全文的日志/证据；``safe_log_summary``
   只输出字段名、计数、解析器标识等非敏感信息。

本模块**只依赖标准库**（``abc`` / ``dataclasses`` / ``enum`` / ``os``），不引入任
何新依赖，也不进行任何网络调用，便于在无数据库/无供应商的环境下单元测试。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "BUSINESS_REQUIRED_FIELDS",
    "DEFAULT_CONFIDENCE_THRESHOLD",
    "DEFAULT_REQUIRED_FIELDS",
    "INPUT_KIND_MODEL_TYPES",
    "MANUAL_ENTRY_CONFIDENCE",
    "MANUAL_ENTRY_PARSER_NAME",
    "MANUAL_ENTRY_PARSER_VERSION",
    "MODEL_INPUT_TYPES",
    "DraftDecision",
    "ExternalParser",
    "InputKind",
    "IntakeParser",
    "ManualEntryParser",
    "MissingCredentialError",
    "ParseRequest",
    "ParseResult",
    "ParserError",
    "ParserKind",
    "ParserRegistry",
    "UnsupportedInputError",
    "build_draft_payload",
    "decide_draft",
    "input_kind_for_model_type",
    "model_input_type",
    "parse_provenance",
    "required_fields_for",
    "safe_log_summary",
]

# --- 输入类型映射 -------------------------------------------------------------


class InputKind(str, Enum):
    """受理解析流水线接受的**四类输入**（PLAN §4.1 / §4.2）。

    继承 ``str`` 便于直接落库/日志（``TEXT`` + ``CHECK``，不用数据库 ENUM）。
    """

    TEXT = "text"  # 文字
    VOICE = "voice"  # 语音
    IMAGE = "image"  # 图片
    DOCUMENT = "document"  # 文档（PDF/Word/Excel 等）


#: ``IntakeJob.input_type`` 的合法取值集合（与 ``app.intake.models`` 的 CHECK 一致）。
MODEL_INPUT_TYPES: frozenset[str] = frozenset(
    {"text", "image", "file", "voice", "spreadsheet"}
)

#: 四类输入 → 模型 ``input_type`` 的**显式映射**。
#:
#: ``document`` 是「文档」这一大类，按其载体细分：表格类（Excel/CSV）→
#: ``spreadsheet``，其余（PDF/Word/文本文件）→ ``file``。这与模型的五值集合一一
#: 对齐，且不引入第五类输入。
INPUT_KIND_MODEL_TYPES: Mapping[InputKind, frozenset[str]] = {
    InputKind.TEXT: frozenset({"text"}),
    InputKind.VOICE: frozenset({"voice"}),
    InputKind.IMAGE: frozenset({"image"}),
    InputKind.DOCUMENT: frozenset({"file", "spreadsheet"}),
}


class ParserError(Exception):
    """解析器层错误基类。

    错误信息**不得**包含凭据值、附件原文或识别全文（W0-5 §3.1/§3.2）。
    """


class MissingCredentialError(ParserError):
    """缺少解析器凭据（环境变量未设置）。

    **fail closed**：构造即失败，绝不降级为「解析成功」。错误信息只提环境变量名，
    **不含凭据值**。
    """


class UnsupportedInputError(ParserError):
    """解析器不支持该输入类型。"""


def model_input_type(kind: InputKind, *, spreadsheet: bool = False) -> str:
    """把 ``InputKind`` 映射为 ``IntakeJob.input_type`` 取值。

    * ``text`` → ``text``；``voice`` → ``voice``；``image`` → ``image``；
    * ``document`` → ``spreadsheet``（``spreadsheet=True``，表格类载体）或
      ``file``（其余文档）。

    ``spreadsheet`` 只对 ``document`` 有意义；对其它类型传 ``True`` 直接
    ``ValueError``（不静默忽略，避免误用）。
    """
    resolved = InputKind(kind) if not isinstance(kind, InputKind) else kind
    if spreadsheet and resolved is not InputKind.DOCUMENT:
        raise ValueError("spreadsheet flag only applies to document inputs")
    if resolved is InputKind.DOCUMENT:
        return "spreadsheet" if spreadsheet else "file"
    return resolved.value


def input_kind_for_model_type(value: str) -> InputKind | None:
    """``IntakeJob.input_type`` 取值 → ``InputKind`` 的**反向**映射。

    ``file`` 与 ``spreadsheet`` 同属 ``document`` 大类。未识别取值返回 ``None``
    （不臆断；调用方据此 fail closed）。
    """
    if value in ("text",):
        return InputKind.TEXT
    if value in ("voice",):
        return InputKind.VOICE
    if value in ("image",):
        return InputKind.IMAGE
    if value in ("file", "spreadsheet"):
        return InputKind.DOCUMENT
    return None


# --- 裁决参数 -----------------------------------------------------------------

#: 字段置信度阈值：低于该值视为「低置信度」，要求人工确认。
#:
#: 该值是**保守默认**（宁可多要求人工确认，也不放过低质量提取）；真实阈值属产品
#: 参数，可在调用 ``decide_draft`` 时覆盖。
DEFAULT_CONFIDENCE_THRESHOLD = 0.75

#: 人工录入的字段置信度：由人逐字段录入，取最大置信度。
#:
#: 注意：**置信度高不等于可自动确认**——草稿 ``review_status`` 恒为 ``pending``，
#: 仍须人工确认后才可能进入正式记录（PLAN §4.2）。
MANUAL_ENTRY_CONFIDENCE = 1.0

MANUAL_ENTRY_PARSER_NAME = "manual_entry"
MANUAL_ENTRY_PARSER_VERSION = "1"

#: 按业务类型声明的**关键字段**：缺失即要求人工确认（PLAN §4.2「关键字段缺失」）。
#:
#: 当前仅给出报销（``expense_claim``）的保守默认：金额与发生日期是报销单成立的
#: 最低要素。具体口径属产品决策，可通过 ``decide_draft`` 的参数或
#: ``required_fields_for`` 覆盖，本表不是最终业务规则。
BUSINESS_REQUIRED_FIELDS: Mapping[str, tuple[str, ...]] = {
    "expense_claim": ("amount", "expense_date"),
}

#: 未登记业务类型的兜底关键字段：空集（不臆断未知业务的关键字段）。
DEFAULT_REQUIRED_FIELDS: tuple[str, ...] = ()


def required_fields_for(business_type: str) -> tuple[str, ...]:
    """返回 ``business_type`` 的关键字段（未登记类型返回 ``DEFAULT_REQUIRED_FIELDS``）。"""
    return BUSINESS_REQUIRED_FIELDS.get(business_type, DEFAULT_REQUIRED_FIELDS)


# --- 请求与结果 ---------------------------------------------------------------


@dataclass(frozen=True)
class ParseRequest:
    """一次解析请求（供应商无关）。

    * ``input_kind``：四类输入之一；
    * ``business_type``：目标业务类型（如 ``expense_claim``）；
    * ``source_file_id``：原件引用（``source_files.id``）。**图片/语音/文档输入必填**
      ——保证提取值可追溯到来源文件；文字输入可空（无附件时）；
    * ``text``：文字输入的文字内容（可选）。**不得写入日志**；
    * ``metadata``：非敏感的结构化元数据（如 MIME、页码）。**不得放附件原文**。
    """

    input_kind: InputKind
    business_type: str
    source_file_id: int | None = None
    text: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        resolved = (
            self.input_kind
            if isinstance(self.input_kind, InputKind)
            else InputKind(self.input_kind)
        )
        object.__setattr__(self, "input_kind", resolved)

        business_type = (
            self.business_type.strip()
            if isinstance(self.business_type, str)
            else ""
        )
        if not business_type:
            raise ValueError("business_type is required")
        object.__setattr__(self, "business_type", business_type)

        # 非文字输入必须携带原件引用，否则提取值无法追溯到来源文件（fail closed）。
        if resolved is not InputKind.TEXT and self.source_file_id is None:
            raise ValueError(
                f"{resolved.value} input requires source_file_id "
                "(original file reference)"
            )

        object.__setattr__(self, "metadata", dict(self.metadata))


@dataclass(frozen=True)
class ParseResult:
    """一次解析结果（供应商无关，**只承载草稿数据**）。

    * ``fields``：逐字段提取值；
    * ``field_confidence``：逐字段置信度，取值 ``[0.0, 1.0]``，**必须与 ``fields``
      的键集合完全一致**（保证「逐字段带置信度」）；
    * ``parser_name`` / ``parser_version``：用于追溯解析器版本；
    * ``source_file_id``：原件引用，与请求保持一致；
    * ``error``：机器可读的失败码（成功时为 ``None``）。**不得是原文**。

    结果**只进草稿区**：由 ``app.intake.pipeline.record_parse_draft`` 写入
    ``extraction_drafts``，绝不直接写正式业务字段。
    """

    input_kind: InputKind
    parser_name: str
    parser_version: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    field_confidence: Mapping[str, float] = field(default_factory=dict)
    source_file_id: int | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        resolved = (
            self.input_kind
            if isinstance(self.input_kind, InputKind)
            else InputKind(self.input_kind)
        )
        object.__setattr__(self, "input_kind", resolved)

        fields_copy = dict(self.fields)
        confidence_copy = {key: float(value) for key, value in self.field_confidence.items()}
        object.__setattr__(self, "fields", fields_copy)
        object.__setattr__(self, "field_confidence", confidence_copy)

        if self.error is None:
            missing_confidence = set(fields_copy) - set(confidence_copy)
            if missing_confidence:
                raise ValueError(
                    "every extracted field must carry a confidence value: "
                    f"missing {sorted(missing_confidence)}"
                )
            orphan_confidence = set(confidence_copy) - set(fields_copy)
            if orphan_confidence:
                raise ValueError(
                    "confidence provided for unknown fields: "
                    f"{sorted(orphan_confidence)}"
                )
            for name, value in confidence_copy.items():
                if not 0.0 <= value <= 1.0:
                    raise ValueError(
                        f"confidence for {name!r} must be within [0.0, 1.0], "
                        f"got {value!r}"
                    )

    @property
    def ok(self) -> bool:
        """解析是否成功（无失败码）。"""
        return self.error is None

    @classmethod
    def failed(
        cls,
        *,
        input_kind: InputKind,
        parser_name: str,
        parser_version: str,
        error: str,
        source_file_id: int | None = None,
    ) -> ParseResult:
        """构造一个**失败**结果（机器可读失败码，**不含原文**）。"""
        return cls(
            input_kind=input_kind,
            parser_name=parser_name,
            parser_version=parser_version,
            fields={},
            field_confidence={},
            source_file_id=source_file_id,
            error=error,
        )


# --- 解析接口 -----------------------------------------------------------------


class IntakeParser(ABC):
    """受理解析器的抽象接口（**可替换**，PLAN §4.2）。

    上层业务只依赖本接口：声音转写（ASR）、图片/票据识别（OCR）、文档抽取值、人工
    录入兜底都实现本接口。切换或新增供应商不影响业务域——与发票查验
    ``InvoiceVerificationProvider`` 同一思路。

    实现约束：

    * ``parse`` 是**只读**的：不得覆盖原始文件，不得写正式业务表；
    * 任何凭据只从环境变量读取、缺失即 fail closed（见 ``ExternalParser``）；
    * 结果里不得携带可用作日志的原文（原文只在草稿数据中）。
    """

    @property
    @abstractmethod
    def parser_name(self) -> str:
        """解析器标识（落库到审计，如 ``manual_entry``）。"""

    @property
    @abstractmethod
    def parser_version(self) -> str:
        """解析器版本（用于追溯「哪一版解析器产出了该值」）。"""

    @property
    @abstractmethod
    def parser_kind(self) -> ParserKind:
        """解析器种类（人工 / OCR / ASR / 文档）。"""

    @property
    @abstractmethod
    def supported_input_kinds(self) -> frozenset[InputKind]:
        """该解析器支持的输入类型集合。"""

    def supports(self, kind: InputKind) -> bool:
        """是否支持 ``kind``。"""
        resolved = kind if isinstance(kind, InputKind) else InputKind(kind)
        return resolved in self.supported_input_kinds

    @abstractmethod
    def parse(self, request: ParseRequest) -> ParseResult:
        """解析一次请求，返回供应商无关的 ``ParseResult``。"""


class ParserKind(str, Enum):
    """解析器种类（用于追溯与选择，非渠道绑定）。"""

    MANUAL_ENTRY = "manual_entry"  # 人工录入兜底
    OCR = "ocr"  # 图片/票据文字识别
    ASR = "asr"  # 语音转写
    DOCUMENT = "document"  # 文档解析（PDF/Word/Excel）
    COMPOSITE = "composite"  # 组合式（如上列多类）


#: 人工录入兜底支持全部四类输入（无供应商也可工作）。
_MANUAL_SUPPORTED_KINDS: frozenset[InputKind] = frozenset(
    {InputKind.TEXT, InputKind.VOICE, InputKind.IMAGE, InputKind.DOCUMENT}
)


class ManualEntryParser(IntakeParser):
    """**人工录入兜底**实现（PLAN §4.2 的「要求人工确认」路径）。

    它**不依赖任何外部服务**：授权人员逐字段录入取值，本类把这些取值包装成与其它
    解析器同形的 ``ParseResult``。它保证「OCR/ASR 供应商未接入时流水线仍可工作」。

    取值由构造参数的 ``values`` 提供（代表操作员为本次受理录入的内容），字段置信度
    默认取 ``MANUAL_ENTRY_CONFIDENCE``，也可逐字段覆盖。人工录入**不改变**「人工
    确认是进入正式记录的唯一路径」：写入的草稿 ``review_status`` 仍为 ``pending``。
    """

    def __init__(
        self,
        *,
        values: Mapping[str, Any],
        field_confidence: Mapping[str, float] | None = None,
        parser_version: str = MANUAL_ENTRY_PARSER_VERSION,
    ) -> None:
        self._values = dict(values)
        provided = dict(field_confidence or {})
        unknown = set(provided) - set(self._values)
        if unknown:
            raise ValueError(
                f"confidence provided for unknown fields: {sorted(unknown)}"
            )
        self._confidence = {
            name: float(provided.get(name, MANUAL_ENTRY_CONFIDENCE))
            for name in self._values
        }
        self._parser_version = parser_version

    @property
    def parser_name(self) -> str:
        return MANUAL_ENTRY_PARSER_NAME

    @property
    def parser_version(self) -> str:
        return self._parser_version

    @property
    def parser_kind(self) -> ParserKind:
        return ParserKind.MANUAL_ENTRY

    @property
    def supported_input_kinds(self) -> frozenset[InputKind]:
        return _MANUAL_SUPPORTED_KINDS

    def parse(self, request: ParseRequest) -> ParseResult:
        if not self.supports(request.input_kind):
            raise UnsupportedInputError(
                f"{self.parser_name} does not support {request.input_kind.value} input"
            )
        return ParseResult(
            input_kind=request.input_kind,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=self._values,
            field_confidence=self._confidence,
            source_file_id=request.source_file_id,
        )


class ExternalParser(IntakeParser):
    """外部解析供应商（OCR/ASR/文档）的**基类**：凭据 fail closed（PLAN §4.2）。

    **本任务不实现任何真实供应商**，只固定未来接入必须遵守的约束：

    * 子类用类属性 ``credential_env_vars`` 声明所需环境变量名；
    * 构造时**只从环境变量读取**凭据（可注入 ``env`` 供测试），任一缺失/空白即抛
      ``MissingCredentialError``（fail closed），错误信息**只提环境变量名，不含
      凭据值**；凭据保存在私有属性中，**绝不写日志、绝不落库**；
    * ``parse`` 统一做错误收敛：不支持的类型抛 ``UnsupportedInputError``；子类
      ``_parse`` 抛出的任意异常被转为**失败结果**（机器可读失败码），**不泄漏
      异常原文**。

    子类只需实现 ``_parse(request, credentials) -> ParseResult`` 与接口属性。
    """

    #: 子类声明所需环境变量名（按顺序校验）。
    credential_env_vars: tuple[str, ...] = ()

    def __init__(self, *, env: Mapping[str, str] | None = None) -> None:
        source = os.environ if env is None else env
        resolved: dict[str, str] = {}
        for name in self.credential_env_vars:
            value = source.get(name)
            if value is None or not str(value).strip():
                raise MissingCredentialError(
                    "missing parser credential: set "
                    f"{name} in the environment"
                )
            resolved[name] = str(value).strip()
        # 私有：绝不外泄、绝不用于日志/审计。
        self._credentials: Mapping[str, str] = resolved

    @property
    def credentials_configured(self) -> bool:
        """是否已配置全部声明凭据（构造成功即恒为 ``True``；不暴露凭据值）。"""
        return all(name in self._credentials for name in self.credential_env_vars)

    def parse(self, request: ParseRequest) -> ParseResult:
        if not self.supports(request.input_kind):
            raise UnsupportedInputError(
                f"{self.parser_name} does not support {request.input_kind.value} input"
            )
        try:
            return self._parse(request, self._credentials)
        except ParserError:
            raise
        except Exception:
            # 供应商侧任意异常 → 失败结果；不泄漏异常原文（可能含附件内容）。
            return ParseResult.failed(
                input_kind=request.input_kind,
                parser_name=self.parser_name,
                parser_version=self.parser_version,
                error="provider_error",
                source_file_id=request.source_file_id,
            )

    @abstractmethod
    def _parse(
        self, request: ParseRequest, credentials: Mapping[str, str]
    ) -> ParseResult:
        """用已解析的凭据执行一次解析（子类实现，**不得**记录凭据或原文）。"""


class ParserRegistry:
    """解析器登记表：按输入类型解析到具体解析器（**替换点**）。

    ``resolve`` 返回 ``None`` 表示该输入类型尚无供应商解析器——调用方应回退到
    ``ManualEntryParser``（人工录入兜底），从而保证「无供应商也可工作」。
    """

    def __init__(self, parsers: Iterable[IntakeParser] = ()) -> None:
        self._by_kind: dict[InputKind, IntakeParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: IntakeParser) -> None:
        """登记解析器；同一类型后登记者覆盖先前登记者（显式替换）。"""
        for kind in parser.supported_input_kinds:
            self._by_kind[kind] = parser

    def resolve(self, kind: InputKind) -> IntakeParser | None:
        """返回支持 ``kind`` 的解析器；无匹配返回 ``None``（由上层回退到人工录入）。"""
        resolved = kind if isinstance(kind, InputKind) else InputKind(kind)
        return self._by_kind.get(resolved)

    @property
    def registered_kinds(self) -> frozenset[InputKind]:
        """已登记的输入类型集合。"""
        return frozenset(self._by_kind)


# --- 裁决与草稿载荷 -----------------------------------------------------------


@dataclass(frozen=True)
class DraftDecision:
    """由一次 ``ParseResult`` 得出的草稿裁决。

    * ``job_status``：``IntakeJob.status`` 的目标值——``parsed`` / ``needs_review``
      / ``failed``；
    * ``review_status``：草稿的 ``review_status``——**恒为** ``pending``（人工确认
      是进入正式记录的唯一路径）；
    * ``missing_fields``：关键字段中缺失的字段名；
    * ``low_confidence_fields``：置信度低于阈值的字段名；
    * ``requires_human_review``：是否需要人工确认（低置信度或关键字段缺失、或解析
      失败）。
    """

    job_status: str
    review_status: str
    missing_fields: tuple[str, ...]
    low_confidence_fields: tuple[str, ...]
    requires_human_review: bool


def _is_missing_value(value: Any) -> bool:
    """值是否视为「缺失」：``None`` / 空白字符串 / 空集合容器。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return len(value) == 0
    return False


def decide_draft(
    result: ParseResult,
    *,
    required_fields: Iterable[str] = (),
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
) -> DraftDecision:
    """按解析结果裁决 ``IntakeJob.status`` 与草稿 ``review_status``（PLAN §4.2）。

    规则：

    * 解析**失败**（``result.error is not None``）→ ``job_status='failed'``，不产出
      草稿，标记需人工处理（转人工录入兜底）；
    * 关键字段**缺失**（字段不存在或值为空）→ ``needs_review``；
    * 存在**低置信度**字段（``< confidence_threshold``）→ ``needs_review``；
    * 否则 → ``parsed``。

    无论哪种情况，``review_status`` **恒为** ``'pending'``：本层绝不自动确认，更不
    写正式业务字段。若传入的 ``required_fields`` 含未在结果中出现的字段，一律计为
    缺失（fail closed）。
    """
    required = tuple(dict.fromkeys(str(name) for name in required_fields))

    if not result.ok:
        return DraftDecision(
            job_status="failed",
            review_status="pending",
            missing_fields=required,
            low_confidence_fields=(),
            requires_human_review=True,
        )

    missing: list[str] = []
    for name in required:
        if name not in result.fields or _is_missing_value(result.fields[name]):
            missing.append(name)

    low: list[str] = [
        name
        for name, confidence in result.field_confidence.items()
        if confidence < confidence_threshold
    ]

    requires_human_review = bool(missing or low)
    return DraftDecision(
        job_status="needs_review" if requires_human_review else "parsed",
        review_status="pending",
        missing_fields=tuple(sorted(missing)),
        low_confidence_fields=tuple(sorted(low)),
        requires_human_review=requires_human_review,
    )


def build_draft_payload(
    *,
    intake_job_id: int,
    business_type: str,
    result: ParseResult,
) -> dict[str, Any]:
    """构建 ``ExtractionDraft`` 的**列载荷**（纯函数，不触碰数据库）。

    返回的字典可直接用于 ``ExtractionDraft(**payload, ...)``：

    * ``extracted_json`` = 逐字段提取值（``result.fields``）；
    * ``confidence_json`` = 逐字段置信度（``result.field_confidence``）；
    * ``source_file_id`` = 原件引用（**原件不被覆盖**，只引用）；
    * ``review_status`` = ``'pending'``（恒不自动确认）。

    **不包含任何正式业务字段**：调用方只能把它写进 ``extraction_drafts``。
    """
    return {
        "intake_job_id": intake_job_id,
        "source_file_id": result.source_file_id,
        "business_type": business_type,
        "extracted_json": dict(result.fields),
        "confidence_json": dict(result.field_confidence),
        "review_status": "pending",
    }


# --- 追溯与脱敏 ---------------------------------------------------------------


def parse_provenance(result: ParseResult) -> dict[str, Any]:
    """构建**可写审计**的解析追溯信息（`details` 级，非敏感）。

    只含：解析器标识/版本/种类、输入类型、原件引用、字段名清单、失败码。**绝不含**
    字段取值、附件原文或识别全文（W0-5 §3.1/§3.2）。每项提取值据此可追溯到来源
    文件与解析器版本；确认人由 ``app.intake.pipeline`` 在确认时以审计 ``actor_id``
    记录。
    """
    return {
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "input_kind": result.input_kind.value,
        "source_file_id": result.source_file_id,
        "field_names": sorted(result.fields),
        "error": result.error,
    }


def safe_log_summary(result: ParseResult) -> dict[str, Any]:
    """构建**可写日志**的安全摘要（非敏感）。

    只含解析器标识/版本、输入类型、原件引用、字段名与计数、失败码——**不含任何
    字段取值或原文**，满足「日志不得记录附件原文或识别全文」。
    """
    return {
        "parser_name": result.parser_name,
        "parser_version": result.parser_version,
        "input_kind": result.input_kind.value,
        "source_file_id": result.source_file_id,
        "field_names": sorted(result.fields),
        "field_count": len(result.fields),
        "error": result.error,
    }
