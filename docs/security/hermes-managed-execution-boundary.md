# Hermes-in-sandbox Managed Execution 边界记录（#97）

- 状态：**实现完成，真实证据 pending live**；本文不声称 Hermes Managed Execution 已部署、已运行或已验证。
- 日期：2026-10-26
- 关联：Issue #97、#96、#83；[设计](hermes-managed-execution-design.md)、[实施计划](hermes-managed-execution-plan.md)、[#96 调度器运维](../operations/runtime-dispatch.md)、[#82 受控执行边界](managed-execution-boundary.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)。
- 证据提交：**无**。本文件在 #97 合并前没有绑定任何真实运行；合并后只有在批准的测试机上完成 live 门禁，才会追加命令、退出码、运行时版本、runtime root 哈希和提交 SHA。

## 1 结论摘要（先读这一节）

- **没有 live 证据。** #97 的代码、单元测试、CLI 测试、脚本语法检查和数据库 E2E 夹具都不能升级真实运行时能力标签。当前所有 #97 Managed 能力仍是 `unverified`。
- **没有实际安装。** `ops/security/provision_hermes_runtime.sh` 与 `ops/security/verify_hermes_isolation.sh` 只做了 `bash -n`、Python `py_compile` 和文本一致性检查；实现者没有以 root 运行它们，没有创建组、用户、目录、sudoers、systemd 单元或密钥。
- **没有真实模型调用。** 没有使用任何模型供应商 key，没有启动宿主 egress broker，没有启动 transient `agentmgr-hermes-*` 单元。
- **没有触碰个人/默认 Hermes。** 没有读取、挂载、启动、停止或替换个人 Profile、认证、`.env`、会话、lock 或 gateway。
- **调度器只到 `verifying`。** 任何运行时事实都不会让 #96 调度器写入 `succeeded`；`stop_confirmed` 只是停止证据，不是业务副作用对账。
- **#83 仍被阻塞。** 本 Issue 不提供业务工具/PEP 连接器；真实业务资源访问和端到端商业化就绪仍由 #83 承担。

## 2 已实现的部分（是代码，不是运行时证据）

| 组件 | 文件 | 现状 |
|---|---|---|
| 纯契约、令牌、attestation、artifact、能力矩阵 | `app/runtime/hermes_managed.py`、`app/runtime/hermes_egress.py` | 单元测试（无 DB、无特权） |
| root helper / wrapper / sudoers | `ops/security/agentmgr_hermes_exec.py`、`agentmgr-hermes-exec`、`agentmgr-hermes.sudoers` | 单元测试；**未安装** |
| 单元内 workload / 本地模型代理 | `ops/security/agentmgr_hermes_workload.py` | 单元测试；**未在真实单元内运行** |
| 宿主 egress broker | `app/runtime/hermes_egress.py`、`ops/security/agentmgr-hermes-egress.service` | 单元测试；**未启动** |
| Managed adapter / helper 客户端 | `app/runtime/hermes_managed.py` | fake supervisor 单元测试；**未接真实 helper** |
| #96 dispatcher 集成 | `app/tasks/execution.py` | fake supervisor + 真实 PostgreSQL 的 E2E 夹具（仅 Master 在批准测试机运行） |
| CLI 启动器 | `app/runtime/hermes_managed_cli.py` | 单元/CLI 测试（无 DB、无特权）；**未在生产配置下运行** |
| 安装/验证脚本 | `ops/security/provision_hermes_runtime.sh`、`verify_hermes_isolation.sh/.py` | 仅语法与静态检查；**未执行** |
| 运维/边界文档 | 本文件、[运维手册](../operations/hermes-managed-execution.md)、[Hermes 运行时](../integrations/runtimes/hermes.md) | 文档 |

这些测试证明的是**契约与失败路径**，不是以下任一事实：真实 transient 单元属性、真实 Hermes 兼容性、真实模型出口、真实停止/超时、真实跨任务隔离或真实 artifact 证据。

## 3 待运行的真实证据（全部 pending）

以下命令和矩阵必须在所有者批准的测试机上、由 Master 运行并记录：

### 3.1 安装与静态验证

```bash
# 以 root 安装固定表面；--dry-run 先验证输入
sudo ops/security/provision_hermes_runtime.sh --dry-run ...
sudo ops/security/provision_hermes_runtime.sh ...
sudo systemctl daemon-reload
sudo systemctl enable --now agentmgr-hermes-egress.service
sudo ops/security/verify_hermes_isolation.sh
```

要证明：helper/wrapper/workload/sudoers/unit/config 的 owner、mode、固定路径、sudoers 唯一规则、runtime root manifest 哈希、组与密钥权限、broker socket、Task 2 argv 属性，以及 runtime root 未包含个人 Profile/auth/.env/session、未被个人路径 bind mount。

### 3.2 真实沙箱负向矩阵

`app/tests/security/test_hermes_sandbox_live.py`（env-gated）：

```bash
AGENTMGR_HERMES_LIVE=1 AGENTMGR_HERMES_SUDO=1 \
  ... pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''
```

要证明：独立 `DynamicUser`、只读宿主、私有 `/run`/`/tmp`、无外部 IP（`ENETUNREACH`）、broker 以外的 loopback 不可达、跨任务目录/令牌/单元不可读、仅 `terminal` 工具集、artifact symlink 拒绝、环境变量不含真实 key/DB DSN/宿主路径。

