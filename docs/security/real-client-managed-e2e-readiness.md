# 真实 MCP 客户端 + 活 uvicorn + 真实 Managed runtime E2E 就绪记录（#83 增量 A）

- 状态：**已在批准测试机 live 通过（4 passed in 16.02s）；生产/容量/备份/商用就绪仍未验证。** 实现者只做 `--collect-only`、`py_compile`、privacy 与 diff-check；实际 DB/live/sudo/systemd 运行由 Master 在测试机执行。本文记录测试机证据，不声称生产部署或商业化完成。
- 日期：2026-10-27
- 关联：Issue #83、#97、#96、#80、#82、#100；[#97 边界记录](hermes-managed-execution-boundary.md)、[#96 调度器运维](../operations/runtime-dispatch.md)、[Managed Execution 边界](managed-execution-boundary.md)。
- 测试文件：`app/tests/e2e/test_real_client_managed_e2e.py`。

## 0 测试机 live 证据

```text
提交: fe94bc5eaaa2439533938c71c8e445d3e67ed474
命令:
  AGENTMGR_E2E_LIVE=1 + #97 live env \
  pytest app/tests/e2e/test_real_client_managed_e2e.py -q -s -o addopts=''
结果: 4 passed in 16.02s（live body 未 skip）
Hermes: 0.22.1
runtime root: sha256:96cd218efbc04d845a75e795803f1877acee48ac3df8d7a4f51610831db07239
provider: inference.systalyze.com；专用测试 key 仅在宿主 broker，未记录/打印
链路: managed artifact -> dispatcher verifying -> 独立 complete_task -> succeeded
       -> real MCP client get_task/get_result -> outsider denied
清理: unit/task dir/broker socket/uvicorn listener 均消失
```

不记录主机名、个人路径、IP、key、token 或真实凭据。

## 1 这个测试证明什么

在批准测试机上，测试已证明以下事实（全部为真实组件，不使用 `TestClient`/ASGI transport）：

1. **真实服务器。** 测试在 `127.0.0.1:<free port>` 上绑定 socket，用真实 `uvicorn.Server` 在独立线程/事件循环中启动 `create_app()`；MCP 端点挂在 `/mcp/`。
2. **真实客户端。** 官方 MCP SDK `mcp==2.3.0` 的 `streamable_http_client` 配真实 `httpx2.AsyncClient` 经 TCP 完成 `initialize`、`tools/list`，并调用 `governance.get_task`/`governance.get_result`；请求头为 `Authorization: Bearer <internal-token>` 与 `X-Employee-Id: <requester-id>`。
3. **真实 PostgreSQL 与受信夹具。** 任务 world/policy/service identity 由真实测试数据库构建；managed 任务由受信测试夹具以 `execution_mode="managed_execution"`、`runtime_capabilities_verified=True` 创建，**不是**通过公开 MCP `submit_task` 创建。
4. **真实 Managed runtime。** 复用 #97 live 环境契约：真实 `EgressBroker`、`HelperSandboxSupervisor`、`HermesManagedAdapter`、`RuntimeDispatcher`、真实 helper/unit 与专用 provider key。模型把 `OK` 写入 `out/result.txt`；dispatcher 只到 `verifying`，不写 `succeeded`。
5. **独立完成验证。** 测试用实际 artifact 的 SHA-256 与 `evidence:*` 引用独立调用 `complete_task`，任务才进入 `succeeded`；`result_ref`/`evidence_refs` 与 artifact 一致。
6. **客户端观察。** requester 通过 MCP 看到 `succeeded`、`execution_mode=managed_execution`、`result.outcome=passed`、`verification.result=passed`、`side_effects_reconciled=false`；另一员工（auditor）读取 `get_task`/`get_result` 均被拒，稳定错误码为 `task_unauthorized_actor`。
7. **密钥与负向断言。** 真实 provider key 只在宿主 broker；不在 unit argv、unit 环境、任务目录文件、unit journal、持久化 result 或 MCP 响应中。没有 auto-`succeeded`、没有 `side_effects_reconciled`。任务 egress token 确实进入沙箱子进程环境，作为正向对照。
8. **清理。** 测试在 `finally` 停止 unit、删除任务目录、停止 broker 并移除 socket、停止 uvicorn；断言任务目录、unit、socket 与监听端口都不再存在。只使用 provisioned runtime/task root，不读取、不启动、不替换个人/默认 Hermes Profile、gateway、auth、`.env` 或 session。

## 2 运行契约

### 2.1 门禁

live body 需要同时满足：

```text
AGENTMGR_E2E_LIVE=1
AGENTMGR_HERMES_LIVE=1
AGENTMGR_HERMES_SUDO=1
```

缺任一项时测试以明确原因 skip；skip 不是验收证据。`live_environment` fixture 还会检查 #97 的 runtime/task/helper/key/provider 输入，并在 `sudo -n true` 失败时 skip。

### 2.2 环境变量形状（只列名称，不记录值）

