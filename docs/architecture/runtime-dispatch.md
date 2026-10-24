# Runtime Dispatch（Managed Execution 生产调度器）

- 状态：**Draft — 待所有者审阅**；本文只定义 #96 的设计，不代表已实现或已批准实施。
- 关联：Issue #96、#83、#97、#81、#82；[Runtime Adapter 契约](../contracts/runtime-adapter.md)、[任务状态机](task-state-machine.md)、[受控执行边界记录](../security/managed-execution-boundary.md)。
- 依赖：`RuntimeAdapter` v1.0.0、task/ExecutionGrant 冻结契约、`app.tasks.worker` 的 lease/fencing。
- 日期：2026-10-24

## 1. 目标与边界

#96 把 **Managed Execution** 的 task/ExecutionGrant 契约与 RuntimeAdapter 契约接成一条生产调度链：

```text
client/trusted intake
  -> task queued (managed_execution)
  -> RuntimeDispatcher claim + lease/fencing
  -> project ExecutionGrant -> RuntimeGrant
  -> adapter.submit_task
  -> start_attempt / mark_running
  -> adapter.get_status / get_result
  -> record_attempt_result -> verifying
  -> core verifier decides succeeded/partial/failed
```

### 1.1 只做 Managed Execution

本 Issue 只处理 `execution_mode=managed_execution`。Advisory/BYOA（B1）和 Governed Resource Access（B2）仍由客户端/PEP 驱动，不经过本调度器，也不要求 RuntimeAdapter 能力。

**模式围栏是硬要求：**

- 调度器不得 claim/执行 advisory/governed 任务；claim 时按 `execution_mode=managed_execution` 过滤，或显式释放非 managed lease。
- 不得把 Hermes 当前的 advisory-only adapter 接成 managed；`RuntimeCapabilities` 不满足 `task.submit`/`task.status = verified` 时 fail closed。
- 不得用 Fake Adapter 或合成隔离基座冒充真实运行时能力；真实 Hermes 属于 #97。

### 1.2 非目标

- 不实现 Hermes-in-sandbox、模型供应商出口、浏览器/终端治理、任务级模型凭据（#97）。
- 不新增公开 MCP/REST 管理入口；Managed 任务由服务端可信入口/测试夹具创建。
- 不新增数据库迁移、依赖或第二套状态机/Grant 规则。
- 不宣称生产多租户、多主机或并发扩展；本 Issue 明确单 worker 单任务串行，多个 worker 只能处理不同任务。
- 不把 runtime “我完成了”当作成功；核心验证仍未通过就只能是 `verifying`/失败关闭。

## 2. 当前缺口

- `app/tasks/worker.py` 已有 claim/lease/fencing/recovery，但没有执行循环。
- `app/tasks/service.py` 已有 `start_attempt`/`mark_running`/`record_attempt_result`/`record_dispatch_failure`/`confirm_stop`，但没有生产 caller。
- `app/api/rest/operations.py` 的提交固定 `advisory`，Managed 任务没有公开入口；本 Issue 不改变这一点。
- `HermesRuntimeAdapter` 是 advisory-only 兼容适配器，明确拒绝 managed；不能作为 #96 的真实后端。

## 3. 组件

### 3.1 `app/runtime/projection.py`（新增）

```python
def project_runtime_grant(task: object, grant: object, *, now: datetime) -> RuntimeGrant: ...
```

调度器负责从 session 读取 `ExecutionGrant`，本函数只接受已加载的结构化对象；`app.runtime` 不导入 task models，避免反向依赖。只读服务端状态：

- `task.grant_id` 必须存在，`task.execution_mode == "managed_execution"`；
- `ExecutionGrant` 必须 `status=active`、`usable=true`、未过期、`grant.task_id == task.task_id`；
- `grant.task_revision`、capabilities、resource_scope 必须与任务/决策投影一致；
- 映射到 `RuntimeGrant(grant_id, task_id, task_revision, execution_mode, capabilities, resource_scope, expires_at)`；
- 失败抛出稳定的 `TaskError`/`RuntimeAuthError` 语义；不读取客户端 payload、不生成/修改 Grant、不把 subject/secret 放进 Grant。

### 3.2 `app/tasks/execution.py`（新增）

```python
@dataclass(frozen=True)
class DispatcherConfig:
    worker_id: str
    lease_seconds: int = 300
    poll_interval: float = 0.5
    health_backoff_seconds: float = 5.0

class RuntimeDispatcher:
    def __init__(self, adapter: RuntimeAdapter, config: DispatcherConfig, *,
                 service_principal: GovernancePrincipal, session: Session): ...
    def run_once(self, *, now: datetime | None = None) -> DispatcherResult: ...
    def run_forever(self, stop_event: threading.Event, *,
                    session_factory: Callable[[], Session]) -> None: ...
```

