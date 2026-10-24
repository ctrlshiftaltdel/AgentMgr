# Runtime Dispatch 运维手册（Managed Execution 调度器）

- 状态：**#96 实现边界**；本手册描述当前代码（`app/tasks/execution.py`）实际提供的启动/停止/故障行为，不描述尚未实现的生产启动器。
- 关联：Issue #96、#97、#83；设计见 [Runtime Dispatch 架构](../architecture/runtime-dispatch.md)、[Runtime Adapter 契约](../contracts/runtime-adapter.md)。
- 日期：2026-10-24

## 1 结论摘要（先读这一节）

- **#96 不验证 Hermes，也不验证任何真实执行。** 仓库内的 `FakeRuntimeAdapter` 仅用于测试；它产出的结果、证据和能力标签**永远不能**作为真实运行时兼容性证据。
- **没有默认生产 adapter。** `--adapter <name>` 只查受信启动器注入的注册表；没有注入时 CLI **非零退出**并打印 `#97 production dispatch configuration not supplied`。`--adapter hermes` 在 #97 完成前一律拒绝。
- **Managed intake 不是公开 API。** 本 Issue 不新增 MCP/REST 管理入口；Managed 任务只能由服务端可信入口/测试夹具创建，客户端不能选择 adapter、执行模式或 Grant。
- **容量边界：单 worker 单任务串行。** 一个调度器进程同一时刻只驱动一个任务；多个 worker 只能处理**不同**任务，靠现有 lease/fencing 保证不会重复 claim。
- **运行时自报不是成功。** 调度器最多把任务推进到 `verifying`；从不调用 `complete_task`。
- **#83 仍被阻塞。** #96 完成不解除真实客户端 + 真实 Hermes E2E / 商业化就绪的阻塞；只有 #97 的真实证据可以升级 Hermes managed/isolation 能力标签。

## 2 启动与停止

### 2.1 命令

```bash
# 单次 pass：claim 一个 managed 任务，完成 preflight/submit/驱动后退出
python -m app.tasks.execution --once --adapter <adapter-name>

# 常驻循环：直到收到 SIGTERM/SIGINT
python -m app.tasks.execution --loop --adapter <adapter-name>
```

- `--once` 与 `--loop` 互斥且必选其一；`--adapter` 必填。
- CLI **不接受**任何凭据、DSN、session 或 import 路径参数。凭据只从部署层环境/Secret Manager 读取，且不得进入 argv、日志、审计或错误消息。
- `--adapter` 只是**名字**。真实 adapter、Session、service principal 由受信启动器（部署代码）构造并注入；CLI 不会按名字 import 模块，也不会从任务 payload/客户端参数解析 adapter。

### 2.2 受信启动器注入（#97 之前无生产实现）

`main(argv=None, *, dispatcher_factory=None) -> int` 的 `dispatcher_factory` 是唯一的 adapter 来源。启动器负责：

```python
def factory(name: str) -> RuntimeDispatcher | None:
    if name != "<adapter-name>":
        return None  # -> CLI 打印 "unknown adapter" 并非零退出
    return RuntimeDispatcher(
        adapter,                     # 受信构造的 RuntimeAdapter
        DispatcherConfig(
            worker_id="<stable-worker-id>",
            lease_seconds=300,
            poll_interval=0.5,
            health_backoff_seconds=5.0,
            error_backoff_seconds=5.0,
            max_error_backoff_seconds=60.0,
        ),
        service_principal=principal, # svc_/rt_ 服务身份
        session=session,             # --once / 单次调用使用
        session_factory=sessionmaker,# --loop 每次迭代的新 Session
    )

raise SystemExit(main(["--loop", "--adapter", "<adapter-name>"],
                      dispatcher_factory=factory))
```

`--loop` 要求 dispatcher 带有 `session_factory`（构造参数或 `run_forever` 参数）；缺失时 `run_forever` 抛 `ValueError`，CLI 非零退出。`--once` 只需要注入的 `session`。

### 2.3 退出码

| 退出码 | 含义 |
| --- | --- |
| `0` | pass/循环正常结束。`--once` 会向 stdout 打印稳定的 `DispatcherResult.outcome`（如 `idle`/`verifying`/`unknown`/`needs_human`）；任务结果仍以核心任务状态为准，不改变退出码。 |
| `1` | 未处理异常（编程错误/数据库错误）。stderr 只打印稳定的异常类型或 domain reason code，不含 message、payload、路径或凭据。 |
| `2` | 用法错误，或没有受信配置（`#97 production dispatch configuration not supplied`），或 adapter 名字未知。 |

`--once` 的 domain 拒绝（rejected/unsupported/timeout/unknown）会作为任务事实提交，CLI 仍返回 `0`，并把 outcome 打到 stdout；请结合核心任务状态与审计，而不是只看退出码。

