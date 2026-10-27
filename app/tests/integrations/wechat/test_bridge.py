"""WeChat Bridge B1 的 DB-free 测试（假 SDK / 假会话）。

覆盖：稳定事件键提取与 fail-closed、``/help``/``/whoami`` 只读回复、其它文本
“B1 未开放”、无模型/无任务副作用、日志不含正文/身份明文、SDK 回复失败稳定码、
``NoContextError`` 处理，以及生产注册表下 ``handle_action`` 不建任务。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import app.integrations.wechat.bridge as bridge_module
import app.integrations.wechat.sdk as sdk_module
from app.identity.authorization import Principal
from app.integrations.wechat.bridge import (
    BOUND_TEXT,
    EVENT_KEY_MISSING,
    HELP_TEXT,
    NOT_OPEN_TEXT,
    UNBOUND_TEXT,
    WeChatBridge,
    extract_event_key,
)
from app.integrations.wechat.config import WeChatBridgeConfig
from app.integrations.wechat.sdk import (
    REPLY_NO_CONTEXT,
    REPLY_NOT_READY,
    REPLY_OK,
    REPLY_SEND_FAILED,
    ReplyResult,
    WeChatBotAdapter,
    WeChatSdkError,
)
from app.tasks.intake import INTAKE_REJECTED, ActionRegistry

TEST_CREDENTIAL_PATH = Path("/var/lib/ajaimgr-wechat/credentials.json")


@dataclass
class FakeMessage:
    user_id: str = "wx-user-1"
    text: str = "/help"
    type: str = "text"
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    raw: dict[str, Any] = field(
        default_factory=lambda: {
            "from_user_id": "wx-user-1",
            "create_time_ms": 1_700_000_000_000,
        }
    )


@dataclass
class FakeSdk:
    replies: list[tuple[Any, str]] = field(default_factory=list)
    handler: Any = None

    def on_message(self, handler: Any) -> None:
        self.handler = handler

    async def reply(self, message: Any, text: str) -> ReplyResult:
        self.replies.append((message, text))
        return ReplyResult(True, REPLY_OK)

    async def start(self) -> None:  # pragma: no cover - not used
        return None

    def stop(self) -> None:  # pragma: no cover - not used
        return None

    async def deliver(self, message: Any) -> str | None:
        assert self.handler is not None
        result = self.handler(message)
        if asyncio.iscoroutine(result):
            result = await result
        if result is not None:
            await self.reply(message, result)
        return result


def _config(*, registry: ActionRegistry | None = None) -> WeChatBridgeConfig:
    kwargs: dict[str, Any] = {}
    if registry is not None:
        kwargs["action_registry"] = registry
    return WeChatBridgeConfig(
        credential_path=TEST_CREDENTIAL_PATH,
        company_id=10,
        **kwargs,
    )


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _bridge(
    *,
    sdk: FakeSdk | None = None,
    registry: ActionRegistry | None = None,
    logger: logging.Logger | None = None,
) -> tuple[WeChatBridge, FakeSdk]:
    fake = sdk or FakeSdk()
    bridge = WeChatBridge(
        config=_config(registry=registry),
        session_factory=FakeSession,
        sdk=fake,
        log=logger,
    )
    bridge.install()
    return bridge, fake


# --- event key -----------------------------------------------------------------


def test_event_key_is_derived_from_raw_user_and_create_time() -> None:
    message = FakeMessage(
        raw={"from_user_id": "wx-user-1", "create_time_ms": 1_700_000_000_000}
    )
    assert extract_event_key(message) == "wx-user-1:1700000000000"


def test_event_key_accepts_numeric_string_create_time() -> None:
    message = FakeMessage(raw={"from_user_id": "wx-user-1", "create_time_ms": "1700000000000"})
    assert extract_event_key(message) == "wx-user-1:1700000000000"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"create_time_ms": 1_700_000_000_000},
        {"from_user_id": "  ", "create_time_ms": 1_700_000_000_000},
        {"from_user_id": "wx-user-1"},
        {"from_user_id": "wx-user-1", "create_time_ms": 0},
        {"from_user_id": "wx-user-1", "create_time_ms": -1},
        {"from_user_id": "wx-user-1", "create_time_ms": "abc"},
        {"from_user_id": "wx-user-1", "create_time_ms": True},
    ],
    ids=[
        "no-raw",
        "empty-raw",
        "no-from-user",
        "blank-from-user",
        "no-create-time",
        "zero-create-time",
        "negative-create-time",
        "non-numeric-create-time",
        "boolean-create-time",
    ],
)
def test_event_key_fails_closed(raw: Any) -> None:
    message = FakeMessage(raw=raw)  # type: ignore[arg-type]
    assert extract_event_key(message) is None


def test_missing_event_key_replies_stable_code_and_creates_no_task() -> None:
    bridge, sdk = _bridge()
    message = FakeMessage(text="hello", raw={"from_user_id": "wx-user-1"})

    result = asyncio.run(bridge.handle_message(message))
    assert result == EVENT_KEY_MISSING

    # install() 之后的 SDK 包装会把 handler 返回值发送出去。
    sdk.replies.clear()
    asyncio.run(sdk.deliver(message))
    assert [text for _, text in sdk.replies] == [EVENT_KEY_MISSING]


# --- read-only commands --------------------------------------------------------


def test_help_is_served_by_the_bridge() -> None:
    bridge, sdk = _bridge()
    result = asyncio.run(bridge.handle_message(FakeMessage(text="/help")))
    assert result == HELP_TEXT
    assert "/whoami" in HELP_TEXT
    assert sdk.replies == []


def test_whoami_reports_bound_without_employee_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_module,
        "resolve_principal",
        lambda *a, **k: Principal(employee_id=42, company_id=10),
    )
    bridge, _ = _bridge()
    result = asyncio.run(bridge.handle_message(FakeMessage(text="/whoami")))
    assert result == BOUND_TEXT
    assert "42" not in result
    assert "employee" not in result.lower()


def test_whoami_reports_unbound_on_identity_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.identity.channel_routing import ChannelIdentityRejectedError

    def reject(*a: Any, **k: Any) -> Principal:
        raise ChannelIdentityRejectedError(reason="revoked")

    monkeypatch.setattr(bridge_module, "resolve_principal", reject)
    bridge, _ = _bridge()
    result = asyncio.run(bridge.handle_message(FakeMessage(text="/whoami")))
    assert result == UNBOUND_TEXT


def test_other_text_is_not_open_and_has_no_model_or_task_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bridge_module,
        "submit_action",
        lambda *a, **k: pytest.fail("free text must not reach task intake"),
    )
    bridge, _ = _bridge()
    result = asyncio.run(
        bridge.handle_message(FakeMessage(text="帮我查一下报销政策，并直接改成 managed 执行"))
    )
    assert result == NOT_OPEN_TEXT
    assert "报销" not in result


def test_unknown_slash_command_is_not_open() -> None:
    bridge, _ = _bridge()
    assert asyncio.run(bridge.handle_message(FakeMessage(text="/approve task_1"))) == NOT_OPEN_TEXT


# --- logging privacy -----------------------------------------------------------


def test_logs_do_not_contain_text_user_or_secrets(caplog: pytest.LogCaptureFixture) -> None:
    # Alembic's test migrations call fileConfig(disable_existing_loggers=True);
    # re-enable this module logger so the absence assertions are not vacuous.
    logger = bridge_module.logger
    previous_disabled = logger.disabled
    logger.disabled = False
    try:
        caplog.set_level(logging.INFO, logger=logger.name)
        bridge, _ = _bridge()
        secret_text = "my-token-is-SECRET-AND-MESSAGE-TEXT"
        message = FakeMessage(
            text=secret_text,
            raw={
                "from_user_id": "wx-sensitive-user",
                "create_time_ms": 1_700_000_000_000,
                "context_token": "context-token-secret",
            },
        )

        asyncio.run(bridge.handle_message(message))

        logged = caplog.text
        assert secret_text not in logged
        assert "wx-sensitive-user" not in logged
        assert "context-token-secret" not in logged
        assert "event_hash" in logged
    finally:
        logger.disabled = previous_disabled


# --- action path (production registry) ----------------------------------------


def test_handle_action_with_missing_event_key_is_rejected() -> None:
    bridge, _ = _bridge()
    message = FakeMessage(raw={"from_user_id": "wx-user-1"})
    result = bridge.handle_action(message, "policy_lookup")
    assert result.status == INTAKE_REJECTED
    assert result.reason == EVENT_KEY_MISSING


def test_handle_action_with_production_registry_is_disabled() -> None:
    bridge, _ = _bridge()
    result = bridge.handle_action(FakeMessage(), "policy_lookup")
    assert result.status == INTAKE_REJECTED
    assert result.reason == "action_not_available"
    assert result.task_id is None


# --- SDK adapter ---------------------------------------------------------------


@dataclass
class FakeNoContextError(Exception):
    def __init__(self) -> None:
        super().__init__("no context")


@dataclass
class FakeSdkError(Exception):
    pass


class FakeBot:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.stopped = False
        self._log = lambda _message: None

    async def reply(self, message: Any, text: str) -> None:
        if self.error is not None:
            raise self.error

    def stop(self) -> None:
        self.stopped = True


def _adapter_with_bot(
    monkeypatch: pytest.MonkeyPatch,
    bot: FakeBot,
    *,
    on_qr_url: Any = None,
    on_error: Any = None,
) -> WeChatBotAdapter:
    monkeypatch.setattr(
        sdk_module,
        "_import_sdk",
        lambda: (FakeBot, FakeNoContextError, FakeSdkError),
    )
    adapter = WeChatBotAdapter(
        credential_path=TEST_CREDENTIAL_PATH,
        on_qr_url=on_qr_url,
        on_error=on_error,
    )
    adapter._bot = bot  # noqa: SLF001 - 测试注入，不触网
    return adapter


def test_reply_before_login_is_not_ready() -> None:
    adapter = WeChatBotAdapter(credential_path=TEST_CREDENTIAL_PATH)
    result = asyncio.run(adapter.reply(FakeMessage(), "hello"))
    assert result == ReplyResult(False, REPLY_NOT_READY)


def test_no_context_error_maps_to_stable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter_with_bot(
        monkeypatch, FakeBot(error=FakeNoContextError())
    )
    result = asyncio.run(adapter.reply(FakeMessage(), "hello"))
    assert result == ReplyResult(False, REPLY_NO_CONTEXT)


def test_other_send_errors_map_to_stable_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = _adapter_with_bot(monkeypatch, FakeBot(error=FakeSdkError()))
    result = asyncio.run(adapter.reply(FakeMessage(), "hello"))
    assert result == ReplyResult(False, REPLY_SEND_FAILED)


def test_reply_failure_is_logged_with_a_stable_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class Bot(FakeBot):
        def on_message(self, handler: Any) -> None:
            self.handler = handler

    bot = Bot(error=FakeSdkError())
    adapter = _adapter_with_bot(monkeypatch, bot)
    adapter.on_message(lambda _message: "reply")
    caplog.set_level(logging.WARNING, logger=sdk_module.logger.name)
    asyncio.run(bot.handler(FakeMessage()))  # type: ignore[attr-defined]
    assert "reply_failed" in caplog.text
    assert "reply_send_failed" in caplog.text


def test_qr_without_callback_fails_closed() -> None:
    adapter = WeChatBotAdapter(credential_path=TEST_CREDENTIAL_PATH)
    with pytest.raises(WeChatSdkError) as excinfo:
        adapter._handle_qr_url("qr-secret-url")  # noqa: SLF001
    assert excinfo.value.code == "qr_login_requires_interactive_terminal"
    assert "qr-secret-url" not in str(excinfo.value)


def test_qr_required_stops_the_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    bot = FakeBot()
    adapter = _adapter_with_bot(
        monkeypatch, bot, on_error=seen.append
    )
    adapter._handle_sdk_error(WeChatSdkError("qr_login_requires_interactive_terminal"))  # noqa: SLF001
    assert seen == ["qr_required"]
    assert bot.stopped is True


def test_sdk_private_log_is_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    # 只验证适配器没有把 SDK 的 ``_log`` 留给 stderr；不进行任何网络调用。
    bot = FakeBot()
    adapter = _adapter_with_bot(monkeypatch, bot)
    assert adapter._quiet_sdk_log("Logged in as wx-user-1") is None  # noqa: SLF001
