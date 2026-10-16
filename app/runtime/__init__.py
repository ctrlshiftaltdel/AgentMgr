"""运行时编排域：每员工 Hermes Profile 的生命周期、配置生成与状态监控。

本目录是仓库的第 12 个领域目录（原 11 个：``identity`` ``intake`` ``expense``
``workflow`` ``documents`` ``inventory`` ``integrations`` ``audit`` ``api`` ``db``
``core``）。建立依据是 ``templates/PLAN.md`` §5.1「平台公共模块」把「Hermes Profile
生命周期及状态监控」列为平台公共能力。

当前交付物是 Profile 的**配置生成器与目录校验器**
（:mod:`app.runtime.profile_provisioning`），**不实际部署**任何 Profile：本机不新建、
不启动、不停止任何真实 Profile，也绝不读写 ``~/.hermes``。

设计与边界见 ``docs/PROFILE-PROVISIONING.md``。
"""
