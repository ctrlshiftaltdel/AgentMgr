"""Bridge 配置 / 本地入口的 DB-free 测试：默认值必须失败关闭。"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from app.integrations.wechat.config import (
    COMPANY_ID_ENV,
    CREDENTIAL_PATH_ENV,
    WeChatBridgeConfig,
    WeChatConfigError,
    config_from_env,
)
from app.integrations.wechat.run import _credential_permissions_ok, main

TEST_CREDENTIAL_PATH = Path("/var/lib/ajaimgr-wechat/credentials.json")


def test_config_from_env_requires_credential_path() -> None:
    with pytest.raises(WeChatConfigError) as excinfo:
        config_from_env({})
    assert excinfo.value.code == "credential_path_missing"


def test_config_from_env_requires_company_id() -> None:
    with pytest.raises(WeChatConfigError) as excinfo:
        config_from_env({CREDENTIAL_PATH_ENV: str(TEST_CREDENTIAL_PATH)})
    assert excinfo.value.code == "company_id_missing"


def test_config_from_env_rejects_non_numeric_company_id() -> None:
    with pytest.raises(WeChatConfigError) as excinfo:
        config_from_env(
            {
                CREDENTIAL_PATH_ENV: str(TEST_CREDENTIAL_PATH),
                COMPANY_ID_ENV: "not-a-number",
            }
        )
    assert excinfo.value.code == "company_id_invalid"


def test_config_from_env_builds_weixin_config() -> None:
    config = config_from_env(
        {
            CREDENTIAL_PATH_ENV: str(TEST_CREDENTIAL_PATH),
            COMPANY_ID_ENV: "42",
        }
    )
    assert config.channel_type == "weixin"
    assert config.company_id == 42
    assert config.command_prefix == "/"
    assert config.credential_path == TEST_CREDENTIAL_PATH
    # 配置没有 token 字段；repr 不应出现凭据内容。
    assert "token" not in repr(config).lower()


def test_config_requires_absolute_credential_path() -> None:
    with pytest.raises(WeChatConfigError) as excinfo:
        WeChatBridgeConfig(credential_path=Path("relative/credentials.json"), company_id=1)
    assert excinfo.value.code == "credential_path_not_absolute"


def test_config_rejects_bad_command_prefix() -> None:
    with pytest.raises(WeChatConfigError) as excinfo:
        WeChatBridgeConfig(
            credential_path=TEST_CREDENTIAL_PATH,
            company_id=1,
            command_prefix="help",
        )
    assert excinfo.value.code == "command_prefix_invalid"


def test_missing_env_fails_closed_with_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CREDENTIAL_PATH_ENV, raising=False)
    monkeypatch.delenv(COMPANY_ID_ENV, raising=False)
    assert main([]) == 2


def test_credential_permissions_accept_root_only(tmp_path: Path) -> None:
    credential = tmp_path / "credentials.json"
    credential.write_text("{}", encoding="utf-8")
    os.chmod(credential, 0o600)
    assert _credential_permissions_ok(str(credential)) is True


def test_credential_permissions_reject_group_or_other(tmp_path: Path) -> None:
    credential = tmp_path / "credentials.json"
    credential.write_text("{}", encoding="utf-8")
    os.chmod(credential, 0o640)
    assert _credential_permissions_ok(str(credential)) is False
    os.chmod(credential, stat.S_IRUSR | stat.S_IWUSR | stat.S_IROTH)
    assert _credential_permissions_ok(str(credential)) is False
