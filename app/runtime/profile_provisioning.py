"""每员工 Hermes Profile 的配置生成器与目录校验器（任务 W1-15）。

职责
====
本模块把 ``templates/PLAN.md`` §2.2「每位员工的隔离边界」与 §5.1「平台公共模块
（Hermes Profile 生命周期及状态监控）」编码为一组**纯函数**，供平台后续部署任务
复用：

* :func:`plan_profile` —— 给定员工与配置输入，产出该员工 Profile 的目录布局、必填
  文件清单、目录/文件权限要求、systemd 服务账户名、内部 API 凭据**引用**、工作目录、
  临时目录与隔离要素清单。**纯函数**：不创建任何文件、不读取 ``~/.hermes``。
* :func:`render_config` / :func:`render_config_text` —— 产出该 Profile 的 config 片段
  （模型 / 技能 / 记忆 / 会话 / 工作目录 / 临时目录），密钥一律以环境变量**引用**
  形式出现（``${VAR}``），**绝不包含明文凭据**。
* :func:`validate_profile_dir` —— 校验已有目录：目录 ``0700``、敏感文件 ``0600``、
  必填文件齐备、无明文凭据；逐项列出差异（:class:`Problem`）。
* :func:`main` —— CLI 入口，两个子命令：``plan``（dry-run，只打印计划，**不产生任何
  文件**）与 ``validate <path>``（不符合时以**非零退出码**退出并列出差异）。

安全约束（硬约束，最高优先级）
==============================
本模块**绝不**在本机新建、启动或停止任何真实 Hermes Profile，**绝不**读写
``~/.hermes``（含 ``~/.hermes/profiles``）。所有涉及路径的入口都先经过
:func:`ensure_safe_path`：一旦目标解析到禁用根目录（``~/.hermes`` 或 ``$HERMES_HOME``）
即抛 :class:`ForbiddenPathError`，且**在抛出前不产生任何文件系统副作用**。测试一律
使用临时目录，并断言未触碰禁用路径。

证据基础（本仓库内的调研结论）
==============================
* ``docs/research/W0-2-profile-isolation.md`` §3：Profile 只隔离「数据与凭据作用域」，
  **不是 OS 边界**；工作目录、临时目录、内部 API 身份（进程/系统账号）、文件系统与
  进程边界**必须由部署层补充**（独立 systemd ``User=``、独立 UID、容器/namespace）。
* ``docs/research/W0-7-profile-experiment.md`` §5.1：命名 Profile 目录实测 ``700``、
  ``config.yaml`` / ``.env`` / ``state.db`` 为 ``600``、``SOUL.md`` 为 ``664``；同一
  OS 用户可互读任意 Profile（故目录权限只防其他用户，不构成同账号隔离）。
* ``docs/DEPLOYMENT-REQUIREMENTS.md`` §3：服务身份与进程隔离为**硬要求**，须以负面
  测试证明，不得以文档声明代替。

**未实际部署**：本模块只生成计划与校验目录，不创建 Profile、不写 config、不调用
``hermes`` 子命令。真实部署与内核级隔离验证见上述部署要求 §3。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "BASE_DIR_ENV",
    "DIR_MODE",
    "DEFAULT_BASE_DIR",
    "DEFAULT_MODEL_BASE_URL",
    "DEFAULT_MODEL_DEFAULT",
    "DEFAULT_MODEL_PROVIDER",
    "CredentialRef",
    "ForbiddenPathError",
    "IsolationElement",
    "Problem",
    "ProfilePlan",
    "REQUIRED_DIRS",
    "REQUIRED_FILES",
    "SENSITIVE_FILES",
    "SENSITIVE_FILE_MODE",
    "ensure_safe_path",
    "forbidden_roots",
    "main",
    "plan_profile",
    "profile_name_for",
    "render_config",
    "render_config_text",
    "scan_plaintext_secrets",
    "validate_profile_dir",
]

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 每个 Profile 目录（及必需子目录）的权限：仅属主可进入/读写。
DIR_MODE = 0o700
#: 敏感文件（``config.yaml`` / ``.env`` / ``state.db``）的权限：仅属主可读写。
SENSITIVE_FILE_MODE = 0o600

#: Profile 目录内必须存在的子目录（对齐 W0-7 §3.2 实测布局中的关键项）。
REQUIRED_DIRS: tuple[str, ...] = (
    "sessions",
    "memories",
    "skills",
    "cron",
    "workspace",
    "tmp",
    "logs",
    "home",
)

#: 必须存在且权限为 ``0600`` 的敏感文件。
SENSITIVE_FILES: tuple[str, ...] = ("config.yaml", ".env", "state.db")

#: 必须存在（非凭据）的普通文件。
REQUIRED_FILES: tuple[str, ...] = ("SOUL.md",)

#: 生产默认的 Profile 根目录（中性路径，**不是** ``~/.hermes``）。
DEFAULT_BASE_DIR = Path("/srv/ajaimgr/profiles")

#: 覆盖默认 Profile 根目录的环境变量名。
BASE_DIR_ENV = "AJAIMGR_PROFILES_ROOT"

DEFAULT_MODEL_DEFAULT = "deepseek-v4.1-flash"
DEFAULT_MODEL_PROVIDER = "opencode-go"
DEFAULT_MODEL_BASE_URL = "https://opencode.ai/inference/openai/v1"

#: 配置文件内容扫描时视为「占位/空」的字面量（不构成明文凭据）。
_PLACEHOLDER_VALUES = frozenset(
    {"", "null", "~", "none", "change_me", "changeme", "<redacted>", "placeholder"}
)

#: 形如 ``<KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH>: value`` 或 ``= value`` 的赋值行。
_SECRET_KEY_ASSIGN = re.compile(
    r"(?im)^[ \t]*(?P<key>[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)[A-Za-z0-9_]*)"
    r"[ \t]*[:=][ \t]*(?P<value>.+?)[ \t]*$"
)

#: 形如 ``SOME_ENV_VAR`` 的值：视为「引用名」而非明文凭据（如 ``api_key_ref`` 的值）。
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")

#: 已知的明文密钥字面量前缀（只识别类型，不回显原值）。
_KEY_PREFIX = re.compile(
    r"(?i)\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ForbiddenPathError(ValueError):
    """目标路径落在禁用根目录（``~/.hermes`` 或 ``$HERMES_HOME``）之下。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CredentialRef:
    """一条凭据的**引用**（环境变量名 / secret 引用），**不含任何凭据值**。"""

    name: str
    purpose: str
    kind: str = "env"

    def as_dict(self) -> dict[str, str]:
        return {"name": self.name, "purpose": self.purpose, "kind": self.kind}


