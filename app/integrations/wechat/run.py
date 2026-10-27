"""``python -m app.integrations.wechat`` 本地入口（B1）。

两种模式：

* ``serve``（默认）：读取受限凭据、注册只读消息处理器、进入长轮询；
* ``login``：由所有者在本机交互式终端完成 QR 登录并写入凭据。

失败关闭：配置缺失、凭据文件缺失或权限过宽、非交互式终端环境下的 QR 需求，都会
以非零状态退出。**没有**默认生产配置、没有公开网络监听、没有 token/HTTP 表面；
B1 是测试机上的受信本地服务。QR URL 只在交互式 TTY 输出，绝不写 journal/日志。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import stat
import sys
from collections.abc import Sequence

from app.integrations.wechat.bridge import WeChatBridge
from app.integrations.wechat.config import (
    WeChatBridgeConfig,
    WeChatConfigError,
    config_from_env,
)
from app.integrations.wechat.sdk import WeChatBotAdapter, WeChatSdkError

__all__ = ["main"]

logger = logging.getLogger("app.integrations.wechat")


def _terminal_qr(url: str) -> None:
    """只在交互式终端展示 QR；非 TTY（systemd/journal）一律失败关闭。"""
    if not sys.stdout.isatty():
        raise WeChatSdkError("qr_login_requires_interactive_terminal")
    print(f"Scan this URL in WeChat: {url}", file=sys.stdout, flush=True)


def _credential_permissions_ok(path: str) -> bool:
    """凭据文件必须对 group/other 无任何权限（0600/0400 等）。"""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return mode & 0o077 == 0


def _build_config() -> WeChatBridgeConfig:
    return config_from_env()


def _serve(config: WeChatBridgeConfig) -> int:
    from app.db.session import SessionLocal

    credential_path = str(config.credential_path)
    if not os.path.isfile(credential_path):
        logger.error("wechat_bridge credential_missing")
        return 2
    try:
        if not _credential_permissions_ok(credential_path):
            logger.error("wechat_bridge credential_permissions_unsafe")
            return 2
    except OSError:
        logger.error("wechat_bridge credential_unreadable")
        return 2

    def _on_error(code: str) -> None:
        logger.warning("wechat_bridge sdk_failure code=%s", code)

    sdk = WeChatBotAdapter(
        credential_path=config.credential_path,
        on_qr_url=_terminal_qr,
        on_error=_on_error,
    )
    bridge = WeChatBridge(
        config=config,
        session_factory=SessionLocal,
        sdk=sdk,
    )
    bridge.install()
    try:
        asyncio.run(sdk.start())
    except KeyboardInterrupt:
        sdk.stop()
        return 0
    except Exception as exc:  # noqa: BLE001 - 入口边界只记录稳定信息
        # 不记录异常文本：SDK 异常可能含渠道用户标识或网络细节。
        logger.error(
            "wechat_bridge stopped unexpected_exception=%s", type(exc).__name__
        )
        return 1
    return 0


def _login(config: WeChatBridgeConfig) -> int:
    sdk = WeChatBotAdapter(
        credential_path=config.credential_path,
        on_qr_url=_terminal_qr,
    )
    try:
        asyncio.run(sdk.login())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 - 只记录类型名，不记录 QR/token
        logger.error("wechat_bridge login_failed exception=%s", type(exc).__name__)
        return 1
    logger.info("wechat_bridge login_ok")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.integrations.wechat",
        description="WeChat B1 local bridge (messages/identity/dedupe only)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("serve", "login"),
        default="serve",
        help="serve (default) or interactive QR login",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )

    try:
        config = _build_config()
    except WeChatConfigError as exc:
        logger.error("wechat_bridge config_invalid code=%s", exc.code)
        return 2

    if args.command == "login":
        return _login(config)
    return _serve(config)
