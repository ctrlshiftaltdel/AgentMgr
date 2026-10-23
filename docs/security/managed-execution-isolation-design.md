# 受控执行隔离设计（Managed Execution 隔离基座，#82）

- 状态：**Draft，待所有者评审**。本文是设计草案，**不是实现、不是验证证据**；文中所有测试与证据均为 `planned / not run`。
- 日期：2026-10-23
- 关联 Issue：#82（Epic：#74）；依赖 #77（威胁模型）、#78（PDP/PEP）、#81（Runtime Adapter，均已 VERIFIED）
- 契约版本：1.0.0（Phase 0 冻结候选，尚未发布）；凭据格式 `agx1`；网关协议版本 `1.0.0`
- 首批受控目标：**隔离基座 + 合成 Agent 负载**。Hermes 入沙箱与端到端受控执行属于 **#83**，不在本文范围。
- 关联文档：[威胁模型](threat-model.md)、[ADR-001 产品边界](../architecture/ADR-001-product-boundary.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)、[execution-grant Schema](../contracts/execution-grant.schema.json)、[任务状态机](../architecture/task-state-machine.md)、[生产部署要求](../DEPLOYMENT-REQUIREMENTS.md)

## 1. 阅读须知与状态标记

本文沿用威胁模型的标记法：

| 标记 | 含义 |
|---|---|
| `设计（未实现）` | 只有契约/设计，没有运行时代码或测试证据 |
| `部分（未按本契约验证）` | 现有资产可部分复用，但未按本契约做负向验证 |
| `已实现/已验证` | 有当前提交 SHA、真实命令与退出码的可复现证据 |

本轮交付**只有本文档**：没有新增 `app/security`、`ops/security`、`app/tests/security` 代码，没有在测试主机执行任何特权命令，没有启动任何隔离单元。因此：

- 任何“受控执行已实现/已验证”的表述都不成立；
- 本文中的命令、单元属性、负向用例是**待实现的验收规格**；
- 真实隔离测试只有在所有者批准本文与对应实现后才运行。

## 2. 背景与目标

### 2.1 背景

现有资产已经具备：服务端 PDP/PEP（`app/governance/pdp.py`、`app/governance/pep.py`）按调用重验 Grant；Worker 租约与故障恢复（`app/tasks/worker.py`）区分“取消请求”与“停止确认”；Hermes Runtime Adapter（`app/runtime/hermes.py`）只报告事实，并**明确拒绝 `managed_execution` Grant**，因为 Hermes 无法提供逐工具能力、资源范围、任务级凭据与 OS 隔离。

也就是说：授权链路与状态机语义已存在，但**执行边界本身尚不存在**。在缺少独立 UID、只读文件系统、受限网络与任务级短期凭据时，任何“受控执行”声明都不成立（ADR-001 §2.4、威胁模型 TH-19）。

### 2.2 目标

为 #82 建立可被负向测试证明的**隔离基座**：

1. 每个受控任务运行在独立的 transient systemd unit 中，拥有独立 `DynamicUser` UID；
2. 任务只能通过**Unix socket 上的 PEP 网关**访问被授权的合成资源，令牌为任务级、短时、可撤销；
3. 文件系统、网络、capability、namespace、进程可见性、CPU/内存/任务数/运行时长全部受限；
4. Supervisor 在 Grant 过期或撤销时停止执行并给出**可核对的停止证据**，而不是“已发送停止请求”；
5. 用合成 Agent 负载证明上述属性，且失败路径有稳定原因码与审计记录。

### 2.3 为什么用 transient systemd unit

- 不需要新增持久用户、包或容器运行时；`DynamicUser` 由 systemd 分配并在停止后回收；
- 沙箱属性由 systemd 统一实现，失败即启动失败（fail closed），不依赖应用层“自觉”；
- 与现有部署约束一致：不引入 Kubernetes、Redis 或其它基础设施（Issue #82 禁止路径）；
- 单元名与属性由控制器固定生成，任务代码无法影响启动参数。

## 3. 已确认决策（所有者）

| 编号 | 决策 | 影响 |
|---|---|---|
| D1 | #82 首个受控目标 = **隔离基座 + 合成 Agent 负载**；Hermes 入沙箱留给 #83 | 本文不涉及 `app/runtime/hermes.py` 改动；不得声称 Hermes Managed Execution 已验证 |
| D2 | 测试机允许临时非交互 sudo 启动/停止 transient unit、创建/清理 `/run/agentmgr-exec` 与 systemd StateDirectory | 仅测试机、仅本轮之后的实现阶段执行；不新建持久用户、不装包、不改 nftables/防火墙、不写现有 Hermes Profile；测试后清理 |
| D3 | 隔离方案 = 每任务 transient unit + `DynamicUser` + 沙箱属性 + Unix-socket PEP 网关 + 任务级短期凭据 + Grant 失效即停止 | 见 §6–§11 |
| D4 | 不新增 Python 依赖、不新增数据库迁移、不修改 `app/governance`、`app/tasks`、`app/runtime` | 凭据签名使用标准库 `hmac`/`hashlib`/`json`；网关不读写数据库 |
| D5 | 本轮（设计阶段）只写本文档，不改代码、不运行测试机命令 | 证据状态全部为 planned |

## 4. 非目标与产品声明限制

### 4.1 非目标

- 不实现 Hermes/Rakazo 入沙箱、不修改 Runtime Adapter 的 `managed_execution` 拒绝逻辑（#83）；
- 不实现容器、Kubernetes、虚拟机级隔离；
- 不实现模型供应商出口治理、浏览器/终端治理、客户端本地工具治理；
- 不实现多租户调度、计费、配额中心；
- 不实现生产 Secret Manager 集成：本文只定义**凭据接口**与宿主侧环境变量注入方式；
- 不新增数据库表、不写任务状态机：Supervisor 只报告执行边界事实，状态转换仍由核心服务负责。

### 4.2 明确的产品声明限制

1. **#82 之后可声称的只有：合成负载在受控隔离单元中运行，且其文件系统、进程、网络与凭据边界按本文负向测试通过。**
2. **不得**声称“Hermes Managed Execution 已受控”“客户端本地工具已被治理”“企业资源已不可绕过”。
3. 即使 #82 全部通过，下列仍不在声明范围内：宿主 root、具备 controller sudo 的运维人员、systemd/内核漏洞、侧信道、物理访问、网关 backend 自身缺陷、未经过网关的资源路径。
4. #83 完成后，Hermes 的 `runtime.isolation` 与 `runtime.task_scoped_credentials` 仍需**单独**的真实契约测试与证据，才可从 `unverified` 升级。
5. Advisory/Governed 模式的剩余风险不因本基座而改变；`execution_mode` 与 `limitations` 必须继续如实记录。

## 5. 威胁与验收映射

### 5.1 威胁映射

| 威胁 | 本基座的作用 | 分类 | 状态 |
|---|---|---|---|
| **TH-14** 客户端本地工具绕行 | 本基座不约束客户端；只保证服务器端受控单元内的合成负载。Advisory 任务继续记录 `client_local_tools_uncontrolled`，产品声明不得扩大。 | 不在本基座强制范围（产品边界） | `设计（未实现）`；明确接受的产品残余 |
| **TH-16** 服务身份权限膨胀 | 每任务 `DynamicUser`，无持久服务账户、无共享服务身份；`CapabilityBoundingSet=` 为空；无 D-Bus/宿主管理接口。 | 强制（宿主 root 除外） | `设计（未实现）` |
| **TH-17** 凭据/日志泄漏 | 令牌只经 `LoadCredential` 注入；源文件 0600、启动后删除；审计字段白名单；错误响应不含令牌/路径/堆栈。 | 强制（宿主 root、崩溃转储、内核侧除外） | `设计（未实现）` |
| **TH-18** 跨任务数据泄漏 | 独立 UID + StateDirectory 0700 + 凭据绑定 `task_id` + 网关跨任务拒绝；任务间无共享 TMPDIR。 | 强制（宿主 root、UID 复用窗口除外） | `设计（未实现）` |
| **TH-19** 运行时隔离逃逸 | §7 的 systemd 属性集合；`RuntimeMaxSec`/资源上限；停止证据。 | 强制（内核/systemd 漏洞、侧信道、物理访问除外） | `设计（未实现）`；**不得**称已验证 |
| **TH-24** 队列饥饿与资源耗尽 | 每单元 `MemoryMax`/`CPUQuota`/`TasksMax`/`RuntimeMaxSec`/`LimitNOFILE`；网关另有每任务/全局并发连接与每秒请求上限（§10.1），单任务无法占满全局配额。 | 单单元强制；网关准入强制；全局部分 | `设计（未实现）` |
| **TH-27** 模式混淆 | `ManagedTaskSpec.execution_mode` 只接受 `managed_execution`；启动前与 Grant 的 `execution_mode` 交叉校验；网关拒绝非 managed Grant。 | 强制（在 B3 入口） | `设计（未实现）` |

### 5.2 与 #82 验收条目的关系

| #82 验收条目 | 本基座对应机制 | 证据状态 |
|---|---|---|
| 受控 Agent 无法读取其他主体的凭据或访问未授权资源 | 独立 UID、StateDirectory 0700、凭据目录 `root:root 0700` 且仅 `LoadCredential` 读取、网关 token/scope/cross-task 校验 | planned / not run |
| Grant 到期或撤销后，后续受控调用失败 | 凭据 `exp = min(provider.expires_at, iat + ttl_seconds)` 且 `exp - iat ≤ 300`；网关每次调用实时读取撤销存储与 `GrantStatusProvider`（无缓存）；Supervisor 停止单元并验证 `process_gone` | planned / not run |
| 绕行测试有真实命令、退出码和证据 | §16 的负向矩阵、`verify_isolation.sh`（环境检查）+ `verify_isolation.py`（同进程 harness）、证据 JSON | planned / not run |
| 无法强制治理的能力有明确风险记录与产品声明限制 | §4、§15、§17 | 本文（设计层）；运行证据 planned |

## 6. 架构

### 6.1 组件

| 组件 | 计划路径 | 职责 | 信任级别 |
|---|---|---|---|
| Controller / Supervisor | `app/security/supervisor.py` | 校验 spec、解析 Grant 状态、签发凭据、调用特权助手启停单元、轮询与停止确认、清理 | 受信（MVP 与网关同进程） |
| Spec / 边界模型 | `app/security/boundary.py` | `ManagedTaskSpec`、`BoundaryClaim`、单元名派生、路径与限额校验 | 受信 |
| 凭据签发/校验 | `app/security/credentials.py` | `agx1` 令牌、HMAC-SHA256、kid 轮换、`RevocationStore`、`GrantStatusProvider` | 受信（密钥只在控制器内存/受限环境） |
| PEP 网关 | `app/security/gateway.py` | Unix socket 上的 NDJSON 请求处理、令牌与 Grant 校验、能力/范围/跨任务判定、`FileResourceBackend`、审计 | 受信边界 B3（MVP 为同进程线程内 server；独立服务属生产/#83） |
| 验证 harness | `ops/security/verify_isolation.py` | 在**同一进程**启动网关线程、注入内存实时 `GrantStatusProvider`、驱动 §16.3 探针、写出证据 JSON | 受信（仅测试机实现阶段） |
| systemd 适配 | `app/security/systemd.py` | 生成 `systemd-run`/`systemctl` 参数、解析 `systemctl show` 证据、状态映射 | 受信 |
| 错误码 | `app/security/errors.py` | 稳定错误码与安全错误消息（无内部路径/密钥） | 受信 |
| 特权助手（生产模板） | `ops/security/agentmgr-exec` | 唯一允许 root 执行的窄接口：`launch/remove-sources/stop/inspect/cleanup` | root，参数强校验 |
| 合成负载 | `ops/security/smoke_workload.py` | 只读凭据、发起允许/拒绝调用、执行负向探针、退出码编码结果 | **不受信（任务代码）** |

关系说明（避免与现有 PEP 混淆）：

- `app/governance/pep.py` 是 **B2 业务工具 PEP**，基于数据库中的 Grant 与策略版本，本轮**不修改**。
- 本文的网关是 **B3 运行时 PEP**：它在隔离单元与资源之间执行调用级判定，不读写数据库、不签发业务 Grant。
- 两者共用同一原因码词汇（`capability_not_granted`、`resource_scope_denied` 等）与 fail-closed 语义；#83 接入真实 Grant 存储时通过 `GrantStatusProvider` 协议桥接，而不是复制判定逻辑。

### 6.2 文本架构图

