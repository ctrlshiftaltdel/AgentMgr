"""B1 动作注册表 / Task Intake 的 DB-free 契约测试。

这些测试不连接数据库：未知/停用动作、客户端权威字段、审批拒绝、幂等键派生与
签名契约都在写库之前判定。真实身份拒绝与任务去重需要 PostgreSQL，见
``test_intake_db.py``（由 Master 在测试机运行）。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

import pytest

import app.tasks.intake as intake_module
from app.governance.principal import GovernancePrincipal
from app.identity.authorization import Principal
from app.tasks.intake import (
    ACTION_NOT_AVAILABLE,
    B1_ACTION_REGISTRY,
    CLIENT_AUTHORITY_FIELDS,
    IDEMPOTENCY_KEY_PREFIX,
    INTAKE_ACCEPTED,
    INTAKE_DUPLICATE,
    INTAKE_REJECTED,
    ActionRegistry,
    ActionRegistryError,
    ActionSpec,
    TaskIntakeResult,
    channel_idempotency_key,
    submit_action,
)

TEST_CAPABILITY = "expense.read@1.0.0"
TEST_POLICY_SET = "polset_wechat_b1_test"
TEST_SCOPE = {"scope_type": "self", "resource_ids": ["res_wechat_0001"]}


def _goal(summary: str = "WeChat B1 test read") -> dict[str, Any]:
    return {
        "summary": summary,
        "completion_criteria": [
            {
                "criterion_id": "crit_wechat_0001",
                "type": "business_state_equals",
                "verifier": {
                    "verifier_id": "ver_wechat_0001",
                    "version": "1.0.0",
                },
                "resource_ref": "res_wechat_0001",
                "expected": "read",
                "evidence_ref": "evidence:wechat_0001",
            }
        ],
        "required_inputs": [],
        "out_of_scope": [],
    }


def _spec(
    *,
    action_id: str = "test_read",
    final_mode: str = "advisory",
    approval_required: bool = False,
    enabled: bool = True,
    capability: str = TEST_CAPABILITY,
    policy_set: str = TEST_POLICY_SET,
) -> ActionSpec:
    return ActionSpec(
        action_id=action_id,
        capability=capability,
        final_mode=final_mode,
        policy_set=policy_set,
        approval_required=approval_required,
        allowed_scope=TEST_SCOPE,
        enabled=enabled,
    )


def _registry(*specs: ActionSpec) -> ActionRegistry:
    return ActionRegistry(specs or (_spec(),))


def _principal() -> GovernancePrincipal:
    return GovernancePrincipal(
        principal_id="prin_0001",
        principal_type="person",
        employee_id=1,
        service_identity_id=None,
        company_id=10,
        role_ids=(),
        account_status="active",
        identity_assurance="verified_session",
    )


# --- registry ------------------------------------------------------------------


def test_enabled_action_requires_a_server_side_allowed_scope() -> None:
    with pytest.raises(ValueError):
        ActionRegistry(
            (
                ActionSpec(
                    action_id="test_read",
                    capability=TEST_CAPABILITY,
                    final_mode="advisory",
                    policy_set=TEST_POLICY_SET,
                    allowed_scope=None,
                    enabled=True,
                ),
            )
        )


def test_unknown_action_is_a_stable_rejection() -> None:
    with pytest.raises(ActionRegistryError) as excinfo:
        _registry().lookup("nope")
    assert excinfo.value.reason == "unknown_action"


def test_disabled_action_is_action_not_available() -> None:
    with pytest.raises(ActionRegistryError) as excinfo:
        _registry(_spec(enabled=False)).lookup("test_read")
    assert excinfo.value.reason == ACTION_NOT_AVAILABLE


def test_registry_is_closed_and_rejects_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        ActionRegistry((_spec(), _spec()))


@pytest.mark.parametrize(
    "spec",
    [
        _spec(action_id=""),
        _spec(capability="expense.read"),
        _spec(final_mode="auto_upgrade"),
        _spec(policy_set=""),
    ],
    ids=["blank-id", "unversioned-capability", "unknown-mode", "blank-policy"],
)
def test_registry_validates_specs(spec: ActionSpec) -> None:
    with pytest.raises(ValueError):
        ActionRegistry((spec,))


def test_b1_production_registry_has_no_enabled_task_actions() -> None:
    assert B1_ACTION_REGISTRY.action_ids() == (
        "document_forward",
        "inventory_entry",
        "policy_lookup",
    )
    for action_id in B1_ACTION_REGISTRY.action_ids():
        with pytest.raises(ActionRegistryError) as excinfo:
            B1_ACTION_REGISTRY.lookup(action_id)
        assert excinfo.value.reason == ACTION_NOT_AVAILABLE


def test_b1_registry_rejects_unknown_action_without_touching_db() -> None:
    result = submit_action(
        None,  # type: ignore[arg-type] - 查找在写库之前完成
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="not_registered",
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == "unknown_action"


def test_b1_disabled_action_is_rejected_without_touching_db() -> None:
    result = submit_action(
        None,  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="policy_lookup",
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == ACTION_NOT_AVAILABLE


# --- client authority fields ---------------------------------------------------


def test_submit_action_signature_has_no_authority_parameters() -> None:
    params = set(inspect.signature(submit_action).parameters)
    assert params.isdisjoint(
        {
            "approver",
            "employee_id",
            "execution_mode",
            "owner",
            "principal_id",
            "requested_capabilities",
        }
    )
    assert {"action_id", "channel_type", "external_message_id", "external_user_id"} <= params


@pytest.mark.parametrize("field", sorted(CLIENT_AUTHORITY_FIELDS))
def test_client_supplied_authority_fields_are_rejected(field: str) -> None:
    result = submit_action(
        None,  # type: ignore[arg-type] - 拒绝发生在写库之前
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={field: "client-value", "goal": _goal()},
        registry=_registry(),
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == "client_authority_field"


def test_missing_goal_payload_is_rejected_before_identity() -> None:
    result = submit_action(
        None,  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={"summary": "no goal"},
        registry=_registry(),
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == "invalid_payload"


def test_approval_required_action_is_rejected_in_b1() -> None:
    result = submit_action(
        None,  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(_spec(approval_required=True)),
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == "approval_not_supported"


@pytest.mark.parametrize(
    ("channel_type", "external_user_id", "external_message_id", "reason"),
    [
        ("wecom", "wx-user", "wx-user:1", "unsupported_channel"),
        ("weixin", "", "wx-user:1", "missing_sender_id"),
        ("weixin", "wx-user", "", "missing_event_key"),
        ("weixin", "wx-user", "   ", "missing_event_key"),
    ],
)
def test_structural_rejections_do_not_touch_identity(
    channel_type: str,
    external_user_id: str,
    external_message_id: str,
    reason: str,
) -> None:
    result = submit_action(
        None,  # type: ignore[arg-type]
        channel_type=channel_type,
        external_user_id=external_user_id,
        external_message_id=external_message_id,
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(),
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == reason


# --- mode / owner / capability contract (fakes; no DB) -------------------------


@dataclass
class _FakeTask:
    task_id: str = "task_0001"


def test_final_mode_comes_only_from_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_submit(session: Any, **kwargs: Any) -> _FakeTask:
        captured.update(kwargs)
        return _FakeTask()

    monkeypatch.setattr(intake_module, "resolve_principal", lambda *a, **k: Principal(1, 10))
    monkeypatch.setattr(
        intake_module, "resolve_employee_principal", lambda *a, **k: _principal()
    )
    monkeypatch.setattr(intake_module, "_find_existing_task", lambda *a, **k: None)
    monkeypatch.setattr(intake_module, "submit_task", fake_submit)

    result = submit_action(
        object(),  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(_spec(final_mode="governed_resource_access")),
    )

    assert result.status == INTAKE_ACCEPTED
    assert result.task_id == "task_0001"
    assert captured["execution_mode"] == "governed_resource_access"
    assert captured["requested_capabilities"] == (TEST_CAPABILITY,)
    assert captured["policy_set_id"] == TEST_POLICY_SET
    assert captured["requester"] is captured["owner"] is captured["executor"]
    assert captured["requester"].principal_id == "prin_0001"
    assert captured["requester"].employee_id == 1
    assert "approver" not in captured


def test_same_event_is_duplicate_and_returns_same_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(intake_module, "resolve_principal", lambda *a, **k: Principal(1, 10))
    monkeypatch.setattr(
        intake_module, "resolve_employee_principal", lambda *a, **k: _principal()
    )
    monkeypatch.setattr(
        intake_module,
        "_find_existing_task",
        lambda *a, **k: _FakeTask(task_id="task_existing"),
    )
    calls: list[str] = []

    def fake_submit(session: Any, **kwargs: Any) -> _FakeTask:
        calls.append(kwargs["idempotency_key"])
        return _FakeTask(task_id="task_existing")

    monkeypatch.setattr(intake_module, "submit_task", fake_submit)

    result = submit_action(
        object(),  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(),
    )
    assert result.status == INTAKE_DUPLICATE
    assert result.task_id == "task_existing"
    # 仍调用任务核心做 canonical request 校验；同请求不产生第二个任务。
    assert len(calls) == 1


def test_same_event_with_changed_payload_is_a_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.errors import IdempotencyConflict

    monkeypatch.setattr(intake_module, "resolve_principal", lambda *a, **k: Principal(1, 10))
    monkeypatch.setattr(
        intake_module, "resolve_employee_principal", lambda *a, **k: _principal()
    )
    monkeypatch.setattr(
        intake_module,
        "_find_existing_task",
        lambda *a, **k: _FakeTask(task_id="task_existing"),
    )

    def conflict(session: Any, **kwargs: Any) -> _FakeTask:
        raise IdempotencyConflict("task_existing", idempotency_key=kwargs["idempotency_key"])

    monkeypatch.setattr(intake_module, "submit_task", conflict)

    result = submit_action(
        object(),  # type: ignore[arg-type]
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
        action_id="test_read",
        payload={"goal": _goal()},
        registry=_registry(),
    )
    assert result.status == INTAKE_REJECTED
    assert result.reason == "task_idempotency_conflict"
    assert result.task_id is None


# --- idempotency + result repr -------------------------------------------------


def test_idempotency_key_is_deterministic_and_event_scoped() -> None:
    first = channel_idempotency_key(
        channel_type="weixin",
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
    )
    second = channel_idempotency_key(
        channel_type="weixin",
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000000",
    )
    other_user = channel_idempotency_key(
        channel_type="weixin",
        external_user_id="wx-other",
        external_message_id="wx-other:1700000000000",
    )
    other_event = channel_idempotency_key(
        channel_type="weixin",
        external_user_id="wx-user",
        external_message_id="wx-user:1700000000001",
    )
    assert first == second
    assert first.startswith(IDEMPOTENCY_KEY_PREFIX)
    assert first != other_user
    assert first != other_event
    assert "wx-user" not in first


def test_idempotency_key_uses_no_message_text() -> None:
    params = inspect.signature(channel_idempotency_key).parameters
    assert "text" not in params
    assert "message_text" not in params


def test_task_intake_result_repr_hides_reason() -> None:
    result = TaskIntakeResult(status=INTAKE_REJECTED, reason="unbound")
    assert "unbound" not in repr(result)
    assert "unbound" not in str(result)
