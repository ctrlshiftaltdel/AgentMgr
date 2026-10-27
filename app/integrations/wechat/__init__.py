"""WeChat 渠道集成的 B1 消息桥接（Issue #104）。

只包含：SDK 适配、配置、消息/身份/去重/通知的最小桥接，以及本地进程入口。
**不含**：模型调用、advisory 回答、任务执行、审批回调、公开 HTTP API。
"""

from __future__ import annotations

__all__: list[str] = []