```text
# #97 runtime / helper / keys
AGENTMGR_HERMES_RUNTIME_ROOT
AGENTMGR_HERMES_RUNTIME_ROOT_HASH
AGENTMGR_HERMES_TASK_ROOT
AGENTMGR_HERMES_EGRESS_SOCKET
AGENTMGR_HERMES_HELPER
AGENTMGR_HERMES_ATTESTATION_KEY
AGENTMGR_HERMES_EGRESS_TOKEN_KEY
AGENTMGR_HERMES_EVIDENCE_REF
AGENTMGR_HERMES_RUNTIME_VERSION
AGENTMGR_HERMES_GROUP            # 可选，默认 agentmgr-hermes
AGENTMGR_HERMES_RUNTIME_NAME     # 可选，默认 hermes
AGENTMGR_HERMES_RUNTIME_MAX_SEC  # 可选，默认 180

# 专用测试 provider
AGENTMGR_HERMES_PROVIDER_KEY_FILE
AGENTMGR_HERMES_PROVIDER_BASE_URL
AGENTMGR_HERMES_MODEL
```

测试自身设置一个仅用于测试的内部 token（`AJAIMGR_INTERNAL_TOKEN`），不记录、不提交真实凭据。真实 provider key 只由宿主 broker 读取；测试只比较/扫描，不打印。

### 2.3 命令

```text
UV_PROJECT_ENVIRONMENT=<repo-root>/.venv \
  uv run --no-sync pytest app/tests/e2e/test_real_client_managed_e2e.py -q -o addopts=''
```

实现者验证（不运行 DB/live/sudo）：

```text
UV_PROJECT_ENVIRONMENT=<repo-root>/.venv \
  uv run --no-sync pytest app/tests/e2e/test_real_client_managed_e2e.py \
  -q -o addopts='' --collect-only
python3 -m py_compile app/tests/e2e/*.py
python3 scripts/check_privacy.py --ci
git diff --check
```

### 2.4 版本与配置

| 组件 | 版本/形状 |
|---|---|
| Python | 3.13 |
| MCP SDK | `mcp==2.3.0`（官方 `streamable_http_client` + `ClientSession`） |
| HTTP 客户端 | `httpx2==2.16.0`（真实 `AsyncClient`，30s connect/read 300s SSE） |
| ASGI 服务器 | `uvicorn==0.54.0`，单进程、loopback TCP、`lifespan=on` |
| Hermes | 0.22.1（#97 测试机 runtime；以 runtime root 哈希为准） |
| 数据库 | 真实 PostgreSQL 测试库（`AJAIMGR_TEST_DB_URL`） |
| 传输 | MCP Streamable HTTP，`stateless_http=True`，端点 `/mcp/` |
| 身份 | 内部 token + `X-Employee-Id` 头（过渡信任边界） |

## 3 证据记录格式

Master 在批准测试机运行后，至少记录：

- 命令与退出码；
- `pytest` 计数（passed/skipped/failed）；
- 提交 SHA；
- Hermes 版本与 runtime root 哈希；
- MCP SDK / httpx2 / uvicorn 版本；
- 是否观察到 `verifying -> succeeded`、artifact SHA-256 与 `result_ref`/`evidence_refs`；
- 清理结果（unit/task dir/socket/uvicorn 均消失）。

证据放在 Issue/PR 或仓库外；**不记录**主机名、个人路径、IP、key、token 或真实凭据。仓库内只使用 `<repo-root>`、`<test-host>` 等占位符。

## 4 仍未证明 / 剩余缺口

以下能力不在本测试范围内，不得据此声称商用就绪：

1. **生产部署。** 测试机安装/运行不等于生产部署；生产 service principal、生产配置、生产 sudoers/broker 未验证。
2. **生产 key 生命周期。** 专用测试 key 的轮换、配额、计费与吊销流程未验证。
3. **容量与并发。** 单进程 uvicorn、单 dispatcher、单 unit；未测量并发、吞吐、启动开销或长稳。
4. **多 worker / 会话粘性。** 未验证多 worker 部署下的 MCP 会话与 dispatcher 竞争。
5. **TLS/网关。** 测试是 loopback + 内部 token；远程客户端必须由受信网关终止 TLS 并注入 token/employee 头，未在本测试中验证。
6. **数据库/附件备份恢复。** 属于 #83 的另一增量；本测试不覆盖备份、恢复到干净目标或逐项校验。
7. **业务工具/PEP。** Hermes 内执行的工具调用仍不经过 AgentMgr PEP；真实业务资源授权与工具治理仍属 #83。
8. **事件流。** 仍无原生事件流；客户端只能轮询 `get_task`/`get_result`。
9. **对抗性/越权矩阵。** 本测试只覆盖一个 outsider 读取拒绝；完整的伪造主体、越权、数据越界、重放、TTL、策略撤销等矩阵仍由 #83 其他增量承担。
10. **测试机证据。** 本文与测试只产生测试机证据；不声称生产、容量、备份或商业化就绪。

## 5 不得作出的声明

- 不得把 skip 当作通过证据；本次测试机 live 结果是 4 passed、无 skip。
- 不得声称生产已部署、已上线或已完成商业化验证。
- 不得声称容量、并发、启动开销、备份恢复或生产 key 轮换已验证。
- 不得把本测试机的单进程 uvicorn + 单 dispatcher 结果外推为生产容量证据。
