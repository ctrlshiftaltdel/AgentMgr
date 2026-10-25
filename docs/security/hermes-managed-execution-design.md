# Hermes-in-sandbox Managed Execution 设计（#97）

- 状态：**Draft — 待所有者审阅**；本文只定义 #97 设计，不代表已实现、已部署或已通过真实证据验证。
- 日期：2026-10-25
- 关联：Issue #97、#96、#82、#83、#81；[Runtime Adapter 契约](../contracts/runtime-adapter.md)、[任务状态机](../architecture/task-state-machine.md)、[#96 调度器](../architecture/runtime-dispatch.md)、[#82 隔离设计](managed-execution-isolation-design.md)、[#82 边界记录](managed-execution-boundary.md)。
- 依赖：`RuntimeAdapter` v1.0.0、#96 的 `RuntimeDispatcher`、#82 的 transient systemd 基座与 PEP 设计。
- 证据边界：本文是设计，不是运行时证据。真实 Hermes Managed Execution 在 #97 合并并完成真实测试前仍为 `unverified`；Fake/Mock 证据不得升级本设计中的能力标签。

## 1. 目标与非目标

### 1.1 目标

在 #96 的生产 Managed Execution 调度器之上，把 Hermes 作为真实运行时接入，并证明：

1. 每个任务运行在独立的 transient systemd 沙箱单元中，具有独立 UID、独立任务目录、只读宿主文件系统和隔离的网络命名空间；
2. 模型流量只能经过受控出口代理；真实模型凭据不进入沙箱，任务只持有短时、任务级、可撤销的出口令牌；
3. Hermes 只启用 `terminal` 工具集，浏览器、Web、MCP、插件、记忆和连接器全部关闭；终端命令只能在沙箱和任务目录内执行；
4. `submit` / `status` / `result` / `cancel` / 超时 / 未知状态 / 重复提交 / 租约恢复的真实映射满足 #96 调度器契约；
5. 结果通过宿主侧读取真实 artifact 并计算哈希得到 `result_ref` / `evidence:*`；调度器只到 `verifying`，由独立验证器决定是否成功；
6. 生产模板、部署前检查、证据记录、回滚和未验证能力清单齐全。

### 1.2 非目标

- 不实现业务工具/PEP 连接器入 Hermes（#83）。
- 不实现浏览器、Web、MCP、插件、记忆或通用连接器治理；这些工具在 #97 中关闭，不是“已治理”。
- 不把 Hermes 的 advisory `/v1/runs` 适配器改造成 Managed；advisory 路径保持不变。
- 不新增公开 MCP/REST 管理入口、数据库迁移或 Python 依赖。
- 不触碰生产/个人 Hermes Profile、认证、`.env`、会话或 gateway；不使用默认 profile 的模型凭据。
- 不声称多主机、多租户、并发容量或所有 Agent 工具链已治理。
- 不把 runtime 的 `stop_confirmed` 当作业务副作用对账；确认停止只作为证据，进入人工对账（owner policy）。

## 2. 已确认决策（所有者）

| 决策 | 选择 | 说明 |
|---|---|---|
| 隔离拓扑 | **每任务 ephemeral systemd 沙箱** | 独立 `DynamicUser`、独立任务目录、独立单元；不是长驻共享 host。 |
| Hermes 传输 | **一次性 CLI supervisor** | 每个任务一个 `hermes -z` 进程；systemd 是进程/停止事实来源；不使用 per-task gateway。 |
| 工具边界 | **仅 `terminal`** | `-t terminal`；其余工具集关闭。业务工具/PEP 留给 #83。 |
| 模型凭据 | **专用测试 key + 宿主出口代理** | key 只在宿主 broker；任务只拿短时任务级令牌；由所有者提供测试 key。 |
| Managed API | **不使用** | 临时 Hermes Profile/API server 只用于 advisory 兼容回归，不进入 Managed 链。 |
| 停止语义 | **证据，不是对账** | 确认停止 -> 保存证据 -> `needs_human`；不自动 `cancelled`，不伪造 `side_effects_reconciled`。 |
| 完成语义 | **只到 `verifying`** | 调度器从不调用 `complete_task`，不直接写 `succeeded`。 |
| 现有 profile | **不触碰** | 运行时根只读挂载；不挂载个人 profile/auth/.env/session/gateway。 |

## 3. 已核实的环境约束

以下事实来自 #97 设计阶段的只读/一次性探测，写入设计以避免重复踩坑；不是运行时能力证据。

1. 测试机上已有个人/默认 Hermes gateway 在运行。Hermes 0.22.1 对同一 OS 用户实施 **one gateway per host**；第二个 gateway 会被拒绝，`--replace` 会替换现有 gateway。**#97 绝不复用、替换或停止该 gateway。**
2. 一次性 CLI `hermes -z` 不经过 gateway，不参与 host lock；适合每任务进程模型。
3. `PrivateNetwork=yes` 的 transient unit 具有可用的隔离 loopback；外部 IP（如 `1.1.1.1:80`）返回 `ENETUNREACH`。这允许单元内运行 Hermes 与本地模型代理，同时保持无直接外网。
4. `ProtectHome=yes` 下，使用 `BindReadOnlyPaths` 把宿主路径挂到单元内仍然可读；因此可以把 Hermes 运行时只读挂入，而不复制 2.6 GB 安装。
5. 独立的 `HERMES_HOME` 首次启动会尝试安装依赖；每任务重新安装不可接受。设计使用 **一次 provision 的只读运行时根 + 每任务轻量 profile**。
6. 个人 profile 的 gateway、auth、`.env`、session 和 lock 不属于运行时根；不得挂载。

