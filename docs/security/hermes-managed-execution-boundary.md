# Hermes-in-sandbox Managed Execution 边界记录（#97）

- 状态：**沙箱隔离、in-process Managed 生命周期和 stub-broker token/cancel/timeout 已在批准测试机取得 live 证据；真实 provider key 的模型 artifact E2E 仍 pending；生产未部署。** 本文区分“已在测试机证明”和“仍未证明”。
- 日期：2026-10-26
- 关联：Issue #97、#96、#83；[设计](hermes-managed-execution-design.md)、[实施计划](hermes-managed-execution-plan.md)、[#96 调度器运维](../operations/runtime-dispatch.md)、[#82 受控执行边界](managed-execution-boundary.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)。
- 证据提交：实现分支 `feat/97-hermes-managed`，提交 `23ef41e`，live 运行时的 runtime root 哈希 `sha256:96cd218efbc04d845a75e795803f1877acee48ac3df8d7a4f51610831db07239`（Hermes 0.22.1，批准测试机）。具体 pytest 计数见 §3.0；不记录主机名、个人路径、IP 或 key。任何 head 变化后必须重跑 live 门禁。

## 1 结论摘要（先读这一节）

- **已在批准测试机安装。** 固定 helper/wrapper/workload/sudoers/broker unit/launcher 配置已安装；`verify_hermes_isolation.sh` 18/18 检查通过。个人/默认 Hermes gateway、Profile、认证、`.env`、会话和 lock 未被读取、挂载、启动、停止或替换。
- **沙箱负向矩阵已真实通过。** live 命令 `test_hermes_sandbox_live.py` 15 passed；其中 3 个是 live-gated 真实单元体（probe 负向矩阵、in-process Managed 生命周期、stop-while-live），其余 12 个是 DB-free 的属性/oracle/argv 断言。覆盖 `DynamicUser`、只读宿主、私有 `/run`/`/tmp`、`ENETUNREACH` 外部隔离、loopback、跨任务目录/token、宿主 systemd/dbus/容器 socket、`terminal`-only runner、动态 UID、unit 属性、干净日志/journal。
- **真实 Managed 生命周期已用 stub 模型上游通过。** `test_hermes_managed_live.py` 的 3 个 live body（token/cancel/timeout）使用真实 PostgreSQL、真实 helper/adapter/broker；同文件另有 7 个 DB-free 断言，2 个 real-key body skipped。`submit` 在 unit active + started 后 accepted；重复提交不重新 launch 与无 artifact 不产生 success 的 live 断言位于 Task 7 managed lifecycle；stop-while-live 只在 cgroup 为空后确认。
- **真实 provider key 的模型调用仍未运行。** 当前只有 dummy key；真实 key 的 artifact E2E（`OK` 写入 `out/result.txt`、宿主哈希、dispatcher -> `verifying`、独立 `complete_task` -> `succeeded`）仍 pending。没有真实 key 前不得声称模型出口或 artifact 信任已端到端验证。
- **生产未部署。** 测试机安装不是生产安装；CLI 未在真实 service principal/生产配置下运行；容量未测量。
- **调度器只到 `verifying`。** 任何运行时事实都不会让 #96 调度器写入 `succeeded`；`stop_confirmed` 只是停止证据，不是业务副作用对账。
- **#83 仍被阻塞。** 本 Issue 不提供业务工具/PEP 连接器；真实业务资源访问和端到端商业化就绪仍由 #83 承担。

## 2 已实现并已在测试机验证的部分

| 组件 | 文件 | 现状 |
|---|---|---|
| 纯契约、令牌、attestation、artifact、能力矩阵 | `app/runtime/hermes_managed.py`、`app/runtime/hermes_egress.py` | 单元测试 + 真实 PostgreSQL dispatcher E2E + live helper/broker |
| root helper / wrapper / sudoers | `ops/security/agentmgr_hermes_exec.py`、`agentmgr-hermes-exec`、`agentmgr-hermes.sudoers` | 已安装；真实 transient unit 验证 |
| 单元内 workload / 本地模型代理 / in-process `run_oneshot` | `ops/security/agentmgr_hermes_workload.py` | 已安装；真实单元内运行（stub 模型上游） |
| 宿主 egress broker | `app/runtime/hermes_egress.py`、`ops/security/agentmgr-hermes-egress.service` | 单元测试 + live stub broker；真实 provider 上游 pending |
| Managed adapter / helper 客户端 | `app/runtime/hermes_managed.py` | fake supervisor + 真实 helper live 生命周期 |
| #96 dispatcher 集成 | `app/tasks/execution.py` | fake supervisor + 真实 PostgreSQL E2E；dispatcher 只到 `verifying` |
| CLI 启动器 | `app/runtime/hermes_managed_cli.py` | 单元/CLI 测试；未在生产 service principal/配置下运行 |
| 安装/验证脚本 | `ops/security/provision_hermes_runtime.sh`、`verify_hermes_isolation.sh/.py` | 测试机真实安装；18/18 检查通过 |
| 运维/边界文档 | 本文件、[运维手册](../operations/hermes-managed-execution.md)、[Hermes 运行时](../integrations/runtimes/hermes.md) | 文档 |

