"""Managed execution security substrate (Issue #82).

Public surface of the trusted boundary: the validated task spec, the live Grant
projection, the truthful boundary claim, unit-name derivation and the
launch/stop/cleanup evidence records.  Credentials, the PEP gateway, systemd
argv and the supervisor build on these names in later tasks.

This package must stay importable without a database or a privileged host.
"""

from __future__ import annotations

from app.security.boundary import (
    BACKEND_FILE,
    BYPASSABLE_BOUNDARY,
    CONTRACT_VERSION,
    DEFERRED_BOUNDARY,
    ENFORCED_BOUNDARY,
    EXECUTION_MODE_MANAGED,
    MAX_CAPABILITIES,
    MAX_CPU_QUOTA_PERCENT,
    MAX_MEMORY_MAX_MB,
    MAX_TASKS_MAX,
    MAX_TTL_SECONDS,
    MIN_CPU_QUOTA_PERCENT,
    MIN_MEMORY_MAX_MB,
    MIN_TASKS_MAX,
    MIN_TTL_SECONDS,
    NETWORK_PROFILES,
    NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK,
    NETWORK_PROFILE_PRIVATE_NETNS,
    PRODUCT_CLAIM,
    SUBSTRATE,
    BoundaryClaim,
    CleanupResult,
    GrantStatus,
    GrantStatusProvider,
    LaunchHandle,
    ManagedTaskSpec,
    ResourceScope,
    RuntimeObservation,
    StopResult,
    unit_name_for_task,
)
from app.security.errors import SECURITY_ERROR_CODES, SecurityError

__all__ = [
    "BACKEND_FILE",
    "BYPASSABLE_BOUNDARY",
    "CONTRACT_VERSION",
    "DEFERRED_BOUNDARY",
    "ENFORCED_BOUNDARY",
    "EXECUTION_MODE_MANAGED",
    "MAX_CAPABILITIES",
    "MAX_CPU_QUOTA_PERCENT",
    "MAX_MEMORY_MAX_MB",
    "MAX_TASKS_MAX",
    "MAX_TTL_SECONDS",
    "MIN_CPU_QUOTA_PERCENT",
    "MIN_MEMORY_MAX_MB",
    "MIN_TASKS_MAX",
    "MIN_TTL_SECONDS",
    "NETWORK_PROFILES",
    "NETWORK_PROFILE_AF_UNIX_ONLY_FALLBACK",
    "NETWORK_PROFILE_PRIVATE_NETNS",
    "PRODUCT_CLAIM",
    "SECURITY_ERROR_CODES",
    "SUBSTRATE",
    "BoundaryClaim",
    "CleanupResult",
    "GrantStatus",
    "GrantStatusProvider",
    "LaunchHandle",
    "ManagedTaskSpec",
    "ResourceScope",
    "RuntimeObservation",
    "SecurityError",
    "StopResult",
    "unit_name_for_task",
]