## 4. 威胁与验收映射

| 威胁/要求 | 本设计的控制 | 证据 |
|---|---|---|
| TH-17 凭据/日志泄漏 | 真实 key 只在宿主 broker；任务令牌短时、任务级；日志/错误/证据只含哈希和稳定码 | 单元/集成测试 + 秘密扫描 |
| TH-18 跨任务数据泄漏 | 每任务独立单元、独立 DynamicUser、独立任务目录；一次只跑一个任务；任务停止后清理 | A/B 任务交叉读取负向测试 |
| TH-19 隔离逃逸 | 只读宿主、无 host home、无 IP 外网、私有 `/run`/`/tmp`、capability/syscall 限制、任务目录唯一可写 | 真实负向矩阵 |
| TH-21 MCP ≠ 运行时能力 | 仅 `terminal`；不启用 MCP；能力标签由真实矩阵决定 | 工具边界测试 |
| 模型出口 | 单元内 localhost 代理 -> 宿主 Unix socket broker；broker 校验令牌和目标 | 令牌/目标/撤销测试 |
| 停止/取消 | systemd cgroup 停止；只有 unit/cgroup gone 才 `stop_confirmed` | 取消/超时/崩溃测试 |
| 完成验证 | 宿主读取 `out/result.txt` 并哈希；独立验证器提交证据 | dispatcher E2E + 验证器测试 |
| 误接个人 gateway | 不启动 gateway；CLI one-shot；不挂载个人 profile；attestation 绑定 helper/unit | 负向配置测试 + 人工检查 |

## 5. 架构

```text
#96 RuntimeDispatcher
  -> HermesManagedAdapter (host)
     -> helper client -> agentmgr-hermes-exec (root, stdin JSON)
        -> transient agentmgr-hermes-<id>.service
           -> hermes_workload.py
              -> local model proxy (127.0.0.1, unit netns)
              -> hermes -z ... -t terminal
     -> task dir (host-visible, group-shared)
        prompt.txt / profile / egress.token / status / stdout / out/result.txt

host egress broker (agentmgr-hermes-egress.service)
  <- Unix socket <- workload local proxy
  -> configured model provider (real key injected here)
```

### 5.1 组件

| 组件 | 位置（计划） | 职责 |
|---|---|---|
| `HermesManagedAdapter` | `app/runtime/hermes_managed.py` | 实现 `RuntimeAdapter`；Grant/attestation 校验、submit/status/result/cancel/lookup/health；不写任务状态。 |
| `HermesSandboxSupervisor` | `app/runtime/hermes_managed.py` 或同目录 | 调用 helper、读写任务目录、校验状态/结果/attestation、映射契约。 |
| root helper + wrapper | `ops/security/agentmgr_hermes_exec.py`、`ops/security/agentmgr-hermes-exec` | 唯一特权入口；stdin JSON、no argv、固定路径/属性、launch/status/stop/cleanup。 |
| workload | `ops/security/agentmgr_hermes_workload.py` | 单元内启动本地模型代理、运行 Hermes one-shot、写心跳/状态/结果、处理超时/信号。 |
| egress broker | `ops/security/agentmgr-hermes-egress.service` + Python 模块 | 宿主唯一模型出口；任务令牌校验、真实 key 注入、目标白名单、流式转发。 |
| CLI launcher | `app/runtime/hermes_managed_cli.py` | 受信配置加载 adapter + #96 `RuntimeDispatcher`；`--once`/`--loop`；无默认 adapter。 |
| 设计/证据文档 | `docs/security/**`、`docs/operations/**` | 边界、部署、证据、回滚、残余风险。 |

### 5.2 不新增的组件

- 不新增 per-task API server。
- 不修改 `app/api/**`。
- 不新增数据库迁移。
- 不新增 Python 依赖；helper/workload/broker 使用标准库。
- 不修改 #96 的调度器语义；CLI launcher 复用 `RuntimeDispatcher`。

## 6. 单元、文件系统与权限

### 6.1 单元命名

```text
attempt_key = <dispatcher-supplied idempotency_key>   # 已含 task/grant/attempt，不接受客户端输入
unit = "agentmgr-hermes-" + sha256(("agentmgr-hermes:v1:" + task_id + "\n" + attempt_key).encode()).hexdigest()[:16]
```

- 只含 `[a-z0-9-]`，固定前缀，长度固定；不包含用户输入、路径或模型文本。
- `runtime_task_id` 使用独立的不透明格式 `rtask_hermes_<...>`；只作为句柄，不能当路径。
- helper 在每次操作前重新校验 unit/task 绑定。

### 6.2 单元属性

