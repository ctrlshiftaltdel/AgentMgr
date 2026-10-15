"""Unified error-code mapping and information-leak tests (W1-11 point 9).

* Domain errors map to the documented status codes with a uniform body
  ``{"error": {"code", "message"}}``.
* No error response leaks stack traces, database DSNs, SQL or driver names.
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from app.tests.api.conftest import World

#: Substrings that must never appear in any error response body.
_FORBIDDEN_SUBSTRINGS = (
    "Traceback",
    "psycopg",
    "postgresql://",
    "SELECT ",
    "sqlalchemy",
    "File \"",
)


def _assert_clean(text: str) -> None:
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in text, f"response leaked {needle!r}: {text!r}"


def _submitted(
    client: TestClient, headers: dict[str, str], world: World
) -> int:
    created = client.post("/claims", json={"currency": "CNY"}, headers=headers)
    claim_id = int(created.json()["id"])
    client.post(
        f"/claims/{claim_id}/lines",
        json={
            "expense_date": "2026-10-01",
            "category_id": world.category.id,
            "description": "打车",
            "amount": "10.00",
        },
        headers=headers,
    )
    client.post(f"/claims/{claim_id}/submit", headers=headers)
    return claim_id


def test_domain_error_bodies_are_uniform_and_do_not_leak(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    claimant = auth(world.claimant)
    outsider = auth(world.outsider)
    claim_id = _submitted(client, claimant, world)

    forbidden = client.get(f"/claims/{claim_id}", headers=outsider)  # 403
    conflict = client.post(f"/claims/{claim_id}/submit", headers=claimant)  # 409
    missing = client.get("/claims/99999999", headers=claimant)  # 404

    assert forbidden.status_code == 403
    assert conflict.status_code == 409
    assert missing.status_code == 404

    for response in (forbidden, conflict, missing):
        body = response.json()
        assert set(body.keys()) == {"error"}
        assert set(body["error"].keys()) == {"code", "message"}
        assert isinstance(body["error"]["code"], str)
        assert isinstance(body["error"]["message"], str)
        _assert_clean(response.text)


def test_request_validation_error_is_uniform(
    client: TestClient, world: World, auth: Callable[..., dict[str, str]]
) -> None:
    # Missing required fields / wrong types → 422 with the uniform body.
    response = client.post(
        "/claims/1/lines",
        json={"description": "no amount"},
        headers=auth(world.claimant),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    _assert_clean(response.text)


def test_unknown_route_returns_uniform_404(client: TestClient) -> None:
    response = client.get("/definitely-not-a-route")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_storage_error_maps_to_400_without_leak() -> None:
    from app.api.app import create_app
    from app.documents.storage import PathTraversalError

    app = create_app()

    @app.get("/__boom_storage")
    def _boom() -> None:
        raise PathTraversalError("..%2f escape attempt")

    with TestClient(app) as test_client:
        response = test_client.get("/__boom_storage")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"
    _assert_clean(response.text)


def test_unexpected_error_returns_generic_500_without_leak() -> None:
    from app.api.app import create_app

    app = create_app()

    @app.get("/__boom_internal")
    def _boom() -> None:
        raise RuntimeError(
            "psycopg failed on postgresql://user:secret@localhost/db Traceback SELECT 1"
        )

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/__boom_internal")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    _assert_clean(response.text)


def test_expected_exception_handlers_are_registered() -> None:
    from app.api.app import create_app
    from app.documents.storage import (
        FileTooLargeError,
        FileTypeMismatchError,
        HashMismatchError,
        PathTraversalError,
    )
    from app.expense.service import (
        AuthorizationDeniedError,
        ClaimNotFoundError,
        ValidationError,
    )
    from app.expense.state_machine import InvalidTransitionError
    from app.workflow.service import (
        InvalidDecisionError,
        StepAlreadyDecidedError,
        WorkflowInstanceNotFoundError,
    )

    app = create_app()
    registered = set(app.exception_handlers)
    expected = {
        AuthorizationDeniedError,
        InvalidTransitionError,
        StepAlreadyDecidedError,
        InvalidDecisionError,
        ValidationError,
        ClaimNotFoundError,
        WorkflowInstanceNotFoundError,
        PathTraversalError,
        FileTypeMismatchError,
        FileTooLargeError,
        HashMismatchError,
    }
    assert expected <= registered, expected - registered