职责：

- `run_once`：claim 一个 managed queued task；校验 Grant/能力/健康；submit；维持 lease；把受理/运行/终态/取消事实写入核心；释放或交给 recovery。
- `run_forever`：轮询 `run_once`，健康不可用时指数退避；支持 SIGTERM/事件停止；不实现多任务并发。
- `RuntimeRecoveryChecker`：供 `worker.recover_expired_leases` 注入，使用 adapter `find_by_idempotency_key`/`get_status`；只有 adapter 明确 `not_found` 且契约允许无副作用结论时才返回 `not_accepted`，否则 `unknown`。
- `cancel`：只处理 `cancel_requested`；`adapter.cancel_task` 的 `requested` 不写停止事实，只有 `stop_confirmed=true` 才 `confirm_stop`。
- 所有写 `start_attempt`/`mark_running`/`record_attempt_result`/`record_dispatch_failure` 都携带当前 worker/fencing；lease 丢失后只做 best-effort cancel，不再写业务事实。

### 3.3 CLI

```text
python -m app.tasks.execution --once --adapter <trusted-adapter-name>
python -m app.tasks.execution --loop --adapter <trusted-adapter-name>
```

- `main(argv=None, *, dispatcher_factory=None) -> int`；`dispatcher_factory` 只由受信启动器/测试注入。没有受信 adapter/session/service-principal 配置时，CLI 必须非零退出并说明 `#97 production dispatch configuration not supplied`，不得回退到 Fake/Advisory。
- adapter 通过受信注册表/构造器解析，不从任务 payload、客户端参数或任意 import 字符串读取；未知/不可用 adapter fail closed，不提交。
- 本 Issue 不提供默认生产 adapter；Fake Adapter 只在测试中注入。`--adapter hermes` 在 #97 完成前必须拒绝，因为 Hermes 尚未满足 managed 能力。
- `run_once` 在调用方注入的 Session 上提交成功/领域拒绝、异常回滚；`run_forever` 使用 `session_factory` 为每次迭代创建新 Session。
- 凭据只从部署层环境/Secret Manager 读取；不得进入 argv、日志、审计或错误。

### 3.4 可信 Managed intake

本 Issue 不新增公开入口。信任的服务端调用者使用现有：

```python
task_service.submit_task(..., execution_mode="managed_execution",
                         runtime_capabilities_verified=True)
```

`runtime_capabilities_verified` 只能来自受信服务对真实 adapter 能力的核验，不能来自客户端。测试使用受控 fixture 创建任务；生产 managed intake 的 API 形态由 #97/#83 决定，不在本 Issue 偷偷放宽 B1 客户端表面。

## 4. 调度流程

### 4.1 claim 与预检

1. `worker.claim_task(..., execution_mode="managed_execution")`；需要给现有 claim 增加可选模式过滤，默认 `None` 保持兼容。
2. 校验任务 `queued`、managed、lease/fencing 当前有效。
3. `project_runtime_grant`；失败则**不调用 runtime**：
   - 缺 Grant/revoked/expired/task mismatch：`record_dispatch_failure(outcome="unavailable")`，任务进入失败/需重新决策；不得无限抢占。
   - adapter capability 未 `verified`：`record_dispatch_failure(outcome="unsupported")`，进入 `needs_human`，记录能力缺口。
4. `adapter.health()`：
   - `unavailable/degraded`：释放 lease，保持 `queued`，按退避重试；不得标记为已提交。
   - `healthy`：继续。
5. 生成稳定 runtime idempotency key：`idem_` + 基于 `task_id`、下一次 attempt 序号、`grant_id` 的 sha256 截断；同一 attempt 的重试必须复用同一 key，新 Grant/新 attempt 才换 key。
6. 构建 runtime prompt：只从已治理的 `goal`/`payload` 生成规范化 JSON，限制大小，不注入凭据；客户端不能直接提供额外 prompt/identity。

### 4.2 submit 结果

| Adapter outcome | 核心动作 |
|---|---|
| `accepted` | `start_attempt(runtime_ref=rtask_..., idempotency_key=key, worker/fencing)`；进入 dispatched |
| `rejected` | `record_dispatch_failure(rejected)`；释放 lease；不重试 |
| `unsupported` | `record_dispatch_failure(unsupported)`；`needs_human`；不重试 |
| `timeout` / `unknown` | 先 `find_by_idempotency_key(task, grant, key)`；found 则采用原 rtask 并 `start_attempt`；只有 adapter 明确 not_found + 无副作用保证才 `record_dispatch_failure(timeout/unknown)`；否则 unknown，Grant 撤销，不重发 |
| `conflict` | 先 lookup；同键同 payload found 则采用；不同 payload/无法证明则 `needs_human`/unknown，绝不能第二次创建 |

