# Hermes Managed Execution 运维手册（#97）

- 状态：**实现完成；已在批准的测试机安装并完成沙箱/生命周期/stub-broker 与真实 provider key artifact E2E live 证据；生产未部署；容量未测量。** 本手册描述 #97 提供的生产启动器、安装脚本、验证脚本与回滚步骤；实际安装/运行必须由所有者在批准的测试机执行，并按[边界记录](../security/hermes-managed-execution-boundary.md)记录证据。
- 日期：2026-10-26
- 关联：Issue #97、#96、#83；[设计](../security/hermes-managed-execution-design.md)、[实施计划](../security/hermes-managed-execution-plan.md)、[边界记录](../security/hermes-managed-execution-boundary.md)、[Runtime Dispatch 运维](runtime-dispatch.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)。

## 1 摘要（先读这一节）

- **一次一个任务。** 一个启动器进程同一时刻只驱动一个 Managed 任务；多 worker 只能处理不同任务，靠 #96 的 lease/fencing 防重复。
- **Managed 不是公开 API。** 本 Issue 不新增 MCP/REST 管理入口，也不修改 `app/api/**`；任务只能由服务端可信入口/测试夹具创建。
- **没有默认配置。** 启动器必须显式传入受信、root-only 的 `--config`；没有配置、attestation、密钥、helper 或 service principal 时**非零退出**，不会回退到 Fake/Advisory/宿主 Hermes。
- **调度器只到 `verifying`。** 启动器复用 #96 `RuntimeDispatcher`，从不写 `succeeded`；`stop_confirmed` 是停止证据，不是业务副作用对账。
- **不触碰个人/默认 Hermes。** 安装、运行、回滚都不读取、挂载、启动、停止或替换个人 Profile、认证、`.env`、会话、lock 或 gateway。
- **#83 仍被阻塞。** 业务工具/PEP 连接器、真实业务资源访问与商业化就绪不在 #97 内。

## 2 组件与固定路径

```text
#96 RuntimeDispatcher
  -> HermesManagedAdapter（宿主）
     -> sudo -n /usr/local/libexec/agentmgr-hermes-exec（固定 wrapper，无参数）
        -> /usr/local/libexec/agentmgr_hermes_exec.py（root helper，stdin JSON）
           -> transient agentmgr-hermes-<16hex>.service
              -> DynamicUser + agentmgr-hermes 组
              -> /workload.py -> 本地 loopback 模型代理 + runtime python 的 in-process one-shot（仅 terminal，无 gateway）
              -> /runtime（只读运行时根）、/task（唯一可写任务目录）
     -> 宿主侧读取 /task/out/result.txt 并计算哈希

agentmgr-hermes-egress.service（宿主 broker）
  <- /var/lib/agentmgr-hermes-control/egress.sock（0660，组 agentmgr-hermes）
  -> 配置的模型 provider（真实 key 只在这里）
```