| 属性 | 值/要求 |
|---|---|
| 类型 | transient `systemd-run` 单元；`Type=simple`/默认 |
| 用户 | `DynamicUser=yes`；每任务独立 UID；`SupplementaryGroups=agentmgr-hermes` |
| 工作目录 | `/task`（任务目录 bind mount 后的固定路径） |
| 文件系统 | `ProtectSystem=strict`、`ProtectHome=yes`、`PrivateTmp=yes`、`TemporaryFileSystem=/run`（或等价私有 `/run`） |
| 运行时根 | 只读 bind mount；不包含 Profile/auth/.env/session；路径由 helper 固定 |
| 任务目录 | bind mount 到 `/task`，唯一可写区；`agentmgr-hermes` 组可读写；`UMask=0007` |
| 出口 socket | broker socket 只读 bind mount 到 `/task/egress.sock`；不依赖宿主 `/run` |
| 网络 | `PrivateNetwork=yes`；仅隔离 loopback；无外部 IP；`RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` |
| 特权 | `NoNewPrivileges=yes`、`CapabilityBoundingSet=`、`AmbientCapabilities=` |
| syscall/namespace | `SystemCallFilter=@system-service`、`SystemCallArchitectures=native`、`RestrictNamespaces=yes`、`LockPersonality=yes`、`MemoryDenyWriteExecute=yes`、`RestrictRealtime=yes`、`RestrictSUIDSGID=yes`、`RemoveIPC=yes` |
| 内核面 | `ProtectClock/KernelTunables/KernelModules/KernelLogs/ControlGroups/Hostname=yes`、`ProtectProc=invisible`、`ProcSubset=pid` |
| 资源 | `MemoryMax`、`CPUQuota`、`TasksMax`、`RuntimeMaxSec`、`LimitNOFILE` 由 helper 白名单校验 |
| 停止 | `KillMode=control-group`、`SendSIGKILL=yes`、`ExitType=cgroup`、`TimeoutStopSec` |
| 重启 | 不自动重启；失败/超时由 adapter 映射 |

`/etc/agentmgr`、`/var/lib/agentmgr`、`/var/lib/postgresql`、`/srv`、宿主 home、其他任务目录、Docker/容器 socket、宿主 gateway lock/socket 必须不可访问。运行时根是唯一允许的只读宿主代码挂载。

### 6.3 任务目录布局

```text
<task-root>/<unit>/
  manifest.json        # 服务器写入的绑定信息/哈希；无秘密
  prompt.txt           # 已治理 prompt；有界
  profile/config.yaml  # 最小生成 profile；无真实 key
  egress.token         # 短时任务令牌；仅任务/宿主 adapter 可读
  status/started.json
  status/heartbeat.json
  status/exit.json
  stdout.txt
  stderr.txt
  out/result.txt       # 唯一允许作为结果的 artifact
  home/ state/ locks/ tmp/
```

- 任务目录由 helper 在固定 root 下创建/校验；拒绝 symlink、路径穿越、错误 owner/group。
- 只允许 `out/result.txt` 作为结果 artifact；宿主以 `O_NOFOLLOW` 打开并限制大小。
- 真实模型 key 不在任务目录；`egress.token` 是唯一的模型出口凭据，短时且任务绑定。

## 7. 特权 helper 协议

### 7.1 入口与信任

- wrapper：`<helper-path>`，root 所有，sudoers 只允许受信 adapter 用户执行**固定路径、无参数**。
- helper：标准库 Python，独立于项目环境；不导入 `app.*`。
- 只从 stdin 接收一个 JSON 请求；**argv 不使用**；所有路径、用户、单元属性由 helper 固定派生/校验，调用方不能提供 `ExecStart`、`Environment`、`BindPaths`、`User`、`Group`、`PrivateNetwork` 等 systemd 属性。
- helper 不记录 stdin、prompt、token 或真实 key；错误只返回稳定 reason code。

### 7.2 操作

| operation | 行为 |
|---|---|
| `prepare` | 校验 task/grant/unit；在固定 root 创建任务目录，设置 owner/group/mode；拒绝已存在的不安全目录。 |
| `launch` | 校验 manifest/prompt/profile/token 的文件哈希、大小、owner、无 symlink；以固定属性启动 transient unit；等待 `ActiveState=active` 且有 started 标记；返回 unit、UID、cgroup、attestation。 |
| `status` | 返回 `systemctl show` 白名单字段（ActiveState/SubState/MainPID/Result/ExecMainStatus/时间戳）和任务目录状态；不返回宿主路径。 |
| `stop` | 发送 SIGTERM，等待 `TimeoutStopSec`，必要时 SIGKILL；只有 unit/cgroup 不再持有进程才返回 `process_gone=true` / `stop_confirmed=true`；返回实际退出证据。 |
| `cleanup` | 仅在 unit 已停止/从未启动且 adapter 已声明结果已读取后，删除任务目录；只删固定 root 下的派生路径。 |
| `attest` | 重新读取 `systemctl show` 与固定路径哈希，签发/返回 HMAC attestation。 |

### 7.3 Attestation

attestation 分两层，均由 helper 用 HMAC-SHA256 签名（密钥只在 helper/adapter 可读的 root-only 文件）：

- **runtime attestation**（schema `agentmgr.hermes.runtime.v1`）：不绑定单个任务；包含 helper 版本、runtime name/version、runtime root manifest 哈希、sandbox profile 常量、broker socket 健康、测试 evidence_ref、签发/过期时间。`capabilities()` 只有在有效 runtime attestation + evidence_ref 存在时才把 Managed 能力标为 `verified`。
- **task attestation**（schema `agentmgr.hermes.sandbox.v1`）：在 launch 时绑定 task/grant/unit/UID/profile/prompt 哈希与 sandbox 事实；`submit_task` 必须校验它。