这些测试仍然**不**证明：真实 provider key 的模型出口、真实 artifact 信任链、生产 service principal/CLI、并发容量或 #83 的业务工具治理。

## 3 真实证据

### 3.0 已在批准测试机完成

```text
# 安装与静态验证
provision_hermes_runtime.sh (--runtime-mode copy)     -> exit 0
verify_hermes_isolation.sh                            -> 18/18 checks passed

# 沙箱负向矩阵 + in-process 生命周期 + stop-while-live
pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''
  -> 15 passed

# 真实 PostgreSQL + 真实 helper/broker；stub 模型上游
pytest app/tests/tasks/test_hermes_managed_live.py -q -o addopts=''
  -> 10 passed, 2 skipped (real-key bodies)
```

运行环境：Hermes 0.22.1；提交 `23ef41e`；runtime root `sha256:96cd218efbc04d845a75e795803f1877acee48ac3df8d7a4f51610831db07239`。证据只记录命令、退出码、版本与哈希；不记录主机名、个人路径、IP、key 或 token。

### 3.1 安装与静态验证（已完成）

`provision_hermes_runtime.sh` 与 `verify_hermes_isolation.sh` 已在测试机真实执行；验证 helper/wrapper/workload/sudoers/unit/config 的 owner、mode、固定路径、sudoers 唯一规则、runtime root manifest 哈希、组与密钥权限、broker socket、Task 2 argv 属性，以及 runtime root 未包含个人 Profile/auth/.env/session、未被个人路径 bind mount。18/18 通过。

### 3.2 真实沙箱负向矩阵（已完成）

`app/tests/security/test_hermes_sandbox_live.py` 的 live 命令通过 15 项；其中 3 个 live-gated 体真实启动 transient unit（probe 矩阵、managed 生命周期、stop-while-live），12 个是不需要 host 的属性/oracle/argv 断言。真实体覆盖独立 `DynamicUser`、只读宿主、私有 `/run`/`/tmp`、无外部 IP（`ENETUNREACH`）、broker 以外的 loopback 不可达、跨任务目录/令牌/单元不可读、仅 `terminal` 工具集的 in-process runner、artifact symlink 拒绝、环境变量不含真实 key/DB DSN/宿主路径。

### 3.3 真实模型出口与 broker（部分完成）

stub 上游已真实验证：任务级短时令牌的签发/过期/撤销/跨任务拒绝；broker 只允许配置的 provider host/path；流式响应与错误映射；撤销标记生效。**真实 provider key 的上游调用与 key 不在单元/任务目录/日志中的断言仍 pending**；只有 dummy key，不能作为真实模型证据。

### 3.4 真实 Managed 生命周期与调度器 E2E（部分完成）

`app/tests/tasks/test_hermes_managed_live.py` 的 live 命令通过 10 项、跳过 2 项（real-key bodies）。10 项中 3 个是 live body（token 边界、cancel confirmed、timeout），使用真实 PostgreSQL、真实 helper/adapter/broker 与 stub 模型上游；7 个是 DB-free 断言。live body 证明：cancel 确认后进入 `needs_human` 而非自动 `cancelled`；超时停止并保留 partial artifact 证据；token 过期/撤销/跨任务拒绝。`submit` accepted、重复提交不产生第二个 unit、无 artifact 不产生 success、stop-while-live 的断言在 Task 7 managed lifecycle 中。adapter 重启/lookup 由 fake supervisor + 真实 PostgreSQL E2E 覆盖，未在真实 helper 上重跑。**真实 key 的 artifact + dispatcher -> `verifying` + 独立 `complete_task` -> `succeeded` 仍 pending。**

### 3.5 证据记录格式

每次真实运行至少记录：命令、退出码、Hermes 版本、runtime root 哈希、提交 SHA、`pytest` 计数、systemd 版本。证据放在仓库外或 Issue/PR，不入库主机名、个人路径、IP、key 或 token。

## 4 能力标签