| 路径 | Owner / mode | 作用 |
|---|---|---|
| `/usr/local/libexec/agentmgr_hermes_exec.py` | `root:root 0644` | root helper（仅标准库） |
| `/usr/local/libexec/agentmgr-hermes-exec` | `root:root 0755` | sudoers 授权的固定 wrapper |
| `/usr/local/libexec/agentmgr_hermes_workload.py` | `root:root 0555` | 单元内 workload |
| `/etc/sudoers.d/agentmgr-hermes` | `root:root 0440` | 唯一固定、无参数规则 |
| `/etc/systemd/system/agentmgr-hermes-egress.service` | `root:root 0644` | broker 单元 |
| `/etc/agentmgr/hermes-exec.json` | `root:root 0640` | helper 安装配置（无密钥） |
| `/etc/agentmgr/hermes/managed-cli.json` | `root:<adapter-private-group> 0440` | 启动器受信配置（无密钥） |
| `/etc/agentmgr/hermes/egress-broker.json` | `root:agentmgr-hermes 0440` | broker 配置（仅路径，无内联密钥） |
| `/etc/agentmgr/hermes/deployment.json` | `root:root 0640` | 安装记录（供验证脚本读取） |
| `/etc/agentmgr/hermes/attestation-key` | `root:<adapter-private-group> 0440` | runtime/task attestation HMAC key |
| `/etc/agentmgr/hermes/egress-token-key` | `root:<adapter-private-group> 0440` | 任务出口令牌 HMAC key |
| `/etc/agentmgr/hermes/provider-key` | `root:root 0400` | 真实 provider key（仅 broker credential） |
| `/etc/agentmgr/hermes/runtime-attestation.json` | `root:<adapter-private-group> 0440` | provisioning 时的 runtime attestation 记录 |
| `/etc/agentmgr/hermes/runtime-root.hash` | `root:root 0444` | 受信 runtime root manifest 哈希 |
| `/var/lib/agentmgr-hermes-runtime` | `root:root 0555` | 只读 Hermes 运行时根（copy 或 bind） |
| `/var/lib/agentmgr-hermes-tasks` | `root:agentmgr-hermes 0770` | 任务根；子目录每任务独立 |
| `/var/lib/agentmgr-hermes-control` | `<broker>:agentmgr-hermes 0750` | broker socket 的宿主控制目录 |
| `/var/lib/agentmgr-hermes-revoked` | `<adapter>:agentmgr-hermes 0750` | 令牌撤销标记（broker 只读） |

`<adapter-private-group>` 是受信适配器服务的**私有组**（默认名 `agentmgr-hermes-adapter`），绝不能是 `agentmgr-hermes`；attestation/egress key 对 sandbox 组不可读。所有路径都在 `agentmgr-hermes` 单元可见范围之外（`/etc/agentmgr` 被 `InaccessiblePaths` 遮住）。

## 3 一次性安装（由所有者执行）

### 3.1 前置输入

以下值由所有者在测试时提供，**不提交、不打印、不进入沙箱**：

- 专用模型 provider key 文件（root-only，单行）；
- provider base URL（生产必须 `https://`）与 provider host；
- 模型名（只能来自受信部署配置，任务 payload 不能选择）；
- 只读 Hermes 运行时根的来源目录（不含个人 Profile/auth/.env/session；必须自包含 `hermes-agent` 源码、`tools` Python 和 `installs` 依赖环境）；
- 受信 service identity（`svc_<id>`，需先在数据库中创建/确认 active）；
- 运行时名称/版本、evidence ref、worker id、adapter 服务账号。

### 3.2 安装命令

```bash
sudo ops/security/provision_hermes_runtime.sh --dry-run \
  --adapter-user <adapter-user> \
  --install-root <install-root> \
  --venv <venv-bin-dir> \
  --service-identity-id svc_<id> \
  --source <runtime-source> \
  --runtime-version <version> \
  --model <model-name> \
  --provider-host <provider-host> \
  --provider-base-url https://<provider-host> \
  --provider-key-file <root-only-provider-key> \
  --evidence-ref evidence:hermes:managed:<id>

# 校验输出后去掉 --dry-run 正式安装
sudo ops/security/provision_hermes_runtime.sh ...
```

