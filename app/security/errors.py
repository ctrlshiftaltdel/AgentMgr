"""Stable errors for the managed execution security substrate.

The codes are the union of the design's supervisor error set (§11.6) and the
gateway reason-code set (§10.3), plus the two credential-shape codes used by the
credential layer.  They are a frozen vocabulary: adding a code is a
backwards-compatible change, renaming or reusing one is not.

Safety rules
------------

* ``str(error)`` contains only the stable code and an optional stable detail
  token.  It never interpolates a path, unit internals, a token, an exception
  body or a stack trace.
* ``detail`` is therefore restricted to a short ``[a-z0-9_]`` token.  Passing a
  path, a token-like value or free-form text is a programming error and raises
  :class:`ValueError` at construction time rather than leaking later.
* Unknown codes are rejected at construction time so a typo cannot silently
  become an unrecognised security event.
"""

from __future__ import annotations

import re

__all__ = ["SECURITY_ERROR_CODES", "SecurityError"]

#: Every stable code defined by the design (supervisor §11.6, gateway §10.3,
#: credentials §9.1).  Adding a code is backwards compatible; do not redefine.
SECURITY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "backend_error",
        "bad_request",
        "busy",
        "capability_not_granted",
        "credential_expired",
        "credential_key_invalid",
        "credential_revoked",
        "cross_task_denied",
        "execution_mode_mismatch",
        "filesystem_denied",
        "grant_expired",
        "grant_missing",
        "grant_revoked",
        "invalid_claim",
        "invalid_spec",
        "network_denied",
        "payload_too_large",
        "privilege_required",
        "rate_limited",
        "resource_denied",
        "resource_scope_denied",
        "timeout",
        "token_invalid",
        "token_not_yet_valid",
        "unit_start_failed",
        "unit_stop_unconfirmed",
        "unknown",
        "unsupported_version",
    }
)


class SecurityError(Exception):
    """A fail-closed security rejection with a stable, safe message.

    ``code`` is always one of :data:`SECURITY_ERROR_CODES`.  ``detail`` is an
    optional stable token such as ``"unit_name_collision"``; it exists for
    callers and audits, never for the untrusted task.
    """

    __slots__ = ("code", "detail")

    def __init__(self, code: str, detail: str | None = None) -> None:
        if not isinstance(code, str) or code not in SECURITY_ERROR_CODES:
            raise ValueError("unknown security error code")
        if detail is not None and not _is_safe_detail(detail):
            raise ValueError("security error detail must be a stable token")
        self.code = code
        self.detail = detail
        super().__init__(code)

    def __str__(self) -> str:
        if self.detail is None:
            return self.code
        return f"{self.code}: {self.detail}"

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(code={self.code!r}, "
            f"detail={self.detail!r})"
        )


_DETAIL_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _is_safe_detail(detail: object) -> bool:
    return isinstance(detail, str) and _DETAIL_PATTERN.fullmatch(detail) is not None
