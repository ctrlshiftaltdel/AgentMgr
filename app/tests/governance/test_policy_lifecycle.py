"""Versioned policy store: publish/rollback history and audit (Issue #78)."""

from __future__ import annotations

from sqlalchemy import func, select

from app.audit.models import AuditEvent
from app.governance.errors import GovernanceError, PolicyError
from app.governance.models import GovernancePolicyVersion
from app.governance.policies import (
    create_draft,
    get_active,
    list_versions,
    publish,
    rollback,
)
from app.governance.principal import resolve_employee_principal
from app.tests.governance.helpers import (
    admin_principal,
    policy_document,
    unique_policy_set,
)


def _draft(session, org, policy_set_id: str, version: str = "1.0.0"):
    return create_draft(
        session,
        document=policy_document(policy_set_id, version=version),
        principal=admin_principal(session, org),
        created_by_employee_id=org.finance.id,
        actor_id=org.finance.id,
    )


def _audit(session, action: str, resource_id: str) -> AuditEvent | None:
    return session.scalar(
        select(AuditEvent).where(
            AuditEvent.action == action,
            AuditEvent.resource_id == resource_id,
        )
    )


def test_publish_switches_active_version_and_audits(session, org) -> None:
    policy_set_id = unique_policy_set()
    draft = _draft(session, org, policy_set_id)
    assert draft.status == "draft"
    assert get_active(session, policy_set_id) is None

    published = publish(
        session,
        policy_version_id=draft.id,
        principal=admin_principal(session, org),
        published_by_employee_id=org.finance.id,
        actor_id=org.finance.id,
    )
    assert published.status == "published"
    assert published.published_at is not None
    assert get_active(session, policy_set_id).id == published.id

    resource_id = f"{policy_set_id}@1.0.0"
    assert _audit(session, "governance.policy.draft_created", resource_id) is not None
    assert _audit(session, "governance.policy.published", resource_id) is not None


def test_publishing_new_version_retires_old_without_mutation(session, org) -> None:
    policy_set_id = unique_policy_set()
    first = _draft(session, org, policy_set_id, "1.0.0")
    publish(session, policy_version_id=first.id, principal=admin_principal(session, org), actor_id=org.finance.id)
    first_hash = first.content_hash
    first_document = dict(first.document)

    second = _draft(session, org, policy_set_id, "2.0.0")
    publish(session, policy_version_id=second.id, principal=admin_principal(session, org), actor_id=org.finance.id)

    session.refresh(first)
    assert first.status == "retired"
    assert first.retired_at is not None
    assert first.content_hash == first_hash
    assert first.document == first_document

    active = get_active(session, policy_set_id)
    assert active is not None
    assert active.version == "2.0.0"

    published_count = session.scalar(
        select(func.count())
        .select_from(GovernancePolicyVersion)
        .where(
            GovernancePolicyVersion.policy_set_id == policy_set_id,
            GovernancePolicyVersion.status == "published",
        )
    )
    assert published_count == 1
    assert [row.version for row in list_versions(session, policy_set_id)] == [
        "2.0.0",
        "1.0.0",
    ]


def test_rollback_creates_new_version_and_preserves_history(session, org) -> None:
    policy_set_id = unique_policy_set()
    first = _draft(session, org, policy_set_id, "1.0.0")
    publish(session, policy_version_id=first.id, principal=admin_principal(session, org), actor_id=org.finance.id)
    second = _draft(session, org, policy_set_id, "2.0.0")
    publish(session, policy_version_id=second.id, principal=admin_principal(session, org), actor_id=org.finance.id)

    rolled_back = rollback(
        session,
        policy_set_id=policy_set_id,
        target_version="1.0.0",
        principal=admin_principal(session, org),
        actor_id=org.finance.id,
    )
    assert rolled_back.status == "published"
    assert rolled_back.version == "2.0.1"
    assert rolled_back.supersedes_id == second.id
    assert rolled_back.document["version"] == "2.0.1"
    # The old rows are untouched.
    session.refresh(first)
    session.refresh(second)
    assert first.version == "1.0.0"
    assert second.version == "2.0.0"
    assert second.status == "retired"

    active = get_active(session, policy_set_id)
    assert active is not None
    assert active.id == rolled_back.id
    assert _audit(
        session,
        "governance.policy.rolled_back",
        f"{policy_set_id}@2.0.1",
    ) is not None


def test_publish_rejects_tampered_content_hash(session, org) -> None:
    policy_set_id = unique_policy_set()
    draft = _draft(session, org, policy_set_id)
    draft.document = {**draft.document, "version": "1.0.1"}
    session.flush()

    try:
        publish(session, policy_version_id=draft.id, principal=admin_principal(session, org), actor_id=org.finance.id)
    except PolicyError as exc:
        assert exc.reason_code == "policy_version_invalid"
    else:  # pragma: no cover
        raise AssertionError("tampered content hash must not be published")

    assert get_active(session, policy_set_id) is None


def test_rollback_rejects_draft_target(session, org) -> None:
    policy_set_id = unique_policy_set()
    first = _draft(session, org, policy_set_id, "1.0.0")
    publish(session, policy_version_id=first.id, principal=admin_principal(session, org), actor_id=org.finance.id)
    draft = _draft(session, org, policy_set_id, "2.0.0")
    assert draft.status == "draft"

    try:
        rollback(
            session,
            policy_set_id=policy_set_id,
            target_version="2.0.0",
            principal=admin_principal(session, org),
            actor_id=org.finance.id,
        )
    except PolicyError as exc:
        assert exc.reason_code == "policy_version_invalid"
    else:  # pragma: no cover
        raise AssertionError("a draft cannot be a rollback target")


def test_policy_management_requires_a_resolved_admin(session, org) -> None:
    policy_set_id = unique_policy_set()
    employee = resolve_employee_principal(session, employee_id=org.employee.id)

    try:
        create_draft(
            session,
            document=policy_document(policy_set_id),
            principal=employee,
            created_by_employee_id=org.employee.id,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    else:  # pragma: no cover
        raise AssertionError("a non-admin must not create policy drafts")

    draft = _draft(session, org, policy_set_id)
    try:
        publish(
            session,
            policy_version_id=draft.id,
            principal=employee,
            published_by_employee_id=org.employee.id,
        )
    except GovernanceError as exc:
        assert exc.reason_code == "capability_not_granted"
    else:  # pragma: no cover
        raise AssertionError("a non-admin must not publish policies")


def test_policy_document_validation_fails_closed(session, org) -> None:
    policy_set_id = unique_policy_set()
    bad = policy_document(policy_set_id)
    bad["default_effect"] = "allow"

    try:
        create_draft(session, document=bad, principal=admin_principal(session, org), created_by_employee_id=org.finance.id)
    except PolicyError as exc:
        assert exc.reason_code == "policy_version_invalid"
    else:  # pragma: no cover
        raise AssertionError("default_effect=allow must be rejected")