- `--venv` 是包含可执行 `python` 的 venv **bin 目录**（broker 单元的 `ExecStart=<venv>/python ...`）。
- `--install-root`/`--venv` 不能位于个人 home、`/root` 或 `.hermes` 下：broker 单元有 `ProtectHome=yes`，装在 home 下会在运行时不可访问。`--source` 与所有部署目录也不能位于仓库或个人 profile 内。
- `--adapter-user` 必须已存在；脚本**不会创建**受信适配器账号。脚本会创建 `agentmgr-hermes` sandbox 组、`agentmgr-hermes-adapter` 私有组和专用 broker 用户，并把 adapter 用户加入前两个组。
- `--runtime-mode copy`（默认）把 `--source` 复制为 root-owned 只读运行时根；`--runtime-mode bind` 立即做只读 bind mount（**不跨重启持久化**，需要部署层自行配置 fstab/systemd mount unit；验证脚本会要求 bind source 与安装记录一致）。
- 运行时根必须自包含 `<source>/hermes-agent`、`<source>/tools/python-*` 和 `<source>/installs/**/site-packages`。workload 用 runtime Python 的 `-I -c` runner 调用 `hermes_cli.oneshot.run_oneshot`，**不执行** `.hermes/bin/hermes`，也不启动 gateway；缺少 `installs` 或 Python tools 会 fail closed。运行时根不得依赖个人 `~/.hermes`。
- 脚本拒绝：非 root 运行、仓库/个人 home/`.hermes` 路径、source **任意深度**包含 `profiles`/`auth.json`/`.env`/`sessions`/`gateway.lock`/`locks`、source 中的绝对 symlink 或逃逸 root 的相对 symlink、非 https 的 provider URL、URL host/port 与 `--provider-host` 不一致、空/多行 provider key、已存在但 manifest 不匹配的 runtime root（除非 `--refresh-runtime`）、以及固定目录/安装目标是 symlink 或非目录/非常规文件。
- 固定部署目录（`/etc/agentmgr`、`/var/lib/agentmgr-hermes-*`、`/usr/local/libexec`）会被 lstat 检查；owner/group/mode 不符时会记录并修复，symlink 直接失败。
- 已存在的 attestation/egress key 会保留并记录“rotation is an explicit operator action”；已存在的 provider key 若与 `--provider-key-file` 不同则失败，必须显式删除后才能轮换。
- 脚本是幂等的：重复执行为同一组路径安装相同内容；runtime root 用确定性 manifest 哈希（斜杠文件、目录、symlink 目标）比较，不靠 mtime。
- **脚本不启动任何单元**，也不会触碰个人/默认 Hermes gateway。密钥内容从不打印。

### 3.3 安装后

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agentmgr-hermes-egress.service
sudo ops/security/verify_hermes_isolation.sh
```

验证脚本是**只读**的：检查安装文件 owner/mode/哈希、sudoers 唯一固定规则、runtime root manifest 哈希、组/密钥权限、任务根/控制目录/socket/撤销目录、runtime root 不含个人 Profile 标记且未被个人路径 bind mount、Task 2 argv 属性以及 helper 拒绝调用方 systemd 属性。任一不匹配都以稳定码失败（exit 1）；它需要 broker socket 已存在，因此先启动 broker。

> 安装 **不会** 创建数据库 service identity，也不会启用公开 Managed API。`managed-cli.json` 中的 `service_identity_id` 必须指向一个 active 的 `svc_` 身份；否则启动器以 `principal_unbound`/`principal_disabled` 失败关闭。

## 4 受信配置

默认位置：`/etc/agentmgr/hermes/managed-cli.json`（由安装脚本写入，root-owned，沙箱组不可读）。配置**不含任何秘密**：provider key 只在 broker 的 `provider-key` 文件中。

| 字段 | 说明 |
|---|---|
| `schema` | 固定 `agentmgr.hermes.managed-cli.v1` |
| `service_identity_id` | `svc_<正整数>`；启动器解析为 active service principal |
| `helper_path` | 只能是 `/usr/local/libexec/agentmgr-hermes-exec` |
| `runtime_root` / `runtime_root_hash` | 只读运行时根（固定 `/var/lib/agentmgr-hermes-runtime`）与受信 `sha256:<64 hex>` manifest 形式 |
| `task_root` / `control_dir` / `broker_socket` / `revocation_dir` | 全部固定部署路径（启动器拒绝其他绝对路径）：`/var/lib/agentmgr-hermes-tasks`、`/var/lib/agentmgr-hermes-control`、`.../egress.sock`、`/var/lib/agentmgr-hermes-revoked` |
| `attestation_key_path` / `egress_token_key_path` / `runtime_attestation_path` | 必须在 `/etc/agentmgr` 下，且三者互不相同 |
| `evidence_ref` | `evidence:` 前缀的稳定引用；与 attestation 匹配 |
| `runtime_name` / `runtime_version` | 由 runtime attestation 绑定 |
| `model_name` | 必须是安全模型名（不能以 `-` 开头或被解析为选项） |
| `egress_provider_host` / `egress_path_prefix` | 令牌与 broker 的目标白名单（固定 `/v1`） |
| `worker_id` / `lease_seconds` / `poll_interval` / `health_backoff_seconds` | #96 调度器身份与节流 |
| `runtime_max_sec` | **至少 9 秒**；上限 86400，同时受 systemd `RuntimeMaxSec` 兜底 |
| 资源/大小上限 | memory/cpu/tasks/stop timeout/prompt/result/token TTL，均在安全范围内 |

启动器加载时验证：配置文件必须 root-owned、非 symlink、非 group/world 可写、非 world-readable；未知字段、内联 secret 字段、错误 helper 路径、非固定 runtime/task/control/socket/revocation 路径、key 路径不在 `/etc/agentmgr`、模型/worker/limit 越界都 fail closed，错误只报稳定码，不打印路径或值。预检还要求两个 HMAC key 为 root-owned、group 不是 `agentmgr-hermes`、非 group/world 可写，wrapper 为 root-owned 且 group/world 不可写。

启动器只校验配置中的 `runtime_root_hash` **形式**及 attestation 绑定；它不重算目录 manifest。重算与比较由安装脚本和 `verify_hermes_isolation.sh` 完成。

## 5 启动、停止与运行模式

```bash
cd <install-root>

