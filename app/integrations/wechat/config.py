"""WeChat Bridge 的本地配置（不读秘密、不打印秘密）。

B1 以测试机上的**受信本地进程**运行：配置只描述渠道类型、root-only 凭据路径、
目标公司、动作注册表与命令前缀。token / key / QR 都不在这里，也不在任何
``repr`` 中——凭据只由 SDK 从受限文件读取。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from app.tasks.intake import B1_ACTION_REGISTRY, ActionRegistry

__all__ = [
    "COMPANY_ID_ENV",
    "COMMAND_PREFIX_ENV",
    "CREDENTIAL_PATH_ENV",
    "WeChatBridgeConfig",
    "WeChatConfigError",
    "config_from_env",
]

CREDENTIAL_PATH_ENV = "AJAIMGR_WECHAT_CREDENTIAL_PATH"
COMPANY_ID_ENV = "AJAIMGR_WECHAT_COMPANY_ID"
COMMAND_PREFIX_ENV = "AJAIMGR_WECHAT_COMMAND_PREFIX"

#: B1 只支持微信单聊（设计 v2 §5）；渠道类型必须与绑定表一致。
CHANNEL_TYPE = "weixin"


class WeChatConfigError(Exception):
    """配置缺失或非法。``code`` 是稳定、可记录的内部原因。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class WeChatBridgeConfig:
    """Bridge 运行所需的非秘密配置。

    * ``credential_path``：SDK 凭据文件路径；必须是绝对路径、由部署设置
      root-only 权限（见 ``docs/operations/wechat-bridge.md``）；
    * ``company_id``：身份解析的期望公司，防跨公司绑定；
    * ``action_registry``：闭集动作注册表；默认是 B1（无启用建任务动作）；
    * ``command_prefix``：只读命令前缀，默认 ``/``。

    该类**没有** token 字段，也不从环境读取 token；``repr`` 因此不会包含秘密。
    """

    credential_path: Path
    company_id: int
    action_registry: ActionRegistry = field(default=B1_ACTION_REGISTRY, repr=False)
    command_prefix: str = "/"
    channel_type: str = field(default=CHANNEL_TYPE, init=False)

    def __post_init__(self) -> None:
        if self.channel_type != CHANNEL_TYPE:
            raise WeChatConfigError("channel_type_invalid")
        if not isinstance(self.credential_path, Path):
            raise WeChatConfigError("credential_path_not_a_path")
        if not self.credential_path.is_absolute():
            raise WeChatConfigError("credential_path_not_absolute")
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, int):
            raise WeChatConfigError("company_id_invalid")
        if self.company_id <= 0:
            raise WeChatConfigError("company_id_invalid")
        if not isinstance(self.command_prefix, str) or not self.command_prefix:
            raise WeChatConfigError("command_prefix_invalid")
        if not self.command_prefix.startswith("/"):
            raise WeChatConfigError("command_prefix_invalid")
        if not isinstance(self.action_registry, ActionRegistry):
            raise WeChatConfigError("action_registry_invalid")


def config_from_env(
    environ: Mapping[str, str] | None = None,
) -> WeChatBridgeConfig:
    """从环境变量构造配置；缺失/非法一律抛出 :class:`WeChatConfigError`。

    没有默认生产凭据路径、没有默认公司：未配置就失败关闭（``run`` 非零退出）。
    ``AJAIMGR_WECHAT_COMMAND_PREFIX`` 可选，默认 ``/``。
    """
    env = os.environ if environ is None else environ

    raw_path = env.get(CREDENTIAL_PATH_ENV)
    if raw_path is None or not raw_path.strip():
        raise WeChatConfigError("credential_path_missing")

    raw_company = env.get(COMPANY_ID_ENV)
    if raw_company is None or not raw_company.strip():
        raise WeChatConfigError("company_id_missing")
    try:
        company_id = int(raw_company)
    except ValueError as exc:
        raise WeChatConfigError("company_id_invalid") from exc

    prefix = env.get(COMMAND_PREFIX_ENV, "/").strip() or "/"

    return WeChatBridgeConfig(
        credential_path=Path(raw_path.strip()),
        company_id=company_id,
        command_prefix=prefix,
    )