```
                       ┌──────────────────────── 受信域（controller，MVP 同进程） ─────────────────────┐
                       │                                                                          │
  ManagedTaskSpec ───► │  Supervisor ──► BoundaryValidator ──► UnitName(agentmgr-task-<12hex>)     │
  (JSON, strict)       │      │                    │                                             │
                       │      │                    └──► root helper 写 specs/<unit>.json（0700 目录）│
                       │      ▼                                                                  │
  GrantStatusProvider ─┤  CredentialIssuer ──► token(agx1) ──► root helper 写 credentials/       │
  (live, no cache)     │      │                                <unit>.json (0600, 启动后删除)     │
                       │      ▼                                                                  │
                       │  RevocationStore (MVP: memory; file store 供 #83 跨进程)                  │
                       │      ▲                                                                  │
                       │  PEP Gateway (MVP: 同进程 harness 线程)                                  │
                       │      ◄── NDJSON over /run/agentmgr-exec/gateway.sock                     │
                       │          (0666 MVP test; socket mode 不授权，token 才授权)                │
                       │      │                                                                  │
                       │      └──► FileResourceBackend (/srv/agentmgr-exec-resources, 只读)       │
                       │                                                                          │
                       │  PrivilegedRunner ──► sudo -n ops/security/agentmgr-exec launch|stop ... │
                       └──────────────────────────────┬───────────────────────────────────────────┘
                                                      │ root
                       ┌──────────────────────────────▼───────────────────────────────────────────┐
                       │ systemd: transient unit agentmgr-task-<12hex>                            │
                       │  DynamicUser=yes → 独立 UID；StateDirectory=agentmgr-exec/<unit> (0700)│
                       │  ProtectSystem=strict / ProtectHome=yes / PrivateTmp / PrivateDevices    │
                       │  PrivateNetwork=yes + RestrictAddressFamilies=AF_UNIX + IPAddressDeny=any│
                       │  LoadCredential=task-credential:/run/agentmgr-exec/credentials/<unit>.json│
                       │  BindReadOnlyPaths: gateway.sock → /run/agentmgr/gateway.sock            │
                       │                     workload.py   → /workload.py                        │
                       │  ExecStart=/usr/bin/python3 /workload.py                                │
                       │  RuntimeMaxSec / MemoryMax / CPUQuota / TasksMax / LimitNOFILE           │
                       └──────────────────────────────────────────────────────────────────────────┘
                                      │ 单元内（不受信）：合成负载
                                      └─ 可写：$STATE_DIRECTORY、私有 /tmp；其余只读或不可见
```

### 6.3 目录、所有权与生命周期

| 路径 | 宿主属主/权限 | 单元内可见性 | 生命周期 |
|---|---|---|---|
| `/run/agentmgr-exec` | controller/gateway 服务用户 `0700`（测试机 `agents`；生产 `<controller-user>`） | 不可见：目录 `0700` 且属主为服务用户，单元不能遍历；单元只看到 bind mount 的目标文件 | controller/helper 在实现阶段创建；清理时删除 |
| `/run/agentmgr-exec/gateway.sock` | controller/gateway 服务用户所有；MVP 测试模式 `0666`，生产可用 `0660` + 专用组 | 只读 bind mount 到 `/run/agentmgr/gateway.sock` | 网关（MVP 为同进程 harness 线程）启动时创建；停止时删除 |
| `/run/agentmgr-exec/revoked/` | controller/gateway 服务用户 `0700` | 不可见 | 撤销存储首次使用时创建；清理时删除 |
| `/run/agentmgr-exec/revoked/<jti>.json` | controller/gateway 服务用户 `0600`（`FileRevocationStore` 必须可读写） | 不可见 | 超过最大凭据 TTL 后 prune |
| `/run/agentmgr-exec/credentials/` | `root:root 0700` | 不直接可见；由 systemd 读取后只把副本放入 `$CREDENTIALS_DIRECTORY` | 由 root helper 创建/清理 |
| `/run/agentmgr-exec/credentials/<unit>.json` | `root:root 0600` | 不直接可见；systemd 读取后放入 `$CREDENTIALS_DIRECTORY/task-credential`（`0400`，单元 UID） | 单元 active 后由 root helper 删除源文件 |
| `/run/agentmgr-exec/specs/` | `root:root 0700` | 不可见 | 由 root helper 创建/清理 |
| `/run/agentmgr-exec/specs/<unit>.json` | `root:root 0600` | 不可见 | 单元启动后由 root helper 删除 |
| `/run/agentmgr-exec/workload.py` | `root:root 0555` | 只读 bind mount 到 `/workload.py` | 由 harness（经 `sudo -n`）部署；清理时删除 |
| `/var/lib/agentmgr-exec/<unit>` → `/var/lib/private/agentmgr-exec/<unit>` | 动态 UID（由 systemd 设置） `0700` | `$STATE_DIRECTORY`，可写 | 单元停止后保留，cleanup 删除 |
| `/srv/agentmgr-exec-resources` | `root:root 0755`，文件 `0444` | 不可见（backend 在控制器侧读取） | 部署资源；不由单元挂载 |

> `DynamicUser=yes` 与 `StateDirectory=` 组合时，systemd 把真实目录放在 `/var/lib/private/` 下，并在 `/var/lib/` 建立指向它的符号链接；单元命名空间内 `$STATE_DIRECTORY` 指向 `/var/lib/agentmgr-exec/<unit>`。这是 systemd 的既定行为，不是本设计的额外约定。

所有权要点：

- `/run/agentmgr-exec` 归 **controller/gateway 服务用户**所有（测试机 `agents`；生产 `<controller-user>` 占位符），`0700`；该用户因此能创建 `gateway.sock`、创建并读写 `revoked/`：gateway 创建 `gateway.sock`，controller 写 `revoked/`，而动态 UID 的任务不能遍历该目录。bind mount 由 root/PID1 在进入单元命名空间前建立，单元只看到挂载目标文件，不能借挂载点遍历父目录。
- `gateway.sock` 归 controller/gateway 服务用户所有；**MVP 测试模式 `0666`**，生产可改为 `0660` + 专用组。socket 文件模式只决定谁能 `connect()`，**不是资源授权**；授权始终由 token claims + 实时 Grant 状态决定。同宿主其它用户若知道路径可能能连接（MVP `0666`），但没有有效 token 会被 `token_invalid` 拒绝。
- `revoked/` 与记录文件归 controller/gateway 服务用户，目录 `0700`、记录 `0600`；这样 `FileRevocationStore` 的读写方（gateway/controller）与属主一致。不得写成 `root:root 0600`，否则非 root gateway 读不到。
- `credentials/`、`specs/` 为 `root:root 0700`，由 root helper 写入和删除；`workload.py` 为 `root:root 0555`。controller 不直接写这些 root-only 目录。

### 6.4 信任边界

| 边界 | 本设计中的落点 | 假设 |
|---|---|---|
| 受信控制器 | Supervisor、凭据签发、网关 | 宿主 root 等价能力；代码受审查；**不与任务代码共享进程** |
| 不受信任任务代码 | `smoke_workload.py` 及其子进程（同一单元/UID） | 可被篡改、可尝试任意系统调用；只能通过内核强制边界被约束 |
| 外部资源 | `FileResourceBackend` 根目录 | 只读；网关负责范围判定，backend 负责真实路径校验 |
| 凭据 | `/run/agentmgr-exec/credentials` | 目录 `root:root 0700`、文件 `0600`；仅 root helper 写入；单元只能通过 `LoadCredential` 获得副本 |
| 审计 | controller 侧审计 sink | 任务无法写审计；拒绝与放行都记录 |

## 7. `ManagedTaskSpec` 与校验

### 7.1 模型

```python
@dataclass(frozen=True, slots=True)
class ManagedTaskSpec:
    task_id: str                 # ^task_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$
    grant_id: str                # ^grnt_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$
    execution_mode: Literal["managed_execution"]
    ttl_seconds: int             # 1..300；有效值 = min(ttl_seconds, provider 剩余, 300)
    cpu_quota_percent: int       # 默认 50，范围 1..100
    memory_max_mb: int           # 默认 256，范围 32..1024
    tasks_max: int               # 默认 32，范围 1..64
    capabilities: tuple[str, ...]        # name@x.y.z，1..16 项
    resource_scope: ResourceScope        # 与 Grant 同构的冻结记录
    backend: Literal["file"]              # 唯一允许的 backend
```

`ResourceScope`（冻结记录）字段：`scope_type`、`resource_ids`、`connector_ids`、`data_classifications`、`exclude_resource_ids`，与 [execution-grant Schema](../contracts/execution-grant.schema.json) 的 `resource_scope` 同名同义；MVP 只接受 `scope_type="self"`、`connector_ids=()`、`data_classifications ⊆ {"internal"}`。

spec **不提供** Grant 过期时间：`GrantStatusProvider.expires_at` 是唯一权威来源（§9.6）。spec 也不提供任何可写路径字段；单元内唯一可写区是 systemd 注入的 `$STATE_DIRECTORY`，其下的子目录由 workload 自行创建，Supervisor 不代建（§8.6）。

### 7.2 校验规则

| 规则 | 拒绝原因 |
|---|---|
| JSON 只接受**精确字段集合**；未知字段、重复键、嵌套未知字段一律拒绝（含 `grant_expires_at`、`workspace`、`state`） | `invalid_spec` |
| 所有字符串必须是 `str`，禁止 NUL、控制字符；ID 必须匹配正则 | `invalid_spec` |
| `execution_mode != "managed_execution"` | `execution_mode_mismatch` |
| `backend` 不在允许集合 | `invalid_spec` |
| `ttl_seconds` ∉ [1, 300] | `invalid_spec` |
| `cpu_quota_percent` ∉ [1, 100]、`memory_max_mb` ∉ [32, 1024]、`tasks_max` ∉ [1, 64] | `invalid_spec` |
| `capabilities` 为空、重复、格式不符、或不是 Grant `capabilities.allowed` 的子集 | `capability_not_granted` |
| `resource_scope` 不是 Grant 范围的子集（含新增资源、扩大 `scope_type`、去掉 `exclude_resource_ids`） | `resource_scope_denied` |
| Grant 不存在 | `grant_missing` |
| Grant 已过期 | `grant_expired` |
| Grant `status=revoked/consumed` 或 `usable=false` | `grant_revoked` |
| Grant 的 `task_id` 与 spec 不一致 | `cross_task_denied` |
| Grant 的 `execution_mode` 与 spec 不一致 | `execution_mode_mismatch` |
| 无法获得非交互特权执行通道 | `privilege_required` |
| 单元名与仍在运行的其它任务冲突 | `invalid_spec`（`detail="unit_name_collision"`） |

校验顺序固定：**结构 → 模式 → 时间 → Grant 状态/绑定 → 能力 → 范围 → 限额 → 权限通道**。任何一步失败都不得签发凭据、不得启动单元。

TTL 硬上限为 **300s**，与凭据 `exp - iat ≤ 300` 同一上限；需要更长任务或凭据刷新的场景由 **#83** 设计，本 MVP 不放开。

### 7.3 `BoundaryClaim`

每次 launch 返回一份边界声明，用于让调用方与审计知道“此刻这台主机上实际强制了什么”：

```python
@dataclass(frozen=True, slots=True)
class BoundaryClaim:
    contract_version: str            # "1.0.0"
    substrate: str                   # "systemd-transient-dynamicuser"
    network_profile: Literal["private_netns", "af_unix_only_fallback"]
    product_claim: Literal["synthetic_substrate_only"]
    enforced: tuple[str, ...]        # 见 §15.1
    bypassable: tuple[str, ...]      # 见 §15.2
    deferred: tuple[str, ...]        # 见 §15.3
```

规则：

- `enforced`/`bypassable`/`deferred` 来自模块常量，不可由任务输入覆盖；
- `network_profile` 由**主机级**探测决定（§8.4），不是每任务临时选择；
- `product_claim` 固定为 `synthetic_substrate_only`；#83 之前不得出现其它取值；
- `BoundaryClaim.as_dict()` 进入 launch 审计与证据 JSON；网关审计只记录 `contract_version` 与 `network_profile`，不记录完整清单。

### 7.4 示例（合成标量）

```json
{
  "task_id": "task_synthetic_0001",
  "grant_id": "grnt_synthetic_0001",
  "execution_mode": "managed_execution",
  "ttl_seconds": 60,
  "cpu_quota_percent": 50,
  "memory_max_mb": 256,
  "tasks_max": 32,
  "capabilities": ["resource.read@1.0.0"],
  "resource_scope": {
    "scope_type": "self",
    "resource_ids": ["res_synthetic_0001"],
    "connector_ids": [],
    "data_classifications": ["internal"],
    "exclude_resource_ids": []
  },
  "backend": "file"
}
```

## 8. systemd 沙箱配置

### 8.1 属性表（精确值）