@dataclass(frozen=True)
class IsolationElement:
    """隔离要素清单中的一项。"""

    code: str
    summary: str
    layer: str  # "hermes-native"（原生）| "deployment"（部署层补充）

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "summary": self.summary, "layer": self.layer}


@dataclass(frozen=True)
class Problem:
    """校验发现的一处差异。``detail`` 绝不回显凭据原值。"""

    code: str
    path: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "detail": self.detail}


@dataclass(frozen=True)
class ProfilePlan:
    """某员工 Profile 的完整计划（**不落地**，纯数据）。"""

    employee_id: int
    employee_no: str | None
    profile_name: str
    base_dir: Path
    profile_dir: Path
    working_dir: Path
    temp_dir: Path
    service_account: str
    service_unit: str
    session_namespace: str
    memory_namespace: str
    required_dirs: tuple[str, ...]
    required_files: tuple[str, ...]
    sensitive_files: tuple[str, ...]
    dir_mode: int
    sensitive_file_mode: int
    credential_refs: tuple[CredentialRef, ...]
    isolation_elements: tuple[IsolationElement, ...]
    model_default: str
    model_provider: str
    model_base_url: str | None

    @property
    def model_api_key_ref(self) -> str:
        """模型供应商密钥的**引用名**（环境变量名，非值）。"""
        upper = re.sub(r"[^A-Z0-9]+", "_", self.profile_name.upper())
        return f"HERMES_PROFILE_{upper}_MODEL_API_KEY"

    @property
    def internal_api_token_ref(self) -> str:
        """该 Profile 独立内部 API 身份的**引用名**（环境变量名，非值）。"""
        upper = re.sub(r"[^A-Z0-9]+", "_", self.profile_name.upper())
        return f"AJAIMGR_PROFILE_{upper}_API_TOKEN"

    def as_dict(self) -> dict[str, Any]:
        """脱敏的 JSON 视图：只有路径、权限、引用名，绝无凭据值。"""
        return {
            "employee_id": self.employee_id,
            "employee_no": self.employee_no,
            "profile_name": self.profile_name,
            "base_dir": str(self.base_dir),
            "profile_dir": str(self.profile_dir),
            "working_dir": str(self.working_dir),
            "temp_dir": str(self.temp_dir),
            "service_account": self.service_account,
            "service_unit": self.service_unit,
            "session_namespace": self.session_namespace,
            "memory_namespace": self.memory_namespace,
            "required_dirs": list(self.required_dirs),
            "required_files": list(self.required_files),
            "sensitive_files": list(self.sensitive_files),
            "dir_mode": f"{self.dir_mode:04o}",
            "sensitive_file_mode": f"{self.sensitive_file_mode:04o}",
            "credential_refs": [ref.as_dict() for ref in self.credential_refs],
            "isolation_elements": [el.as_dict() for el in self.isolation_elements],
            "model": {
                "default": self.model_default,
                "provider": self.model_provider,
                "base_url": self.model_base_url,
                "api_key_ref": self.model_api_key_ref,
            },
        }


