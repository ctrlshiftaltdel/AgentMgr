# 受控执行边界记录（合成隔离基座，#82）

- 状态：**合成隔离基座已在 `4175a3d` 验证；Hermes Managed Execution 与真实业务运行时仍未验证**
- 日期：2026-10-24
- 关联：Issue #82、PR #94（Epic #74）；[隔离设计](managed-execution-isolation-design.md)、[威胁模型](threat-model.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)、[ADR-001 产品边界](../architecture/ADR-001-product-boundary.md)
- 证据提交：`4175a3da45a00ff2be79c5f467cedc714c9df6d4`
- 证据工件：`<evidence-file>`（仓库外，不提交）；`run_id`、命令与退出码记录在 Issue #82 / PR #94

## 1. 结论与范围

### 1.1 本次验证了什么

在所有者批准的测试机 `<test-host>` 上，按[隔离设计 §16.3](managed-execution-isolation-design.md#163-真实隔离负向矩阵)的负向矩阵，对**合成 Agent 负载**真实运行了受控执行基座：

- 每个任务运行在独立 transient systemd 单元中，使用 `DynamicUser` 分配的数值 UID；
- 宿主文件系统、宿主 home/Profile、数据库目录、其它任务状态目录与凭据源均不可读；`/etc`、`/usr`、`/var/lib`、`/srv` 不可写；
- 单元内无 IP 网络，唯一外部入口是 Unix socket 上的 PEP 网关；
- 任务级短期凭据经网关校验：过期、撤销、身份不匹配、范围外资源、未授予能力全部被拒绝；
- Grant 撤销后网关拒绝并**停止单元**，取得可核对的停止证据；TTL 到期同样终止；
- 网关准入限额（每任务连接数与请求速率）生效；
- 清理后无残留单元、运行目录、状态目录或资源目录。

### 1.2 本次没有验证什么

本记录**不覆盖**：Hermes 入沙箱、模型供应商出口、浏览器/终端治理、真实业务连接器与数据库 backend、独立网关进程/生产 Secret Manager、多主机/多租户、客户端本地工具、生产部署模板的实际安装。上述能力仍为 `unverified`，详见 §3、§4。

### 1.3 证据命令与结果

在 `<repo-root>` 下、于 `<test-host>` 执行：

```bash
AGENTMGR_ISOLATION_TEST=1 AGENTMGR_ISOLATION_SUDO=1 \
AGENTMGR_ISOLATION_EVIDENCE=<evidence-file> \
uv run --no-sync pytest app/tests/security/test_isolation_live.py -q -o addopts='' --noconftest
```

| 项 | 值 |
|---|---|
| 提交 | `4175a3da45a00ff2be79c5f467cedc714c9df6d4` |
| `run_id` | `iso-20261023T191652Z-7130`（证据 JSON 内；仓库外） |
| 结果 | **32 passed in 16.57s**，退出码 `0` |
| Harness 汇总 | `result=pass`、`failures=[]`、`probe_count=56`、`failed_probes=[]` |
| 宿主/运行时 | Linux；kernel `6.12.111+deb13-amd64`；systemd `257 (257.13-1~deb13u1)`；cgroup v2 |
| 网络 profile | `private_netns`（`af_unix_only_fallback` 也已启动并通过网关连接探针，本次未使用） |
| 动态 UID | `62316`（数值；systemd 本次分配，不是持久账户，也不代表任何个人账号） |

运行环境未记录主机名、密钥、个人路径或真实凭据；证据 JSON 自检为 `token_absent=true`、`home_path_absent=true`、`ipv4_absent=true`。

> 真实测试只有同时设置 `AGENTMGR_ISOLATION_TEST=1` 与 `AGENTMGR_ISOLATION_SUDO=1` 才会执行；缺失任一变量时 pytest 会 skip。**跳过的运行不能作为验收证据。** 证据绑定上述 head，head 变化后必须重跑。

## 2. 已验证的强制属性

下表逐类对应设计 §16.3 的行号与实际探针 id；所有条目均来自本次运行，`failed_probes=[]`。

| 类别 | §16.3 行 | 探针 id（证据 JSON） | 本次结果 |
|---|---|---|---|
| 宿主文件系统/宿主数据 | #1–#3、#14 | `read-etc-shadow`、`read-home-profile`、`read-postgresql`、`write-etc`、`write-usr`、`write-var-lib`、`write-srv` | 通过（全部拒绝） |
| 跨任务状态与凭据源 | #4、#5 | `read-other-state-configured`、`read-other-state-private`、`read-other-credential-source` | 通过（拒绝；两个单元同时运行，跨任务状态目录与凭据源不可读） |
| capability / namespace / 进程可见性 | #10–#13 | `proc-cap-eff`、`proc-no-new-privs`、`unshare-user-mount-ns`、`proc-enumeration-self-only` | 通过（`CapEff=0000000000000000`、`NoNewPrivs=1`、`unshare=EPERM`、`proc_only_self=true`） |
| 无 IP 网络 | #6–#9 | `socket-af-inet`、`connect-loopback-5432`、`connect-documentation-443`、`connect-metadata-endpoint` | 通过（全部拒绝；`private_netns`） |
| 允许路径（唯一外部入口） | #15 | `gateway-allowed-read` | 通过（`ok`，返回资源与凭据范围一致） |
| 凭据过期/撤销/Grant 撤销 | #16、#17、#21 | `gateway-expired-credential`、`gateway-revoked-credential`、`gateway-grant-revoked` | 通过（`credential_expired`、`credential_revoked`、`grant_revoked`） |
| 身份/范围/能力 | #18–#20 | `gateway-cross-task-identity`、`gateway-cross-task-resource`、`gateway-out-of-scope-resource`、`gateway-ungranted-capability` | 通过（`cross_task_denied`、`resource_scope_denied`、`capability_not_granted`） |
| TTL 超时 | #22 | `ttl-timeout` | 通过（有效 TTL `2s`；≤8s 内终止；`observed=timeout`；`process_gone=true`、`stop_confirmed=true`、`MainPID=0`） |
| 撤销即停止 | #23 | `stop-on-revoke` | 通过（≤5s；保留 `active/exited` 与 `ExecMainExitTimestamp`；`MainPID=0`） |
| 网关准入限额 | #25、#26 | `gateway-rate-limit`、`gateway-busy` | 通过（第 17 次请求 `rate_limited`；第 9 个空闲连接 `busy`，随后单次调用仍被允许） |
| 清理 | #24 | `cleanup` | 通过（无单元、无状态/运行/资源目录） |

### 2.1 关键证据字段

- **TTL 上限有效性**：矩阵请求 `300s`，实际生效 `239s`（被 Grant 剩余有效期封顶，验证的是 `min()` 语义而非固定 300）；超时任务请求 `2s`、生效 `2s`。
- **停止证据**：撤销后停止在 5s 内取得 `MainPID=0` 并保留退出时间戳；`process_gone` 与 `stop_confirmed` 分别记录，不以“已发送停止请求”代替停止事实。
- **运行目录**：`/run/agentmgr-exec` 属主与 controller 一致、模式 `0700`；`gateway.sock` 属主一致、模式 `0666`（MVP）；单元激活后凭据源与 spec 源文件均不存在；单元无法遍历 `/run/agentmgr-exec`。
- **网关形态**：`mode=in_process_harness`、`grant_provider=in_memory_live`；限额为每任务 8 连接 / 16 req/s、全局 64 连接 / 200 req/s、单请求 64 KiB、读写超时 5s。每任务限额由 live 矩阵 #25/#26 验证；全局限额由 `test_gateway.py` 的 fake clock/计数器覆盖，本次**没有**制造 64 个并发任务，不能把全局上限当作 live 证据。
- **审计**：网关审计共 59 条事件，`token_absent=true`；出现的原因码包括 `bad_request`、`busy`、`capability_not_granted`、`credential_expired`、`credential_revoked`、`cross_task_denied`、`grant_revoked`、`manual_stop`、`rate_limited`、`resource_scope_denied`、`timeout`。
- **清理**：`units_remaining=0`、`remaining_unit_names=[]`、`remaining_state_dirs=[]`、`run_dir_present=false`、`resources_dir_present=false`、`state_clean=true`。

## 3. 未强制或无法消除的剩余风险

| 路径 | 说明 | 处置 |
|---|---|---|
| 宿主 root / controller sudo | 可读凭据、改 cgroup、杀进程、改 systemd；本基座不防御 | 组织控制、最小 sudo、审计；不对外声称防御 root |
| systemd/内核漏洞 | 沙箱属性由内核/systemd 实现，漏洞可致逃逸 | 补丁管理、版本基线、持续复测 |
| 侧信道 | 时间、缓存、CPU 争用、cgroup 统计 | 降低共享与并发；**不**视为已解决 |
| 物理访问 | 控制台、磁盘、内存 | 物理安全与磁盘加密（部署要求） |
| 网关 backend 缺陷 | 范围校验、路径处理、文件读取错误 | 代码评审 + 负向测试；backend 内重复校验 |
| 未经网关的资源路径 | 若部署把业务资源挂进单元或使网络可达，本基座不覆盖 | 部署基线检查；本设计不挂载业务资源 |
| 同 UID 内的子进程 | 任务派生的子进程共享同一凭据 | 凭据按任务而非按进程；`KillMode=control-group` 兜底 |
| UID 复用窗口 | 停止后状态目录未清理时，UID 可能被复用 | `process_gone`（或 `never_started`）后立即清理；残留告警 |
| 同宿主其它用户连接 `gateway.sock` | MVP socket 模式为 `0666`，知道路径即可 `connect()`（目录 `0700` 通常阻断遍历） | 模式不是授权；无有效 token 仍被拒绝；生产改 `0660` + 专用组 |
| `TemporaryFileSystem` 遮蔽 `/run/systemd`、`/run/dbus` | 该属性阻断宿主 systemd/D-Bus 访问；只在本次测试机验证，其它宿主/版本未验证。同时意味着依赖宿主 systemd/D-Bus 的负载无法在此单元内运行 | 部署基线检查；跨宿主重跑；不属于通用兼容性保证 |
| 单一宿主/单一版本 | 本次只在上述内核与 systemd 版本运行，未覆盖其它发行版、cgroup 混合模式或容器内 systemd | 更换宿主即重跑 §16.3；不据此外推 |
| Hermes 与真实业务负载 | Hermes 仍拒绝 `managed_execution`；模型出口、浏览器/终端、真实数据库/对象存储、业务连接器均无本基座证据 | #83 单独实现并验证 |
| 生产形态 | 本次是**同进程** harness + 内存实时 Grant provider；独立网关进程、文件撤销存储、真实 Secret Manager 未验证 | #83 生产路径；不得把 MVP 形态当作生产已就绪 |

## 4. 产品声明限制

**可以声称**：在 `4175a3d`，于已批准测试机的指定内核/systemd 版本上，合成 Agent 负载在受控隔离单元中运行，设计 §16.3 的负向矩阵全部通过；文件系统、进程、网络、凭据、停止与清理边界有可复现证据。

**不得声称**：

1. “Hermes Managed Execution 已验证/已受控”——Hermes 入沙箱、逐工具能力、任务级凭据与 OS 隔离属于 #83，尚未验证；
2. “所有企业资源已不可绕过/所有凭据已托管”——真实业务连接器、数据库、模型出口、浏览器/终端与客户端本地工具均不在本次证据范围；
3. “生产隔离已就绪”——本次形态是同进程 harness，不是生产网关与 Secret Manager；
4. “多主机/多租户已验证”——只在单一宿主运行；
5. 把本记录当作对宿主 root、内核漏洞、侧信道或物理访问的防护证明。

**#83 必须独立验证**：Hermes 在沙箱内运行及其能力/范围/任务级凭据、模型供应商出口、真实业务连接器与独立端到端执行链路；升级 `runtime.isolation` / `runtime.task_scoped_credentials` 需要单独的真实契约测试与证据。

## 5. 复现、清理与回滚

### 5.1 复现

1. 只使用所有者批准的 `<test-host>`；不得在开发机或 CI 运行真实隔离测试。
2. 从 `<repo-root>` 检出 `4175a3da45a00ff2be79c5f467cedc714c9df6d4`。
3. 使用 §1.3 命令，证据写入仓库外 `<evidence-file>`；不得把证据提交到仓库，也不得在证据中包含主机名、个人路径、IP、令牌或密钥。
4. 任一断言失败即非零退出；失败输出与证据 JSON 一并保留，不得只记录“通过”。

### 5.2 测试机清理

harness 的 teardown 在成功与失败路径都会执行：停止全部 `agentmgr-task-<hex>` 单元、`systemctl reset-failed`、关闭并 unlink 自己的网关 socket、删除 `/run/agentmgr-exec` 与 `/var/lib/private/agentmgr-exec`。人工复核：

```bash
systemctl list-units --all 'agentmgr-task-*' --no-legend
test ! -e /run/agentmgr-exec
test ! -e /var/lib/private/agentmgr-exec
```

本次运行后上述检查均无残留。对已被 systemd 回收的单元，`reset-failed` 必须视为 no-op；若仍有单元、运行目录或状态目录残留，按失败处理并报告。

### 5.3 生产模板与回滚

- `ops/security/agentmgr-exec`、`.sudoers`、`agentmgr-pep-gateway.service`、`agentmgr-task@.service` 是**生产模板**，本次**未安装**到测试机；#82 的 live 路径使用 `SudoSystemdOps`。模板不构成已验证的生产部署。
- 生产网关必须是独立进程、接真实 Grant 存储的实时适配器；socket 建议 `0660` + 专用组；Secret Manager 注入替代测试用的受限环境变量。
- 回滚不需要数据库迁移；`DynamicUser` 由 systemd 回收，无持久用户需删除；停止并 disable 网关、移除 sudoers 行、删除运行/状态/资源目录即可，详见[设计 §18](managed-execution-isolation-design.md#18-上线与回滚)。
- 证据失效条件：head 变化、systemd/内核升级、沙箱属性或网关限额变更、宿主更换。任一变化后必须重跑 §16.3，旧结论不得沿用。

## 6. 历史失败与本次修复

本次 live 证据之所以在 `4175a3d`，是因为之前几轮真实运行失败并已修复；这些失败不是环境噪声，而是基座缺陷：

1. **`226/NAMESPACE`（对整个目录使用 `InaccessiblePaths`）**：`InaccessiblePaths=-/run/systemd` 会让 systemd 的命名空间设置失败，单元以 `226/NAMESPACE` 启动失败。改为 `TemporaryFileSystem=/run/systemd` 与 `/run/dbus` 遮蔽宿主 socket 目录后成功（`20753c2`）。
2. **`StateDirectory` 的字面 `%N`**：transient 单元的 `systemd-run -p` 不展开 specifier，字面 `%N` 会创建共享目录并破坏每任务隔离。改为显式 `agentmgr-exec/<unit>`（`3c5abf5`）。
3. **transient 单元回收导致退出证据丢失**：普通 transient 单元退出后被回收，`ExecMainExitTimestamp`/`Result` 消失，无法证明停止。改为 `RemainAfterExit=yes` + `ExitType=cgroup` 并保留退出时间戳（`a7957b8`），随后修正停止顺序、微秒级时间戳与审计失败清理。最后一条 `4175a3d` 让跨任务资源探针接受单元级与 controller 级的同 id 条目。

结论：只有在这些修复之后，§16.3 的停止、TTL、跨任务与清理断言才真实成立；更早的失败运行不得作为证据引用。

## 7. 参考

- [受控执行隔离设计 §16](managed-execution-isolation-design.md#16-测试计划)
- [威胁模型](threat-model.md)
- Issue #82、PR #94；Epic #74；#83（Hermes 与真实业务验证）