task attestation 示例：

```json
{
  "schema": "agentmgr.hermes.sandbox.v1",
  "unit": "agentmgr-hermes-...",
  "task_id": "task_...",
  "grant_id": "grnt_...",
  "runtime_handle": "rtask_hermes_...",
  "uid": 12345,
  "runtime_root_hash": "sha256:...",
  "profile_hash": "sha256:...",
  "prompt_hash": "sha256:...",
  "sandbox": {
    "private_network": true,
    "protect_home": true,
    "protect_system": "strict",
    "no_new_privileges": true,
    "capability_bounding_set": "empty",
    "toolsets": ["terminal"],
    "external_ip": false
  },
  "issued_at": "...",
  "expires_at": "...",
  "nonce": "...",
  "hmac": "..."
}
```

- adapter 校验 HMAC、schema、task/grant/unit 绑定、有效期、runtime root/profile/prompt 哈希，并再次用 `systemctl show` 校验关键属性。
- 没有有效 attestation：`capabilities()` 的 Managed 能力为 `unverified`；`submit_task` 不启动、返回 `unsupported`/`unknown`，绝不把宿主 Hermes 当 Managed。

## 8. 单元内 workload

### 8.1 启动顺序

1. 读取 `/task/manifest.json`、`prompt.txt`、`profile/`、`egress.token`；校验文件大小/哈希/无 symlink。
2. 设置最小环境：`HOME=/task/home`、`HERMES_HOME=/task/profile`、`XDG_STATE_HOME=/task/state`、`HERMES_GATEWAY_LOCK_DIR=/task/locks`、`TMPDIR=/task/tmp`、`PATH` 固定最小集合；清除所有宿主凭据环境变量。
3. 启动 localhost 模型代理（127.0.0.1，随机端口），只转发到 `/task/egress.sock`（宿主 broker socket 的只读 bind mount）；等待监听成功。
4. 写 `status/started.json`（unit、task、handle、时间、child pid/无 secret）。
5. 启动 Hermes one-shot CLI；周期写 `heartbeat.json`；捕获有界 stdout/stderr。
6. 子进程退出后写 `status/exit.json`（exit code、signal、duration、timed_out/cancelled 标志）。
7. 若 `out/result.txt` 存在，保留给宿主读取；不自行生成/覆盖 artifact。
8. 退出；systemd 以 cgroup 为空作为进程停止事实。

### 8.2 Hermes 调用

基准命令（实现时用 argv 数组，不经过 shell；使用运行时根内的 launcher，不调用宿主个人 wrapper）：

```text
<runtime-root>/hermes-agent/.hermes/bin/hermes chat \
  --query-file /task/prompt.txt --oneshot -Q --format text \
  -t terminal --provider custom --base-url http://127.0.0.1:<local-proxy-port>/v1 \
  --model <model-name> --safe-mode --run-budget <seconds>
```

- 必须使用生成的**最小 profile**（只含模型 provider/base_url/model 和明确关闭的平台/工具），不读取宿主 profile；`--safe-mode` 关闭用户配置、AGENTS.md/记忆、插件和 MCP，显式 `--provider custom --base-url` 保证本地代理仍可解析。
- **prompt 只能通过 `--query-file /task/prompt.txt` 读取，绝不出现在 argv/宿主 `ps`。** 任务出口 token 通过子进程环境变量（如 `CUSTOM_API_KEY`/`OPENAI_API_KEY`）传入，**禁止** `--api-key` argv；token 是任务级、短时、可撤销的。
- `-t terminal` 是唯一启用的工具集；不启用 `web`、`browser`、`file`、`code_execution`、MCP、插件、memory、connectors。
- 不使用 `--yolo`；one-shot 自身会绕过交互审批，但沙箱是边界。
- 子进程不继承宿主环境；真实 key、adapter token、DB DSN、宿主路径不进入环境（任务 token 除外，且只以环境变量形式存在）。
- `--safe-mode` 与显式 endpoint 的组合必须在实现中先做一次真实兼容性测试；若该 Hermes 版本不接受，退回生成 profile + `--ignore-user-config --ignore-rules`，但不得退回 argv prompt 或 argv token。

### 8.3 模型 provider 配置

生成的 profile 只含：

```yaml
model:
  provider: custom
  base_url: "http://127.0.0.1:<local-proxy-port>/v1"
  name: "<model-name>"
```

- 本地代理的 API key 使用任务级 `egress.token`；Hermes 只看到该 token，不看到真实 key。
- 使用 `--safe-mode` 时 profile 只提供空的隔离 home；模型 endpoint/model 以显式 `--provider custom --base-url --model` 为准。若实现退回生成 profile 路线，profile 只含这三项，不含 key。
- `model-name` / provider 上游由受信部署配置提供；任务 payload 不能选择 provider/base URL 或密钥。
- workload 不把 profile 内容、token 或 prompt 写入 stdout/stderr。

### 8.4 超时、取消与停止