# --------------------------------------------------------------------------- #
# 路径安全
# --------------------------------------------------------------------------- #


def forbidden_roots() -> tuple[Path, ...]:
    """返回禁用根目录：``~/.hermes``，以及 ``$HERMES_HOME``（若设置）。"""
    candidates = [Path(os.path.expanduser("~")) / ".hermes"]
    env_home = os.environ.get("HERMES_HOME")
    if env_home:
        candidates.append(Path(env_home))
    out: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:  # pragma: no cover - defensive
            continue
        if resolved not in out:
            out.append(resolved)
    return tuple(out)


def _is_within(path: Path, root: Path) -> bool:
    """``path`` 是否等于 ``root`` 或位于 ``root`` 之下。"""
    return path == root or root in path.parents


def ensure_safe_path(path: os.PathLike[str] | str) -> Path:
    """把 ``path`` 解析为绝对路径；命中禁用根目录则抛 :class:`ForbiddenPathError`。

    ``Path.resolve()`` 会跟随符号链接，因此「合法目录被软链到 ``~/.hermes``」这类
    绕过也会被拦下。**抛出前不产生任何文件系统副作用。**
    """
    resolved = Path(path).expanduser().resolve()
    for root in forbidden_roots():
        if _is_within(resolved, root):
            raise ForbiddenPathError(
                f"拒绝访问受保护路径 {resolved}：它位于禁用根目录 {root} 之下"
                "（本模块绝不读写 ~/.hermes 或 $HERMES_HOME）"
            )
    return resolved


# --------------------------------------------------------------------------- #
# 员工输入归一化
# --------------------------------------------------------------------------- #


def _coerce_employee(employee: Any) -> tuple[int, str | None]:
    """把员工输入归一化为 ``(employee_id, employee_no)``。

    接受：整数 id；带 ``id``/``employee_no`` 属性的对象（如 ORM 的 ``Employee``）；
    含 ``id``/``employee_id``/``employee_no`` 键的映射。**不访问数据库。**
    """
    if isinstance(employee, bool):
        raise TypeError("employee 不能是 bool")
    if isinstance(employee, int):
        return employee, None
    if isinstance(employee, Mapping):
        raw_id = employee.get("id", employee.get("employee_id"))
        raw_no = employee.get("employee_no")
    else:
        raw_id = getattr(employee, "id", None)
        raw_no = getattr(employee, "employee_no", None)
    if raw_id is None:
        raise TypeError("employee 必须提供 id / employee_id")
    if isinstance(raw_id, bool):
        raise TypeError("employee.id 不能是 bool")
    return int(raw_id), (str(raw_no) if raw_no is not None else None)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    return slug or "unknown"


def _profile_name(employee_id: int, employee_no: str | None) -> str:
    base = employee_no if employee_no else str(employee_id)
    return f"emp-{_slug(base)}"