**受理后绑定失败：** 如果 adapter 已 accepted 但 `start_attempt` 因 lease/fencing/状态拒绝，立即 best-effort `cancel_task`。无论停止是否确认，都不得写 `dispatch.rejected`（Runtime 实际已接收，语义不符）；统一 `record_dispatch_failure(unknown)`，任务进入 `unknown`、Grant 撤销，等待对账，不重试。停止未确认时保持 unknown 并继续 best-effort 对账。

### 4.3 运行/完成映射

| Adapter status/result | 核心动作 |
|---|---|
| `accepted` | 保持 dispatched；继续轮询 |
| `running` | `mark_running(evidence_ref=evidence:runtime_start_<safe-ref>)` |
| `waiting_input` | 本 Issue 不支持通用输入；先 cancel 并取得停止证据，再转 `needs_human`；停止未确认则 `unknown` |
| `completed` + `get_result` | 只接受合法 `result_*`/`evidence:*`；`record_attempt_result(completed)` → `verifying`；**不自动 succeeded** |
| `failed` + stop_confirmed | `record_attempt_result(failed, stop_confirmed=True, evidence_refs=...)` |
| `stopped` + cancel_requested | `record_attempt_result(stopped, stop_confirmed=True)` |
| `timed_out` + stop_confirmed | `record_attempt_result(timed_out, stop_confirmed=True)` |
| `unknown` / `not_found` | `record_attempt_result(lost)`；任务 `unknown`，Grant 撤销；best-effort cancel；不恢复执行 |
| `completed` 但 result/evidence 缺失 | fail closed：`unknown`/`needs_human`，不得构造/伪造 `result_*`/`evidence:*` |

`RuntimeResult` 的映射必须经过核心验证器；dispatcher **从不调用** `complete_task`。runtime 自报不是独立完成证据；自报 evidence 由 `app.tasks.verification` 在后续验证阶段拒绝。Fake Adapter 的测试结果可以提供结构化 evidence；真实 Hermes 的缺口由 #97 处理。

### 4.4 取消与停止

- `request_cancel` 由核心写入 `cancel_requested` 后，调度器才能调用 adapter cancel。
- `cancel_task.outcome=requested` 不是停止；继续轮询。
- **Runtime `stop_confirmed` 只证明运行时不再产生新动作，不证明业务副作用已对账。** 本 Issue 不自动写 `cancelled`/`side_effects_reconciled=true`：从 `cancel_requested`/`running`/`dispatched` 得到 confirmed stop 时，保存 stop evidence 并 `escalate_task` 到 `needs_human`（T52/T40）；由人工在业务对账后执行 T63。`unknown` + stop evidence 保持 `unknown`，直到人工对账。
- `failed`/`timed_out` 从 `cancel_requested` 也进入 `needs_human` 并保留 runtime outcome/stop evidence；不得改写成自动 `cancelled`。
- `already_terminal`：按实际结果走完成/失败映射，不丢弃已完成结果。
- cancel 与 completed 竞态：以 runtime 终态 + 核心验证为准；已完成的成功结果不能被取消吞掉。
- `unsupported`/`unknown` cancel：保持 `cancel_requested`/`unknown` 并对账；不得报“已停止”。
- **事务边界：** 驱动器在每次状态观察后提交并重新读取任务；不得在 sleep/轮询期间持有 task/lease 行锁。外部 `request_cancel` 必须能在运行中提交。单独的 `cancel_requested` 对账 pass 处理崩溃后无 live lease 的取消请求；`cancel_requested` 不是 `unknown`，二者都要有明确对账去向。

### 4.5 崩溃、lease 过期与恢复

- `run_forever` 定期调用 `worker.recover_expired_leases(checker=RuntimeRecoveryChecker(...))`。
- dispatched：checker 只能依据 adapter lookup/status；无法证明 `not_accepted` 时必须 `unknown`。
- running/verifying：不重放；进入 `unknown`/证据不可用；不把 `running` 恢复为继续执行。
- 任务已 `unknown` 但有 `runtime_task_id`：best-effort cancel/stop；confirmed stop 只保存 stop evidence 并保持 `unknown`/转人工，不自动 `cancelled`；业务对账由人工完成。
- `cancel_requested` 的过期/崩溃对账：有 handle 则 cancel/status；confirmed stop -> 保存证据并 `needs_human`；无 handle 则按从未开始/人工路径处理；不得静默释放 lease 后留下永久 `cancel_requested`。
- 任何重试都必须由新的 PDP 决策 + 新 Grant；不得复用旧 Grant 或旧 idempotency key。