- workload 使用自身 deadline（来自 helper 的 `runtime_max_sec`，不超过配置上限）和 systemd `RuntimeMaxSec` 双保险。
- 超时：向 Hermes 进程组发 SIGTERM，等待，必要时 SIGKILL；写 `timed_out`；只有 cgroup 为空才允许 `stop_confirmed`。
- 取消：helper 对 unit 发 SIGTERM/SIGKILL；workload 转发给子进程；写 `cancelled`；helper 观察 cgroup。
- `stop_confirmed` 只来自 unit/cgroup 已无进程；不是 Hermes 文本、不是模型自述。
- 任何残留子进程由 `KillMode=control-group` 清理。

## 9. 模型出口代理与任务级凭据

### 9.1 组件

- **宿主 broker**：`agentmgr-hermes-egress.service`，独立服务用户；监听固定 Unix socket（`<broker-socket>`），group `agentmgr-hermes`，mode 0660。
- **单元内 local proxy**：workload 启动的 stdlib HTTP 服务，监听 127.0.0.1；只接受来自 Hermes 的请求，转发到 `/task/egress.sock`。
- **真实 key**：只存在于 broker 可读的 root-only 文件/credential；adapter、workload、Hermes、任务目录都不可读。

### 9.2 令牌

```text
egt.<base64url(payload)>.<base64url(hmac_sha256(payload, key))>
payload = {
  "v": 1,
  "task_id": "...",
  "grant_id": "...",
  "unit": "...",
  "model": "...",
  "provider_host": "...",
  "path_prefix": "/v1",
  "issued_at": "...",
  "expires_at": "...",
  "nonce": "..."
}
```

- TTL 硬上限 300 秒；adapter 在 launch 前生成并写入 `egress.token`（mode 0640，group `agentmgr-hermes`）。
- broker 校验 HMAC、有效期、task/grant/unit/model/目标；拒绝过期、撤销、跨任务、跨 unit、非白名单目标。
- 撤销：adapter 在 stop/cleanup/完成读取后写撤销标记/删除 token；broker 在每次请求检查撤销存储（或短 TTL + 任务目录不可再写）。
- token 只允许访问配置的 provider host/path；不能访问任意互联网、AgentMgr API、DB 或 localhost 其它端口。
- token 可以被同任务的 terminal 读取（任务级边界内）；不能跨任务。真实 key 永不进入单元。

### 9.3 转发语义

- 只允许配置的 OpenAI-compatible 方法/路径（至少 `POST /v1/chat/completions`；按 Hermes 实际调用补齐），限制请求/响应大小和超时。
- broker 移除客户端 `Authorization`，注入真实 key；拒绝 3xx 跟随；不把上游错误体原样回显到日志。
- 流式 SSE 原样转发；断开时结束上游连接；不缓存 prompt/response。
- 上游可用性/鉴权/限流错误映射为稳定适配器错误；broker 不写任务状态。

## 10. Managed Adapter

### 10.1 构造与能力

```python
class HermesManagedAdapter:
    def __init__(self, config: HermesManagedConfig, *, supervisor: SandboxSupervisor): ...
    def capabilities(self, *, probe: bool = False) -> RuntimeCapabilities: ...
    def submit_task(...): ...
    def find_by_idempotency_key(...): ...
    def get_status(...): ...
    def poll_events(...): ...
    def cancel_task(...): ...
    def get_result(...): ...
    def provide_input(...): ...
    def health(...): ...
```

- `capabilities()` 只有在受信配置存在有效 **runtime attestation + evidence_ref** 时才把 Managed 能力标为 `verified`；否则全部 `unverified`。不读取任务 payload 或客户端输入。
- `runtime_version` 来自 provisioned runtime root 的 operator-verified 版本；`version_source=operator_verified`；不使用 Hermes 自述作为唯一依据。
- `probe=True` 只检查 helper/broker/运行时根健康，不启动任务、不调用模型。

### 10.2 方法与映射

| 方法 | 行为 |
|---|---|
| `submit_task` | 校验 `RuntimeGrant`（managed、task/revision/expiry/capabilities/scope 形状）；校验 runtime + task attestation；按 dispatcher 的 idempotency key 生成 unit/handle；`prepare`；写 prompt/profile/token；`launch`；等待 started；返回 `accepted` + `rtask_*`。 |
| `find_by_idempotency_key` | 读取持久 manifest/unit/status：有 unit/状态则 `found`；只有意图但无 unit/started 标记 -> `unknown`（不证明无副作用）；明确 pre-launch 拒绝才 `not_found`；同键不同 payload -> `conflict`。 |
| `get_status` | 校验 task 绑定；读取 helper `status` + 任务目录；映射 accepted/running/completed/failed/stopped/timed_out/unknown/not_found；`stop_confirmed` 只来自 unit/cgroup gone。 |
| `get_result` | 只读 `out/result.txt`（O_NOFOLLOW、大小上限）；计算哈希；返回 `result_ref=result_<...>`、`evidence:hermes_artifact_<...>`、结构化 payload、`structured_result=true`；缺失/不可读 -> unknown/needs_human，不合成。 |
| `cancel_task` | helper stop；`requested` 而 unit 仍活着则 `stop_confirmed=false`；unit/cgroup gone -> `stopped`/`already_terminal`/`stop_confirmed=true`；不把 Hermes 文本当停止。 |
| `poll_events` | 无原生事件；返回空 batch + `unsupported`/limitation，能力表标 `unverified`；不伪造事件。 |
| `provide_input` | `unsupported`。 |
| `health` | helper 可用、broker socket 可用、runtime root 可读、attestation 有效 -> healthy；否则 degraded/unavailable。 |