| systemd 属性 | 值 | 目的 |
|---|---|---|
| `DynamicUser=` | `yes` | 每单元独立、非持久的 UID；停止后回收 |
| `StateDirectory=` | `agentmgr-exec/<unit>` | 每单元可写状态目录；transient 单元使用显式校验过的单元名（见 §8.5） |
| `StateDirectoryMode=` | `0700` | 状态目录仅该 UID 可访问 |
| `RemainAfterExit=` | `yes` | 进程退出后保留单元为 `active/exited`，使退出证据可读取 |
| `CollectMode=` | `inactive` | 不自动回收 inactive/failed 单元，直到 supervisor 读取证据并清理 |
| `RuntimeMaxSec=` | `<ttl>`（秒，1..300） | 硬运行时长上限，超时由 systemd 终止；上限与凭据 TTL 一致 |
| `TimeoutStopSec=` | `5` | SIGTERM 后 5s 升级 SIGKILL |
| `KillMode=` | `control-group` | 终止整个 cgroup，含子进程 |
| `SendSIGKILL=` | `yes` | 允许 SIGKILL 兜底 |
| `ProtectSystem=` | `strict` | `/usr`、`/boot`、`/etc`、`/var` 等只读 |
| `ProtectHome=` | `yes` | `/home`、`/root`、`/run/user` 不可访问 |
| `InaccessiblePaths=` | `-/etc/agentmgr -/var/lib/agentmgr -/var/lib/postgresql -/srv -/opt` | 显式屏蔽控制面、数据库与业务目录；`-` 为可选路径语法（不存在则忽略，存在则仍屏蔽） |
| `TemporaryFileSystem=` | `/run/systemd /run/dbus` | 以私有 tmpfs 遮蔽宿主 systemd 私有 socket 与 D-Bus 目录；两种 profile 均为固定属性 |
| `PrivateTmp=` | `yes` | 独立 `/tmp` 与 `/var/tmp`，跨任务不可见 |
| `PrivateDevices=` | `yes` | 无原始设备节点 |
| `PrivateNetwork=` | `yes` | 独立网络命名空间；默认 profile |
| `RestrictAddressFamilies=` | `AF_UNIX` | 只允许 Unix socket；阻断 `AF_INET`/`AF_INET6`/`AF_NETLINK` |
| `IPAddressDeny=` | `any` | cgroup 级 IP 出口全拒绝（纵深防御） |
| `NoNewPrivileges=` | `yes` | 禁止 setuid/file capability 提权 |
| `CapabilityBoundingSet=` | 空 | 无任何 capability |
| `AmbientCapabilities=` | 空 | 无 ambient capability |
| `SystemCallFilter=` | `@system-service` | 仅系统服务所需 syscall 白名单 |
| `SystemCallArchitectures=` | `native` | 禁止非本机 ABI 系统调用 |
| `RestrictNamespaces=` | `yes` | 禁止创建 user/mount/net/pid 等命名空间 |
| `LockPersonality=` | `yes` | 锁定执行域 |
| `MemoryDenyWriteExecute=` | `yes` | 禁止 W+X 映射 |
| `RestrictRealtime=` | `yes` | 禁止实时调度 |
| `RestrictSUIDSGID=` | `yes` | 禁止创建 setuid/setgid 文件 |
| `RemoveIPC=` | `yes` | 单元停止时清理其 IPC 对象 |
| `ProtectClock=` | `yes` | 禁止修改时钟 |
| `ProtectKernelTunables=` | `yes` | 禁止写内核可调参数 |
| `ProtectKernelModules=` | `yes` | 禁止加载/卸载模块 |
| `ProtectKernelLogs=` | `yes` | 禁止读取内核日志 |
| `ProtectControlGroups=` | `yes` | cgroup 文件系统只读 |
| `ProtectHostname=` | `yes` | 禁止修改主机名 |
| `ProtectProc=` | `invisible` | 隐藏其它进程 |
| `ProcSubset=` | `pid` | `/proc` 只暴露 PID 相关条目 |
| `UMask=` | `0077` | 新建文件默认 0600/0700 |
| `MemoryMax=` | `<memory_max_mb>M` | 内存硬上限；spec 默认 **256**，范围 **32..1024** |
| `CPUQuota=` | `<cpu_quota_percent>%` | CPU 带宽上限；spec 默认 **50**，范围 **1..100** |
| `TasksMax=` | `<tasks_max>` | 进程/线程上限；spec 默认 **32**，范围 **1..64** |
| `LimitNOFILE=` | `256` | 文件描述符上限 |
| `LoadCredential=` | `task-credential:/run/agentmgr-exec/credentials/<unit>.json` | 令牌只经 systemd 注入 |
| `BindReadOnlyPaths=` | `/run/agentmgr-exec/gateway.sock:/run/agentmgr/gateway.sock` | 唯一可达的网关入口 |
| 同上 | `/run/agentmgr-exec/workload.py:/workload.py` | 固定负载脚本，只读 |
| `ExecStart=` | `/usr/bin/python3 /workload.py` | 固定命令，无参数、无 shell |

说明：`MemoryMax`/`CPUQuota`/`TasksMax` 不是固定常量，而是由 §7.2 校验过的 spec 整数渲染成上表占位形式；未显式给值时取默认值。范围之外的取值在校验阶段即 `invalid_spec`，不会启动单元。表格其余属性（含 `StateDirectory=agentmgr-exec/<unit>`）是固定常量，任务输入不能覆盖。

所有 `InaccessiblePaths=` 条目都使用 systemd 的**可选路径语法** `-<path>`：目标路径不存在时 systemd 忽略该条（因此 `/etc/agentmgr`、`/var/lib/agentmgr` 等部署目录尚未创建的精简测试机仍能启动单元），目标路径存在时仍被设为不可访问。该前缀只影响“路径不存在时是否拒绝启动”，不放宽任何已存在路径的沙箱属性；不得为了兼容主机而删掉条目或改用不带 `-` 的普通路径。

**不得对 `/run/systemd` 目录整体使用 `InaccessiblePaths`。** 在已批准测试机上，`InaccessiblePaths=-/run/systemd` 会使 systemd 的命名空间设置失败，单元以 `226/NAMESPACE` 启动失败；只屏蔽 `/run/dbus` 或改用 `TemporaryFileSystem` 则可成功。对照 matrix 显示 `TemporaryFileSystem=/run/systemd` 与 `/run/dbus` 以私有 tmpfs 遮蔽宿主 socket 目录后能够启动，因此两者在**两种 profile** 中都作为固定属性使用。该结论仅基于已批准测试机，不外推到其它宿主；Master 仍需按 §16 重跑 live 验证。

**`StateDirectory` 在 transient 单元中使用显式单元名。** `systemd-run -p` 不会展开 `%N`、`%i` 等 specifier；写成 `StateDirectory=agentmgr-exec/%N` 会在已批准测试机上创建一个字面目录 `/var/lib/private/agentmgr-exec/%N`，而 harness 查找的 `<unit>` 目录并不存在。因此 `build_systemd_run_argv` 渲染 `StateDirectory=agentmgr-exec/<unit>`（`<unit>` 已匹配 `^agentmgr-task-[0-9a-f]{12}$`）。单元文件模板不经过 `systemd-run -p`，可以继续使用模板自己的 `%i`（§8.5）。

**`RemainAfterExit=yes` + `CollectMode=inactive` 保留退出证据。** 普通 transient 单元退出后会被 systemd 立即回收，`systemctl show` 只剩 `LoadState=not-found`，`ExecMainExitTimestamp`/`Result` 随之丢失，导致 stop-on-revoke 无法证明停止。两个属性让成功退出的单元保留为 `active/exited`、超时单元保留为 `failed`，直到 supervisor 读取证据后再 `systemctl stop`/cleanup。该行为在已批准测试机上手动验证；清理时 `reset-failed` 对已回收单元必须视为 no-op，不得把 `not-found` 当成失败。

同一 profile 内的附加加固（不改变上表语义）：

| 附加属性 | 值 | 说明 |
|---|---|---|
| `SystemCallErrorNumber=` | `EPERM` | 被过滤 syscall 返回 EPERM 而非 SIGSYS，避免异常路径差异 |

版本前提：**systemd ≥ 249**（其中 `ProtectProc`/`ProcSubset` 需 ≥ 247，`LoadCredential` 需 ≥ 244；249 是本属性集合的安全基线），内核支持 cgroup v2 与 BPF cgroup IP 过滤。运行时不满足时**启动失败**（fail closed），不得降级为一组“尽力而为”的属性。

参数构造示例（由 `app/security/systemd.py` 生成，实际以 argv 数组传递，不经过 shell）：

```
systemd-run --unit=agentmgr-task-<12hex> --description=agentmgr-exec \
  --property=DynamicUser=yes \
  --property=StateDirectory=agentmgr-exec/agentmgr-task-<12hex> \
  --property=StateDirectoryMode=0700 \
  --property=RemainAfterExit=yes \
  --property=CollectMode=inactive \
  --property=RuntimeMaxSec=<ttl>s \
  --property=TimeoutStopSec=5 \
  --property=KillMode=control-group \
  --property=SendSIGKILL=yes \
  --property=ProtectSystem=strict \
  --property=ProtectHome=yes \
  --property=InaccessiblePaths=-/etc/agentmgr \
  --property=InaccessiblePaths=-/var/lib/agentmgr \
  --property=InaccessiblePaths=-/var/lib/postgresql \
  --property=InaccessiblePaths=-/srv \
  --property=InaccessiblePaths=-/opt \
  --property=TemporaryFileSystem=/run/systemd \
  --property=TemporaryFileSystem=/run/dbus \
  --property=PrivateTmp=yes \
  --property=PrivateDevices=yes \
  --property=PrivateNetwork=yes \
  --property=RestrictAddressFamilies=AF_UNIX \
  --property=IPAddressDeny=any \
  --property=NoNewPrivileges=yes \
  --property=CapabilityBoundingSet= \
  --property=AmbientCapabilities= \
  --property=SystemCallFilter=@system-service \
  --property=SystemCallArchitectures=native \
  --property=RestrictNamespaces=yes \
  --property=LockPersonality=yes \
  --property=MemoryDenyWriteExecute=yes \
  --property=RestrictRealtime=yes \
  --property=RestrictSUIDSGID=yes \
  --property=RemoveIPC=yes \
  --property=ProtectClock=yes \
  --property=ProtectKernelTunables=yes \
  --property=ProtectKernelModules=yes \
  --property=ProtectKernelLogs=yes \
  --property=ProtectControlGroups=yes \
  --property=ProtectHostname=yes \
  --property=ProtectProc=invisible \
  --property=ProcSubset=pid \
  --property=UMask=0077 \
  --property=MemoryMax=<memory_max_mb>M \
  --property=CPUQuota=<cpu_quota_percent>% \
  --property=TasksMax=<tasks_max> \
  --property=LimitNOFILE=256 \
  --property=LoadCredential=task-credential:/run/agentmgr-exec/credentials/agentmgr-task-<12hex>.json \
  --property=BindReadOnlyPaths=/run/agentmgr-exec/gateway.sock:/run/agentmgr/gateway.sock \
  --property=BindReadOnlyPaths=/run/agentmgr-exec/workload.py:/workload.py \
  /usr/bin/python3 /workload.py
```

### 8.2 为什么 `PrivateNetwork=yes` 仍然能连网关

- `PrivateNetwork=yes` 创建**独立网络命名空间**，其中只有状态为 down 的 loopback；它不挂载或隔离文件系统路径。
- Unix domain socket 是**文件系统对象**，`connect(2)` 走 VFS 而非 IP 路由。因此把 socket 文件 `BindReadOnlyPaths` 到单元内的固定路径（`/run/agentmgr/gateway.sock`）后，单元可以连接，而 IP 出口完全不存在。
- 网关在 controller 侧监听同一 socket；连接的 `SO_PEERCRED` 提供对端 UID/PID/GID，仅用于**准入计数与审计**（单元 UID 在启动后才可知），不作为资源授权依据；授权仍由 token claim + 范围判定决定（§10.1）。
- `RestrictAddressFamilies=AF_UNIX` 使 `socket(AF_INET, ...)` 直接返回 `EAFNOSUPPORT`；即使属性被误删，`IPAddressDeny=any` 仍是第二道 cgroup 级出口阻断。

### 8.3 单任务 UID 与状态目录

- `DynamicUser=yes` 让 systemd 为每个单元分配一个未使用的 UID（通常在 `61184..65519` 的动态范围），并在停止后释放。
- `StateDirectory=agentmgr-exec/<unit>`（transient 单元使用显式单元名，见 §8.5）创建 `/var/lib/private/agentmgr-exec/agentmgr-task-<hex>/`，权限 `0700`，属主为动态 UID；单元内 `$STATE_DIRECTORY` 指向它。
- 任务之间因此没有共享可写路径：不同单元不同 UID、不同目录、不同私有 `/tmp`。
- **UID 复用风险**：停止后状态目录不会自动删除；若同一数值 UID 之后被另一单元分配，旧目录可能被读到。缓解：Supervisor 的 `cleanup` 必须在 `process_gone=true`（或 `never_started=true`）后删除状态目录（§11.4），并把“清理残留”作为告警指标（§19）。

### 8.4 网络 profile 与回退

| profile | 属性 | 何时使用 |
|---|---|---|
| `private_netns`（默认） | `PrivateNetwork=yes` + `RestrictAddressFamilies=AF_UNIX` + `IPAddressDeny=any` | 主机探测通过（socket 可连接） |
| `af_unix_only_fallback` | 去掉 `PrivateNetwork=yes`，保留 `RestrictAddressFamilies=AF_UNIX` + `IPAddressDeny=any`；`TemporaryFileSystem=/run/systemd`、`/run/dbus` 与默认 profile 相同 | 默认 profile 下 bind mount 的 Unix socket 无法连接（例如宿主/内核限制） |

规则：

- 探测在**主机级**执行一次（启动时或 harness 首次运行）：用同一 profile 启动一个只做 `connect()` 的最小单元；成功即 `private_netns`，失败即 `af_unix_only_fallback`；结果缓存到进程内并写入审计。
- 回退**不是**“放宽网络”：`AF_INET`/`AF_INET6` 仍被 `RestrictAddressFamilies` 拒绝，IP 出口仍被 `IPAddressDeny=any` 拒绝。失去的是“独立 netns”这一层纵深防御，不是“无 IP”不变量。
- 两种 profile 都以 `TemporaryFileSystem=/run/systemd` 与 `/run/dbus` 遮蔽宿主 systemd 私有 socket 与 D-Bus；回退不再追加 `InaccessiblePaths`，也不改变文件系统沙箱。
- 回退必须出现在 `BoundaryClaim.network_profile` 与证据 JSON 中；不得静默切换。
- 两个分支都必须用真实 `connect()` 探针在目标主机上验证（默认分支与回退分支各一次），不得只凭文档假设；结果写入证据 JSON（§16.4）。
- 每个任务不得单独选择 profile；同一主机同一时刻只使用一种。

### 8.5 单元命名

```
unit = "agentmgr-task-" + sha256(("agentmgr-task:v1:" + task_id).encode("utf-8")).hexdigest()[:12]
```

- 只含 `[a-z0-9-]`，固定前缀，长度固定 25；
- 启动前用 `^agentmgr-task-[0-9a-f]{12}$` 复核；不满足即 `invalid_spec`；
- **不存在**由用户、模型或任务输入直接构造的单元名；
- 单元名不含 `@`，因此是非模板 transient unit。`systemd-run -p` **不会展开** `%N`/`%i` 等 specifier，所以 transient 单元的 `StateDirectory` 必须由 builder 渲染为显式的 `agentmgr-exec/<unit>`；写成 `%N` 会创建字面目录并破坏每任务隔离。若部署改用模板单元 `agentmgr-task@<hex>.service`，模板文件中的 `%i` 由 systemd 的 unit-file 展开机制处理，路径仍是 `agentmgr-exec/agentmgr-task-<hex>`，布局不变（见 §8.1、§8.3）。