# 单次 pass：claim 一个 Managed 任务，完成 submit/驱动后退出
sudo -u <adapter-user> <venv-bin-dir>/python -m app.runtime.hermes_managed_cli \
  --once --config /etc/agentmgr/hermes/managed-cli.json

# 常驻：直到 SIGTERM/SIGINT
sudo -u <adapter-user> <venv-bin-dir>/python -m app.runtime.hermes_managed_cli \
  --loop --config /etc/agentmgr/hermes/managed-cli.json
```

- 必须使用 `<install-root>` 作为工作目录（或以等价方式安装包），否则 `-m` 无法导入 `app.*`。

- 启动器以 adapter 用户运行；它通过 `sudo -n` 调用固定 wrapper。不要以 root 运行启动器，也不要把 adapter 用户加入 sudo 之外的额外权限。
- `--once` 在 stdout 打印一个稳定 `DispatcherResult.outcome`：`idle`/`verifying`/`failed`/`timed_out`/`needs_human`/`unavailable`/`unsupported`/`adapter_unavailable`/`rejected`/`timeout`/`unknown`。没有 `succeeded`。
- `--loop` 每次迭代用**新 session**，顺序执行 lease recovery、cancel 对账、`run_once`、unknown-run 停止；空闲按 `poll_interval` 等待。
- **SIGTERM/SIGINT** 设置停止事件；调度器在两次观察之间退出，不取消运行中的 runtime。当前 lease 继续存活到过期，下一个进程用同一 idempotency key 附加同一 unit，不会重发。
- 单 worker 单任务串行；多 worker 必须使用不同 `worker_id` 并只处理不同任务。

## 6 健康、退避与恢复

- `adapter.health()` 检查 helper 可执行、runtime root 可读、broker socket 存在、runtime attestation 有效；结果 `healthy`/`degraded`/`unavailable`。
- 调度器在 `adapter_unavailable` 时按 `health_backoff_seconds` 退避；可恢复的 domain/lease 错误按 `error_backoff_seconds`（默认 5 秒起，指数翻倍，上限 60 秒）退避；编程错误不重试。
- 每轮 `run_forever` 执行 #96 的 lease recovery、cancel 对账与 unknown-run best-effort 停止；运行时单元的生命周期独立于启动器。
- 启动器重启后按持久 manifest/unit 状态附加，不重放已受理运行；无法证明未受理时返回 `unknown`，绝不返回 `not_accepted`。
- `capabilities()` 在没有有效 runtime attestation + 匹配 `evidence_ref` 时全部 `unverified`，调度器会拒绝调度而不是误接宿主 Hermes。

## 7 取消、超时与停止

- 核心先写 `cancel_requested`；adapter 才调用 helper stop。
- `requested` **不是**停止；只有 unit/cgroup 不再持有进程才 `stop_confirmed=true`。
- 确认停止是**证据，不是业务对账**：调度器保存哈希化的 stop evidence 并转 `needs_human`；不自动写 `cancelled`、不写 `side_effects_reconciled`。
- 如果 artifact 已完成且有效，取消竞态不得丢弃它：优先进入 `verifying`。
- 超时由 workload 自身 deadline + systemd `RuntimeMaxSec` 双保险；到期停止 cgroup，写 `timed_out`；未确认停止则 `unknown`/人工，不重发。
- 结果只来自宿主读取普通文件 `out/result.txt` 的 SHA-256；缺失、空、超限、FIFO、symlink 或非普通文件都 fail closed，模型文本不能作为完成证据。
- `task.events.poll`、`task.input.provide`、`task.approval.resolve`、`task.unknown.resolve` 仍是 `unverified`；没有原生事件流，长任务只能靠 heartbeat/status。

## 8 回滚

```bash
# 先停止新任务和新调度器；再执行
sudo ops/security/provision_hermes_runtime.sh --rollback
# 如需同时删除组/用户（默认保留）：
sudo ops/security/provision_hermes_runtime.sh --rollback --purge-groups
```

顺序：停止所有 `agentmgr-hermes-*` transient 单元 -> 停止并 disable broker -> 删除 broker unit/sudoers/helper/wrapper/workload -> 删除 `/etc/agentmgr/hermes/**` -> unmount（bind 模式）并删除 runtime root/task root/control/revocation 目录 -> 可选删除组与 broker 用户。

- 回滚**不**触碰个人/默认 Hermes gateway、Profile、认证、`.env`、会话或 lock。
- 回滚不删除数据库记录；service identity 与审计/证据按部署保留策略处理。
- `--dry-run` 只打印计划，不执行。
- 回滚后如需恢复，重新执行安装；runtime root 会被重新复制/挂载并重新计算 manifest 哈希。

## 9 容量与并发

- 单 worker 单任务串行；每次提交一个 transient unit 与一个 workload 进程（内部为 in-process `run_oneshot`，无 gateway）。
- 多 worker 只能并行处理**不同**任务；同一任务的重复提交由 idempotency key/unit 唯一性阻止。
- 每任务资源上限由配置的 `memory_max_mb`/`cpu_quota_percent`/`tasks_max`/`runtime_max_sec` 约束；broker 并发由 `max_connections`（默认 32）约束。
- **并发容量、启动开销和吞吐未测量**；不得把单 worker 串行当作生产容量证明。

## 10 故障排查（稳定码）

启动器 stderr 只出现稳定码/异常类型，不出现配置路径、prompt、token 或 key。

| 前缀 | 常见码 | 处理 |
|---|---|---|
| `managed execution configuration rejected` | `config_unavailable`、`config_invalid`、`config_mode_invalid`、`config_owner_invalid` | 检查 config 是否为 root-owned、非 symlink、0600/0640/0440 类只读权限 |
| 同上 | `config_missing_field`、`config_unknown_field`、`config_secret_field` | 按安装脚本重新生成；配置里不能有内联 key |
| 同上 | `config_helper_path_invalid`、`config_control_path_invalid`、`config_key_path_invalid` | 使用固定 wrapper、control socket 与 `/etc/agentmgr` 下的 key |
| 同上 | `config_runtime_hash_invalid`、`config_model_invalid`、`config_limit_invalid` | 检查 manifest 哈希/模型名/`runtime_max_sec >= 9` 等范围 |
| 同上 | `attestation_unavailable`、`attestation_invalid` | 检查 `runtime-attestation.json` 是否存在并绑定当前 runtime/evidence |
| 同上 | `attestation_key_unavailable`、`egress_token_key_unavailable` | 检查 key owner 为 root、group 不是 `agentmgr-hermes`、mode/大小安全 |
| 同上 | `runtime_root_unavailable`、`task_root_unavailable`、`control_dir_unavailable`、`revocation_dir_unavailable` | 检查安装目录与权限 |
| 同上 | `helper_unavailable` | 检查 `/usr/local/libexec/agentmgr-hermes-exec` root:root 0755 且 sudoers 已安装 |
| 同上 | `principal_unbound`、`principal_disabled`、`service_principal_invalid` | 检查 `service_identity_id` 是否存在、active、owner active |
| `dispatch pass failed` / `dispatch loop failed` | 异常类型（如 `RuntimeError`） | 只给出类型；结合 #96 日志的稳定码与审计记录排查 |
| `--once` 输出 | `adapter_unavailable`/`rejected`/`unavailable`/`unknown`/`needs_human` | 分别对应健康、Grant/能力拒绝、未知/人工；不得自动重试 |

安装/验证脚本失败时打印 `provision: error: ...` / `verify: FAIL <check>: <code>`；按码检查对应固定路径。不要把真实 key、token、prompt 或宿主路径粘贴到 Issue/PR。

## 11 测试与 live 门禁

本地（无 DB、无特权）：

```bash
UV_PROJECT_ENVIRONMENT=<test-host-venv> uv run --no-sync pytest \
  app/tests/runtime/test_hermes_managed_cli.py -q -o addopts='' --noconftest
python3 -m py_compile app/runtime/hermes_managed_cli.py ops/security/verify_hermes_isolation.py
bash -n ops/security/provision_hermes_runtime.sh ops/security/verify_hermes_isolation.sh
python3 scripts/check_privacy.py --ci
git diff --check
```

这些只证明 CLI/config/脚本静态契约，不证明安装或运行。真实门禁（仅批准测试机、env-gated、Master 运行）：

```bash
# 安装 + 静态验证（见 §3）
sudo ops/security/provision_hermes_runtime.sh ...
sudo systemctl daemon-reload && sudo systemctl enable --now agentmgr-hermes-egress.service
sudo ops/security/verify_hermes_isolation.sh

# 真实沙箱负向矩阵、模型出口、Managed 生命周期
AGENTMGR_HERMES_LIVE=1 AGENTMGR_HERMES_SUDO=1 ... \
  pytest app/tests/security/test_hermes_sandbox_live.py -q -o addopts=''
AGENTMGR_HERMES_LIVE=1 ... \
  pytest app/tests/tasks/test_hermes_managed_live.py -q -o addopts=''
```

证据记录：命令、退出码、Hermes 版本、runtime root 哈希、提交 SHA、pytest 计数；证据放在仓库外/Issue，不入库主机信息或秘密。**边界记录区分 `live`/`partial`/`unverified`；真实 provider key artifact 与 `runtime.managed_execution` 已在测试机 live，生产/capacity 仍未验证。**

## 12 未包含 / 边界

- 不新增公开 Managed API、数据库迁移或 Python 依赖。
- 不修改 #96 的状态机/Grant/lease 语义。
- 不治理业务工具、PEP、浏览器/Web/MCP/记忆/插件/连接器；这些在 #97 中关闭，不是“已治理”。
- 不声称多主机、多租户、生产容量、自动化 key 轮换/配额/计费流程；key 轮换是显式人工操作（删除后重跑安装）。
- #83 仍负责真实业务工具治理与商业化就绪；Hermes 内执行的工具调用不经过 AgentMgr PEP。
