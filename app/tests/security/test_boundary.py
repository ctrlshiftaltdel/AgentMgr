"""Boundary, spec and error model tests for the managed execution substrate.

These tests are deliberately pure in-memory: no database, no privilege probe and
no host mutation.  They pin the *structure* of the trusted boundary (exact field
sets, normalisation, subset rules, unit-name derivation and the honesty of the
:class:`BoundaryClaim`) rather than any sandbox behaviour.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from app.security.boundary import (
    BYPASSABLE_BOUNDARY,
    CONTRACT_VERSION,
    DEFERRED_BOUNDARY,
    ENFORCED_BOUNDARY,
    SUBSTRATE,
    BoundaryClaim,
    CleanupResult,
    GrantStatus,
    LaunchHandle,
    ManagedTaskSpec,
    ResourceScope,
    RuntimeObservation,
    StopResult,
    unit_name_for_task,
)
from app.security.errors import SecurityError

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
TASK_ID = "task_synthetic_0001"
GRANT_ID = "grnt_synthetic_0001"
CAPABILITY = "resource.read@1.0.0"
SUBJECT_ID = "prin_synthetic_0001"


def _scope_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "scope_type": "self",
        "resource_ids": ["res_synthetic_0001"],
        "connector_ids": [],
        "data_classifications": ["internal"],
        "exclude_resource_ids": [],
    }
    value.update(overrides)
    return value


def _spec_mapping(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "task_id": TASK_ID,
        "grant_id": GRANT_ID,
        "execution_mode": "managed_execution",
        "ttl_seconds": 60,
        "cpu_quota_percent": 50,
        "memory_max_mb": 256,
        "tasks_max": 32,
        "capabilities": [CAPABILITY],
        "resource_scope": _scope_mapping(),
        "backend": "file",
    }
    value.update(overrides)
    return value


def _grant_status(**overrides: object) -> GrantStatus:
    value: dict[str, object] = {
        "grant_id": GRANT_ID,
        "task_id": TASK_ID,
        "subject_id": SUBJECT_ID,
        "execution_mode": "managed_execution",
        "status": "active",
        "usable": True,
        "expires_at": NOW + timedelta(seconds=60),
        "revocation_reason": None,
        "capabilities": (CAPABILITY,),
        "resource_scope": ResourceScope.from_mapping(_scope_mapping()),
    }
    value.update(overrides)
    return GrantStatus(**value)  # type: ignore[arg-type]


def _boundary_claim() -> BoundaryClaim:
    return BoundaryClaim(
        contract_version=CONTRACT_VERSION,
        substrate=SUBSTRATE,
        network_profile="private_netns",
        product_claim="synthetic_substrate_only",
        enforced=ENFORCED_BOUNDARY,
        bypassable=BYPASSABLE_BOUNDARY,
        deferred=DEFERRED_BOUNDARY,
    )


def test_managed_task_spec_accepts_exact_fields() -> None:
    spec = ManagedTaskSpec.from_mapping(_spec_mapping())

    assert spec == ManagedTaskSpec(
        task_id=TASK_ID,
        grant_id=GRANT_ID,
        execution_mode="managed_execution",
        ttl_seconds=60,
        cpu_quota_percent=50,
        memory_max_mb=256,
        tasks_max=32,
        capabilities=(CAPABILITY,),
        resource_scope=ResourceScope(
            scope_type="self",
            resource_ids=("res_synthetic_0001",),
            connector_ids=(),
            data_classifications=("internal",),
            exclude_resource_ids=(),
        ),
        backend="file",
    )
    assert spec.resource_scope.as_dict() == _scope_mapping()
    spec.validate(NOW)


def test_managed_task_spec_round_trips_via_as_dict() -> None:
    spec = ManagedTaskSpec.from_mapping(_spec_mapping())

    assert ManagedTaskSpec.from_mapping(spec.as_dict()) == spec
    assert spec.as_dict()["resource_scope"] == _scope_mapping()
    assert spec.as_dict()["capabilities"] == [CAPABILITY]


@pytest.mark.parametrize(
    "mapping",
    [
        _spec_mapping(grant_expires_at="2026-01-01T12:01:00Z"),
        _spec_mapping(workspace="/srv/work"),
        _spec_mapping(state="/var/lib/agentmgr-exec/state"),
        _spec_mapping(resource_scope=_scope_mapping(workspace="/srv/work")),
    ],
    ids=["grant_expires_at", "workspace", "state", "nested_unknown"],
)
def test_managed_task_spec_rejects_unknown_fields(mapping: dict[str, object]) -> None:
    with pytest.raises(SecurityError) as raised:
        ManagedTaskSpec.from_mapping(mapping)

    assert raised.value.code == "invalid_spec"
    assert raised.value.detail == "unknown_field"


@pytest.mark.parametrize(
    "overrides",
    [
        {"task_id": "task_syn\x00thetic"},
        {"task_id": "task_../escape"},
        {"task_id": "task_/opt/worker"},
        {"grant_id": "grnt_syn\x1fthetic"},
        {"workspace": "/opt/example/work"},
        {"state": "/opt/example/state"},
    ],
    ids=[
        "task_nul",
        "task_parent_traversal",
        "task_absolute_path",
        "grant_control_char",
        "workspace_absolute_path",
        "state_absolute_path",
    ],
)
def test_managed_task_spec_rejects_control_chars_and_paths(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(SecurityError) as raised:
        ManagedTaskSpec.from_mapping(_spec_mapping(**overrides))

    assert raised.value.code == "invalid_spec"


@pytest.mark.parametrize(
    ("field", "value", "accepted"),
    [
        ("ttl_seconds", 0, False),
        ("ttl_seconds", 1, True),
        ("ttl_seconds", 300, True),
        ("ttl_seconds", 301, False),
        ("memory_max_mb", 31, False),
        ("memory_max_mb", 32, True),
        ("memory_max_mb", 1024, True),
        ("memory_max_mb", 1025, False),
        ("cpu_quota_percent", 0, False),
        ("cpu_quota_percent", 1, True),
        ("cpu_quota_percent", 100, True),
        ("cpu_quota_percent", 101, False),
        ("tasks_max", 0, False),
        ("tasks_max", 1, True),
        ("tasks_max", 64, True),
        ("tasks_max", 65, False),
    ],
)
def test_ttl_and_limits_boundaries(field: str, value: int, accepted: bool) -> None:
    spec = ManagedTaskSpec.from_mapping(_spec_mapping(**{field: value}))

    if accepted:
        assert spec.validate(NOW) is None
    else:
        with pytest.raises(SecurityError) as raised:
            spec.validate(NOW)
        assert raised.value.code == "invalid_spec"


def test_mode_backend_capabilities_and_scope_are_closed() -> None:
    with pytest.raises(SecurityError) as wrong_mode:
        ManagedTaskSpec.from_mapping(
            _spec_mapping(execution_mode="governed_auto")
        ).validate(NOW)
    assert wrong_mode.value.code == "execution_mode_mismatch"

    with pytest.raises(SecurityError) as wrong_backend:
        ManagedTaskSpec.from_mapping(_spec_mapping(backend="database")).validate(NOW)
    assert wrong_backend.value.code == "invalid_spec"

    for capabilities in (
        [],
        [CAPABILITY, CAPABILITY],
        ["Resource.Read@1.0.0"],
        ["resource.read"],
        [f"cap.item{i}@1.0.0" for i in range(17)],
    ):
        with pytest.raises(SecurityError) as raised:
            ManagedTaskSpec.from_mapping(
                _spec_mapping(capabilities=capabilities)
            ).validate(NOW)
        assert raised.value.code == "capability_not_granted"

    for scope in (
        _scope_mapping(scope_type="department"),
        _scope_mapping(connector_ids=["conn_synthetic_0001"]),
        _scope_mapping(data_classifications=["confidential"]),
    ):
        with pytest.raises(SecurityError) as raised:
            ManagedTaskSpec.from_mapping(
                _spec_mapping(resource_scope=scope)
            ).validate(NOW)
        assert raised.value.code == "resource_scope_denied"


def test_resource_scope_subset_rejects_expansion() -> None:
    base = ResourceScope.from_mapping(
        _scope_mapping(
            resource_ids=["res_synthetic_0001", "res_synthetic_0002"],
            exclude_resource_ids=["res_synthetic_0002"],
        )
    )
    narrower = ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001",),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    )

    assert narrower.is_subset_of(base)
    assert not ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001", "res_synthetic_0003"),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    ).is_subset_of(base)
    assert not ResourceScope(
        scope_type="department",
        resource_ids=("res_synthetic_0001",),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    ).is_subset_of(base)
    assert not ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001",),
        connector_ids=("conn_synthetic_0001",),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    ).is_subset_of(base)
    assert not ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001",),
        connector_ids=(),
        data_classifications=("internal", "public"),
        exclude_resource_ids=("res_synthetic_0002",),
    ).is_subset_of(base)


def test_resource_scope_exclude_is_subset() -> None:
    base = ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001", "res_synthetic_0002"),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    )
    keeps_exclusion = ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001",),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=("res_synthetic_0002",),
    )
    drops_exclusion = ResourceScope(
        scope_type="self",
        resource_ids=("res_synthetic_0001",),
        connector_ids=(),
        data_classifications=("internal",),
        exclude_resource_ids=(),
    )

    assert keeps_exclusion.is_subset_of(base)
    assert not drops_exclusion.is_subset_of(base)


def test_unit_name_is_deterministic_and_strict() -> None:
    expected = "agentmgr-task-" + hashlib.sha256(
        b"agentmgr-task:v1:task_synthetic_0001"
    ).hexdigest()[:12]

    assert unit_name_for_task(TASK_ID) == expected
    assert re.fullmatch(r"^agentmgr-task-[0-9a-f]{12}$", expected)
    assert unit_name_for_task("task_synthetic_0002") != expected
    with pytest.raises(SecurityError):
        unit_name_for_task("task_../escape")


def test_boundary_claim_contains_all_three_sections() -> None:
    claim = _boundary_claim()

    assert claim.product_claim == "synthetic_substrate_only"
    assert claim.network_profile == "private_netns"
    for section in (claim.enforced, claim.bypassable, claim.deferred):
        assert isinstance(section, tuple)
        assert section
        assert all(isinstance(item, str) and "/" not in item for item in section)
    assert set(claim.enforced) == {
        "per_task_dynamic_uid",
        "filesystem_read_only",
        "no_host_home_access",
        "separate_private_tmp",
        "no_capabilities",
        "no_new_privileged_namespaces",
        "no_ip_network",
        "resource_and_time_limits",
        "cross_task_state_isolation",
        "task_bound_revocable_credential",
        "stop_and_exit_evidence_separated",
        "gateway_admission_limits",
        "execution_mode_cross_checked",
    }
    assert "gateway_socket_only_external_entry" not in claim.enforced
    assert set(claim.bypassable) == {
        "host_root_or_controller_sudo",
        "systemd_or_kernel_vulnerability",
        "side_channels",
        "physical_access",
        "gateway_backend_defect",
        "resource_path_without_gateway",
        "same_host_user_gateway_socket_connect",
        "same_uid_child_processes",
        "uid_reuse_window",
        "host_unix_sockets_outside_masked_dirs",
        "host_world_readable_files",
    }
    assert {
        "hermes_internal_tools_in_sandbox",
        "model_provider_egress",
        "real_database_or_object_store_backend",
    } <= set(claim.deferred)

    assert claim.as_dict() == {
        "contract_version": CONTRACT_VERSION,
        "substrate": SUBSTRATE,
        "network_profile": "private_netns",
        "product_claim": "synthetic_substrate_only",
        "enforced": list(claim.enforced),
        "bypassable": list(claim.bypassable),
        "deferred": list(claim.deferred),
    }


def test_grant_status_requires_subject_id() -> None:
    status = _grant_status()

    assert isinstance(status.subject_id, str)
    assert re.fullmatch(
        r"^(prin|svc|rt)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$", status.subject_id
    )
    for invalid in (None, 123, "", "user_synthetic_0001"):
        with pytest.raises(SecurityError):
            _grant_status(subject_id=invalid)


def test_runtime_records_expose_contract_fields() -> None:
    unit = unit_name_for_task(TASK_ID)
    claim = _boundary_claim()
    observation = RuntimeObservation(
        state="completed",
        process_gone=True,
        stop_confirmed=False,
        never_started=False,
        main_pid=0,
        result="success",
        exec_main_status=0,
    )
    handle = LaunchHandle(
        unit=unit,
        task_id=TASK_ID,
        grant_id=GRANT_ID,
        jti="cred_" + "a" * 32,
        expires_at=NOW + timedelta(seconds=60),
        network_profile="private_netns",
        boundary_claim=claim,
        launched_at=NOW,
    )
    stop = StopResult(
        stop_confirmed=True,
        process_gone=True,
        result="success",
        exec_main_status=0,
    )
    cleanup = CleanupResult(
        units_remaining=0,
        state_dir_removed=True,
        credential_removed=True,
    )

    assert observation.process_gone is True
    assert observation.stop_confirmed is False
    assert observation.never_started is False
    assert handle.unit == unit
    assert handle.boundary_claim is claim
    assert stop.stop_confirmed is True
    assert cleanup.units_remaining == 0


def test_errors_do_not_include_context_paths() -> None:
    error = SecurityError("invalid_spec", "bad")

    assert error.code == "invalid_spec"
    assert error.detail == "bad"
    for rendered in (str(error), repr(error)):
        assert "/home" not in rendered
        assert "agx1" not in rendered
        assert "\x00" not in rendered

    with pytest.raises(ValueError):
        SecurityError("invalid_spec", "/opt/example/secret")
    with pytest.raises(ValueError):
        SecurityError("invalid_spec", "agx1." + "a" * 32)
    with pytest.raises(ValueError):
        SecurityError("not_a_stable_code")