def profile_name_for(employee: Any) -> str:
    """由员工输入推导 Hermes Profile 名（小写字母/数字/连字符）。"""
    employee_id, employee_no = _coerce_employee(employee)
    return _profile_name(employee_id, employee_no)


def _service_account(profile_name: str) -> str:
    """systemd ``User=`` 形式的独立服务账户名（截断到 31 字符内）。"""
    name = f"ajaimgr-{profile_name}"
    if len(name) > 31:
        name = name[:31].rstrip("-")
    return name


def _isolation_elements() -> tuple[IsolationElement, ...]:
    """PLAN §2.2 的隔离要素清单，标注「原生」或「部署层补充」（依据 W0-2 §3）。"""
    return (
        IsolationElement(
            "profile_dir", "独立 Profile 目录（0700，仅服务账户可读）", "hermes-native"
        ),
        IsolationElement(
            "config", "独立 config.yaml / 角色指令 / 技能清单 / 模型配置", "hermes-native"
        ),
        IsolationElement(
            "session_namespace", "独立会话命名空间与 state.db（agent:<profile>:…）",
            "hermes-native",
        ),
        IsolationElement(
            "memory_namespace", "独立记忆命名空间（memories/）", "hermes-native"
        ),
        IsolationElement(
            "internal_api_identity", "独立内部 API 身份（短期调用凭据，仅引用）",
            "hermes-native",
        ),
        IsolationElement(
            "tool_authorization", "独立工具授权集合", "hermes-native"
        ),
        IsolationElement(
            "audit_context", "独立访问审计上下文", "hermes-native"
        ),
        IsolationElement(
            "working_dir", "独立工作目录（terminal.cwd 显式绝对路径）", "deployment"
        ),
        IsolationElement(
            "temp_dir", "独立临时目录（TMPDIR，0700）", "deployment"
        ),
        IsolationElement(
            "service_account", "独立 systemd 服务账户（User=）", "deployment"
        ),
        IsolationElement(
            "process_unit", "独立 systemd unit（hermes-gateway-<profile>.service）",
            "deployment",
        ),
        IsolationElement(
            "kernel_isolation",
            "内核级隔离（独立 UID / ProtectSystem / ProtectHome / namespace）",
            "deployment",
        ),
    )


# --------------------------------------------------------------------------- #
# 生成器
# --------------------------------------------------------------------------- #