| 能力 | 当前标签 | 说明 |
|---|---|---|
| `task.submit` | **live（stub 上游）** | 真实 unit active + started；真实 provider 待验 |
| `task.status` | **live（stub 上游）** | 真实 systemd/任务目录映射；真实 provider 待验 |
| `task.cancel` | **live（stub 上游）** | 真实 cgroup-gone 停止证据 + `needs_human` |
| `task.result` | **partial** | 无 artifact/失败路径 live；真实 artifact 哈希/`verifying` 待验 |
| `task.lookup` | **partial** | fake supervisor + 真实 PostgreSQL E2E；真实 helper 重启用未重跑 |
| `runtime.health` | **live** | helper/broker/root/attestation 真实检查 |
| `runtime.isolation` | **live** | 3 个 live 单元体 + 12 个 DB-free oracle 真实通过 |
| `runtime.task_scoped_credentials` | **live（stub 上游）** | 真实令牌/撤销/跨任务；真实 provider key 隔离待验 |
| `runtime.tool_permissions` | **live（runner 断言）** | runner 代码固定 `toolsets="terminal"`；实际工具执行面仍属 #83 |
| `runtime.managed_execution` | **unverified** | 真实 key artifact/生产配置/容量未完成 |
| `task.events.poll` | `unverified` | 无原生事件流；不得伪造事件 |
| `task.input.provide` | `unverified` | 不支持 |
| `task.approval.resolve` | `unverified` | 不支持；one-shot 自动审批 |
| `task.unknown.resolve` | `unverified` | 不支持 |

这些标签是文档口径；运行时 `capabilities()` 仍要求有效 runtime attestation + evidence_ref，Fake/Mock/Advisory 证据永远不升级标签。

## 5 残余风险

以下风险在 live 证据通过后仍然存在，不得在文档或汇报中省略：

1. **共享内核与宿主 root。** 每个任务有独立 `DynamicUser`，但共享内核与宿主 root；本设计不防御宿主 root、内核或 systemd 漏洞。
2. **world-readable 宿主文件可能仍可读。** 只读挂载阻止写入，不阻止读取 world-readable 文件；测试证明的是列出的 `/etc/shadow` DAC 基线、`/home`/DB/`/run/systemd/private`/`/run/dbus`/容器 socket 隐藏，不是所有宿主文件或 socket。
3. **`terminal` 是边界，不是命令白名单。** 模型可驱动 terminal 在沙箱内执行任意命令；artifact 验证与沙箱限制后果，但 prompt injection 的风险仍然存在。
4. **任务令牌可被同任务 terminal 读取。** 令牌短时、任务绑定、可撤销，但不是进程级隔离。
5. **egress broker 是单点。** broker 的漏洞可能扩大上游访问；目标白名单、大小、超时与 key 隔离的 stub 路径已测，真实 provider key 路径仍 pending。
6. **无业务工具/PEP。** 真实业务资源访问与工具治理属于 #83；本次没有治理 Hermes 工具调用。
7. **无原生事件流。** 长任务只能靠 heartbeat/status 轮询；核心必须记录该限制。
8. **每任务一个 unit/进程有启动开销。** 并发容量未测量；单 worker 单任务串行。
9. **真实 key 由所有者提供。** key 轮换、配额与计费流程不在本设计内；当前测试机只有 dummy key。
10. **live 证据只在批准测试机产生。** 测试机安装/验证不等于生产部署；生产 service principal、CLI、真实 provider key 与容量仍未验证。

## 6 不得作出的声明

即使测试机的沙箱/生命周期证据已通过，仍不得声称：

- Hermes Managed Execution 已在生产部署、上线或完成商业化验证；
- 真实 provider key 的模型调用、artifact 信任链或 `task.result` 端到端已验证；
- 生产模板已安装、生产 sudoers 已生效、生产 broker 已运行；
- 所有宿主文件/socket 都已隔离（只证明了列出的负向矩阵）；
- 容量、并发或启动开销已测量；
- #97 解除了 #83 的阻塞。

可以作出的声明仅限：在批准测试机上，指定提交和 runtime root 哈希下，沙箱负向矩阵、in-process Managed 生命周期、stub-broker token/cancel/timeout 和安装/验证脚本已真实通过；真实 key 与 #83 仍未完成。

## 7 #83 边界

#97 只提供 Hermes 的受控运行时与调度器接入。业务工具、PEP 连接器、真实业务资源授权、真实客户端 E2E、商业化就绪与对抗性声明验证仍属于 #83；Hermes 内执行的工具调用不经过 AgentMgr PEP，除非 #83 建立相应连接器与验证器。