### 2.4 停止（SIGTERM/SIGINT）

- 信号只设置 `stop_event`，**不会**打断正在写入的事实。循环在两个 observation 之间观察到事件后，回滚当前工作单元并返回。
- **不会**因为停机而取消 runtime：停机不是核心 `cancel_requested`。稳定的 idempotency key 保证下一个进程可以接管同一个 runtime run，而不会创建第二个。
- **取舍（已知行为）：** 回滚后 claim lease 仍然存活到 `lease_expires_at`（最多 `lease_seconds`）。因此该任务在 lease 过期前不会被其他 worker claim。这里**故意不**提前释放 lease：任务可能仍在 runtime 中运行，提前释放会制造“可重跑”的假象并诱发重放。lease 过期后由 recovery 对账。
- 需要快速交接时，应缩短 `lease_seconds`（代价是更频繁的续租与更大的时钟/延迟敏感度），而不是在停机路径里手工释放 lease。

## 3 生命周期与 lease

一个调度 pass：

```text
claim (managed queued only)
  -> 校验 Grant / 决策绑定 / 能力 / adapter.health()
  -> 生成稳定 idempotency key + 有界 prompt
  -> adapter.submit_task
  -> accepted: start_attempt(rtask_*, key, worker/fencing)
  -> adapter.get_status / get_result
  -> record_attempt_result -> verifying
  -> 核心验证器独立决定 succeeded/partial/failed
```

- **只 claim `execution_mode=managed_execution`**；advisory/BYOA/governed 任务不经过本调度器。
- **lease/fencing：** 默认 `lease_seconds=300`，驱动期间约半衰期续租；所有 `start_attempt`/`mark_running`/`record_attempt_result`/`record_dispatch_failure` 都携带当前 worker 与 fencing token。lease 丢失后只做 best-effort cancel，不再写业务事实。
- **事务边界：** 每次状态观察后提交并重新读取任务；sleep/轮询期间不持有 task/lease 行锁。外部 `request_cancel` 因此能在运行中提交。
- **时间上限：** 驱动受 run 自身 `max_runtime_seconds` 与 lease 约束；超时先 best-effort cancel，确认停止则保存证据并转 `needs_human`，否则记 `lost`。
- **串行：** 一个 dispatcher 同时只驱动一个任务。不要在同一 `worker_id` 上并行启动多个进程；多进程请使用不同 `worker_id`，它们只会 claim 到不同任务。

## 4 失败、取消与恢复

### 4.1 submit / preflight 失败

| 事实 | 行为 |
| --- | --- |
| Grant 缺失/撤销/过期/task 不匹配 | 不调用 runtime；`record_dispatch_failure(unavailable)`，进入失败/需重新决策。 |
| 决策绑定或 Grant 文档不一致 | 不调用 runtime；`unavailable`/拒绝。 |
| 能力未 `verified` | `unsupported` -> `needs_human`。 |
| adapter 不健康/降级 | 释放 lease，任务保持 `queued`，按 `health_backoff_seconds` 重试；不标记为已提交。 |
| `rejected` / `unsupported` | 记录事实，释放 lease，不重试。 |
| `timeout` / `unknown` / `conflict` | 先 `find_by_idempotency_key`：可证明的 found 复用原 run；否则记录 `timeout`/`unknown`、撤销 Grant、**不重发**。 |

### 4.2 运行与完成

- `completed` 且带合法 `result_*` + 至少一个 `evidence:*` -> `verifying`；**不自动 succeeded**。
- `completed` 但结果/证据缺失 -> fail closed（`unknown`/`needs_human`），不构造、不伪造引用。
- `failed` / `timed_out` + 确认停止 -> 记录对应终态；未确认停止不伪造。
- `waiting_input`：#96 不支持通用输入，best-effort cancel；确认停止 -> `needs_human`，否则 `unknown`。
- `unknown` / `not_found` -> `lost` -> `unknown`，撤销 Grant，best-effort cancel，不恢复执行。

### 4.3 取消

- 核心写入 `cancel_requested` 后，调度器才调用 `adapter.cancel_task`，且每个 pass 只调用一次。
- `cancel_task.outcome=requested` **不是**停止：继续观察状态。
- runtime `stop_confirmed` 只证明运行时不再产生新动作，**不证明业务副作用已对账**。调度器保存 `evidence:runtime_stop_*` 证据并 `escalate_task(needs_human)`；**从不**写 `cancelled` 或 `side_effects_reconciled=true`，由人工对账后执行 T63。
- `already_terminal` 与取消/完成竞态：以 runtime 终态 + 核心验证为准；已完成的成功结果不会被取消吞掉。
- 已 `unknown` 但仍持有 `rtask_*` 的任务：每个循环迭代做一次幂等 best-effort cancel，任务保持 `unknown`（没有 per-task 退避，属于已知运维噪声；确认停止后即停止重试）。