### 8.6 文件系统可写面

| 路径 | 权限 | 说明 |
|---|---|---|
| `$STATE_DIRECTORY`（`/var/lib/agentmgr-exec/<unit>`） | 该 UID 可读写，父级不可遍历 | 唯一持久可写区 |
| `/tmp`、`/var/tmp` | 私有 tmpfs，可写 | 单元停止即消失 |
| `/workload.py`、`/run/agentmgr/gateway.sock` | 只读 bind mount | 不可修改 |
| 其余（`/etc`、`/usr`、`/var`、`/home`、`/root`、`/srv`、`/opt`、`/var/lib/postgresql`） | 只读或不可见 | 写入返回 `EROFS`/`EACCES` |

`$STATE_DIRECTORY` 是唯一可写区；`ManagedTaskSpec` 不含任何路径字段（无 `workspace`/`state`）。workload 可在其中自行创建子目录，Supervisor 不创建也不解析任务内路径。

## 9. 凭据模型

### 9.1 Claims

| 字段 | 类型 | 约束 |
|---|---|---|
| `v` | int | 固定 `1` |
| `iss` | str | 固定 `"agentmgr-exec"` |
| `aud` | str | 固定 `"pep-gateway"` |
| `kid` | str | `^[a-z0-9][a-z0-9_-]{1,15}$`，必须存在于 keyring |
| `jti` | str | `^cred_[0-9a-f]{32}$`，全局唯一，撤销键 |
| `task_id` | str | 与 spec/Grant 一致 |
| `grant_id` | str | 与 spec/Grant 一致 |
| `subject_id` | str | 由 Grant 解析，`^(prin\|svc\|rt)_[A-Za-z0-9][A-Za-z0-9_-]{3,63}$` |
| `capabilities` | array[str] | 排序去重，非空，spec 的子集 |
| `resource_scope` | object | 与 spec 同构；网关按此判定 |
| `iat` | int | 签发时间（Unix 秒，UTC） |
| `nbf` | int | `iat - 5`（允许 5s 时钟偏差） |
| `exp` | int | `min(GrantStatusProvider.expires_at, iat + ttl_seconds)`，且 `exp - iat ≤ 300` |
| `nonce` | str | `^[0-9a-f]{32}$`，每次 launch 随机；与单元名一同进入审计 |

`exp` 必须**严格小于等于** `GrantStatusProvider` 在签发时刻返回的剩余有效期；不允许“凭据比授权活得久”。spec 不提供任何过期时间字段，调用方无法延长或伪造 Grant 生命周期。

### 9.2 序列化与签名

1. 用 `app/security/credentials.py::canonical_json()` 按 RFC 8785（JCS）规范化 claims，得到 UTF-8 字节。实现要求（必须有测试向量，见 §16.2）：
   - 对象键必须是字符串，并按 **UTF-16 code units** 排序（不能依赖 Python `str` 的 code point 顺序，也不能只用 `json.dumps(sort_keys=True)`）；
   - 字符串按 JSON 规则转义并保留非 ASCII（`ensure_ascii=False`）；控制字符、引号、反斜杠按 RFC 8785 转义；
   - 数组保持输入顺序；
   - 数字按 ECMAScript `Number` 文本形式输出（`-0` → `0`）；整数只在 `±(2^53-1)` 内可精确表示，超出即拒绝；拒绝 `NaN`/`Infinity`；
   - 拒绝非 JSON 类型（`bytes`、`datetime`、`Decimal` 等）。
2. Base64url（无填充）编码为 `payload_segment`；
3. 计算 `mac = HMAC-SHA256(key, b"agx1." + payload_segment)`（即对 JCS/UTF-8 字节的编码形式签名）；
4. token = `agx1.<payload_segment>.<base64url(mac)>`。

**实现选择**：`app/security` 自带的规范化器只依赖标准库。虽然仓库已有经向量测试的 `app.governance.policies.canonical_json`，但它的导入链会拉入治理模型与 `app.db.base`（SQLAlchemy ORM），违反 §16.1 “安全单元测试不导入 `app.db`”的约束，也让安全基座依赖业务治理模块。若未来把 JCS 抽成 stdlib-only 共享模块，应改为复用并删除本地实现；在此之前禁止用 `json.dumps(sort_keys=True)` 冒充 JCS。

校验：

- 先按 `.` 分割并检查段数与长度（总长 ≤ 4096 字节）；
- 对 `payload_segment` 重算 MAC，用 `hmac.compare_digest` 常数时间比较；
- 再解析 JSON，并校验 claim 的**精确键集合**（未知字段、重复键、类型错误一律拒绝）；
- 最后校验 `iss/aud/v/kid/task/grant/capabilities/scope/iat/nbf/exp/nonce` 与实时状态。

MAC 覆盖**编码后的段**而不是重新序列化后的对象，避免“解析-再序列化”导致规范形式差异。

### 9.3 密钥与轮换

| 项 | 设计 |
|---|---|
| 密钥来源 | 环境变量 `AGENTMGR_EXEC_CREDENTIAL_KEYRING`（JSON：`{"kid":"base64"}`）与 `AGENTMGR_EXEC_CREDENTIAL_ACTIVE_KID` |
| 长度 | 每个解码后密钥 ≥ 32 字节（256 bit）；短于 32 字节、非法 base64、未知 active kid → 启动失败（fail closed） |
| 无默认值 | 代码中不存在内置密钥；测试用显式注入的临时密钥 |
| 轮换 | 新增 kid 并切 active；旧 kid 保留用于验签，至少到“最大凭据 TTL（300s）+ 时钟偏差（5s）”之后移除 |
| 禁用 | 移除 kid 后，旧 token 验签失败（`token_invalid`）；不提供“全部 kid 都接受”的路径 |
| 禁止位置 | 不得进入 argv、unit 文件、仓库、任务输入、提示词、日志、错误响应；只在 controller 进程环境 |

### 9.4 注入

- Supervisor 生成 token 后，**不把 token 放进 argv 或环境变量**；它把已校验的 spec 与 token 经 stdin 交给 root helper；
- root helper 在 `/run/agentmgr-exec/credentials/`（`root:root 0700`）内以 `O_CREAT|O_EXCL|O_WRONLY` 写入 `<unit>.json`（`0600`，`root:root`），内容为 token + `\n`；
- systemd 在启动前以 root 读取该文件，放入单元的 `$CREDENTIALS_DIRECTORY/task-credential`（`0400`，属主为动态 UID）；
- **单元进入 `active` 后**，Supervisor 调用 helper `remove-sources <unit>` 删除源文件；systemd 已完成读取，删除不影响运行中的单元；
- 负载只从 `$CREDENTIALS_DIRECTORY/task-credential` 读取并 `strip()`；
- 明确禁止：环境变量、命令行参数、任务输入、提示词、stdout/stderr、审计详情、错误消息。

### 9.5 撤销存储

```python
class RevocationStore(Protocol):
    def revoke(self, jti: str, *, reason: str, revoked_at: datetime) -> None: ...
    def lookup(self, jti: str) -> RevocationRecord | None: ...
    def prune(self, now: datetime) -> int: ...
```

| 实现 | 用途 | 语义 |
|---|---|---|
| `InMemoryRevocationStore` | 单元测试、单进程 MVP 默认 | 加锁 dict；`lookup` 返回记录或 None；进程重启即丢失 |
| `FileRevocationStore` | 跨进程/崩溃恢复（生产独立网关、#83） | `/run/agentmgr-exec/revoked/<jti>.json`，属主为 controller/gateway 服务用户（测试 `agents`；生产 `<controller-user>`），记录 `0600`、目录 `0700`；写临时文件 → `fsync` → `os.replace` → `fsync(dir)`；按 `revoked_at + 2*max_ttl` prune。MVP 同进程 harness 可使用内存实现，本文件实现为可选，但语义保持不变 |

原子性要求：reader 只能看到完整文件；半写文件（`*.tmp`）被忽略并清理。`revoke` 幂等：同一 `jti` 重复撤销保留首次 `reason` 与时间。

### 9.6 `GrantStatusProvider` 与双重校验

```python
class GrantStatusProvider(Protocol):
    def status(self, grant_id: str, *, task_id: str) -> GrantStatus: ...

@dataclass(frozen=True)
class GrantStatus:
    grant_id: str
    task_id: str
    subject_id: str
    execution_mode: str
    status: Literal["active", "revoked", "consumed"]
    usable: bool
    expires_at: datetime
    revocation_reason: str | None
    capabilities: tuple[str, ...]
    resource_scope: ResourceScope
```

网关在**每次调用**上同时检查：

1. **凭据撤销存储**（`credential_revoked`）与凭据自身 `exp/nbf`（`credential_expired` / `token_not_yet_valid`）；
2. **实时 `GrantStatusProvider`**（`grant_revoked` / `grant_expired` / `grant_missing` / `execution_mode_mismatch`）。

这样即使 Supervisor 尚未完成停止（或停止失败），一个已撤销的 Grant 也会在**下一次调用**被拒绝。两个来源任一不可用 → 拒绝（fail closed，`unknown`），不得回退为“只看令牌”。

`GrantStatusProvider` 必须是**实时只读适配器**：网关在每次调用上重新调用 `status()`，不缓存、不复用上一次结果、不接受 spec 自带的过期时间；实现只能读取授权状态，不能签发或修改 Grant。**MVP 验证形态**是 `ops/security/verify_isolation.py` harness 在**同一进程**启动网关线程，并注入内存实时 `GrantStatusProvider`（测试可改变其返回值，下一次调用立即生效）；这满足“每次调用实时读取、无缓存”。独立 `agentmgr-pep-gateway.service` 是**生产/#83** 路径，必须接真实 Grant 存储的实时适配器；网关在任何形态下都不直接读数据库。

### 9.7 传播边界与残余风险

| 场景 | 传播延迟 | 残余 |
|---|---|---|
| 同进程网关（MVP 验证 harness） | 立即（同一调用栈） | 无跨进程延迟；撤销存储可用内存实现，`FileRevocationStore` 可选 |
| 文件撤销存储 + 独立网关进程（生产/#83） | 每次调用读文件（无缓存） | 文件系统不可用 → 拒绝 |
| Grant 状态源不可达 | 立即拒绝 | 业务可用性下降（fail closed 的代价） |
| 停止与撤销之间 | 撤销先生效，停止随后 | 窗口内任务仍可运行，但无法通过网关取得新结果 |
| 令牌已泄漏给同单元内的子进程 | 同 UID 内无法隔离 | 该单元的一切进程共享同一授权；这是任务级而非进程级凭据 |

## 10. 网关协议

### 10.1 传输

- 监听：`AF_UNIX`，`/run/agentmgr-exec/gateway.sock`，`SOCK_STREAM`；socket 文件归 controller/gateway 服务用户所有（测试机 `agents`；生产 `<controller-user>`）。**MVP 测试模式 `0666`**；生产可用 `0660` + 专用组。父目录 `/run/agentmgr-exec` 为服务用户 `0700`，只把 socket bind mount 暴露给单元。
- **socket 模式不是授权**：它只决定谁可以 `connect()`；资源授权始终由 token claims + 实时 `GrantStatusProvider` 决定。即使同宿主其它用户知道路径并能连接（MVP `0666`），没有有效 token 也会被 `token_invalid` 拒绝；目录 `0700` 通常已阻断遍历，`0660` + 专用组是生产的收紧选项。
- **MVP 形态**：网关在 `ops/security/verify_isolation.py` harness 内以线程方式与 controller 同进程运行（同一内存 `GrantStatusProvider`）；独立 `agentmgr-pep-gateway.service` 是生产/#83 路径。
- 编码：**newline-delimited JSON**，一个连接一个请求、一个响应，随后关闭；
- 不接受流水线、不接受多请求连接、不接受空行之外的空请求；
- 网关只读 Unix socket，不监听任何 IP 端口。

准入与速率限额（默认值，部署可配置）：

| 限额 | 默认 | 作用域 | 超限原因码 |
|---|---|---|---|
| 并发连接 | **8** | 每任务（按 `SO_PEERCRED` UID 计数；一任务一 DynamicUser UID） | `busy` |
| 并发连接 | **64** | 全局 | `busy` |
| 请求速率 | **16/s** | 每任务（按 peer UID 计；连接建立后计数，不等待 token） | `rate_limited` |
| 请求速率 | **200/s** | 全局 | `rate_limited` |
| 单请求字节 | **64 KiB（65536，含换行）** | 每连接 | `payload_too_large` |
| 读/写超时 | **5s** | 每连接 | `timeout` |

规则：

- 限额是**部署配置**（例如 `AGENTMGR_EXEC_GATEWAY_MAX_CONNECTIONS_PER_TASK`、`..._MAX_CONNECTIONS_GLOBAL`、`..._MAX_RPS_PER_TASK`、`..._MAX_RPS_GLOBAL`），在网关启动时校验；缺失、非法或非正数 → **启动失败**（fail closed），不得退化为“无限制”。任务输入、token claim、提示词都不能修改这些值。
- 计数用单调时钟的令牌桶；`SO_PEERCRED` UID 只用于**准入计数与审计**，不参与资源授权（授权仍由 token claim + 范围判定决定）。同一单元内的子进程共享该 UID，因此共享同一份限额。空闲 UID 的桶在 300s 无活动后回收，避免无限增长。
- 准入顺序是**先 per-peer-UID、后 global**：per-task 超限直接拒绝，不消耗全局配额。单个任务最多占 8 个连接 / 16 req/s，不能独占全局的 64 / 200。
- 超限**立即拒绝并关闭**，不排队、不无限重试；连接槽在 `finally` 中释放，任何连接最多占用 **5s**。因此一个任务不能无限期饿死其它任务；全局配额被占满时，其它任务最多等待一个 5s 超时窗口后获得槽位，不会排队或静默丢弃。全局上限满时返回 `busy`（fail closed）。
- 单请求读取超过 64 KiB 时立即 `payload_too_large` 并关闭，不读完剩余内容。