### 10.3 错误与未知

- 启动前失败：`rejected`/`unavailable`，无 runtime 副作用。
- 启动后但未确认 started：`unknown`，helper stop/cleanup，记录 reconciliation；不重发。
- unit 状态/结果不可读：`unknown`，不猜测 completed/failed。
- 取消/完成竞态：以 unit 实际状态 + artifact 为准；已完成 artifact 不被取消吞掉。
- 任何路径都不产生第二次 unit；重试需要新决策 + 新 Grant + 新 attempt。

### 10.4 CLI launcher 与 service identity

- `python -m app.runtime.hermes_managed_cli --once|--loop` 从受信 root-only 配置加载：helper 路径、runtime root、task root、broker socket、attestation key、service identity、provider/model、资源上限、evidence ref。
- 没有受信配置/attestation -> 非零退出并给出稳定原因；不回退到 Fake/Advisory/Hermes host。
- 使用专用 `service`/`runtime` principal，从数据库按 `service_identity_id` 解析并验证 active；不接受 person、客户端或任务 payload 提供身份。
- 复用 #96 `RuntimeDispatcher`；本 Issue 不修改其状态机/Grant/lease 语义。
- 日志只含稳定码、task/grant 哈希、unit 哈希；不含 prompt、token、key、路径、上游响应体。

## 11. 结果与证据

### 11.1 Artifact 约定

- prompt 明确要求把任务结果写入相对路径 `out/result.txt`；该文件是唯一结果 artifact。
- workload 不创建/修改内容；宿主 adapter 在单元停止后读取该文件（`O_NOFOLLOW`、常规文件、大小上限），计算 SHA-256。
- `result_ref = "result_" + digest[:32]`；`evidence_ref = "evidence:hermes_artifact_" + digest[:32]`；`structured_payload` 包含相对路径、`sha256:<hex>`、字节数；不含绝对宿主路径。
- 缺失/空/超限/非普通文件 -> 无结果，adapter 返回 unknown/needs_human；调度器不得成功。

### 11.2 独立验证

- 调度器只到 `verifying`。
- 独立验证器（#97 测试夹具或 #83 的真实验证器）读取同一 artifact，计算哈希，用 `source="artifact_store"`/`verifier` 的 `EvidenceRecord` 调用 `complete_task`；不能使用 `runtime`/`agent`/`model` 自报来源。
- `goal` 的 `artifact_hash`/`verifier_result` 条件必须与 artifact 哈希匹配；不匹配则验证失败/人工。
- 模型 final text 只能作为摘要，不作为完成证据。

## 12. 取消、超时、幂等与恢复

### 12.1 取消

- 核心 `request_cancel` 先写 `cancel_requested`；adapter 才能 stop。
- helper 发 SIGTERM、等待、必要时 SIGKILL；只有 unit/cgroup gone 才 `stop_confirmed=true`。
- `stop_confirmed` 后 adapter 返回 `stopped`；核心按 owner policy 保存证据并转 `needs_human`，不自动 `cancelled`、不写 `side_effects_reconciled`。
- 如果 artifact 已完成且有效，取消竞态不得丢弃它；进入 `verifying` 优先。
- unsupported/unknown cancel 保持诚实状态；不报“已停止”。

### 12.2 超时

- workload deadline + systemd `RuntimeMaxSec`；到期停止 cgroup，写 `timed_out`；adapter 只有 cgroup gone 才 `stop_confirmed=true`。
- 超时未确认停止 -> unknown/needs_human；不重发。

### 12.3 幂等

- unit/handle 由 task/grant/attempt 派生；manifest 持久化 idempotency key、payload hash、unit、handle。
- 同键同 payload 返回同一 handle/unit；同键不同 payload -> conflict；不创建第二个 unit。
- adapter 重启后从 manifest/unit 状态恢复查找；不依赖进程内 map。

### 12.4 恢复

- 每个 unit 的生命周期独立于 adapter；adapter 崩溃不杀 unit。
- 重新启动的 adapter 可以按 manifest 附加到 unit/status；运行中的 unit 绝不重放。
- 租约过期/dispatcher 崩溃由 #96 recovery 处理；adapter 的 recovery checker 只报告 unit 事实；无法证明未受理时 `unknown`，不返回 `not_accepted`（除非未来契约显式提供无副作用保证）。
- 已完成但未读取结果的 unit：fail closed 为 unknown/人工，不自动恢复执行。

## 13. 能力矩阵

#97 只有在真实证据通过后才可把下列能力标为 `verified`；否则 `unverified`/`partially_verified`。Fake/Mock 不升级。

