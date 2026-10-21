"""模型模块导入自足性测试（任务 W1-24）。

每个模型模块都必须**单独导入**即可完成 ORM 配置：导入后 ``configure_mappers()``
通过，且该模块声明的每一个外键都能解析到被引用表（``sorted_tables`` 可拓扑排序）。
不得依赖 ``migrations/env.py`` 或 ``app/tests/conftest.py`` 的「统一导入」规避——
那正是 W1-24 要消除的隐性依赖：``app/integrations/models.py`` 的
``source_file_id`` 指向 ``source_files``（定义在 ``app.documents.models``），
单独导入时曾找不到被引用表。

为什么用子进程逐模块探测
------------------------
``app.db.base.Base.metadata`` 在同一进程内是全局共享的：只要进程里任何一个测试
（或 conftest）导入过全部模型，后续检查就再也无法暴露「某个模块漏了显式导入」的
缺陷。因此本测试对每个模块启动**独立子进程**，在全新解释器中只导入该模块，
完全复现「单独导入任一模型模块」的场景。

为什么断言里除 ``configure_mappers()`` 还解析外键
------------------------------------------------
``configure_mappers()`` 只配置 mapper 与关系；在没有任何 ``relationship()`` 的
模块上，它不会触发外键目标表的解析，因此对「外键指向未导入的表」这一缺陷是
**沉默**的。真正的自足性判据是：元数据里的每个外键都能解析（``fk.column``），
且 ``sorted_tables`` 能完成 FK 依赖的拓扑排序——这也是 Alembic autogenerate 与
``create_all`` 实际会走到的路径。子进程依次执行 ``configure_mappers()``、逐个
解析 ``fk.column``、再做 ``sorted_tables``，任一环节失败即判失败。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 全部模型模块（``app/*/models.py`` 与 ``app/db/models/*.py``）。
MODEL_MODULES: tuple[str, ...] = (
    "app.audit.models",
    "app.db.models",
    "app.db.models.organization",
    "app.documents.models",
    "app.expense.models",
    "app.governance.models",
    "app.identity.models",
    "app.intake.models",
    "app.integrations.models",
    "app.workflow.models",
)

#: 在全新解释器中单独导入一个模型模块并校验 ORM 自足性的探针。
#: ``sys.argv[1]`` 为待测模块名；任一环节抛出异常即非零退出。
_PROBE = """
import importlib
import sys

importlib.import_module(sys.argv[1])

from sqlalchemy.orm import configure_mappers

configure_mappers()

from app.db.base import Base

# 解析每个外键的目标（被引用表未导入时抛 NoReferencedTableError），
# 再强制按外键依赖做一次拓扑排序。
for _table in Base.metadata.tables.values():
    for _fk in _table.foreign_keys:
        _fk.column
Base.metadata.sorted_tables
"""


@pytest.mark.parametrize("module_name", MODEL_MODULES)
def test_model_module_import_is_self_sufficient(module_name: str) -> None:
    """单独导入 ``module_name`` 后必须能完成 ``configure_mappers`` 与外键解析。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module_name],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"单独导入 {module_name} 后 ORM 配置失败（退出码 {result.returncode}）：\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