### 10.2 请求与响应

```json
{"version":"1.0.0","token":"agx1...","capability":"resource.read@1.0.0","resource":"res_synthetic_0001","payload":{}}
```

| 字段 | 必填 | 约束 |
|---|---|---|
| `version` | 是 | 必须为 `"1.0.0"`；其它值 `unsupported_version` |
| `token` | 是 | `agx1` 格式；不接受空串 |
| `capability` | 是 | `name@x.y.z` |
| `resource` | 是 | `^res_[A-Za-z0-9][A-Za-z0-9_-]{2,95}$` |
| `payload` | 是 | 对象；MVP 只接受空对象；非空 `bad_request` |

顶层与 `payload` 内的未知字段一律 `bad_request`；重复键 `bad_request`。

成功：

```json
{"ok":true,"result":{"resource_id":"res_synthetic_0001","media_type":"application/json","body":{"kind":"synthetic","value":"..."}}}
```

失败：

```json
{"ok":false,"reason":"resource_scope_denied"}
```

失败响应**只含** `ok` 与稳定原因码；不含异常文本、堆栈、路径、令牌、Grant 详情。

### 10.3 稳定原因码

| 原因码 | 触发点 |
|---|---|
| `bad_request` | JSON 非法、未知字段、重复键、字段类型错误 |
| `unsupported_version` | `version` 不匹配 |
| `payload_too_large` | 超过 64 KiB |
| `rate_limited` | 超过每任务或全局每秒请求上限 |
| `busy` | 超过每任务或全局并发连接上限（或全局准入已满） |
| `token_invalid` | 格式/签名/kid/aud/iss/claim 集合错误 |
| `token_not_yet_valid` | `nbf` 未到（含允许偏差） |
| `credential_expired` | `exp` 已过 |
| `credential_revoked` | 撤销存储命中 `jti` |
| `grant_missing` | 状态源查无此 Grant |
| `grant_expired` | Grant `expires_at` 已过 |
| `grant_revoked` | Grant `status=revoked/consumed` 或 `usable=false` |
| `execution_mode_mismatch` | Grant 非 `managed_execution` |
| `capability_not_granted` | 能力不在令牌/`Grant` 的 allowed 中，或 `denied` 命中 |
| `resource_scope_denied` | 资源不在范围，或命中 `exclude_resource_ids` |
| `cross_task_denied` | 令牌 `task_id`/`grant_id` 与 Grant 或资源归属不一致 |
| `resource_denied` | backend 明确拒绝（不允许的资源 ID、非普通文件、越界） |
| `backend_error` | backend 内部失败（消息脱敏） |
| `timeout` | 读/写/处理超时 |
| `unknown` | 状态源不可用等不可归类失败（fail closed） |

### 10.4 校验顺序

1. 连接准入：先查 peer UID 并发连接 ≤8，再查全局 ≤64（`busy`）；
2. `SO_PEERCRED` 记录（uid/pid，仅审计）并施加每秒请求限额：先查 peer UID ≤16/s，再查全局 ≤200/s（`rate_limited`）；
3. 行长度与 JSON 结构（`payload_too_large` / `bad_request`）；
4. `version`；
5. token 格式与 MAC（`token_invalid`）；
6. claim 键集合、类型、`iss`/`aud`/`v`、`kid`；
7. `nbf` / `exp`；
8. 撤销存储（`credential_revoked`）；
9. 实时 `GrantStatusProvider`（`grant_*`、`execution_mode_mismatch`）；
10. 令牌与 Grant 的 `task_id`/`grant_id`/`subject_id` 一致性（`cross_task_denied`）；
11. capability（`capability_not_granted`）；
12. resource 格式、范围、`exclude`（`resource_scope_denied`）、backend 允许列表（`resource_denied`）；
13. 调用 backend 并审计（`backend_error` / `timeout`）。

任何一步失败都**不产生副作用**，且写一条 deny 审计。第 1–2 步在读取请求前发生，审计只有 `peer_uid` 与限额原因码，不含 token 或请求内容；第 3 步之后才可能记录 `task_id`/`grant_id`。

### 10.5 `FileResourceBackend` 与未来 `ResourceBackend`

```python
class ResourceBackend(Protocol):
    def read(self, resource_id: str, *, scope: ResourceScope, capability: str) -> dict: ...
```

`FileResourceBackend` 规则：

- 根目录固定为部署配置的 `/srv/agentmgr-exec-resources`（`root:root 0755`，文件 `0444`）；
- 资源 ID 必须匹配正则，且同时出现在 `spec.resource_scope.resource_ids` 与 backend 的允许清单中；
- 路径 = `root / f"{resource_id}.json"`；**拒绝**任何含 `/`、`..`、NUL 的值；
- 用 `os.open(..., O_RDONLY|O_NOFOLLOW)` 打开，再 `os.fstat` 确认是普通文件；`os.path.realpath` 必须仍位于根目录内；
- 只读；不创建、不删除、不写入；
- 返回 `{"resource_id", "media_type", "body"}`；单文件上限 1 MiB。

未来 `ResourceBackend`（数据库/API/对象存储）实现同一协议，并在 backend 内重复做范围校验；网关的范围校验不因 backend 可信而省略。

### 10.6 审计与脱敏

允许字段（白名单）：

| 字段 | 说明 |
|---|---|
| `ts` | UTC 时间 |
| `event` | `security.exec.gateway.allow` / `.deny` |
| `task_id` / `grant_id` | 标识 |
| `credential_jti` | 凭据关联 ID（不是令牌） |
| `capability` / `resource` | 调用对象 |
| `decision` | `allow` / `deny` |
| `reason` | 稳定原因码 |
| `peer_uid` | 对端 UID（数值） |
| `limit` | 触发限额的稳定名：`per_task_connections` / `per_task_rps` / `global_connections` / `global_rps`（仅限流拒绝时） |
| `boundary_claim_version` / `network_profile` | 边界声明版本 |

禁止字段：token、token 前缀/哈希以外的任何派生值、payload、异常文本、堆栈、绝对路径、宿主名、用户名。审计写入失败时：**拒绝调用**（fail closed）还是放行由部署策略决定，MVP 选择拒绝并告警，避免“无审计的执行”。

## 11. Supervisor 生命周期

### 11.1 状态与映射

| 观测（`systemctl show`） | Supervisor 状态 | `process_gone` | `stop_confirmed` | `never_started` |
|---|---|---|---|---|
| `ActiveState=activating` | `starting` | false | false | false |
| `ActiveState=active` 且 `SubState=running` | `running` | false | false | false |
| `ActiveState=active` 且 `SubState=exited` 且**停止请求存在且退出不早于请求** | `cancelled` | **true** | **true** | false |
| `ActiveState=active` 且 `SubState=exited` 且**无停止请求（或退出早于请求）** | `completed` | **true** | **false** | false |
| `ActiveState=deactivating` | `stopping` | false | false | false |
| `ActiveState=inactive` 且 `MainPID=0` 且 `Result=success` 且 `ExecMainStatus=0` 且**无 stop/cancel 请求（或退出早于请求）** | `completed` | **true** | **false** | false |
| `ActiveState=inactive/failed` 且 `MainPID=0` 且 `Result=timeout` | `timed_out` | **true** | **true** | false |
| `ActiveState=inactive/failed` 且 `MainPID=0` 且**停止请求早于退出** 且 `Result` 非 `timeout` | `cancelled` | **true** | **true** | false |
| `ActiveState=failed` 且 `MainPID=0` 且无 stop/cancel 请求（其它 `Result`） | `failed` | **true** | **false** | false |
| 单元不存在（`LoadState=not-found`）且**已有启动证据** | `unknown` | **true** | false | false |
| 单元不存在且**无启动证据** | `not_started` | false | **false** | **true**（可安全重试） |
| `ActiveState=unknown` / 字段缺失 | `unknown` | false | false | false |

三个布尔值语义不同，不能互相替代：

- **`process_gone`**：单元已不在运行。判定为 `ActiveState ∈ {inactive, failed}` 且 `MainPID` 解析为整数 `0`、`ActiveState=active` 且 `SubState=exited`，或 `LoadState=not-found` 且已有启动证据。`MainPID=0` 单独不足以证明“曾经运行过”——从未启动的单元也是 0，因此必须结合 `ActiveEnterTimestamp`/`ExecMainStartTimestamp`/`ExecMainExitTimestamp`/`LoadState` 区分 `not_started`。
- **`stop_confirmed`**：由 Supervisor 发起 terminate/stop/cancel，或由 `RuntimeMaxSec` 触发的 timeout 停止，并且已取得上述停止证据。自然 `completed`/`failed` 只说明进程结束，**不是**“停止请求已被证实”，一律为 false；`deactivating`、`activating`、`unknown`、字段缺失也为 false。`cancelled` 与 `completed` 用 `ExecMainExitTimestamp` 和停止请求时间排序判定：退出早于请求即自然完成，请求早于退出即已确认停止；同一秒内无法可靠排序时返回 `unknown`，不得猜测。
- **`never_started`**：`LoadState=not-found` 且没有任何启动证据；它表示“从未开始”，不是“已停止”。

`RuntimeObservation` 同时暴露这三个字段；`cleanup` 的前置条件是 `process_gone || never_started`（§11.4），不是 `stop_confirmed`。

### 11.2 launch

1. 解析并校验 `ManagedTaskSpec`（§7.2）；失败即返回 `invalid_spec` 等，无副作用。
2. 实时读取 `GrantStatusProvider.status()`：校验存在、未过期、`active`、`usable`、`execution_mode=managed_execution`、`task_id`/`grant_id` 一致。**过期时间只取本次返回的 `expires_at`**；spec 不提供过期时间。
3. 计算有效 TTL：`min(spec.ttl_seconds, provider.expires_at - now, 300)`，且 ≥ 1。
4. 计算单元名（§8.5），确认不与运行中单元冲突。
5. 生成 `jti`/`nonce`，签发 token。
6. 通过 `PrivilegedRunner` 把已校验的 spec 与 token（经 stdin，不进 argv/env）交给 root helper；helper 校验后在 `specs/` 与 `credentials/`（`root:root 0700`）写入 `<unit>.json`（`0600`，`O_EXCL`），再执行 `systemd-run`（§12）。
7. 轮询 `inspect` 直到 `ActiveState=active`，上限 **10s**；超时：进入“可能已启动”处理（§11.5）。
8. 调用 helper `remove-sources <unit>` 删除凭据源文件与 spec 文件；返回 handle（含 `network_profile`、`BoundaryClaim`、`expires_at`）。
9. 写 `security.exec.supervisor.launch` 审计（含单元名、uid、TTL、profile、claim 版本；不含 token）。

### 11.3 轮询

- `Supervisor.poll(handle)` 每次执行一次 `inspect`，返回 `RuntimeObservation`（状态、`MainPID`、`Result`、`ExecMainStatus`、`ActiveEnterTimestamp`、`ExecMainStartTimestamp`、`ExecMainExitTimestamp`）；`ActiveState=active` 且 `SubState=exited` 也作为已退出证据；
- 监控循环默认 **1s** 一次；发现 `grant_revoked`/`grant_expired` 立即进入 stop 流程；
- `RuntimeMaxSec` 到期由 systemd 终止；Supervisor 观测到 `Result=timeout` 后标记 `timed_out` 并撤销凭据；
- 轮询不修改任务状态机，只报告事实。

### 11.4 stop 与 cleanup

`stop(handle, reason)`：

1. 先在撤销存储写入 `jti`（reason = `grant_revoked`/`grant_expired`/`manual_stop`/`task_terminal`）；
2. 调用 `terminate <unit>`（固定 `systemctl kill --signal=SIGTERM --kill-whom=all <unit>`），让负载收到 SIGTERM；`RemainAfterExit=yes`/`CollectMode=inactive` 保证单元退出后仍保留为 `active/exited`；
3. 轮询 `inspect` 直到取得退出证据（`ActiveState=active` 且 `SubState=exited` 且有 `ExecMainExitTimestamp`，或 `ActiveState ∈ {inactive, failed}` 且 `MainPID=0`），上限 **10s**；
4. 若进程仍在运行：调用 `systemctl kill --signal=SIGKILL --kill-whom=all <unit>`，再轮询 3s；
5. 仍无退出证据 → 返回 `unit_stop_unconfirmed`（`process_gone=false`、`stop_confirmed=false`），写 `security.exec.supervisor.stop_unconfirmed` 审计并告警；**不得**报告已停止；
6. 取得证据后调用 `stop <unit>`（`systemctl stop`）结束保留的 `active/exited` unit，再进入 cleanup；
7. 写 stop 审计（含 `process_gone=true`、`stop_confirmed=true`、`Result`、`ExecMainStatus`）。

`cleanup(handle)`：