| 能力 | #97 目标标签 | 说明/限制 |
|---|---|---|
| `task.submit` | verified | accepted 只在 unit active + started 后；无重复 unit |
| `task.status` | verified | systemd + 任务目录；无原生事件 |
| `task.cancel` | verified | requested vs unit/cgroup gone |
| `task.result` | verified | 宿主读取 artifact 哈希，不是模型自报 |
| `task.lookup` | verified | 持久 manifest/unit；不确定时 unknown |
| `runtime.health` | verified | helper/broker/runtime root/attestation |
| `runtime.isolation` | verified（有明确限制） | 独立 UID/只读宿主/私有 tmp/无外网/loopback 隔离；残余见 §17 |
| `runtime.task_scoped_credentials` | verified（有明确限制） | 短时任务级出口令牌；真实 key 在宿主；terminal 可读同任务令牌 |
| `runtime.tool_permissions` | verified（仅 terminal） | 仅 terminal；无 browser/web/MCP/memory/plugins |
| `runtime.managed_execution` | verified | 上述真实矩阵通过后 |
| `task.events.poll` | unverified | 无原生事件流；仅心跳/状态轮询；核心必须记录限制 |
| `task.input.provide` | unverified | 不支持 |
| `task.approval.resolve` | unverified | 不支持；one-shot 自动审批 |
| `task.unknown.resolve` | unverified | 不支持 |

`capabilities()` 的默认（无 attestation/evidence）必须是 `unverified`，确保 #96 在未配置时拒绝调度，而不是误接宿主 Hermes。

## 14. 测试计划

### 14.1 单元/Mock（CI，无特权、无 DB）

- helper：spec 字段白名单、unit/task 绑定、路径穿越/symlink/错误 owner、limit 边界、no argv、HMAC attestation 验证/篡改/过期。
- workload：prompt/profile/token 读取、环境清理、local proxy、heartbeat/status/result、超时/信号、cgroup 子进程清理（用 fake helper/process）。
- broker：token HMAC/claims/TTL/revoke/cross-task、目标白名单、请求/响应大小、流式、错误映射、无 key/日志泄漏。
- adapter：grant 形状、attestation 门禁、submit/lookup/status/result/cancel 映射、artifact 哈希/缺失/symlink、无 auto-success、错误码、幂等。
- CLI/launcher：无配置 fail closed、无默认 adapter、稳定退出码、不打印 secret。
- dispatcher 集成（fake supervisor + 真实 PostgreSQL，Master 跑）：accepted -> verifying；cancel -> needs_human；timeout/unknown；duplicate；Grant/lease 守卫。

### 14.2 真实测试（仅批准测试机，env-gated）

- helper 真实 launch/status/stop/cleanup；unit 属性 `systemctl show` 校验；attestation 校验。
- 沙箱负向矩阵：
  - 读宿主 home、`/etc/shadow`、DB socket、其他任务目录、宿主 systemd/dbus、Docker/容器 socket；
  - 直接访问外部 IP -> `ENETUNREACH`；
  - 访问 broker 以外的 loopback 服务 -> 拒绝/不可达；
  - 跨任务 unit/status/token/result 读取 -> 拒绝；
  - 浏览器/web/MCP 工具不可用；terminal 只能写任务目录；
  - symlink artifact -> 拒绝；
  - 环境变量/进程环境不含真实 key、DB DSN、宿主路径。
- 生命周期：accepted/running/completed/verifying；artifact 哈希；独立验证器成功；失败/超时/取消/unknown；重复提交；adapter 重启附加；dispatcher E2E（真实 PostgreSQL + 真实 adapter）。
- 模型出口：本地 stub 上游 + 真实专用 key 各一次；令牌过期/撤销/跨任务；真实 key 不在 unit/任务目录/日志；流式响应。
- 可选 advisory API server 回归：临时 profile/API server 唯一端口/home，运行现有 `test_hermes_integration.py`；完成后删除；不作为 Managed 证据。
- 每次真实运行记录：命令、退出码、Hermes 版本、runtime root 哈希、提交 SHA；证据放仓库外/Issue，不入库主机信息。

### 14.3 测试时输入

以下值由所有者在测试时通过 root-only 文件/环境提供，不提交、不打印、不进入沙箱：

- 专用模型 provider key（只对 broker 可读）与 provider base URL/model 名称；
- 只读运行时根路径与 manifest 哈希（或测试机的只读 bind 源）；
- 受信 service identity ID、helper/broker socket 路径、attestation key 路径；
- 可选的临时 advisory API server profile 名称/端口/API key；测试后删除。

任何缺失/无效输入都必须 fail closed；不得回退到个人 profile、宿主 Hermes 或 Fake Adapter。

### 14.4 验收命令

- 单元：`uv run --no-sync pytest app/tests/runtime/test_hermes_managed.py app/tests/security/test_hermes_managed_unit.py -q -o addopts=''`
- 真实：`AGENTMGR_HERMES_LIVE=1 AGENTMGR_HERMES_SUDO=1 ... pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''`
- 全量：`uv run --no-sync pytest app/tests -q -o addopts=''`
- `python3 scripts/check_privacy.py --ci`；`python3 scripts/validate_kit.py --project-ready`；`git diff --check`。

## 15. 运维、部署与回滚

### 15.1 Provisioning（一次性）