### 4.4 崩溃与 lease 过期

- `run_forever` 每轮先执行 `recover_leases`：worker 的固定窗口会被**分批排空**（`limit=None` 表示全部），因此积压不会让后面的行永久饿死；每批独立提交。排空整个积压可能让单轮循环变长；需要限制单轮工作量时传入显式 `limit`（硬上限，`limit=0` 不恢复任何行）。
- 恢复的 adapter 观测在 task/lease 行锁**之外**完成，锁内只落已缓存事实（每批默认 10 行），因此外部 `request_cancel` 不会被一次网络观测阻塞。
- 升级 `needs_human` 或写对账文档前，会重新校验当前 claim/recovery lease 与 fencing token；已失效的 pass 直接回滚，不写半截 `needs_human`/证据。
- `RuntimeRecoveryChecker` 只依据 adapter 事实回答，且只处理 managed 任务：
  - 已绑定 handle：观察 `get_status`；仍 live 的 run 报 `running`，worker 进入 `unknown`，**绝不重放**；确认 `failed`/`timed_out` 才可关闭 attempt。
  - 无绑定 handle：**即使 adapter 回答 `not_found` 也报 `unknown`**。契约没有显式的“无副作用”保证字段，缺失 run 不能证明未来不会落地，更不能作为用同一 Grant/key 重排的依据；缺 key、lookup 抛错、`unknown`/`conflict`、found 但无 handle 同样一律 `unknown`。
- `running`/`verifying` 的过期任务不会恢复执行；进入 `unknown` 并等待对账。
- 任何重试都必须来自新的 PDP 决策 + 新 Grant；不得复用旧 Grant 或旧 idempotency key。
- 旧的 attempt 的停止/查找事实不会继承到新 attempt：reconciliation 事实以 `runtime_key` 归属，新 key 不继承旧 stop/lookup。

## 5 常驻循环与错误退避

`run_forever` 每轮依次执行四个独立提交的 pass：

1. `recover_leases`（排空过期 lease）
2. `reconcile_cancel_requests`（无 live lease 的 `cancel_requested` 对账）
3. `run_once`（一次 claim/submit/驱动）
4. `cancel_unknown_runs`（`unknown` 且有 handle 的 best-effort 停止）

退避规则：

| 情况 | 延迟 |
| --- | --- |
| 正常/空闲 | `poll_interval`（默认 0.5 s） |
| adapter 不健康（`adapter_unavailable`） | `health_backoff_seconds`（默认 5 s） |
| 可恢复的 domain/lease 错误（`TaskError`/`GovernanceError`） | `error_backoff_seconds` 起，按连续失败次数翻倍，封顶 `max_error_backoff_seconds` |
| 编程错误（`TypeError`/`AttributeError`/数据库错误等） | **不吞掉**，进程退出，由进程管理器重启并告警 |

可恢复错误只记录稳定代码，例如：

```text
runtime_dispatch_pass_error code=task_state_revision_conflict consecutive_errors=2 backoff_seconds=10.000
```

日志**不包含**任务 payload、prompt、凭据、宿主路径或 runtime 私有 URL。若进程反复以非零退出，先检查数据库/依赖与代码版本，而不是盲目重启。

## 6 运维检查清单

- **启动前：** 数据库迁移到 head；服务身份（`svc_`/`rt_`）有效；adapter 的 `task.submit`/`task.status` 能力为 `verified`；确认没有第二个进程使用同一 `worker_id`。
- **运行中：** 关注 `unknown`、`needs_human`、`cancel_requested` 的堆积；关注 `runtime_dispatch_pass_error` 日志；确认 lease 续租正常（长期无进展的任务会到 deadline 后 fail closed）。
- **停止：** 发送 SIGTERM，等待进程退出；不要 `kill -9`。残留 lease 会在 `lease_expires_at` 后由 recovery 处理。
- **回滚：** 本调度器不引入迁移或新依赖；回滚 = 停止进程并部署上一版本。已进入 `unknown`/`needs_human` 的任务仍需人工对账，不能靠重启“修好”。
- **证据：** Fake adapter 的测试结果必须标注为 Fake；不得用于宣称 Hermes 或任何真实运行时支持 Managed Execution。

## 7 边界与后续

| 事项 | 归属 |
| --- | --- |
| 真实 Hermes-in-sandbox managed 执行、任务级凭据、模型出口治理 | #97 |
| 真实客户端 + 真实 Hermes E2E / 商业化就绪 | #83（#96 不解除阻塞） |
| 公开 Managed 管理 API | 未实现；由 #97/#83 决定，本 Issue 不放开 B1 客户端表面 |
| 生产 adapter 注册表 / 启动器 / Secret 读取 | #97（本 Issue 只定义注入接口） |
| 多任务并发、多主机扩展 | 非目标；当前明确单 worker 单任务串行 |
