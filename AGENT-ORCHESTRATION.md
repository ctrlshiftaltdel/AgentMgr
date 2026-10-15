# AGENT-ORCHESTRATION.md — Local State Machine and Safety Gates

本文件定义不依赖托管平台的编排契约。任务库 CLI 为 `.orchestration/state.sqlite3`；任务正文、Git 状态、当前 head 和实际验证输出为证据。若某项能力依赖运行时支持而本机并不具备，必须降级，不得假装执行。

## 1. 任务状态机

```text
PLANNED → READY → DISPATCHED → IN_PROGRESS → REVIEW_READY → MASTER_REVIEW → APPROVED → MERGED → VERIFIED
非成功状态：BLOCKED / NEEDS_HUMAN_DECISION / FAILED / CANCELED
```

- `PLANNED`：任务草稿已写入本地任务库，但依赖或验收可能尚未齐备。
- `READY`：目标、路径、验收和依赖均明确；所有依赖任务为 `VERIFIED`。
- `DISPATCHED`：先通过本地 CLI 成功获取完整文件锁，再把自包含任务包交给真实执行实例。
- `IN_PROGRESS`：执行实例已开始；应记录分支 / Worktree。
- `REVIEW_READY`：实现者交回 Diff、head SHA、验证命令 / 输出和剩余风险。
- `MASTER_REVIEW`：Master 独立检查任务、变更范围、证据和风险。
- `APPROVED`：验收全部通过；批准必须绑定精确 head SHA。
- `MERGED`：已确认合并到项目本地主分支。远端或本地分支清理状态应单独记录，不可混为合并成功的判据。
- `VERIFIED`：在合并后的主分支上完成必要验证，记录命令 / 退出码 / 结果。完成后释放文件锁。
- `BLOCKED`：外部依赖、文件冲突或环境问题暂时阻止推进；记录恢复条件。
- `NEEDS_HUMAN_DECISION`：需项目所有者选择或授权；必须记录具体问题与选项。
- `FAILED`：执行实例失败、超时、取消或产物不可用；状态未知一律视为未完成。
- `CANCELED`：项目所有者或 Master 明确取消；记录原因后释放锁。

状态改变由 `scripts/orchestrator.py` 记录。不要手工编辑 SQLite。任务库提供的是治理记账，不会代替运行时真正派发或运行测试。

## 2. 派发与活动任务锁

1. 任务先进入 `READY`，然后运行 `python scripts/orchestrator.py task dispatch <ID>`。CLI 使用 SQLite `BEGIN IMMEDIATE` 原子检查锁并登记 `DISPATCHED`。
2. 锁范围由任务允许路径确定；比较采用路径相同或父子路径重叠的方式。例如 `src/` 与 `src/core.py` 冲突，`src/a.py` 与 `src/b.py` 不冲突。
3. 只有当全部路径可锁定时才一次性取得全部锁；冲突则整个派发失败，不允许半锁状态。
4. 任务处于活动状态时，不得修改锁定路径集合。确需扩大范围时，先阻塞任务、记录理由、检查新路径冲突并更新登记。
5. 仅进入 `VERIFIED` 或 `CANCELED` 自动释放锁。失败、超时、未知状态、审查退回或单纯删除 Worktree 都不自动释放。异常清理需运行显式 `task unlock` 并记录理由；强制解锁可能允许冲突，应先确认原任务已停止。
6. 每次启动或恢复 Master 时，先执行 `orchestrator.py task list`，再核对 `git status`、主分支、各任务 Worktree 和任务库事件；不要从聊天记忆重建。

## 3. 依赖与并行

- 依赖应构成有向无环图。依赖必须达到 `VERIFIED`，不能仅因为代码已提交或已合并就派发下游任务。
- 同一文件或目录前缀冲突的任务串行；不冲突且无依赖的任务可以并行。
- 单机可以并行运行多个执行实例，但所有实例必须先通过本地任务库登记及获取锁。不要让两个实例编辑同一个 Worktree。
- 如果运行时不能真实 spawn / wait / collect，Master 产出任务包，由同一位操作者在另一个本地会话启动执行实例；Master 不声称它已启动。

## 4. 独立审查门

Master 需要审查：

1. 任务验收标准是否逐条通过，哪些证据支持结论。
2. 实际变更路径是否全部属于允许路径；有无未授权文件。
3. `git status --short` 与 Diff 是否干净、范围是否正确；是否从最新本地主分支创建工作分支。
4. 每个必需的测试 / lint / typecheck / build 是否真正在当前 head 上执行，结果是否通过。
5. 当前 head SHA 是否与测试证据和批准记录一致。
6. 是否引入新依赖、迁移、权限变化、安全风险或未实现的行为。
7. 失败测试有无被删除、跳过或弱化；证据是否可复现。

任一验收项不通过 → 退回 `IN_PROGRESS` 或 `BLOCKED`，写明具体缺口。默认最多 3 轮修复；达到上限仍未通过 → `BLOCKED`，由项目所有者决定拆分、扩大范围或取消。

## 5. 批准与合并

- `APPROVED` 必须带当前完整 commit SHA。工作树或提交头有任何变化，批准立即失效，需重新审查。
- 只有 Master 执行合并（例如 `git merge --no-ff <branch>`，具体策略由项目配置决定）。执行者不能自批自合。
- 合并前备份或确认可恢复；遇到冲突不自动覆盖他人 / 主分支内容。
- 合并后运行 `ORCHESTRATION-PROJECT.md` 指定的验证命令，保存日志与退出码；全部通过后转 `VERIFIED`。
- 合并成功与分支清理分开记账。清理失败不能触发重复合并。

## 6. 项目接入与新项目

新项目的技术栈、目录与验证命令可能不存在。首次任务应是“项目初始化 / 技术方案 / 最小骨架”，而不是假设已有测试脚本。先记录产品目标、限制、假设与待人拍板问题，再通过任务库派发初始化任务。将未知验证项明确记为“待建立”，创建相应任务；不能声称尚不存在的命令已经通过。

## 7. 阶段收口

阶段只在必需任务达到 `VERIFIED`、相关风险已登记、阻塞与人工决策有明确去向、下一阶段任务已记录时算完成。汇报至少包含：范围与状态、已验证任务、仍阻塞事项、实际验证证据、未解决风险、下一步及需用户决定的事项。
