# MASTER.md — GitHub Master Runbook

Master 是计划者、调度者、独立审查者与唯一合并决策者，不默认承担执行实现。

## 接手时

1. 阅读 `AGENTS.md`、`AGENT-ORCHESTRATION.md` 与 `ORCHESTRATION-PROJECT.md`。
2. 运行 `git fetch --prune`；读取 Issue 与 PR：
   ```bash
   gh issue list --state open
   gh pr list --state open
   ```
3. 先检查任务 Issue 中的活动登记评论（包括尚无 PR 的任务），再核对每个开放 PR 的文件集合、head SHA、CI 状态和审查评论。
4. 重建任务状态、依赖图、修复轮次、阻塞原因和路径锁。不依赖聊天记忆。登记缺失或互相矛盾时先阻塞相关路径，不重复派发。
5. 选择本轮目标和执行模式。单会话模式前必须真实探测 C1–C4；不具备则降级为多会话。

## 从用户目标到 VERIFIED

1. 收敛目标并将工作写成任务 Issue：单一目标、背景、验收标准、允许 / 禁止路径、依赖、验证命令和风险。
2. 派发前在 Issue 评论登记活动路径锁、模式、运行实例 / 句柄、时间和状态；锁登记后才真实启动执行。
3. Execution Agent 在最新默认分支上建立分支并尽早开 Draft PR。不得并行修改重叠路径。
4. Execution 交回后，确认 rebase / update branch 与当前 head 上的检查，再由 Master 独立审查。
5. G1–G6 全部通过、批准 SHA 与待合并 SHA 相同后，由 Master 合并。
6. 在合并后默认分支重新执行配置的验证命令；通过才记 VERIFIED、更新活动登记并关闭 Issue。
7. 阶段收口时归纳已验证任务、阻塞、风险和下一阶段 Issue。

## 新项目启动

使用 `prompts/START-MASTER-NEW-PROJECT.md`。先检查仓库实际状态，不要假设已有代码、默认分支、CI 或测试；建立项目章程和最小任务图，再逐步交付最小可运行垂直切片。用户的重大产品决策不能被 Agent 自行替代。

## 审查记录模板

```text
结论：批准 / 退回 / 阻塞
用户可感知变化：
验收矩阵：每条标准 / 结论 / 证据
范围和基线：文件列表、默认分支、当前 head SHA
验证：CI check 名、当前 SHA、命令 / 结果
风险与下一步：
```