1. 仅当 `process_gone=true` 或 `never_started=true` 时执行；两者都为 false（例如停止未确认）时拒绝清理并返回 `unit_stop_unconfirmed`，不得删除状态目录或凭据残留；
2. `systemctl reset-failed <unit>`（清理失败态残留）；若单元已被回收（`LoadState=not-found`），该调用是 **no-op**，不得视为失败；
3. 删除 `/var/lib/private/agentmgr-exec/<unit>`（及 `/var/lib/agentmgr-exec/<unit>` 符号链接，若仍存在）；
4. 由 root helper 删除残留凭据/spec 文件（`credentials/`、`specs/` 为 `root:root 0700`）；
5. prune 过期撤销记录；
6. 返回 `CleanupResult`（`units_remaining=0`、`state_dir_removed=true`、`credential_removed=true`）。

### 11.5 竞态处理

| 竞态 | 处理 |
|---|---|
| 完成 vs 停止 | 以最终 `Result`/`ExecMainStatus` 与时间戳为准：`Result=success` 且 `ExecMainStatus=0` 且退出早于停止请求 → 保持 `completed`（`process_gone=true`、`stop_confirmed=false`），`systemctl stop` 视为幂等空操作；不得把自然完成降级为 `cancelled` |
| 停止请求与自然完成竞争 | 用 `ExecMainExitTimestamp` 与停止请求时间排序：退出早于请求 → `completed`（`stop_confirmed=false`）；请求早于退出 → `cancelled`（`process_gone=true`、`stop_confirmed=true`）。不得仅凭“发过 stop 请求”声明停止成功，也不得把已确认的停止降级为自然完成 |
| 凭据撤销写失败 | 写失败重试一次；launch 阶段失败则**不启动**（返回 `unknown`）；stop 阶段失败仍继续停止，标记 `credential_revoke_failed`，告警，且网关对 `GrantStatusProvider` 的实时检查仍会拒绝 |
| 撤销存储与 Grant 状态源均不可用 | 网关拒绝全部调用（`unknown`），触发告警 |
| `systemd-run` 超时 | 视为“可能已启动”：`inspect` 确认；存在则 stop + 取证，不存在（`LoadState=not-found` 且无时间戳）才返回 `unit_start_failed` |
| 单元启动失败（属性不支持） | `inspect` 读取 `Result`/`ExecMainStatus`，返回 `unit_start_failed`，不签发“已隔离”声明，不重试降级 |
| 单元名冲突 | `invalid_spec`（`unit_name_collision`），不覆盖已有单元 |

### 11.6 稳定错误码

| 错误码 | 含义 | 是否可重试 |
|---|---|---|
| `invalid_spec` | spec 结构/取值非法（含未知字段如 `grant_expires_at`/`workspace`/`state`、超出范围） | 否（修正输入） |
| `grant_missing` | 状态源无此 Grant | 否 |
| `grant_expired` | Grant 已过期 | 否（需新 Grant） |
| `grant_revoked` | Grant 已撤销/已消费/不可用 | 否 |
| `execution_mode_mismatch` | 非 `managed_execution` 或与 Grant 不一致 | 否 |
| `privilege_required` | 无可用特权执行通道 | 是（修复部署后） |
| `unit_start_failed` | systemd 拒绝或单元未进入 active | 视原因；不得自动降级属性 |
| `unit_stop_unconfirmed` | 停止未取得证据 | 是（升级处置） |
| `credential_expired` | 凭据过期 | 否（需重新签发） |
| `credential_revoked` | 凭据已撤销 | 否 |
| `capability_not_granted` | 能力不在授权内 | 否 |
| `resource_scope_denied` | 资源不在范围 | 否 |
| `cross_task_denied` | 跨任务访问 | 否（安全事件） |
| `network_denied` | 观测到的网络拒绝（证据分类） | 不适用 |
| `filesystem_denied` | 观测到的文件系统拒绝（证据分类） | 不适用 |
| `timeout` | 操作超时（语义不明确） | 先对账，不盲目重试 |
| `unknown` | 无法归类/状态源不可用 | 先对账 |

错误消息只包含错误码与**稳定 detail 字符串**（如 `unit_name_collision`），不含路径、单元内部标识、令牌、异常堆栈。

## 12. 特权操作

### 12.1 信任模型

- **controller 受信**：它持有凭据密钥、拥有并写入 `/run/agentmgr-exec`（`gateway.sock`、`revoked/`；`credentials/` 与 `specs/` 由 root helper 写）、能调用特权助手。它等价于宿主 root 能力的一部分。
- **任务代码不受信**：`smoke_workload.py` 及其子进程只能通过内核强制边界被约束；它们永远不持有 sudo、不持有密钥、不控制单元属性。
- 因此本设计**不试图**防御“controller 被完全攻陷”；那等价于宿主 root 失陷，属于 §15.2 的绕行清单。

### 12.2 测试机（本轮之后的实现阶段）

- 使用所有者已授权的非交互 sudo；实现阶段开始前先用只读命令确认 `sudo -n` 可用，不可用则返回 `privilege_required`，不尝试其它提权方式；
- 只操作 `agentmgr-task-[0-9a-f]{12}` 单元，以及 `/run/agentmgr-exec`、`/var/lib/private/agentmgr-exec` 路径；
- 测试机上 `/run/agentmgr-exec` 由 controller 服务用户 `agents` 拥有（`0700`）；controller/gateway 在其中创建 `gateway.sock` 与 `revoked/`；`credentials/`、`specs/` 由 root helper 以 `root:root 0700` 创建。不新增持久用户；
- 不新建持久用户、不安装软件包、不改 nftables/防火墙、不写现有 Hermes Profile；
- 测试结束执行 §18.1 清理；
- **本轮（设计阶段）不安装任何 sudo 规则、不执行任何命令。**

### 12.3 生产模板：窄助手 + 固定 sudoers

- 助手路径：`/usr/local/libexec/agentmgr-exec`（`root:root 0755`）；
- sudoers 只固定一个可执行文件：`agentmgr ALL=(root) NOPASSWD: /usr/local/libexec/agentmgr-exec`。**模板中的 `agentmgr` 是 controller 服务用户的占位符**（测试机为 `agents`；生产替换为 `<controller-user>`），不是仓库账号，也不是本设计新增的持久用户。sudoers **不限制参数**，也不承担参数校验；它只回答“这个用户能不能执行这个 helper”。
- 所有参数校验（unit 名称正则、spec 字段白名单、子命令与参数个数）都在 root-owned helper 内完成，任何不符合的输入 fail closed。

| 子命令 | 参数 | 行为 |
|---|---|---|
| `launch` | stdin JSON（`spec` + `credential`） | 校验后把 spec/凭据写入 `root:root 0700` 的 `specs/`、`credentials/`（`0600`），再构造固定 `systemd-run` argv；token 不进入 argv/env |
| `remove-sources` | `<unit>` | 删除该单元的 `credentials/<unit>.json` 与 `specs/<unit>.json`（单元 active 后或清理时） |
| `stop` | `<unit>` | `systemctl stop`，返回结构化结果 |
| `terminate` | `<unit>` | 固定 `systemctl kill --signal=SIGTERM --kill-whom=all <unit>`，用于保留退出证据 |
| `inspect` | `<unit>` | `systemctl show -p ActiveState,SubState,MainPID,Result,ExecMainStatus,ActiveEnterTimestamp,ExecMainStartTimestamp,ExecMainExitTimestamp,LoadState` |
| `reset-failed` | `<unit>` | `systemctl reset-failed`；`LoadState=not-found` 时 no-op |
| `cleanup` | `<unit>` | `reset-failed` + 删除经校验的状态目录与残留文件 |

助手的强制校验：

- 单元名必须匹配 `^agentmgr-task-[0-9a-f]{12}$`，否则拒绝；
- 只从 stdin 读取请求 JSON（不进 argv/env），只接受 §7.1 的字段集合与一个短 token，**不得**包含 `ExecStart`、`User`、`Environment`、`BindPaths` 等属性；写入的 `specs/`、`credentials/` 目录必须是 `root:root 0700`，文件以 `O_CREAT|O_EXCL` 创建、`0600`、非符号链接；
- 所有 systemd 属性值来自常量模板或经范围校验的整数/枚举，不接受任意字符串拼接；
- 不调用 shell（无 `sh -c`），argv 数组直接 `execve`；
- 不继承调用方环境变量（`env -i` 等价）；
- stdout 只输出结构化 JSON；错误码稳定，不含内部路径。

**为什么 sudoers 只放行固定可执行文件**：`systemctl *` 等于“任意单元任意动作”，`systemd-run *` 等于“以 root 运行任意命令/任意属性”，两者都直接等价于 root shell。即使写成 `systemctl stop agentmgr-task-*`，通配符仍可被路径/参数技巧绕过。sudoers 不是参数校验器，因此本设计**不用它表达参数约束**：它只固定一个可执行文件，全部参数校验由 root 拥有的助手完成。

## 13. 计划文件布局

```
app/security/__init__.py
app/security/boundary.py          # ManagedTaskSpec, ResourceScope, BoundaryClaim, 单元名
app/security/credentials.py       # agx1, HMAC/JCS, RevocationStore, GrantStatusProvider
app/security/gateway.py           # NDJSON 协议, 准入/限流, 校验链, FileResourceBackend, 审计; `python -m app.security.gateway` 仅作生产独立服务入口（#83），不是 MVP 验证入口
app/security/systemd.py           # systemd-run/systemctl argv, 状态解析与映射
app/security/supervisor.py        # launch/poll/stop/inspect/cleanup, 竞态与监控循环
app/security/errors.py            # 稳定错误码与安全错误消息

app/tests/security/__init__.py
app/tests/security/test_boundary.py
app/tests/security/test_credentials.py
app/tests/security/test_gateway.py
app/tests/security/test_systemd_unit.py
app/tests/security/test_supervisor.py
app/tests/security/test_isolation_live.py   # 默认 skip，需双环境变量

ops/security/agentmgr-exec                 # 生产窄助手（root 拥有）
ops/security/agentmgr-exec.sudoers         # 固定一行 sudoers 模板
ops/security/agentmgr-pep-gateway.service  # 网关单元模板（生产）
ops/security/agentmgr-task@.service        # 受控任务模板（备选；默认 transient）
ops/security/verify_isolation.sh           # 真实隔离验证入口：只做环境检查并调用 harness
ops/security/verify_isolation.py           # 同进程 harness：网关线程 + 内存实时 GrantStatusProvider + §16.3 探针 + 证据 JSON
ops/security/smoke_workload.py             # 合成负载与负向探针
ops/security/README.md                     # 安装、验证、回滚说明

docs/security/managed-execution-isolation-design.md   # 本文
docs/security/managed-execution-boundary.md           # 部署时生成的边界记录（实测证据引用）
```

约束：

- `docs/security/managed-execution-boundary.md` 是**部署记录**，在实现阶段用真实证据填写；它不是本设计的替代，也不得在没有证据时写成“已验证”。
- 上述路径仅在 Issue #82 的允许路径内；若实现时确需调整，只能在 `app/security/**`、`app/tests/security/**`、`ops/security/**`、`docs/security/**` 内调整，并在 PR 中说明。

## 14. 数据流

### 14.1 启动（launch）

```
调用方 → Supervisor: ManagedTaskSpec (JSON)
Supervisor → BoundaryValidator: 结构/模式/时间/限额
Supervisor → GrantStatusProvider: status(grant_id, task_id)   [信任边界：受信→受信]
Supervisor → CredentialIssuer: issue(claims) → token(agx1)
Supervisor → PrivilegedRunner: launch(spec, token) via stdin   [受信→root 助手]
root 助手 → specs/<unit>.json + credentials/<unit>.json (root:root 0700, 0600)
root 助手 → systemd-run: 固定属性 + LoadCredential + BindReadOnlyPaths
systemd → 单元: DynamicUser/只读 FS/私有 netns/AF_UNIX/限额
单元 → /workload.py: 读取 $CREDENTIALS_DIRECTORY/task-credential
Supervisor → systemctl show: 直到 ActiveState=active
Supervisor → root 助手: remove-sources <unit>；返回 handle + BoundaryClaim
```

### 14.2 允许的调用

```
单元(不受信) → connect(/run/agentmgr/gateway.sock)
              send {"version","token","capability","resource","payload"}
网关: SO_PEERCRED → 结构 → version → MAC → claims → nbf/exp
网关 → RevocationStore.lookup(jti)                 [受信]
网关 → GrantStatusProvider.status(grant_id)        [受信，每次调用]
网关: task/grant 一致 → capability → scope/exclude → backend allowlist
网关 → FileResourceBackend.read(resource)          [受信→只读资源]
网关 → 审计 allow
网关 → 单元: {"ok":true,"result":{...}}
```

### 14.3 被拒绝的调用

| 拒绝点 | 输入 | 响应 |
|---|---|---|
| 令牌无效/过期 | 错误 MAC、过期 `exp` | `token_invalid` / `credential_expired` |
| 凭据已撤销 | Supervisor 已撤销 `jti` | `credential_revoked` |
| Grant 已撤销/过期 | 状态源返回 revoked/expired | `grant_revoked` / `grant_expired` |
| 跨任务（身份） | 令牌的 `task_id`/`grant_id`/`subject_id` 与实时 Grant 投影不一致 | `cross_task_denied` |
| 能力不足 | `resource.write@1.0.0` 不在 allowed | `capability_not_granted` |
| 范围外 / 另一任务资源 | 资源 ID 不在本凭据/本 Grant 的 `resource_ids`（含另一任务的资源） | `resource_scope_denied` |
| 资源不允许 | backend 允许清单外/非普通文件 | `resource_denied` |
| 并发超限 | 每任务 >8 或全局 >64 个并发连接 | `busy` |
| 速率超限 | 每任务 >16/s 或全局 >200/s | `rate_limited` |
| 协议错误 | 未知字段、>64 KiB | `bad_request` / `payload_too_large` |

所有拒绝均无副作用，且写 deny 审计；不返回任何内部细节。