def plan_profile(
    employee: Any,
    *,
    base_dir: os.PathLike[str] | str | None = None,
    profile_name: str | None = None,
    service_account: str | None = None,
    model_default: str = DEFAULT_MODEL_DEFAULT,
    model_provider: str = DEFAULT_MODEL_PROVIDER,
    model_base_url: str | None = DEFAULT_MODEL_BASE_URL,
) -> ProfilePlan:
    """为一名员工生成 Profile 计划（**纯函数，不产生任何文件**）。

    参数
    ----
    employee:
        整数 id、ORM ``Employee`` 对象，或含 ``id``/``employee_no`` 的映射。
    base_dir:
        Profile 根目录。缺省取环境变量 :data:`BASE_DIR_ENV`，再缺省为
        :data:`DEFAULT_BASE_DIR`。解析后若落在禁用根目录下即抛
        :class:`ForbiddenPathError`。
    profile_name:
        显式 Profile 名；须匹配 ``^[a-z0-9][a-z0-9-]*$``。缺省由员工号/id 推导。
    service_account:
        显式 systemd 服务账户名；缺省由 Profile 名推导。
    """
    employee_id, employee_no = _coerce_employee(employee)

    if base_dir is None:
        base_dir = os.environ.get(BASE_DIR_ENV) or DEFAULT_BASE_DIR
    root = ensure_safe_path(base_dir)

    name = profile_name or _profile_name(employee_id, employee_no)
    if not _PROFILE_NAME_RE.match(name):
        raise ValueError(
            f"非法 Profile 名 {name!r}：只允许小写字母、数字与连字符，且以字母/数字开头"
        )

    # 二次安全校验：即便 name 被构造为含路径分隔符，也会在此被拦下。
    profile_dir = ensure_safe_path(root / name)
    working_dir = profile_dir / "workspace"
    temp_dir = profile_dir / "tmp"

    account = service_account or _service_account(name)
    unit = f"hermes-gateway-{name}.service"
    upper = re.sub(r"[^A-Z0-9]+", "_", name.upper())

    credential_refs = (
        CredentialRef(
            "AJAIMGR_INTERNAL_TOKEN",
            "内部业务 API 的 Bearer 令牌（工具代理 → 后端）；仅以引用名出现，不落明文",
        ),
        CredentialRef(
            f"AJAIMGR_PROFILE_{upper}_API_TOKEN",
            "该 Profile 的独立内部 API 身份 / 短期调用凭据；仅以引用名出现，不落明文",
        ),
        CredentialRef(
            f"HERMES_PROFILE_{upper}_MODEL_API_KEY",
            "该 Profile 的模型供应商密钥；仅以引用名出现，不落明文",
        ),
    )

    return ProfilePlan(
        employee_id=employee_id,
        employee_no=employee_no,
        profile_name=name,
        base_dir=root,
        profile_dir=profile_dir,
        working_dir=working_dir,
        temp_dir=temp_dir,
        service_account=account,
        service_unit=unit,
        session_namespace=f"agent:{name}",
        memory_namespace=f"profile:{name}:memory",
        required_dirs=REQUIRED_DIRS,
        required_files=REQUIRED_FILES,
        sensitive_files=SENSITIVE_FILES,
        dir_mode=DIR_MODE,
        sensitive_file_mode=SENSITIVE_FILE_MODE,
        credential_refs=credential_refs,
        isolation_elements=_isolation_elements(),
        model_default=model_default,
        model_provider=model_provider,
        model_base_url=model_base_url,
    )


def render_config(plan: ProfilePlan) -> dict[str, Any]:
    """产出该 Profile 的 config 片段（映射）。**不含任何明文凭据**，密钥为引用。"""
    model: dict[str, Any] = {
        "default": plan.model_default,
        "provider": plan.model_provider,
        "api_mode": "chat_completions",
        # 密钥以 ${VAR} 引用出现，绝不写值。
        "api_key": "${" + plan.model_api_key_ref + "}",
    }
    if plan.model_base_url:
        model["base_url"] = plan.model_base_url
    return {
        "model": model,
        "skills": {"dir": str(plan.profile_dir / "skills")},
        "memory": {
            "dir": str(plan.profile_dir / "memories"),
            "namespace": plan.memory_namespace,
        },
        "sessions": {
            "dir": str(plan.profile_dir / "sessions"),
            "namespace": plan.session_namespace,
        },
        "terminal": {
            "cwd": str(plan.working_dir),
            "home_mode": "profile",
            "tmpdir": str(plan.temp_dir),
        },
        "gateway": {"profile": plan.profile_name, "service": plan.service_unit},
    }


