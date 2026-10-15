"""权限目录与角色-权限矩阵（任务 W1-3）。

本模块是 ``templates/PLAN.md`` §9「权限模型」与
``docs/PROJECT-CHARTER.md``（强制原则：管理员不自动获得财务批准权）在代码中的
可执行事实来源。它只描述**数据**（有哪些角色、有哪些权限、角色被授予哪些权限），
不实现判定逻辑；判定在后端每次调用时执行。

设计要点：

* **单一来源**：角色、权限、矩阵都只在本模块声明一次，``seed.py`` 与迁移
  ``0003_permission_catalog`` 均从本模块读取，避免三处各写一份而漂移。
* **权限编号稳定**：权限用从 1 开始的连续编号（``PermissionSpec.number``）
  标识，矩阵按编号引用；编号一旦发布不得复用或重排，否则会改变既有含义。
* **强制规则可校验**：``validate_catalog()`` 把 PLAN §9 的强制规则编码成断言，
  任何未来编辑只要破坏规则就会在种子/测试阶段立刻失败：

  - 系统管理员（``sysadmin``）不得拥有 ``expense_claim/approve`` 或
    ``expense_claim/finance_review``（管理员不自动获得财务批准权）；
  - 审计人员（``auditor``）只读：不得拥有 create / submit / approve /
    finance_review / payment 类动作。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RoleSpec:
    """一个角色的种子定义：英文 ``name`` + 中文 ``description``。"""

    name: str
    description: str


@dataclass(frozen=True)
class PermissionSpec:
    """一条权限的种子定义：``(resource, action, scope)`` 三元组 + 稳定编号。"""

    number: int
    resource: str
    action: str
    scope: str

    @property
    def key(self) -> tuple[str, str, str]:
        """数据库唯一键 ``(resource, action, scope)``。"""
        return (self.resource, self.action, self.scope)


#: 七个角色（PLAN §9 的七列），name 用英文，description 用中文。
ROLES: tuple[RoleSpec, ...] = (
    RoleSpec("employee", "普通员工"),
    RoleSpec("manager", "主管"),
    RoleSpec("finance_operator", "财务经办"),
    RoleSpec("finance_lead", "财务主管"),
    RoleSpec("warehouse", "仓储/采购"),
    RoleSpec("sysadmin", "系统管理员"),
    RoleSpec("auditor", "审计人员"),
)

#: 十九条权限目录，编号 1..19，与 PLAN §9 的能力列一一对应。
PERMISSIONS: tuple[PermissionSpec, ...] = (
    PermissionSpec(1, "expense_claim", "create", "own"),
    PermissionSpec(2, "expense_claim", "read", "own"),
    PermissionSpec(3, "expense_claim", "read", "team"),
    PermissionSpec(4, "expense_claim", "read", "company"),
    PermissionSpec(5, "expense_claim", "read", "authorized"),
    PermissionSpec(6, "expense_claim", "submit", "own"),
    PermissionSpec(7, "expense_claim", "withdraw", "own"),
    PermissionSpec(8, "expense_claim", "approve", "team"),
    PermissionSpec(9, "expense_claim", "finance_review", "company"),
    PermissionSpec(10, "invoice", "verify", "company"),
    PermissionSpec(11, "invoice", "read", "company"),
    PermissionSpec(12, "invoice", "read", "authorized"),
    PermissionSpec(13, "payment", "record", "company"),
    PermissionSpec(14, "inventory", "read", "company"),
    PermissionSpec(15, "inventory", "execute", "company"),
    PermissionSpec(16, "inventory", "read", "authorized"),
    PermissionSpec(17, "audit", "read", "authorized"),
    PermissionSpec(18, "identity", "manage", "company"),
    PermissionSpec(19, "channel_binding", "manage", "company"),
)

#: 角色名 -> 该角色被授予的权限编号（共 31 条映射）。
ROLE_PERMISSION_NUMBERS: dict[str, tuple[int, ...]] = {
    "employee": (1, 2, 6, 7),
    "manager": (1, 2, 3, 6, 7, 8),
    "finance_operator": (4, 9, 10, 11),
    "finance_lead": (4, 9, 10, 11, 13),
    "warehouse": (1, 2, 6, 7, 14, 15),
    "sysadmin": (18, 19),
    "auditor": (5, 12, 16, 17),
}

#: 目录规模的期望值，供 ``validate_catalog()`` 与测试共同引用。
EXPECTED_ROLE_COUNT = 7
EXPECTED_PERMISSION_COUNT = 19
EXPECTED_ROLE_PERMISSION_COUNT = 31

#: PLAN §9 强制规则：系统管理员不得拥有的 (resource, action) 组合。
SYSADMIN_FORBIDDEN: frozenset[tuple[str, str]] = frozenset(
    {
        ("expense_claim", "approve"),
        ("expense_claim", "finance_review"),
    }
)

#: PLAN §9 强制规则：审计人员不得拥有的动作（必须只读）。
AUDITOR_FORBIDDEN_ACTIONS: frozenset[str] = frozenset(
    {"create", "submit", "approve", "finance_review", "payment"}
)


def permission_by_number(number: int) -> PermissionSpec:
    """按编号取权限；未知编号直接报错（目录是封闭集合）。"""
    for spec in PERMISSIONS:
        if spec.number == number:
            return spec
    raise KeyError(f"unknown permission number: {number}")


def permissions_for_role(role_name: str) -> tuple[PermissionSpec, ...]:
    """返回某角色被授予的权限列表（按编号顺序），未知角色报错。"""
    if role_name not in ROLE_PERMISSION_NUMBERS:
        raise KeyError(f"unknown role: {role_name}")
    return tuple(
        permission_by_number(number)
        for number in ROLE_PERMISSION_NUMBERS[role_name]
    )


def validate_catalog() -> None:
    """自检目录，违反 PLAN §9 强制规则或规模不符时抛 ``ValueError``。

    供种子函数与测试调用：任何破坏强制规则的编辑都会立刻暴露。
    """
    problems: list[str] = []

    if len(ROLES) != EXPECTED_ROLE_COUNT:
        problems.append(f"expected {EXPECTED_ROLE_COUNT} roles, got {len(ROLES)}")
    if len(PERMISSIONS) != EXPECTED_PERMISSION_COUNT:
        problems.append(
            f"expected {EXPECTED_PERMISSION_COUNT} permissions, got {len(PERMISSIONS)}"
        )

    role_names = [spec.name for spec in ROLES]
    if len(set(role_names)) != len(role_names):
        problems.append("duplicate role names")
    if set(role_names) != set(ROLE_PERMISSION_NUMBERS):
        problems.append("ROLE_PERMISSION_NUMBERS does not cover exactly the roles")

    numbers = [spec.number for spec in PERMISSIONS]
    if numbers != list(range(1, EXPECTED_PERMISSION_COUNT + 1)):
        problems.append("permission numbers must be a contiguous 1..N sequence")

    keys = [spec.key for spec in PERMISSIONS]
    if len(set(keys)) != len(keys):
        problems.append("duplicate permission (resource, action, scope) triples")

    mapping_count = sum(len(v) for v in ROLE_PERMISSION_NUMBERS.values())
    if mapping_count != EXPECTED_ROLE_PERMISSION_COUNT:
        problems.append(
            f"expected {EXPECTED_ROLE_PERMISSION_COUNT} role-permission mappings, "
            f"got {mapping_count}"
        )

    valid_numbers = set(numbers)
    for role_name, role_numbers in ROLE_PERMISSION_NUMBERS.items():
        if len(set(role_numbers)) != len(role_numbers):
            problems.append(f"role {role_name} has duplicate permission numbers")
        unknown = set(role_numbers) - valid_numbers
        if unknown:
            problems.append(f"role {role_name} references unknown numbers {sorted(unknown)}")

    # 强制规则 1：sysadmin 不得拥有 approve / finance_review。
    sysadmin_actions = {
        (spec.resource, spec.action) for spec in permissions_for_role("sysadmin")
    }
    for forbidden in sorted(SYSADMIN_FORBIDDEN):
        if forbidden in sysadmin_actions:
            problems.append(f"sysadmin must not hold {forbidden[0]}/{forbidden[1]}")

    # 强制规则 2：auditor 只读。
    for spec in permissions_for_role("auditor"):
        if spec.action in AUDITOR_FORBIDDEN_ACTIONS:
            problems.append(
                f"auditor must be read-only but holds {spec.resource}/{spec.action}"
            )

    if problems:
        raise ValueError("invalid permission catalog: " + "; ".join(problems))


__all__ = [
    "AUDITOR_FORBIDDEN_ACTIONS",
    "EXPECTED_PERMISSION_COUNT",
    "EXPECTED_ROLE_COUNT",
    "EXPECTED_ROLE_PERMISSION_COUNT",
    "PERMISSIONS",
    "ROLES",
    "ROLE_PERMISSION_NUMBERS",
    "PermissionSpec",
    "RoleSpec",
    "SYSADMIN_FORBIDDEN",
    "permission_by_number",
    "permissions_for_role",
    "validate_catalog",
]
