"""``wechatbot`` SDK 的薄适配层（B1）。

设计约束（见 ``docs/integrations/clients/wechat.md`` §5）：

* SDK 只在**运行时**导入；未安装 SDK 时不污染应用其它模块的导入；
* Bridge 依赖 :class:`WeChatSdk` 协议，测试可用假实现驱动，无需网络/真实账号；
* SDK 的 ``reply`` 在冷用户上可能抛 ``NoContextError``，其它发送失败也可能发生：
  一律翻译成**稳定失败码**，不把 SDK 异常文本暴露给用户或写进日志；
* SDK v0.3.0 的 ``_log`` 会把 ``user_id`` 与错误文本直接写到 stderr。本适配器
  覆盖该私有方法并丢弃消息（固定版本 0.3.0），避免 journal 记录渠道用户明文；
* QR URL 默认由 SDK 写 stderr。适配器**只**通过调用方提供的回调展示 QR，且
  回调必须自行保证只在交互式终端输出；回调缺失时失败关闭。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "REPLY_NO_CONTEXT",
    "REPLY_NOT_READY",
    "REPLY_OK",
    "REPLY_SEND_FAILED",
    "ReplyResult",
    "WeChatBotAdapter",
    "WeChatSdk",
    "WeChatSdkError",
]

#: 回复成功。
REPLY_OK = "reply_ok"
#: 冷用户 / 无 context_token：SDK 无法主动创建 context。
REPLY_NO_CONTEXT = "reply_no_context"
#: 其它发送失败（网络/API/媒体等）。
REPLY_SEND_FAILED = "reply_send_failed"
#: 适配器尚未登录 / 尚未构建 bot。
REPLY_NOT_READY = "reply_not_ready"

MessageHandler = Callable[[Any], Awaitable[str | None] | str | None]


class WeChatSdkError(Exception):
    """适配器层的稳定失败（不携带 SDK 原始文本）。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ReplyResult:
    """一次回复尝试的结果；``code`` 是稳定码，不含消息正文。"""

    ok: bool
    code: str


@runtime_checkable
class WeChatSdk(Protocol):
    """Bridge 需要的 SDK 能力（假实现只需这四个方法）。"""

    def on_message(self, handler: MessageHandler) -> None:
        ...

    async def reply(self, message: Any, text: str) -> ReplyResult:
        ...

    async def start(self) -> None:
        ...

    def stop(self) -> None:
        ...


def _import_sdk() -> tuple[Any, type[Exception], type[Exception]]:
    """延迟导入 SDK，返回 ``(WeChatBot, NoContextError, WeChatBotError)``。"""
    from wechatbot import (  # noqa: PLC0415 - 运行时导入是刻意设计
        NoContextError,
        WeChatBot,
        WeChatBotError,
    )

    return WeChatBot, NoContextError, WeChatBotError


class WeChatBotAdapter:
    """把 ``wechatbot.WeChatBot`` 适配为 :class:`WeChatSdk`。"""

    def __init__(
        self,
        *,
        credential_path: str | Path,
        on_qr_url: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._credential_path = Path(credential_path)
        self._on_qr_url = on_qr_url
        self._on_error = on_error
        self._bot: Any | None = None
        self._handler: MessageHandler | None = None

    # -- construction ---------------------------------------------------

    def _build_bot(self) -> Any:
        if self._bot is not None:
            return self._bot
        WeChatBot, _, _ = _import_sdk()
        bot = WeChatBot(
            cred_path=str(self._credential_path),
            on_qr_url=self._handle_qr_url,
            on_scanned=lambda: None,
            on_expired=lambda: None,
            on_error=self._handle_sdk_error,
        )
        # v0.3.0 的 ``_log`` 会写 user_id / 错误原文到 stderr。部署把 stderr 收进
        # journal，因此必须丢弃；固定版本下这是最窄的拦截点。
        bot._log = self._quiet_sdk_log  # noqa: SLF001 - pinned SDK workaround
        self._bot = bot
        return bot

    @staticmethod
    def _quiet_sdk_log(_message: str) -> None:
        return None

    def _handle_qr_url(self, url: str) -> None:
        if self._on_qr_url is None:
            raise WeChatSdkError("qr_login_requires_interactive_terminal")
        self._on_qr_url(url)

    def _handle_sdk_error(self, error: Exception) -> None:
        code = (
            "qr_required"
            if isinstance(error, WeChatSdkError)
            else "sdk_error"
        )
        if self._on_error is not None:
            self._on_error(code)
        if code == "qr_required":
            self.stop()

    # -- WeChatSdk protocol --------------------------------------------

    def on_message(self, handler: MessageHandler) -> None:
        bot = self._build_bot()
        self._handler = handler

        async def _wrapped(message: Any) -> None:
            result = handler(message)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                await self.reply(message, result)

        bot.on_message(_wrapped)

    async def reply(self, message: Any, text: str) -> ReplyResult:
        if self._bot is None:
            return ReplyResult(False, REPLY_NOT_READY)
        try:
            _, no_context_error, sdk_error = _import_sdk()
        except Exception:  # noqa: BLE001 - 导入失败也只是稳定失败
            return ReplyResult(False, REPLY_SEND_FAILED)
        try:
            await self._bot.reply(message, text)
        except no_context_error:
            return ReplyResult(False, REPLY_NO_CONTEXT)
        except sdk_error:
            return ReplyResult(False, REPLY_SEND_FAILED)
        except Exception:  # noqa: BLE001 - 发送边界统一稳定失败
            return ReplyResult(False, REPLY_SEND_FAILED)
        return ReplyResult(True, REPLY_OK)

    async def login(self) -> None:
        """执行/复用 QR 登录（仅交互式 owner 会话应调用）。"""
        await self._build_bot().login()

    async def start(self) -> None:
        """登录（复用已存凭据）后进入长轮询。"""
        bot = self._build_bot()
        await bot.login()
        await bot.start()

    def stop(self) -> None:
        if self._bot is not None:
            self._bot.stop()
