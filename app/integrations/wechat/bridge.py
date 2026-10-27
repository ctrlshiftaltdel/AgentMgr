"""WeChat 单聊消息 Bridge（B1：消息 / 身份 / 去重 / 通知，不含模型与执行）。

行为契约
--------

* 稳定事件键只从 SDK ``raw`` 的 ``from_user_id`` + ``create_time_ms`` 提取。缺失、
  空白、非正整数或 ``raw`` 不是映射时 **fail closed**：只回复/记录稳定码
  ``event_key_missing``，不解析身份、不建任务、不调用模型。**绝不**用消息正文
  哈希当事件键。
* ``/help``、``/whoami`` 是 Bridge 自身的**只读**命令，不是任务动作：
  ``/whoami`` 只回答“已绑定 / 未绑定”，不返回员工 ID、姓名或角色。
* 其它文本只回复“B1 未开放”，不调用模型、不做 advisory 回答、不创建任务。
* 可选的 :meth:`WeChatBridge.handle_action` 供测试注入注册表使用；它把事件交给
  :func:`app.tasks.intake.submit_action`，去重由 Task Intake 的幂等键保证。生产
  B1 注册表没有启用的建任务动作，因此生产路径不会走到这里。
* 日志只含稳定事件码、渠道事件哈希（SHA-256 前缀）、动作 ID、任务号与内部原因；
  **不记录** token / key / QR / 消息正文 / 渠道用户明文 / 员工明文。
* 没有审批回调、没有公开 HTTP 入口、没有模型调用。
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from app.identity.channel_routing import (
    ChannelIdentityRejectedError,
    resolve_principal,
)
from app.integrations.wechat.config import WeChatBridgeConfig
from app.integrations.wechat.sdk import WeChatSdk
from app.tasks.intake import (
    INTAKE_ACCEPTED,
    INTAKE_DUPLICATE,
    INTAKE_REJECTED,
    ActionRegistry,
    TaskIntakeResult,
    submit_action,
)

__all__ = [
    "BOUND_TEXT",
    "EVENT_KEY_MISSING",
    "HELP_TEXT",
    "NOT_OPEN_TEXT",
    "UNBOUND_TEXT",
    "WHOAMI_ERROR_TEXT",
    "WeChatBridge",
    "extract_event_key",
]

logger = logging.getLogger(__name__)

#: 稳定事件标识缺失时的回复/日志码（不含任何用户数据）。
EVENT_KEY_MISSING = "event_key_missing"

HELP_TEXT = (
    "B1 可用命令：\n"
    "/help —— 显示本说明\n"
    "/whoami —— 查看当前微信是否已绑定员工身份"
)
UNBOUND_TEXT = "未绑定"
BOUND_TEXT = "已绑定"
WHOAMI_ERROR_TEXT = "无法确认绑定状态"
NOT_OPEN_TEXT = "B1 未开放：当前仅支持 /help、/whoami。"

#: 允许进入日志的字段（其余字段一律丢弃）。
_LOG_FIELDS = frozenset(
    {"action_id", "command", "event_hash", "reply_code", "reason", "status", "task_id"}
)


def _non_blank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def extract_event_key(message: Any) -> str | None:
    """从 ``IncomingMessage.raw`` 提取稳定事件键；无法确定时返回 ``None``。

    SDK v0.3.0 的 ``IncomingMessage`` 没有 ``message_id``。``raw`` 中的
    ``from_user_id`` 与 ``create_time_ms`` 组合是当前可用的稳定键；二者任何一个
    缺失/非法都 fail closed，**不退化为消息正文哈希**。
    """
    raw = getattr(message, "raw", None)
    if not isinstance(raw, Mapping):
        return None
    from_user = raw.get("from_user_id")
    if not _non_blank(from_user):
        return None
    create_time_ms = _positive_int(raw.get("create_time_ms"))
    if create_time_ms is None:
        return None
    return f"{from_user}:{create_time_ms}"


def _event_hash(event_key: str) -> str:
    return hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:16]


class WeChatBridge:
    """把入站微信消息转换成只读回复或（测试用）显式动作提交。"""

    def __init__(
        self,
        *,
        config: WeChatBridgeConfig,
        session_factory: Callable[[], Session],
        sdk: WeChatSdk,
        registry: ActionRegistry | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._sdk = sdk
        self._registry = registry if registry is not None else config.action_registry
        self._log = log if log is not None else logger

    # -- lifecycle ------------------------------------------------------

    def install(self) -> None:
        """把 :meth:`handle_message` 注册到 SDK（回复由 SDK 适配层发送）。"""
        self._sdk.on_message(self.handle_message)

    # -- read-only commands --------------------------------------------

    async def handle_message(self, message: Any) -> str | None:
        """处理一条入站消息，返回要回复的文本（``None`` 表示不回复）。

        本方法**不**发送消息：SDK 适配层在 handler 返回文本后调用 ``reply``。
        """
        identity = extract_event_key(message)
        if identity is None:
            self._record("event_key_missing")
            return EVENT_KEY_MISSING

        text = getattr(message, "text", "")
        command = self._command(text if isinstance(text, str) else "")
        event_hash = _event_hash(identity)

        if command == "help":
            self._record("command", command="help", event_hash=event_hash)
            return HELP_TEXT
        if command == "whoami":
            reply = self._whoami(identity)
            self._record("command", command="whoami", event_hash=event_hash)
            return reply

        self._record("text_not_open", event_hash=event_hash)
        return NOT_OPEN_TEXT

    def _command(self, text: str) -> str | None:
        stripped = text.strip()
        prefix = self._config.command_prefix
        if not stripped.startswith(prefix):
            return None
        body = stripped[len(prefix) :].strip()
        if not body:
            return None
        return body.split(maxsplit=1)[0].lower()

    def _whoami(self, event_key: str) -> str:
        """只回答绑定/未绑定；不返回员工 ID、姓名、角色或公司。"""
        from_user = event_key.rsplit(":", 1)[0]
        try:
            with self._session_factory() as session:
                resolve_principal(
                    session,
                    channel_type=self._config.channel_type,
                    external_user_id=from_user,
                    expected_company_id=self._config.company_id,
                )
        except ChannelIdentityRejectedError:
            return UNBOUND_TEXT
        except Exception:  # noqa: BLE001 - 读取失败也失败关闭，不泄漏内部错误
            self._record("whoami_failed")
            return WHOAMI_ERROR_TEXT
        return BOUND_TEXT

    # -- explicit action path (tests / future increments) ---------------

    def handle_action(
        self,
        message: Any,
        action_id: str,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> TaskIntakeResult:
        """把一条消息交给 Task Intake（显式 action_id）；生产 B1 无启用动作。

        去重由 Task Intake 的事件幂等键保证：同一事件重复投递返回同一 ``task_id``。
        """
        event_key = extract_event_key(message)
        if event_key is None:
            self._record("event_key_missing")
            return TaskIntakeResult(
                status=INTAKE_REJECTED, reason=EVENT_KEY_MISSING
            )

        from_user = event_key.rsplit(":", 1)[0]
        with self._session_factory() as session:
            result = submit_action(
                session,
                channel_type=self._config.channel_type,
                external_user_id=from_user,
                external_message_id=event_key,
                action_id=action_id,
                payload=payload,
                expected_company_id=self._config.company_id,
                registry=self._registry,
            )
            if result.status in (INTAKE_ACCEPTED, INTAKE_DUPLICATE):
                session.commit()
            else:
                session.rollback()

        self._record(
            "action_result",
            action_id=action_id,
            status=result.status,
            reason=result.reason,
            task_id=result.task_id,
            event_hash=_event_hash(event_key),
        )
        return result

    # -- logging --------------------------------------------------------

    def _record(self, event: str, **fields: Any) -> None:
        """记录**白名单字段**；任何正文/身份明文都不会进入日志。"""
        safe = {
            key: value
            for key, value in fields.items()
            if key in _LOG_FIELDS and value is not None
        }
        self._log.info("wechat_bridge event=%s %s", event, safe)
