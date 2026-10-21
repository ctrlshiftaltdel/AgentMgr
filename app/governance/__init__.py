"""Governance domain: trusted principals, versioned policy, PDP, grants and PEP.

The public entry point is :mod:`app.governance.service`, a thin facade over the
deterministic pieces.  This package deliberately contains **no HTTP or MCP
layer**; callers own the transport, the authentication session and the
transaction boundary.
"""

from __future__ import annotations

__all__: list[str] = []
