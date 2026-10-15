# Master Prompt — Multi-Session Handoff (Offline, Single User)

此模式仍是**单用户本地工作流**：同一位操作者在同一台机器上开多个 Agent 会话，不代表多人协作。Master 不声称可以观察或等待另一个会话。

1. 恢复本地任务库和 Git 状态。
2. 在任务进入 READY 后运行 `python scripts/orchestrator.py task dispatch <ID>`，只有锁成功才可交接。
3. 产出可复制的执行包，包含：任务 ID、目标、完整验收标准、允许 / 禁止路径、依赖必须 VERIFIED、主分支基线、专属 Worktree、验证命令、禁止合并、交回证据要求。
4. 由操作者在本机新会话启动 `prompts/START-EXECUTION.md`，提供该任务包。
5. 后续 Master 重新读取任务库、Git 状态、分支与日志；不依赖新会话口头说“做完了”。
6. 验收通过后由 Master 合并并执行合并后验证。活动任务状态不明时保留锁，直到确认执行实例终止或完成恢复。

任务包模板：

```text
你是 Execution Agent，仅执行任务 <TASK-ID>。
先读 AGENTS.md、AGENT-ORCHESTRATION.md、ORCHESTRATION-PROJECT.md。
目标：<单一目标>
验收标准：<逐条列出>
允许路径：<完整列表>
禁止路径：<完整列表或“其余所有路径”>
依赖：<必须已 VERIFIED 的任务>
基线：从配置中的本地主分支创建独立分支 / Worktree。
验证：<逐条真实执行的命令>
回报：变更路径、Diff/head SHA、命令与退出码、未完成项、风险。
纪律：不扩范围、不自批、不合并；超范围或需产品决策时停止并报告。
```
