# AGENT-ORCHESTRATION.md — GitHub State Machine and Safety Gates

本文件定义 GitHub 作为跨会话事实来源时的编排方式；角色授权以 `AGENTS.md` 和项目配置为准。

## 1. 生命周期

```text
PLANNED → READY → DISPATCHED → PR_OPEN → MASTER_REVIEW → APPROVED → MERGED → VERIFIED
非成功状态：BLOCKED / NEEDS_HUMAN_DECISION / FAILED / CANCELED
```

- `PLANNED`：Issue 已创建，需求或依赖尚待补齐。
- `READY`：验收、允许 / 禁止路径、依赖及验证命令明确；依赖均已 VERIFIED；路径锁无冲突。
- `DISPATCHED`：Issue 评论中已登记活动任务锁，执行实例真实启动（或在人启动的多会话模式下真实接手）。
- `PR_OPEN`：Draft PR 已存在并关联 Issue。
- `MASTER_REVIEW`：作者声明交付，已更新到最新默认分支，当前 head 有可核对验证结论。
- `APPROVED`：Master 已通过全部验收并记录批准时的 head SHA。
- `MERGED`：已由 Master 合入默认分支。分支清理是单独的收尾状态，不决定是否已合并。
- `VERIFIED`：合并后验证成功，证据已记录；随后可关闭 Issue 并释放活动锁。
- `BLOCKED`：依赖、冲突或环境暂时阻塞；需要记录恢复条件。
- `NEEDS_HUMAN_DECISION`：需用户决定或授权的事项，必须列出问题与选项。
- `FAILED`：实例失败、超时、取消或产物不可用；未知状态一律未完成。
- `CANCELED`：经明确决定取消，必须记录原因和锁释放依据。

## 2. 活动任务登记与路径锁

**不能仅靠开放 PR 建立锁表。** 执行实例可能已启动但还未开 PR，因此每次派发前必须在目标 Issue 添加标准活动登记评论。

登记评论用以下固定字段，更新时编辑同一条评论或追加新记录并标记旧记录已过时：

```text
<!-- ORCH-TASK-REGISTRY v1 -->
status: DISPATCHED
mode: single-session | multi-session
locked_paths:
  - src/example.py
branch: <branch or NONE>
pr: <number or NONE>
runner: <runtime + handle/instance id, or HUMAN-STARTED>
dispatched_at: <ISO-8601 with timezone>
updated_at: <ISO-8601 with timezone>
<!-- /ORCH-TASK-REGISTRY -->
```

- 锁定路径来自 Issue 的允许路径；路径父子重叠也视为冲突。目录范围要尽可能具体。
- Master 接手时遍历所有活动登记（`DISPATCHED` / `PR_OPEN` / `MASTER_REVIEW` / `APPROVED` / `MERGED` / 未解决的阻塞），并结合开放 PR 实际 Diff 重建锁表。
- 任务尚无 PR 不意味着没有锁。执行句柄 / 状态未知时不释放锁；先确认实例已停止或恢复任务。
- 在活动登记中更新状态、分支、PR、head SHA、阻塞原因与更新时间。PR 被关闭不等于执行实例必然停止。
- `VERIFIED` 后记录最终状态及验证 SHA，再关闭 Issue 和释放锁。异常解锁需要明确理由和可核查记录。

## 3. 依赖与并行

依赖必须合入默认分支并通过合并后验证（`VERIFIED`），下游才可派发。同文件或父子路径范围冲突的任务串行；无依赖且文件集合不重叠的任务可并行。派发前执行 `gh pr list --state open` 并逐 PR 核对变更路径，同时读取未开 PR 的活动 Issue 登记。

## 4. 平台能力探测与降级

单会话自动编排必须实际具备：

- C1：启动独立执行实例并返回可寻址句柄。
- C2：传入独立任务上下文并保持隔离。
- C3：等待并取回状态、产物与证据。
- C4：区分成功、失败、取消和未知状态。

启动前逐项探测。任一能力缺失 → 不可声称已 spawn；降级为由人启动另一会话的多会话交接，并让 Master 下次依据 GitHub 状态恢复。提示词不能创造运行时能力。

## 5. Master 审查

按任务逐条检查：

1. 验收标准与实际行为 / 结果。
2. 变更文件集合完全在允许路径内。
3. PR 与 Issue 关联正确；差异来自最新默认分支。
4. `mergeable` 可合并；无未解决冲突。
5. 当前 head SHA 上的必需 CI 全绿；本地必要测试已实际执行。
6. 测试证据真实、可追溯；head 变化后重新运行。
7. 风险、未完成项和需要用户拍板的变化已经处置。

不通过则退回执行者，评论必须给可执行修复建议并增加轮次。默认 3 轮；达到上限仍未通过，进入 `BLOCKED` 并汇报。

## 6. 合并门禁 G1–G6

| 门 | 条件 |
|---|---|
| G1 验收 | 每一条验收标准有明确通过结论与证据 |
| G2 范围 | 只改允许路径，没有未授权更改 |
| G3 基线 | 基于最新默认分支，PR 可合并 |
| G4 CI | 项目配置中所有 required checks 在当前 head SHA 上通过 |
| G5 证据 | 真实命令、输出 / 退出码及当前 SHA 可核验 |
| G6 风险 | 未完成项、迁移、权限、安全和公开行为变化已处置；需用户决定的已阻塞 |

只有 G1–G6 全部通过，且批准时 head SHA 与待合并 head 完全一致，Master 才能合并。Execution Agent 不合并。合并后重新验证；分支删除失败要单独重试，不重复合并。

## 7. 阶段收口

阶段只有在必需任务均达到 VERIFIED、required checks 通过、阻塞和需人工决策都有明确去向、下一阶段 Issue 已登记且路径依赖关系清楚时才算完成。至少汇报：阶段范围 / 任务状态、已合并及验证的 PR ↔ Issue、阻塞与恢复条件、真实检查证据、未解决风险、用户需处理事项。