## 5. 能力、健康与安全

- 能力来源只认 `adapter.capabilities()`；Managed 至少要求 `task.submit`、`task.status` 为 `verified`。`runtime.isolation`/`runtime.task_scoped_credentials` 的升级由 #97 证据决定；本 Issue 的 Fake Adapter 只在测试中声明，不产生真实产品声明。
- 不接受 advisory fallback，不接受客户端选择的 adapter/mode/grant。
- service principal 从受信配置解析（`svc_`/`rt_`），不由任务 payload 提供。
- 日志/审计/错误不包含 API key、Grant token、任务 payload 原文、宿主路径或运行时私有 URL。
- 只使用 `worker` 的 lease/fencing；delivery 不是“已执行”；写入失败必须回滚或进入 unknown，不能静默重试副作用。

## 6. 测试与验收

### 6.1 Fake Adapter 矩阵

使用真实 PostgreSQL 与 `app/tests/tasks` 的 world/fixtures；Fake Adapter 实现 `RuntimeAdapter` 协议，脚本化 outcome/status/result/cancel。

必须覆盖：

1. accepted → running → completed → `verifying`；合法 result/evidence；核心验证器可另行完成 success。
2. 重复 submit 同键同 payload：adapter 只收到一次创建；同一 rtask；无第二个副作用。
3. 同键不同 payload：conflict；不创建第二个 run；进入对账/人工。
4. rejected / unsupported / timeout / unknown：状态映射、Grant 撤销、无盲目重发。
5. timeout 后 lookup found/not_found/unknown/conflict 四种分支。
6. cancel requested → stopped；requested 不写 stopped；cancel/completed 竞态；stop 未确认保持未知。
7. lease 过期/进程崩溃：recovery checker 只返回可证明事实；running/dispatched 不盲目重放。
8. Grant 过期/撤销/task mismatch/revision mismatch；capability 未 verified；adapter unhealthy；均不提交。
9. completed 无 result/evidence 或 runtime self-report：不得 succeeded。
10. accepted 后绑定失败：cancel/unknown，不产生可重跑假象。
11. 日志/审计无 token/secret；CLI 无默认生产 adapter；`--adapter hermes` 在 #97 前拒绝。
12. 单 worker 串行；多 worker 不重复 claim 同一任务（依赖 lease/fencing 现有约束）。

### 6.2 验收命令

- `uv run --no-sync pytest app/tests/tasks/test_execution.py -q -o addopts=''`
- `uv run --no-sync pytest app/tests/runtime/test_projection.py -q -o addopts=''`
- 全量 `pytest app/tests -q -o addopts=''`；Fast/privacy/CI。
- 证据记录提交 SHA、命令、退出码；Fake 证据明确标记为 Fake，不作为真实运行时兼容性证据。

### 6.3 文档

- `docs/architecture/runtime-dispatch.md`（本文）
- `docs/operations/runtime-dispatch.md`：启动/停止/lease/健康/故障/回滚，明确 `Fake Adapter` 仅测试、#97 前无生产 adapter、Managed intake 不是公开 API。

## 7. 路径与依赖

允许：
- `app/tasks/**`
- `app/runtime/**`
- `app/tests/tasks/**`
- `app/tests/runtime/**`
- `docs/architecture/**`
- `docs/operations/**`

禁止：`migrations/**`、`app/api/**`、`app/security/**`、`ops/security/**`、真实 Hermes Profile/凭据/生产数据库；新增依赖须单独批准。

依赖顺序：

```text
#96 managed dispatcher (fake adapter)
  -> #97 Hermes-in-sandbox managed execution
      -> #83 real client + real Hermes E2E / commercial readiness
```

#96 完成不解除 #83 的真实 Hermes E2E 阻塞；只有 #97 的真实证据可以升级 Hermes managed/isolation 能力标签。

## 8. 待所有者确认

1. 本设计是否接受“Managed 任务由服务端可信 intake 创建，公开 MCP/REST 保持 advisory”？
2. 是否接受“单 worker 单任务串行、多个 worker 处理不同任务”作为 #96 的明确容量边界？
3. 是否接受“completed 只进入 verifying；核心验证器独立完成 success”？
4. 是否接受“Fake Adapter 证据永远不升级真实运行时能力标签”？

审阅通过后再写实施计划（writing-plans）并派发；当前不开始代码。