### 14.4 Grant 撤销 / 过期

```
撤销方(核心/运维) → GrantStatusProvider: status=revoked
Supervisor 监控循环(≤1s) → 发现 revoked/expired
Supervisor → RevocationStore.revoke(jti)
Supervisor → 助手 terminate → systemctl kill --signal=SIGTERM --kill-whom=all
Supervisor → systemctl show: active/exited + ExecMainExitTimestamp（或 inactive/failed 且 MainPID=0）→ process_gone=true
Supervisor → 已发起 terminate 且有上述证据 → stop_confirmed=true
Supervisor → 助手 stop → systemctl stop（结束保留的 active/exited unit）
Supervisor → 审计 stop + 告警（如涉及 unconfirmed）
Supervisor → cleanup（process_gone=true）: reset-failed（not-found no-op）+ 删除状态目录
```

在停止完成之前，任何新调用都会在网关第 9 步（实时 `GrantStatusProvider`）被 `grant_revoked` 拒绝。

### 14.5 TTL 到期

```
凭据 exp 到点 → 网关拒绝新调用（credential_expired）
systemd RuntimeMaxSec 到点 → SIGTERM→SIGKILL，Result=timeout
Supervisor → 观测 timed_out（process_gone=true, stop_confirmed=true）
Supervisor → 撤销 jti（幂等）→ cleanup（process_gone=true）
```

### 14.6 清理

```
Supervisor.cleanup(handle):
  前置: process_gone=true 或 never_started=true（否则拒绝）
  reset-failed → 删除 /var/lib/private/agentmgr-exec/<unit>
  由 root helper 删除残留 credentials/specs 文件
  prune 过期撤销记录
  证据: units_remaining=0, state_dir_removed=true
```

## 15. 安全不变量与绕行清单

### 15.1 本基座强制的属性（`BoundaryClaim.enforced`）

| 不变量 | 强制点 |
|---|---|
| 每任务独立 UID | `DynamicUser=yes` |
| 文件系统只读（除状态目录与私有 tmp） | `ProtectSystem=strict`、`ProtectHome=yes`、`InaccessiblePaths=` |
| 无宿主 home/Profile 访问 | `ProtectHome=yes` |
| 独立 TMPDIR | `PrivateTmp=yes` |
| 无 capability | 空 `CapabilityBoundingSet=`/`AmbientCapabilities=`、`NoNewPrivileges=yes` |
| 禁止新建 namespace | `RestrictNamespaces=yes` |
| 无 IP 网络 | `PrivateNetwork=yes`（默认）、`RestrictAddressFamilies=AF_UNIX`、`IPAddressDeny=any` |
| 唯一外部入口为网关 socket | 只读 bind mount 一个 socket；`TemporaryFileSystem=/run/systemd`、`/run/dbus` 遮蔽宿主 systemd/D-Bus 目录 |
| 资源与时间上限 | `MemoryMax`、`CPUQuota`、`TasksMax`、`LimitNOFILE`、`RuntimeMaxSec` |
| 跨任务状态隔离 | 独立 UID + `StateDirectory=agentmgr-exec/<unit>`（`StateDirectoryMode=0700`） |
| 凭据任务绑定/短时/可撤销 | `task_id`/`grant_id` claims、`exp ≤ GrantStatusProvider 剩余`、`RevocationStore` |
| 停止/退出区分 | `process_gone` = 单元不再运行（含 `active/exited`）；`stop_confirmed` 仅在 terminate/stop/cancel/timeout 取得退出证据时为 true；`RemainAfterExit`+`CollectMode=inactive` 保留退出证据；`cleanup` 用 `process_gone \|\| never_started` |
| 网关准入上限 | per-task 8 连接 / 16 req/s、global 64 连接 / 200 req/s、单请求 64 KiB、读写 5s |
| 模式一致 | spec 与 Grant 双重校验 `managed_execution` |

### 15.2 无法由本基座消除（`BoundaryClaim.bypassable`）

| 绕行路径 | 说明 | 处置 |
|---|---|---|
| 宿主 root / controller sudo | 可读凭据、改 cgroup、杀进程、改 systemd | 组织控制、最小 sudo、审计；产品不声称防御 root |
| systemd/内核漏洞 | 沙箱属性由内核实现，漏洞可致逃逸 | 补丁管理、最小版本基线、持续复测 |
| 侧信道 | 时间、缓存、CPU 争用、cgroup 统计 | 降低并发与共享；不作为已解决 |
| 物理访问 | 控制台、磁盘、内存 | 物理安全与磁盘加密（部署要求） |
| 网关 backend 缺陷 | 范围校验错误、路径处理错误 | 代码评审 + 负向测试；backend 内重复校验 |
| 未经网关的资源路径 | 若部署把资源挂进单元或网络可达 | 部署基线检查；本设计不挂载业务资源 |
| 同宿主其它用户连接 `gateway.sock` | MVP 测试模式 `0666`：若知道路径可 `connect()`（目录 `0700` 通常已阻断遍历） | socket 模式不是授权；无有效 token 仍被 `token_invalid` 拒绝；生产可改 `0660` + 专用组 |
| 同 UID 内的子进程 | 任务可派生子进程共享凭据 | 任务级而非进程级凭据；`KillMode=control-group` 兜底 |
| UID 复用窗口 | 停止后状态目录残留 | `process_gone`（或 `never_started`）后立即 cleanup；残留告警 |

### 15.3 延后 / 未验证（`BoundaryClaim.deferred`）

| 能力 | 状态 |
|---|---|
| Hermes 内部工具入沙箱 | #83；当前 Adapter 继续拒绝 `managed_execution` |
| 模型供应商出口 | 未实现、未验证 |
| 浏览器/终端治理 | 未实现、未验证 |
| Advisory/Governed 客户端本地工具 | 不可远程强制；产品边界 |
| 真实数据库/对象存储 backend | 未实现；MVP 仅文件 backend |
| 多主机/多租户调度 | 未实现 |

**产品声明**：在 #83 完成并通过真实证据前，Managed Execution 的可用声明仅限“合成负载隔离基座”；不得用于对外描述 Hermes 或任何真实业务负载。

## 16. 测试计划

### 16.1 分层

| 层 | 位置 | 数据库 | 默认运行 |
|---|---|---|---|
| 单元测试（纯内存、无特权） | `app/tests/security/test_boundary.py`、`test_credentials.py`、`test_gateway.py`、`test_systemd_unit.py`、`test_supervisor.py` | 不需要 | 是 |
| 真实隔离测试 | `app/tests/security/test_isolation_live.py` + `ops/security/verify_isolation.sh`（环境检查）→ `ops/security/verify_isolation.py`（同进程 harness） | 不需要 | **否**（skip） |

真实测试开关：`AGENTMGR_ISOLATION_TEST=1` **且** `AGENTMGR_ISOLATION_SUDO=1`；缺一即 `pytest.skip`，原因写明所需变量。真实测试只在所有者批准的隔离测试机运行。

安全单元测试不导入 `app.db`、不建立数据库连接、不使用数据库 fixture；它们可以在无数据库环境用 `pytest --noconftest app/tests/security` 单独运行（该命令为设计目标，本轮未执行）。

### 16.2 单元测试覆盖

| 测试 | 覆盖 |
|---|---|
| `test_boundary.py` | 未知字段/重复键/非法 JSON（显式覆盖 `grant_expires_at`/`workspace`/`state` 被拒）、NUL/控制字符、模式拒绝、`ttl_seconds` 的 1/300 边界、资源范围默认值与边界、Grant 子集校验、`BoundaryClaim` 常量完整性、单元名确定性与正则、哈希碰撞分支 |
| `test_credentials.py` | **RFC 8785 JCS 向量**：UTF-8（`café`、非 BMP）、UTF-16 键序 vs code point 顺序、整数边界（`0`、`-0`、`2^53-1`、`2^53` 拒绝）、嵌套对象/数组、稳定重放；MAC 校验、篡改/截断/错误 kid/过期/`nbf`、claim 键集合、`exp - iat ≤ 300`、短密钥与缺失密钥 fail closed、轮换窗口、`InMemoryRevocationStore`、`FileRevocationStore` 原子写入与并发读、prune |
| `test_gateway.py` | NDJSON 单请求、64 KiB 边界、未知字段、版本、所有 deny 原因码（含 `rate_limited`/`busy`）、per-UID/全局连接与速率限额、一个任务不能占满全局配额（fake clock/计数器）、超限不排队、`SO_PEERCRED` 记录（用 socketpair/mock）、审计白名单（token 不出现）、backend 路径校验/符号链接拒绝、超时 |
| `test_systemd_unit.py` | argv 构造精确匹配、`RuntimeMaxSec` 1..300、`MemoryMax` 32..1024、`CPUQuota` 1..100、`TasksMax` 1..64、空 capability 语法、显式 `StateDirectory=agentmgr-exec/<unit>`（无 specifier）、无 shell 拼接、`systemctl show` 输出解析、状态映射表逐行 |
| `test_supervisor.py` | 用 fake runner 测 launch/stop/cleanup 状态机、竞态（完成 vs 停止按 `ExecMainExitTimestamp` 与停止请求时间排序、撤销写失败、启动超时、停止超时）、错误码；逐行验证 `completed`/`failed` → `process_gone=true, stop_confirmed=false`、`not_started` → `never_started=true`、`timed_out`/`cancelled` → `stop_confirmed=true`、`cleanup` 前置条件 |

### 16.3 真实隔离负向矩阵

| # | 探针 | 期望 | 分类码 |
|---|---|---|---|
| 1 | 读 `/etc/shadow` | 拒绝（`EACCES`/`ENOENT`） | `filesystem_denied` |
| 2 | 读 `<test-host-home>/.hermes` 下 Profile | 拒绝（`ProtectHome=yes`） | `filesystem_denied` |
| 3 | 读 `/var/lib/postgresql` | 拒绝（`InaccessiblePaths`） | `filesystem_denied` |
| 4 | 读另一任务的 StateDirectory | 拒绝（不同 UID + 0700） | `cross_task_denied` |
| 5 | 读另一任务的凭据源文件 | 拒绝（目录 `root:root 0700`、文件已删除） | `cross_task_denied` |
| 6 | `socket(AF_INET, SOCK_STREAM)` | 失败（`EAFNOSUPPORT`/`EPERM`） | `network_denied` |
| 7 | `connect 127.0.0.1:5432` | 失败（同上；无 IP 能力） | `network_denied` |
| 8 | `connect 203.0.113.10:443` | 失败（同上） | `network_denied` |
| 9 | 连接 `<metadata-endpoint>` | 失败（同上） | `network_denied` |
| 10 | 读 `/proc/self/status` 的 `CapEff` | `0000000000000000` | 证据字段 |
| 11 | 同上 `NoNewPrivs` | `1` | 证据字段 |
| 12 | `unshare(CLONE_NEWUSER\|CLONE_NEWNS)` | `EPERM` | 观测标签 `namespace_denied`（仅证据字段） |
| 13 | `ps` / `/proc` 枚举 | 只看到本单元进程 | 证据字段 |
| 14 | 写 `/etc`、`/usr`、`/var/lib`、`/srv` | `EROFS`/`EACCES`/`ENOENT` | `filesystem_denied` |
| 15 | 经网关读允许资源 | `ok:true`，`resource_id` 匹配 | 允许路径 |
| 16 | 过期凭据（TTL 2s 后调用） | `credential_expired` | `credential_expired` |
| 17 | 撤销后调用 | `credential_revoked` | `credential_revoked` |
| 18 | 凭据/Grant 身份不匹配（task/grant/subject）与另一任务的资源 ID | 身份不匹配 → `cross_task_denied`；另一任务的资源 ID 不在本凭据范围 → `resource_scope_denied` | `cross_task_denied` / `resource_scope_denied` |
| 19 | 范围外资源 ID | `resource_scope_denied` | `resource_scope_denied` |
| 20 | 未授予能力 | `capability_not_granted` | `capability_not_granted` |
| 21 | Grant 撤销后网关调用（单元尚在停止中） | `grant_revoked` | `grant_revoked` |
| 22 | TTL=2s 的 `sleep 30` | unit 在 ≤8s 内终止，`Result=timeout`；`process_gone=true`、`stop_confirmed=true` | `timeout` |
| 23 | 撤销 Grant 后停止 `sleep 30` | ≤5s 内取得 `active/exited` + `ExecMainExitTimestamp`（或经 `systemctl stop` 后 `inactive/failed`）且 `MainPID=0`；`process_gone=true`、`stop_confirmed=true` | 证据字段 |
| 24 | 清理 | `process_gone=true`（或 `never_started=true`）后清理；无 `agentmgr-task-*` 单元、无 `/run/agentmgr-exec`、无 `/var/lib/private/agentmgr-exec` | 证据字段 |
| 25 | 同一任务在 1s 内发起 17 次允许调用 | 第 17 次起 `rate_limited`，连接立即关闭，前 16 次正常返回 | `rate_limited` |
| 26 | 同一任务保持 9 个并发连接不发送请求 | 第 9 个连接收到 `busy` 并关闭；前 8 个在 5s 内超时关闭，不排队 | `busy` |

说明：