### 3.3 真实模型出口与 broker

同一 live 套件中的 broker 场景（本地 stub 上游 + 所有者提供的专用测试 key 各一次）：

要证明：任务级短时令牌的签发/过期/撤销/跨任务拒绝；broker 只允许配置的 provider host/path；真实 key 不在单元、任务目录、日志、证据中；流式响应与错误映射；撤销标记生效。

### 3.4 真实 Managed 生命周期与调度器 E2E

`app/tests/tasks/test_hermes_managed_live.py`：

```bash
AGENTMGR_HERMES_LIVE=1 ... pytest app/tests/tasks/test_hermes_managed_live.py -q -o addopts=''
```

要证明：`submit` 只在 unit active + started 后返回 `accepted`；`status`/`result`/`cancel`/超时/未知映射；artifact 由宿主计算哈希并作为 `result_ref`/`evidence:*`；重复提交不产生第二个 unit；adapter 重启后按 manifest 附加；调度器停在 `verifying`；取消确认后进入 `needs_human` 而非自动 `cancelled`。

### 3.5 证据记录格式

每次真实运行至少记录：命令、退出码、Hermes 版本、runtime root 哈希、提交 SHA、`pytest` 计数、systemd 版本。证据放在仓库外或 Issue/PR，不入库主机名、个人路径、IP、key 或 token。

## 4 能力标签

| 能力 | 当前标签 | 说明 |
|---|---|---|
| `task.submit` | `unverified` | 等待真实 unit + started 证据 |
| `task.status` | `unverified` | 等待真实 systemd/任务目录映射 |
| `task.cancel` | `unverified` | 等待真实 cgroup-gone 停止证据 |
| `task.result` | `unverified` | 等待真实宿主 artifact 哈希 |
| `task.lookup` | `unverified` | 等待真实 manifest/unit 恢复证据 |
| `runtime.health` | `unverified` | 等待真实 helper/broker/root/attestation |
| `runtime.isolation` | `unverified` | 等待真实负向矩阵 |
| `runtime.task_scoped_credentials` | `unverified` | 等待真实令牌/撤销/key 隔离 |
| `runtime.tool_permissions` | `unverified`（目标：仅 `terminal`） | 等待真实工具边界测试 |
| `runtime.managed_execution` | `unverified` | 以上全部通过前不得升级 |
| `task.events.poll` | `unverified` | 无原生事件流；不得伪造事件 |
| `task.input.provide` | `unverified` | 不支持 |
| `task.approval.resolve` | `unverified` | 不支持；one-shot 自动审批 |
| `task.unknown.resolve` | `unverified` | 不支持 |

Fake/Mock/Advisory 证据永远不升级这些标签。没有有效 runtime attestation + evidence_ref 时，`capabilities()` 必须返回全部 `unverified`；#96 会据此拒绝调度。

## 5 残余风险

以下风险在 live 证据通过后仍然存在，不得在文档或汇报中省略：

1. **共享内核与宿主 root。** 每个任务有独立 `DynamicUser`，但共享内核与宿主 root；本设计不防御宿主 root、内核或 systemd 漏洞。
2. **world-readable 宿主文件可能仍可读。** 只读挂载阻止写入，不阻止读取 world-readable 文件；`/run/systemd`、`/run/dbus` 之外的宿主 `AF_UNIX` socket 仍可能可读/可连。
3. **`terminal` 是边界，不是命令白名单。** 模型可驱动 terminal 在沙箱内执行任意命令；artifact 验证与沙箱限制后果，但 prompt injection 的风险仍然存在。
4. **任务令牌可被同任务 terminal 读取。** 令牌短时、任务绑定、可撤销，但不是进程级隔离。
5. **egress broker 是单点。** broker 的漏洞可能扩大上游访问；目标白名单、大小、超时与 key 隔离依赖真实测试。
6. **无业务工具/PEP。** 真实业务资源访问与工具治理属于 #83；本次没有治理 Hermes 工具调用。
7. **无原生事件流。** 长任务只能靠 heartbeat/status 轮询；核心必须记录该限制。
8. **每任务一个 unit/进程有启动开销。** 并发容量未测量；单 worker 单任务串行。
9. **真实 key 由所有者提供。** key 轮换、配额与计费流程不在本设计内。
10. **live 证据只在批准测试机产生。** 未运行前不得声称 `verified`、已部署或已安装。

## 6 不得作出的声明

在 live 门禁通过前，不得声称：

- Hermes Managed Execution 已验证或已上线；
- 每个任务已真实运行在 transient systemd 沙箱中；
- 模型出口、任务级凭据或 `terminal` 工具边界已真实生效；
- 跨任务隔离已真实证明；
- 生产模板已安装、sudoers 已生效、broker 已运行；
- #97 解除了 #83 的阻塞。

## 7 #83 边界

#97 只提供 Hermes 的受控运行时与调度器接入。业务工具、PEP 连接器、真实业务资源授权、真实客户端 E2E、商业化就绪与对抗性声明验证仍属于 #83；Hermes 内执行的工具调用不经过 AgentMgr PEP，除非 #83 建立相应连接器与验证器。