def _scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _yaml_dump(value: Any, indent: int = 0) -> str:
    """极简 YAML 序列化（不引入第三方依赖）。仅处理 dict/list/标量。"""
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (Mapping, list, tuple)):
                lines.append(f"{pad}{key}:")
                lines.append(_yaml_dump(item, indent + 1))
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, (Mapping, list, tuple)):
                lines.append(f"{pad}-")
                lines.append(_yaml_dump(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
    else:
        lines.append(f"{pad}{_scalar(value)}")
    return "\n".join(lines)


def render_config_text(plan: ProfilePlan) -> str:
    """把 :func:`render_config` 渲染为 YAML 文本（密钥仍为引用，绝无明文）。"""
    return _yaml_dump(render_config(plan)) + "\n"


# --------------------------------------------------------------------------- #
# 明文凭据扫描
# --------------------------------------------------------------------------- #


def scan_plaintext_secrets(text: str) -> list[str]:
    """扫描文本中疑似**明文凭据**的痕迹；返回发现描述（**不回显原值**）。

    规则（保守，避免把 ``${VAR}`` 引用或占位符误报）：

    1. 形如 ``<KEY|TOKEN|SECRET|PASSWORD|...>: value`` / ``= value`` 的赋值行，其值既非
       ``${VAR}`` 引用、也非占位/空值时，报「疑似明文凭据」。
    2. 出现已知密钥前缀字面量（``sk-`` / ``ghp_`` / ``xox?-`` / ``AKIA``）时，报「疑似
       明文密钥字面量」。
    """
    findings: list[str] = []

    for match in _SECRET_KEY_ASSIGN.finditer(text):
        key = match.group("key")
        raw_value = match.group("value").strip()
        value = raw_value.strip().strip('"').strip("'").strip()
        if not value:
            continue
        if value.startswith("${") and value.endswith("}"):
            continue
        if value.startswith("$"):
            continue
        if value.lower() in _PLACEHOLDER_VALUES:
            continue
        if value.startswith("<") and value.endswith(">"):
            continue
        if _ENV_NAME.match(value):
            # 值本身就是环境变量名（引用名），不是明文凭据。
            continue
        findings.append(f"键 `{key}` 的值不是 ${{VAR}} 引用（疑似明文凭据）")

    for _match in _KEY_PREFIX.finditer(text):
        findings.append("出现疑似明文密钥字面量（按前缀识别，不回显原值）")

    # 去重并保序。
    seen: list[str] = []
    for finding in findings:
        if finding not in seen:
            seen.append(finding)
    return seen


# --------------------------------------------------------------------------- #
# 校验器
# --------------------------------------------------------------------------- #


def _mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _check_mode(
    path: Path, expected: int, code: str, label: str, problems: list[Problem]
) -> None:
    actual = _mode_of(path)
    if actual != expected:
        problems.append(
            Problem(code, str(path), f"{label}应为 {expected:04o}，实际为 {actual:04o}")
        )


def _check_not_world_writable(path: Path, problems: list[Problem]) -> None:
    actual = _mode_of(path)
    if actual & 0o022:
        problems.append(
            Problem(
                "insecure_mode",
                str(path),
                f"文件权限 {actual:04o} 允许组/其他用户写入（建议 0600）",
            )
        )


def validate_profile_dir(path: os.PathLike[str] | str) -> list[Problem]:
    """校验已有 Profile 目录，返回差异列表（空列表表示符合要求）。

    检查项：路径安全（禁用根目录）、目录存在且为目录、Profile 目录与必需子目录权限
    ``0700``、敏感文件存在且权限 ``0600``、必需文件存在、配置文件内无明文凭据。
    **只读**：仅 ``stat`` 与读取配置文件文本，绝不写任何文件。
    """
    problems: list[Problem] = []

    try:
        target = ensure_safe_path(path)
    except ForbiddenPathError as exc:
        # 拒绝访问，且不触碰该路径。
        return [Problem("forbidden_path", str(path), str(exc))]

    if not target.exists():
        return [Problem("missing_dir", str(target), "Profile 目录不存在")]
    if not target.is_dir():
        return [Problem("not_a_directory", str(target), "Profile 路径不是目录")]

    _check_mode(target, DIR_MODE, "dir_mode", "Profile 目录权限", problems)

    for name in REQUIRED_DIRS:
        child = target / name
        if not child.is_dir():
            problems.append(
                Problem("missing_dir", str(child), f"缺少必需子目录 {name}/")
            )
            continue
        _check_mode(child, DIR_MODE, "dir_mode", f"子目录 {name}/ 权限", problems)

    for name in REQUIRED_FILES:
        child = target / name
        if not child.is_file():
            problems.append(Problem("missing_file", str(child), f"缺少必需文件 {name}"))
            continue
        _check_not_world_writable(child, problems)

    for name in SENSITIVE_FILES:
        child = target / name
        if not child.is_file():
            problems.append(
                Problem("missing_file", str(child), f"缺少必需敏感文件 {name}")
            )
            continue
        _check_mode(
            child, SENSITIVE_FILE_MODE, "sensitive_file_mode", f"敏感文件 {name} 权限",
            problems,
        )

    # 明文凭据扫描（跳过二进制 state.db）。
    for name in ("config.yaml", ".env", "SOUL.md"):
        child = target / name
        if not child.is_file():
            continue
        try:
            text = child.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:  # pragma: no cover - defensive
            problems.append(
                Problem("unreadable", str(child), f"无法读取以扫描凭据：{exc}")
            )
            continue
        for finding in scan_plaintext_secrets(text):
            problems.append(Problem("plaintext_credential", str(child), finding))

    return problems


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _format_plan(plan: ProfilePlan) -> str:
    lines = [
        "== 员工 Profile 计划（dry-run，未创建任何文件） ==",
        f"员工 id           : {plan.employee_id}",
        f"员工编号          : {plan.employee_no or '(未提供)'}",
        f"Profile 名        : {plan.profile_name}",
        f"Profile 目录      : {plan.profile_dir}  (mode {plan.dir_mode:04o})",
        f"工作目录          : {plan.working_dir}",
        f"临时目录          : {plan.temp_dir}",
        f"服务账户 (User=)  : {plan.service_account}",
        f"systemd unit      : {plan.service_unit}",
        f"会话命名空间      : {plan.session_namespace}",
        f"记忆命名空间      : {plan.memory_namespace}",
        "",
        "必需子目录（均 0700）:",
    ]
    lines += [f"  - {name}/" for name in plan.required_dirs]
    lines.append("")
    lines.append(f"敏感文件（均 {plan.sensitive_file_mode:04o}）:")
    lines += [f"  - {name}" for name in plan.sensitive_files]
    lines.append("")
    lines.append("必需文件:")
    lines += [f"  - {name}" for name in plan.required_files]
    lines.append("")
    lines.append("内部 API / 模型凭据引用（仅引用名，绝不含明文值）:")
    lines += [f"  - {ref.name}  [{ref.kind}]  {ref.purpose}" for ref in plan.credential_refs]
    lines.append("")
    lines.append("隔离要素清单:")
    lines += [
        f"  - {el.code} ({el.layer}): {el.summary}" for el in plan.isolation_elements
    ]
    lines.append("")
    lines.append("config 片段（密钥为引用）:")
    lines.append(render_config_text(plan).rstrip("\n"))
    return "\n".join(lines)


def _cmd_plan(args: argparse.Namespace) -> int:
    employee: dict[str, Any] = {"id": args.employee_id}
    if args.employee_no is not None:
        employee["employee_no"] = args.employee_no
    try:
        plan = plan_profile(
            employee, base_dir=args.base_dir, profile_name=args.profile_name
        )
    except ForbiddenPathError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(_format_plan(plan))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    problems = validate_profile_dir(args.path)
    if args.as_json:
        print(
            json.dumps(
                [p.as_dict() for p in problems], ensure_ascii=False, indent=2
            )
        )
    elif not problems:
        print(f"OK：{args.path} 符合 Profile 目录要求")
    else:
        print(f"不符合：{args.path}（{len(problems)} 项差异）")
        for problem in problems:
            print(f"  - [{problem.code}] {problem.path}: {problem.detail}")
    return 1 if problems else 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``plan``（dry-run）与 ``validate <path>``。"""
    parser = argparse.ArgumentParser(
        prog="python -m app.runtime.profile_provisioning",
        description=(
            "每员工 Hermes Profile 的配置生成器与目录校验器（不部署、不触碰 ~/.hermes）"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser(
        "plan", help="dry-run：打印某员工的 Profile 计划，不产生任何文件"
    )
    p_plan.add_argument("--employee-id", type=int, required=True)
    p_plan.add_argument("--employee-no", default=None)
    p_plan.add_argument(
        "--base-dir",
        default=None,
        help=f"Profile 根目录（缺省取 ${BASE_DIR_ENV} 或 {DEFAULT_BASE_DIR}）",
    )
    p_plan.add_argument("--profile-name", default=None)
    p_plan.add_argument("--json", action="store_true", dest="as_json")
    p_plan.set_defaults(func=_cmd_plan)

    p_val = sub.add_parser(
        "validate", help="校验已有 Profile 目录；不符合时以非零退出码退出"
    )
    p_val.add_argument("path")
    p_val.add_argument("--json", action="store_true", dest="as_json")
    p_val.set_defaults(func=_cmd_validate)

    args = parser.parse_args(list(argv) if argv is not None else None)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess/tests
    raise SystemExit(main())
