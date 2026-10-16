"""W1-26 受理解析接口 / 人工录入兜底 / 裁决与追溯的**纯逻辑**测试。

本文件**不触碰数据库、不调用任何外部服务**，可脱离 PostgreSQL 运行：

    python -m pytest app/tests/intake/test_parsing_pipeline.py -q \
        -o addopts="" --noconftest

覆盖 Issue #3 验收第 6 条的五类场景：成功、低置信度、解析失败、凭据缺失、原件
追溯；另覆盖四类输入 → ``input_type`` 映射、请求校验、草稿载荷、脱敏摘要，以及
「日志/审计不得记录附件原文或识别全文」。
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.intake.parsing import (
    BUSINESS_REQUIRED_FIELDS,
    DEFAULT_REQUIRED_FIELDS,
    INPUT_KIND_MODEL_TYPES,
    MANUAL_ENTRY_CONFIDENCE,
    MODEL_INPUT_TYPES,
    ExternalParser,
    InputKind,
    ManualEntryParser,
    MissingCredentialError,
    ParseRequest,
    ParseResult,
    ParserKind,
    ParserRegistry,
    UnsupportedInputError,
    build_draft_payload,
    decide_draft,
    input_kind_for_model_type,
    model_input_type,
    parse_provenance,
    required_fields_for,
    safe_log_summary,
)

#: 一段可识别的「附件原文/识别全文」样本：任何日志/审计/摘要都不得包含它。
RAW_ATTACHMENT_TEXT = "发票号码 00001111 合计金额 123.45 附件原文"

#: 可识别的凭据样本：任何日志/审计/摘要都不得包含它。
FAKE_CREDENTIAL_VALUE = "super-secret-credential-value"

OCR_KEY_ENV = "AJAIMGR_TEST_OCR_API_KEY"

IMAGE_REQUEST = ParseRequest(
    input_kind=InputKind.IMAGE,
    business_type="expense_claim",
    source_file_id=42,
)


# --- 四类输入 → input_type 映射 ------------------------------------------------


def test_model_input_type_maps_all_four_kinds() -> None:
    """四类输入映射到模型五值 input_type；document 按载体细分。"""
    assert model_input_type(InputKind.TEXT) == "text"
    assert model_input_type(InputKind.VOICE) == "voice"
    assert model_input_type(InputKind.IMAGE) == "image"
    assert model_input_type(InputKind.DOCUMENT) == "file"
    assert model_input_type(InputKind.DOCUMENT, spreadsheet=True) == "spreadsheet"
    # Raw string is coerced too.
    assert model_input_type("voice") == "voice"


def test_model_input_type_rejects_spreadsheet_for_non_document() -> None:
    """spreadsheet 只对 document 有意义，其它类型显式误用即报错。"""
    with pytest.raises(ValueError):
        model_input_type(InputKind.TEXT, spreadsheet=True)
    with pytest.raises(ValueError):
        model_input_type(InputKind.IMAGE, spreadsheet=True)


def test_input_kind_mapping_is_exhaustive_and_sound() -> None:
    """映射覆盖四类输入，取值全在模型五值集合内，且并集等于五值集合。"""
    assert set(INPUT_KIND_MODEL_TYPES) == set(InputKind)
    covered: set[str] = set()
    for types in INPUT_KIND_MODEL_TYPES.values():
        assert types <= MODEL_INPUT_TYPES
        covered |= set(types)
    assert covered == MODEL_INPUT_TYPES


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("text", InputKind.TEXT),
        ("voice", InputKind.VOICE),
        ("image", InputKind.IMAGE),
        ("file", InputKind.DOCUMENT),
        ("spreadsheet", InputKind.DOCUMENT),
        ("unknown", None),
        ("", None),
    ],
)
def test_input_kind_for_model_type(value: str, expected: InputKind | None) -> None:
    """反向映射：file/spreadsheet 都归入 document；未识别取值为 None。"""
    assert input_kind_for_model_type(value) is expected


# --- 请求与结果校验 -----------------------------------------------------------


@pytest.mark.parametrize(
    "kind",
    [InputKind.VOICE, InputKind.IMAGE, InputKind.DOCUMENT],
)
def test_non_text_request_requires_source_file(kind: InputKind) -> None:
    """图片/语音/文档输入缺原件引用直接失败（保证可追溯来源文件）。"""
    with pytest.raises(ValueError):
        ParseRequest(input_kind=kind, business_type="expense_claim")


def test_text_request_without_source_file_is_allowed() -> None:
    """纯文字输入无附件时允许缺 source_file_id。"""
    request = ParseRequest(
        input_kind=InputKind.TEXT, business_type="expense_claim", text="打车 123.45"
    )
    assert request.source_file_id is None
    assert request.input_kind is InputKind.TEXT


def test_request_requires_business_type() -> None:
    """业务类型必填。"""
    with pytest.raises(ValueError):
        ParseRequest(input_kind=InputKind.TEXT, business_type="  ")


def test_result_requires_confidence_for_every_field() -> None:
    """逐字段带置信度：字段与置信度键集合必须一致。"""
    with pytest.raises(ValueError):
        ParseResult(
            input_kind=InputKind.TEXT,
            parser_name="p",
            parser_version="1",
            fields={"amount": "1.00"},
            field_confidence={},
        )
    with pytest.raises(ValueError):
        ParseResult(
            input_kind=InputKind.TEXT,
            parser_name="p",
            parser_version="1",
            fields={},
            field_confidence={"amount": 0.9},
        )


def test_result_rejects_out_of_range_confidence() -> None:
    """置信度必须在 [0.0, 1.0]。"""
    with pytest.raises(ValueError):
        ParseResult(
            input_kind=InputKind.TEXT,
            parser_name="p",
            parser_version="1",
            fields={"amount": "1.00"},
            field_confidence={"amount": 1.5},
        )


# --- 人工录入兜底 -------------------------------------------------------------


def test_manual_entry_parser_produces_draft_fields() -> None:
    """人工录入兜底：不依赖外部服务，逐字段产出取值与高置信度。"""
    parser = ManualEntryParser(
        values={"amount": "123.45", "expense_date": "2026-10-01"}
    )
    assert parser.parser_kind is ParserKind.MANUAL_ENTRY
    assert parser.parser_name == "manual_entry"
    assert parser.supports(InputKind.IMAGE)
    assert parser.supports(InputKind.VOICE)
    assert parser.supports(InputKind.DOCUMENT)

    result = parser.parse(IMAGE_REQUEST)
    assert result.ok
    assert result.fields == {"amount": "123.45", "expense_date": "2026-10-01"}
    assert set(result.field_confidence) == {"amount", "expense_date"}
    assert all(
        value == MANUAL_ENTRY_CONFIDENCE
        for value in result.field_confidence.values()
    )
    assert result.source_file_id == 42


def test_manual_entry_parser_allows_per_field_confidence() -> None:
    """人工录入可逐字段给出置信度（例如抄录不确定的字段给低值）。"""
    parser = ManualEntryParser(
        values={"amount": "1.00", "note": "x"},
        field_confidence={"note": 0.4},
    )
    result = parser.parse(
        ParseRequest(
            input_kind=InputKind.TEXT, business_type="expense_claim"
        )
    )
    assert result.field_confidence == {
        "amount": MANUAL_ENTRY_CONFIDENCE,
        "note": 0.4,
    }


def test_manual_entry_parser_rejects_unknown_confidence() -> None:
    """给不存在的字段配置信度应报错。"""
    with pytest.raises(ValueError):
        ManualEntryParser(values={"amount": "1.00"}, field_confidence={"nope": 0.5})


# --- 裁决：成功 / 低置信度 / 关键字段缺失 / 解析失败 ---------------------------


def _result(
    fields: Mapping[str, object],
    confidence: Mapping[str, float],
    **overrides: object,
) -> ParseResult:
    params: dict[str, object] = {
        "input_kind": InputKind.IMAGE,
        "parser_name": "stub",
        "parser_version": "9",
        "fields": fields,
        "field_confidence": confidence,
        "source_file_id": 42,
    }
    params.update(overrides)
    return ParseResult(**params)  # type: ignore[arg-type]


def test_decide_success_when_complete_and_confident() -> None:
    """字段齐全且置信度达标 → parsed，review_status 仍为 pending。"""
    result = _result(
        {"amount": "123.45", "expense_date": "2026-10-01"},
        {"amount": 0.98, "expense_date": 0.95},
    )
    decision = decide_draft(result, required_fields=("amount", "expense_date"))
    assert decision.job_status == "parsed"
    assert decision.review_status == "pending"
    assert decision.requires_human_review is False
    assert decision.missing_fields == ()
    assert decision.low_confidence_fields == ()


def test_decide_low_confidence_requires_review() -> None:
    """低置信度 → needs_review，且列出低置信度字段。"""
    result = _result(
        {"amount": "123.45", "expense_date": "2026-10-01"},
        {"amount": 0.30, "expense_date": 0.99},
    )
    decision = decide_draft(result, required_fields=("amount", "expense_date"))
    assert decision.job_status == "needs_review"
    assert decision.requires_human_review is True
    assert decision.low_confidence_fields == ("amount",)
    assert decision.review_status == "pending"


def test_decide_missing_key_field_requires_review() -> None:
    """关键字段缺失（含空值）→ needs_review，且列出缺失字段。"""
    result = _result(
        {"amount": "", "note": "x"},
        {"amount": 0.99, "note": 0.99},
    )
    decision = decide_draft(result, required_fields=("amount", "expense_date"))
    assert decision.job_status == "needs_review"
    assert decision.missing_fields == ("amount", "expense_date")
    assert decision.requires_human_review is True


def test_decide_threshold_is_configurable() -> None:
    """阈值可覆盖：0.9 的置信度在高阈值下也算低。"""
    result = _result({"amount": "1.00"}, {"amount": 0.9})
    assert decide_draft(result, confidence_threshold=0.95).job_status == (
        "needs_review"
    )
    assert decide_draft(result, confidence_threshold=0.5).job_status == "parsed"


def test_decide_failure_marks_job_failed() -> None:
    """解析失败 → failed，标记需人工处理（转人工录入兜底），不产出可用字段。"""
    failed = ParseResult.failed(
        input_kind=InputKind.VOICE,
        parser_name="stub",
        parser_version="1",
        error="provider_error",
        source_file_id=42,
    )
    decision = decide_draft(failed, required_fields=("amount",))
    assert decision.job_status == "failed"
    assert decision.requires_human_review is True
    assert decision.missing_fields == ("amount",)
    assert decision.review_status == "pending"


def test_required_fields_for_unknown_business_type_is_empty() -> None:
    """未登记业务类型不臆断关键字段。"""
    assert required_fields_for("unknown_type") == DEFAULT_REQUIRED_FIELDS
    assert required_fields_for("expense_claim") == BUSINESS_REQUIRED_FIELDS[
        "expense_claim"
    ]


# --- 草稿载荷 -----------------------------------------------------------------


def test_build_draft_payload_only_targets_draft_columns() -> None:
    """载荷只含草稿列：逐字段值/置信度、原件引用、pending 状态。"""
    result = _result(
        {"amount": "123.45"}, {"amount": 0.98}
    )
    payload = build_draft_payload(
        intake_job_id=7, business_type="expense_claim", result=result
    )
    assert set(payload) == {
        "intake_job_id",
        "source_file_id",
        "business_type",
        "extracted_json",
        "confidence_json",
        "review_status",
    }
    assert payload["intake_job_id"] == 7
    assert payload["source_file_id"] == 42
    assert payload["business_type"] == "expense_claim"
    assert payload["extracted_json"] == {"amount": "123.45"}
    assert payload["confidence_json"] == {"amount": 0.98}
    assert payload["review_status"] == "pending"


# --- 凭据 fail closed（外部供应商基类） ---------------------------------------


class _StubOcrParser(ExternalParser):
    """测试桩：冒充未来 OCR 供应商，验证凭据 fail closed 与错误收敛。"""

    credential_env_vars = (OCR_KEY_ENV,)

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        payload: Mapping[str, object] | None = None,
        boom: bool = False,
    ) -> None:
        self._payload = dict(payload or {})
        self._boom = boom
        super().__init__(env=env)

    @property
    def parser_name(self) -> str:
        return "stub_ocr"

    @property
    def parser_version(self) -> str:
        return "0.1-test"

    @property
    def parser_kind(self) -> ParserKind:
        return ParserKind.OCR

    @property
    def supported_input_kinds(self) -> frozenset[InputKind]:
        return frozenset({InputKind.IMAGE})

    def _parse(
        self, request: ParseRequest, credentials: Mapping[str, str]
    ) -> ParseResult:
        if self._boom:
            raise RuntimeError(f"upstream blew up with {RAW_ATTACHMENT_TEXT}")
        return ParseResult(
            input_kind=request.input_kind,
            parser_name=self.parser_name,
            parser_version=self.parser_version,
            fields=self._payload,
            field_confidence={name: 0.9 for name in self._payload},
            source_file_id=request.source_file_id,
        )


def test_external_parser_fails_closed_without_credentials() -> None:
    """凭据缺失 → 构造即失败，错误信息只提环境变量名。"""
    with pytest.raises(MissingCredentialError) as exc:
        _StubOcrParser(env={})
    assert OCR_KEY_ENV in str(exc.value)

    # 空白值同样视为缺失。
    with pytest.raises(MissingCredentialError):
        _StubOcrParser(env={OCR_KEY_ENV: "   "})


def test_external_parser_reads_credentials_from_env_only() -> None:
    """凭据只从环境变量读取；缺失即失败，决不从其它途径降级。"""
    # No env, no os.environ fallback value present -> fail closed.
    with pytest.raises(MissingCredentialError):
        _StubOcrParser(env={})

    parser = _StubOcrParser(
        env={OCR_KEY_ENV: FAKE_CREDENTIAL_VALUE}, payload={"amount": "1.00"}
    )
    assert parser.credentials_configured is True
    result = parser.parse(IMAGE_REQUEST)
    assert result.ok and result.fields == {"amount": "1.00"}


def test_external_parser_never_leaks_credential_or_payload() -> None:
    """凭据值与识别原文不得出现在结果摘要/追溯/日志文本中。"""
    parser = _StubOcrParser(
        env={OCR_KEY_ENV: FAKE_CREDENTIAL_VALUE},
        payload={"amount": "1.00", "raw": RAW_ATTACHMENT_TEXT},
    )
    result = parser.parse(IMAGE_REQUEST)
    for rendered in (
        str(result),
        str(safe_log_summary(result)),
        str(parse_provenance(result)),
    ):
        assert FAKE_CREDENTIAL_VALUE not in rendered
    # Field *names* may appear; field *values* must not.
    summary = safe_log_summary(result)
    assert "raw" in summary["field_names"]
    assert RAW_ATTACHMENT_TEXT not in str(summary)
    assert "1.00" not in str(summary)


def test_external_parser_maps_provider_exception_to_failure() -> None:
    """供应商异常收敛为失败结果，绝不泄漏异常原文。"""
    parser = _StubOcrParser(env={OCR_KEY_ENV: "x"}, boom=True)
    result = parser.parse(IMAGE_REQUEST)
    assert result.ok is False
    assert result.error == "provider_error"
    assert result.fields == {}
    assert RAW_ATTACHMENT_TEXT not in str(result)


def test_external_parser_rejects_unsupported_input() -> None:
    """不支持的类型显式报错（不静默）。"""
    parser = _StubOcrParser(env={OCR_KEY_ENV: "x"})
    with pytest.raises(UnsupportedInputError):
        parser.parse(
            ParseRequest(
                input_kind=InputKind.TEXT, business_type="expense_claim"
            )
        )


# --- 脱敏摘要与追溯 -----------------------------------------------------------


def test_safe_log_summary_excludes_values() -> None:
    """日志摘要只含字段名/计数/解析器标识，不含取值或原文。"""
    result = _result(
        {"amount": "123.45", "raw": RAW_ATTACHMENT_TEXT},
        {"amount": 0.9, "raw": 0.9},
    )
    summary = safe_log_summary(result)
    assert summary["field_names"] == ["amount", "raw"]
    assert summary["field_count"] == 2
    assert summary["input_kind"] == "image"
    assert summary["source_file_id"] == 42
    assert summary["parser_version"] == "9"
    rendered = str(summary)
    assert RAW_ATTACHMENT_TEXT not in rendered
    assert "123.45" not in rendered


def test_parse_provenance_records_parser_version_and_source() -> None:
    """追溯信息含解析器版本与来源文件，不含取值。"""
    result = _result({"amount": "123.45"}, {"amount": 0.9})
    provenance = parse_provenance(result)
    assert provenance["parser_name"] == "stub"
    assert provenance["parser_version"] == "9"
    assert provenance["source_file_id"] == 42
    assert provenance["field_names"] == ["amount"]
    assert provenance["error"] is None
    assert "123.45" not in str(provenance)


# --- 解析器登记表（可替换点） -------------------------------------------------


def test_registry_resolves_registered_parser_and_falls_back_otherwise() -> None:
    """登记后按类型命中；未登记类型返回 None（上层回退人工录入兜底）。"""
    stub = _StubOcrParser(env={OCR_KEY_ENV: "x"})
    registry = ParserRegistry([stub])
    assert registry.resolve(InputKind.IMAGE) is stub
    assert registry.registered_kinds == frozenset({InputKind.IMAGE})
    # No provider registered for voice -> None -> manual entry fallback.
    assert registry.resolve(InputKind.VOICE) is None

    fallback = ManualEntryParser(values={"amount": "1.00"})
    request = ParseRequest(
        input_kind=InputKind.VOICE,
        business_type="expense_claim",
        source_file_id=5,
    )
    assert registry.resolve(request.input_kind) is None
    result = fallback.parse(request)
    assert result.ok
    assert result.fields == {"amount": "1.00"}
    assert result.parser_name == "manual_entry"


def test_registry_last_registration_wins() -> None:
    """后登记者覆盖先登记者（显式替换供应商）。"""
    first = _StubOcrParser(env={OCR_KEY_ENV: "x"}, payload={"a": 1})
    second = _StubOcrParser(env={OCR_KEY_ENV: "y"}, payload={"b": 2})
    registry = ParserRegistry([first, second])
    assert registry.resolve(InputKind.IMAGE) is second