1. 创建 `agentmgr-hermes` 组/服务用户；把受信 AgentMgr 服务用户加入组。
2. 安装 root-owned helper/wrapper/workload/broker unit；sudoers 只允许固定 helper 路径、无参数。
3. 准备只读运行时根（根所有；Hermes 安装 + Python tools；不含 Profile/auth/.env/session）；计算并记录 runtime root manifest 哈希。
4. 创建任务根/状态根，group `agentmgr-hermes`，mode 0770；其他用户不可遍历。
5. 写入 root-only provider key/credential 和 attestation HMAC key；key 只对 broker/helper+adapter 可读。
6. 启动 broker；配置只允许 provider host/path；验证 broker 健康。
7. 运行 `ops/security/verify_hermes_isolation.sh`；校验 helper 版本、unit 属性、运行时根哈希、个人 profile 未挂载、broker key 不可被 unit 读取。
8. 创建/验证专用 service identity；记录 ID；不创建公开 Managed API。

### 15.2 运行

- `python -m app.runtime.hermes_managed_cli --once` / `--loop`，受信配置从 root-only 文件读取。
- 单 worker 单任务串行；多 worker 只处理不同任务；容量限制在运维文档明确。
- 健康/失败/退避由 adapter + #96 dispatcher 处理；不自动重试已受理任务。

### 15.3 回滚

- 停止所有 `agentmgr-hermes-*` unit；停止 broker；撤销所有任务令牌。
- 删除任务根/运行时根/helper/sudoers/组（按部署策略）；不触碰个人/默认 Hermes gateway/profile。
- 可选 API server 回归 profile/root 删除；恢复原状。
- 保留审计/证据（不含秘密）并按部署要求归档。

### 15.4 未配置时

- 无 attestation/helper/broker/service identity -> CLI 非零退出，Managed 能力 `unverified`；不回退 Fake/Advisory/宿主 Hermes。

## 16. 路径与授权

允许（经所有者设计批准）：

- `app/runtime/**`
- `app/tests/runtime/**`
- `app/tests/security/**`
- `docs/security/**`
- `docs/operations/**`
- `docs/integrations/runtimes/hermes.md`
- `ops/security/agentmgr-hermes-exec`
- `ops/security/agentmgr-hermes.sudoers`
- `ops/security/agentmgr_hermes_exec.py`
- `ops/security/agentmgr_hermes_workload.py`
- `ops/security/agentmgr_hermes_egress.py`
- `ops/security/agentmgr-hermes-egress.service`
- `ops/security/provision_hermes_runtime.sh`
- `ops/security/verify_hermes_isolation.sh`
- `ops/security/verify_hermes_isolation.py`
- `ops/security/README.md`

禁止：`migrations/**`、`app/api/**`、`app/tasks/**`（除非 #96 CLI 小改动另行批准）、生产数据库、生产/个人 Hermes Profile/认证/.env、真实企业凭据、公开 Managed API。新增依赖需单独批准。

## 17. 残余风险与未验证

- 共享内核/宿主 root、内核或 systemd 漏洞；本设计不防御宿主 root。
- world-readable 宿主文件、`/run/systemd`/`/run/dbus` 之外的宿主 AF_UNIX socket 仍可能可读/可连（与 #82 相同）。
- terminal 工具可在沙箱内执行任意命令；沙箱是边界，不是命令白名单。模型 prompt injection 可驱动 terminal；artifact 验证和沙箱限制后果。
- 无 PEP 业务工具；#83 才能证明真实业务工具治理。
- 任务令牌可被同任务 terminal 读取；它短时、任务绑定、可撤销，但不是进程级隔离。
- egress broker 是单点；其漏洞可能扩大上游访问；目标白名单/大小/超时/密钥隔离需测试。
- `task.events.poll` 无原生事件；核心需记录限制；长任务只靠 heartbeat/status。
- 每任务一个 unit/进程有启动开销；并发容量未测量；单 worker 串行。
- 真实 provider 专用 key 由所有者提供；key 轮换/配额/计费流程不在本设计。
- 真实测试只在批准测试机执行；证据必须记录命令/退出码/版本/SHA；未运行前不得声称 `verified`。

## 18. 验收映射

| Issue #97 验收 | 本设计证据 |
|---|---|
| Hermes 进程在受控 systemd 沙箱 | 真实 unit 属性 + 负向矩阵 + attestation |
| 任务级短时凭据/模型出口/工具边界 | broker 令牌/撤销/真实 key 隔离 + 仅 terminal 工具测试 |
| submit/status/result/cancel/stop 映射 | adapter 单元矩阵 + 真实生命周期 + dispatcher E2E |
| 失败/超时/取消/未知/恢复/重复不产生重复副作用 | 幂等 manifest + unit 唯一 + 取消/超时/崩溃测试 |
| 生产模板/部署检查/回滚/未验证记录 | ops 文档 + verify 脚本 + 本文件 §15/§17 |

## 19. 待所有者确认

1. 是否接受“managed 路径使用 one-shot CLI，不使用 per-task API server”；临时 API server 仅用于 advisory 回归？
2. 是否接受 runtime root 使用只读 bind mount（测试机）与生产 root-owned 副本两种部署方式，并以哈希/attestation 证明？
3. 是否接受 `out/result.txt` 作为唯一 artifact 约定，独立验证器完成 `succeeded`？
4. 是否确认专用测试 provider key 在测试时由所有者提供，只放 root-only 文件，不进入仓库/沙箱/日志？

审阅通过后再写实施计划（writing-plans）并派发；当前不开始代码。