- 第 6–9 项的探针在**单元内**执行；`network_denied` 是观测分类，不是网关返回码。
- 第 22 项的时间上限包含 systemd 运行时检查粒度；断言使用 `≤8s`，避免把调度抖动误判为缺陷。
- 第 12 项 `unshare` 的失败原因可能是 `RestrictNamespaces=yes` 或 `SystemCallFilter`；证据需记录 `errno` 原文。`namespace_denied` 只是证据 JSON 中的观测标签，不属于网关原因码集合。
- 第 15 项成功后必须验证返回体**不含**其它资源内容。
- 第 18 项区分两种拒绝：令牌/凭据身份（`task_id`/`grant_id`/`subject_id`）与实时 Grant 不一致才是 `cross_task_denied`；另一任务的资源 ID 属于资源范围判定，返回 `resource_scope_denied`，不要为它增加重复的资源归属检查。
- 第 25/26 项验证 **per-task** 上限；全局上限（64 连接 / 200 req/s）由 `test_gateway.py` 的 fake clock/计数器覆盖，不在单机 live 矩阵中制造 64 个并发任务。两项都必须核对：超限响应后连接立即关闭，进程/文件描述符没有泄漏。

### 16.4 证据 JSON

字段（示例结构；尖括号为运行时取值，不是真实结果）：

```json
{
  "run_id": "iso-<utc>-<seq>",
  "started_at": "<utc>",
  "commit_sha": "<40 hex>",
  "contract_version": "1.0.0",
  "host": {
    "os": "linux",
    "kernel": "<uname -r>",
    "systemd": "<systemctl --version 首行>",
    "systemd_runtime": "system"
  },
  "network_profile": "private_netns",
  "unit": "agentmgr-task-<12 hex>",
  "uid": 61000,
  "systemd_properties": {
    "ActiveState": "inactive",
    "Result": "success",
    "MainPID": 0
  },
  "process": {
    "cap_eff": "0000000000000000",
    "no_new_privs": "1"
  },
  "gateway": {
    "mode": "in_process_harness",
    "grant_provider": "in_memory_live",
    "limits": {
      "per_task_connections": 8,
      "per_task_requests_per_second": 16,
      "global_connections": 64,
      "global_requests_per_second": 200,
      "max_request_bytes": 65536,
      "read_write_timeout_seconds": 5
    },
    "probes": [
      {"id": "gateway-rate-limit", "expected": "rate_limited", "observed": "<observed>"},
      {"id": "gateway-busy", "expected": "busy", "observed": "<observed>"}
    ]
  },
  "probes": [
    {
      "id": "read-etc-shadow",
      "command": "python3 /workload.py --probe read-etc-shadow",
      "exit_code": 13,
      "expected": "denied",
      "observed": "EACCES",
      "reason": "filesystem_denied"
    }
  ],
  "cleanup": {
    "units_remaining": 0,
    "run_dir_present": false,
    "state_dir_present": false
  },
  "result": "<pass|fail>"
}
```

规则：

- 证据写入 `$AGENTMGR_ISOLATION_EVIDENCE`（仓库外路径），**不提交**到仓库；
- `probes[]` 覆盖 §16.3 的 #6–#9 网络拒绝，`observed` 记录 `errno` 名称与错误类别，不记录目标主机真实地址；
- `gateway.mode` / `gateway.grant_provider` 记录本次形态：MVP 固定为 `in_process_harness` + `in_memory_live`；独立网关进程 + 文件撤销存储是生产/#83 的形态，需如实标注；
- `gateway.limits` 记录本次运行实际生效的部署限额，必须与网关启动配置一致；`gateway.probes[]` 对应 §16.3 的 #25/#26，`observed` 只在真实运行后填写，本轮为 planned / not run；
- `systemd_properties` 由 `systemctl show` 原样读取，`process` 由单元内探针读取；
- `uid` 只记录数值（示例中的 `61000` 是结构示意，不是真实分配值）；`unit` 已由哈希派生；
- 不含宿主名、用户名、绝对 home 路径、IP、令牌、密钥、Profile 内容；
- 结果与摘要（命令、退出码、`commit_sha`、日期）记录在 Issue/PR 评论中，正文只保留结论与引用。

### 16.5 运行入口

`ops/security/verify_isolation.sh` **只做环境检查并调用 harness**，不自己启动网关进程：

1. 前置检查：`AGENTMGR_ISOLATION_TEST=1`、`AGENTMGR_ISOLATION_SUDO=1`、`systemd ≥ 249`、`systemd-run`/`systemctl` 可用；
2. harness 以 root/`sudo -n` 创建 `/run/agentmgr-exec`（属主 controller/gateway 服务用户，`0700`）与资源目录，把 `workload.py` 部署为 `root:root 0555`；
3. 调用 `python3 ops/security/verify_isolation.py`。harness 在**同一进程**启动网关线程（Unix socket `/run/agentmgr-exec/gateway.sock`，MVP `0666`），并注入**内存实时** `GrantStatusProvider`；**不**启动独立 `python -m app.security.gateway` 进程，也**不**写 `gateway.pid`。systemd 单元仍由 `sudo -n` 启动；
4. harness 依次执行 §16.3 探针；
5. **teardown（无论断言成败都执行）**：停止所有 `agentmgr-task-<hex>` 单元 → `systemctl reset-failed` → harness 关闭自己持有的网关线程与监听 socket、unlink socket 文件（该线程/套接字归 harness 所有，没有独立网关 PID）→ 由 root helper 删除 `/run/agentmgr-exec` 与 `/var/lib/private/agentmgr-exec` → 核对残留；
6. harness 写出证据 JSON，任一断言失败即非零退出。

**生产形态**：独立 `agentmgr-pep-gateway.service`（`python -m app.security.gateway`）是 **#83** 路径，必须接真实 Grant 存储的实时适配器；它不是本 MVP 的验证入口。

**真实测试只有在所有者批准本文与实现后才运行；本轮未运行。**

## 17. 验收映射

| # | #82 验收条目 | 设计落点 | 证据状态（2026-10-23） |
|---|---|---|---|
| 1 | 受控 Agent 无法读取其他主体的凭据或访问未授权资源 | §6.3 目录权限、§8 沙箱属性、§10.4 校验链、§10.1 准入限额、§16.3 #4/#5/#15/#18/#19/#25/#26 | **planned / not run** |
| 2 | Grant 到期或撤销后，后续受控调用失败 | §9.6 实时双重校验（不缓存）、§11.4 stop/cleanup、§14.4/§14.5、§16.3 #16/#17/#21/#23 | **planned / not run** |
| 3 | 绕行测试有真实命令、退出码和证据 | §16.3 全部 26 项、§16.4 证据格式、`verify_isolation.sh` + `verify_isolation.py` | **planned / not run** |
| 4 | 无法强制治理的能力有明确风险记录和对应产品声明限制 | §4、§15.2、§15.3、`BoundaryClaim` | 设计层已记录；运行证据 **planned** |

补充：本文不满足、也不声称满足 #82 的“已实现”条件；它只固定实现与验证的验收规格。

## 18. 上线与回滚

### 18.1 测试机清理（实现阶段使用）

```bash
# 1. 停止并移除本任务创建的单元（逐个列出，不使用宽泛通配）
sudo -n systemctl stop agentmgr-task-<12hex>
sudo -n systemctl reset-failed agentmgr-task-<12hex>

# 2. 确认无残留
systemctl list-units --all 'agentmgr-task-*' --no-legend

# 3. 网关：MVP 由 verify_isolation.py harness 持有线程与 socket
#    harness 正常退出/teardown 时自行关闭网关线程、关闭监听 socket 并 unlink socket 文件；
#    没有 gateway.pid，也没有独立网关 PID 需要 kill。harness 异常终止时先确认无监听进程，
#    再删除残留 socket；不要按名字 kill 任意进程。
#    生产形态才使用：sudo -n systemctl stop agentmgr-pep-gateway.service

# 4. 删除运行目录与状态目录
#    /run/agentmgr-exec 属主为 controller/gateway 服务用户（测试机 agents，0700），
#    其中 credentials/ 与 specs/ 为 root:root 0700；删除需 root helper 或 sudo。
sudo -n rm -rf /run/agentmgr-exec
sudo -n rm -rf /var/lib/private/agentmgr-exec
sudo -n rm -f /var/lib/agentmgr-exec/* 2>/dev/null || true
```

清理后必须核对：无 `agentmgr-task-*` 单元、`/run/agentmgr-exec` 不存在、状态目录不存在、socket 不存在。任一残留即报告失败。

### 18.2 生产停用

1. 停止 controller 的监控循环与新任务创建（不删除已有审计）；
2. 停止并 disable 网关单元（`agentmgr-pep-gateway.service`）；
3. 移除 sudoers 行（`/etc/sudoers.d/agentmgr-exec`）并 `visudo -c` 校验；
4. 删除 `/usr/local/libexec/agentmgr-exec` 与 systemd 模板单元；
5. 删除 `/run/agentmgr-exec`、`/var/lib/private/agentmgr-exec`、`/srv/agentmgr-exec-resources`；
6. 无数据库迁移需要回滚；`DynamicUser` 的 UID 由 systemd 自动回收，无持久用户需删除；
7. 审计与日志保留按现有策略；不删除既有证据。

### 18.3 残余风险

- 停止后状态目录若未清理，存在 UID 复用导致旧数据被读的窗口；
- 网关进程崩溃时任务无法取得新资源（可用性风险，非越权）；
- 宿主 root/controller sudo 始终可绕过全部属性；
- 内核/systemd 版本升级可能改变属性语义；
- 文件 backend 只覆盖只读资源；真实业务资源接入（#83+）需重新评估范围校验与后端权限。

## 19. 监控与告警

| 指标（计划名） | 类型 | 告警条件 |
|---|---|---|
| `agentmgr_exec_units_active` | gauge | — |
| `agentmgr_exec_unit_start_failures_total` | counter | 5 分钟内 ≥1 |
| `agentmgr_exec_unit_failures_total{result}` | counter | 非 `timeout` 的失败 ≥1 |
| `agentmgr_exec_stop_unconfirmed_total` | counter | ≥1（立即） |
| `agentmgr_exec_gateway_denials_total{reason}` | counter | `token_invalid`/`cross_task_denied` 突增 |
| `agentmgr_exec_gateway_throttled_total{reason,limit}` | counter | `rate_limited`/`busy` 持续出现，或单任务长期占满配额 |
| `agentmgr_exec_gateway_connections{scope}` | gauge | 接近全局 64 上限（容量告警，不是越权） |
| `agentmgr_exec_credential_revoke_failures_total` | counter | ≥1（立即） |
| `agentmgr_exec_cleanup_leftovers` | gauge | >0 |
| `agentmgr_exec_grant_status_unavailable_total` | counter | ≥1（网关 fail closed） |
| `agentmgr_exec_gateway_up` | gauge | 0 持续 30s |

日志要求：单元失败、停止未确认、撤销失败、清理残留必须带 `task_id`/`grant_id`/`unit`/原因码；不得带令牌、payload、绝对路径。

## 20. 开放问题

**无阻塞性开放问题。** 以下为设计阶段已作出的取舍，记录以备评审：

1. `StateDirectory=agentmgr-exec/<unit>`：transient 单元使用显式、已校验的单元名，因为 `systemd-run -p` 不展开 `%N`/`%i`；字面 `%N` 会创建 `/var/lib/private/agentmgr-exec/%N` 并破坏每任务隔离。单元文件模板仍可使用自己的 `%i`（§8.1、§8.5）。
2. **MVP 验证固定为同进程**：`ops/security/verify_isolation.py` 在同一进程启动 gateway 线程并注入内存实时 `GrantStatusProvider`，因此“每次调用实时读取、无缓存”成立；`FileRevocationStore` 用于跨进程/重启恢复，在 MVP 中可选（内存实现即可），语义不变。独立网关服务是生产/#83 路径，必须接真实 Grant 存储适配器（§9.6、§9.7、§16.5）。
3. 网络回退是**主机级**选择，不是每任务选择（§8.4）；避免同一主机出现两种声明。
4. 停止语义拆成三个独立事实：`process_gone`（单元不再运行）、`stop_confirmed`（stop/cancel/timeout 已取得证据）、`never_started`（从未启动）。`completed`/`failed` 不等于 `stop_confirmed`；`cleanup` 的前置条件是 `process_gone || never_started`（§11.1、§11.4）。
5. `network_denied`/`filesystem_denied` 仅作为证据分类码，不由 supervisor 抛出（§11.6）。
6. 凭据与任务 TTL 统一硬上限 **300s**（`exp - iat ≤ 300`、`RuntimeMaxSec ≤ 300`）；更长任务与凭据刷新由 **#83** 设计，本 MVP 不放开。
7. JCS 规范化器放在 `app/security/credentials.py`（stdlib-only），不复用 `app.governance.policies.canonical_json`，避免安全单元测试导入 `app.db`/治理模型；向量测试见 §16.2。若未来出现 stdlib-only 共享实现，改为复用。
8. 网关限额默认 per-task 8 连接 / 16 req/s、global 64 连接 / 200 req/s，是**部署可配置**的容量限制；调高必须重新评估 TH-24，不能关闭限额。per-task 限额按 peer UID 计数（一个单元一个 DynamicUser UID），仅用于准入，不参与授权。

## 21. 参考

- [威胁模型 v1.0.0](threat-model.md)
- [ADR-001 产品边界与执行模式](../architecture/ADR-001-product-boundary.md)
- [Runtime Adapter 契约 v1.0.0](../contracts/runtime-adapter.md)
- [execution-grant Schema](../contracts/execution-grant.schema.json)
- [任务状态机](../architecture/task-state-machine.md)
- [生产部署要求](../DEPLOYMENT-REQUIREMENTS.md)
- `app/governance/pep.py`（现有 B2 PEP，本轮不修改）
- `app/tasks/worker.py`（租约与停止确认语义，本轮不修改）
- `app/runtime/hermes.py`（Adapter 现状：拒绝 `managed_execution`，本轮不修改）
- systemd 文档：`systemd.exec(5)`、`systemd.resource-control(5)`、`systemd-run(1)`、`systemctl(1)`
- Issue #82、#77、#78、#81；Epic #74
