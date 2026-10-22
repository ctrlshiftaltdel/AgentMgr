"""运行时编排域：Hermes Profile 生命周期，以及 B3 Runtime Adapter 契约与适配器。

本目录是仓库的第 12 个领域目录（原 11 个：``identity`` ``intake`` ``expense``
``workflow`` ``documents`` ``inventory`` ``integrations`` ``audit`` ``api`` ``db``
``core``）。建立依据是 ``templates/PLAN.md`` §5.1「平台公共模块」把「Hermes Profile
生命周期及状态监控」列为平台公共能力。

当前包含两部分：

* :mod:`app.runtime.profile_provisioning` —— Profile 的**配置生成器与目录校验器**，
  **不实际部署**任何 Profile：本机不新建、不启动、不停止任何真实 Profile，也绝不读写
  ``~/.hermes``。设计与边界见 ``docs/PROFILE-PROVISIONING.md``。
* :mod:`app.runtime.contract` / :mod:`app.runtime.hermes` —— 冻结的 Runtime Adapter
  契约（``docs/contracts/runtime-adapter.md``）及其 Hermes ``/v1/runs`` 实现。契约层
  不含任何 Hermes 私有对象；适配器只报告事实，不写任务状态机。真实证据与限制见
  ``docs/integrations/runtimes/hermes.md`` 与
  ``docs/research/runtime-compatibility/hermes-0.22.1.md``。
"""

from __future__ import annotations

from app.runtime.contract import (
    ADAPTER_CONTRACT_VERSION,
    BOUNDARY,
    CancelOutcome,
    CancelResult,
    Capability,
    CapabilityStatus,
    ControlResult,
    ErrorCode,
    EventBatch,
    HealthResult,
    HealthStatus,
    InputOutcome,
    InputResult,
    LookupOutcome,
    LookupResult,
    ResultOutcome,
    RuntimeAdapter,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeGrant,
    RuntimeResult,
    RuntimeState,
    RuntimeType,
    StatusResult,
    SubmitOutcome,
    SubmitResult,
    VersionSource,
)
from app.runtime.errors import (
    RuntimeAdapterError,
    RuntimeAuthError,
    RuntimeConflict,
    RuntimeNotFound,
    RuntimeProtocolError,
    RuntimeRateLimited,
    RuntimeTimeout,
    RuntimeUnavailable,
    RuntimeUnknown,
    RuntimeUnsupported,
)
from app.runtime.hermes import HermesConfig, HermesRuntimeAdapter

__all__ = [
    "ADAPTER_CONTRACT_VERSION",
    "BOUNDARY",
    "CancelOutcome",
    "CancelResult",
    "Capability",
    "CapabilityStatus",
    "ControlResult",
    "ErrorCode",
    "EventBatch",
    "HealthResult",
    "HealthStatus",
    "HermesConfig",
    "HermesRuntimeAdapter",
    "InputOutcome",
    "InputResult",
    "LookupOutcome",
    "LookupResult",
    "ResultOutcome",
    "RuntimeAdapter",
    "RuntimeAdapterError",
    "RuntimeAuthError",
    "RuntimeCapabilities",
    "RuntimeCapability",
    "RuntimeConflict",
    "RuntimeErrorInfo",
    "RuntimeEvent",
    "RuntimeGrant",
    "RuntimeNotFound",
    "RuntimeProtocolError",
    "RuntimeRateLimited",
    "RuntimeResult",
    "RuntimeState",
    "RuntimeTimeout",
    "RuntimeType",
    "RuntimeUnavailable",
    "RuntimeUnknown",
    "RuntimeUnsupported",
    "StatusResult",
    "SubmitOutcome",
    "SubmitResult",
    "VersionSource",
]
