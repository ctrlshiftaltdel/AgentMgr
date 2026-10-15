# AGENT-BOOTSTRAP.md — 会话自动引导入口

本文件定义每次 Agent 会话开始的确定性入口。无论会话由谁发起、上下文是否丢失，都要按本流程恢复状态并自动路由，不依赖聊天记忆或人工提醒。

## 1. 引导流程

### 第一步：读取权威文件

依次读取以下文件，建立编排契约与项目章程的全貌：

1. `AGENTS.md` — 角色权限与协作契约。
2. `AGENT-ORCHESTRATION.md` — 任务状态机与安全门。
3. `ORCHESTRATION-PROJECT.md` — 项目章程（目标、边界、验证命令）。

### 第二步：恢复本地状态

运行以下命令，用真实输出恢复本地状态，不从聊天记忆重建：

```bash
python scripts/orchestrator.py task list
git status --short
git branch --show-current
git log -1 --oneline
```

必要时可补充 `git worktree list`、任务库事件查询与最近提交记录。

### 第三步：按状态自动路由

根据第一步与第二步的事实，按顺序匹配下列分支，命中即加载对应提示词并继续；都不命中时落入 d。

| # | 条件 | 动作 |
|---|------|------|
| a | `ORCHESTRATION-PROJECT.md` 存在 `FILL_IN` 占位符，或章程缺失 | 加载 `prompts/START-CHARTER.md`，先补齐章程再进入编排 |
| b | 章程已填写完整，且任务库中没有任何任务（新项目） | 加载 `prompts/START-MASTER-NEW-PROJECT.md`，进入初始规划 |
| c | 任务库中已有活动任务（`DISPATCHED` / `IN_PROGRESS` 等），或项目已有推进记录 | 加载 `prompts/START-MASTER-EXISTING-PROJECT.md`，先汇报当前状态、活动任务与阻塞，再继续调度 |
| d | 任务库中没有任务，且用户本轮未给出目标 | 简要汇报当前状态（分支、工作区、最近提交、章程情况）后等待用户目标，不自行造任务 |

判断以任务库与 Git 的真实状态为准；多种条件同时命中时，按 a → b → c 优先级处理（例如章程缺失时先补章程，即使任务库有任务）。

## 2. 引导纪律

- 引导产出的是"状态与路由"汇报，它不替代 Master 的任务汇报模板（结论、用户可感知变化、验收结果、风险与下一步）。
- 不得用聊天记忆代替任务库状态；所有"有任务 / 有阻塞 / 有活动锁"的结论都必须有命令输出或文件证据支撑。
- 引导本身不改产品代码、不改编排状态；发现的冲突、缺失或异常先登记为任务或标记 `NEEDS_HUMAN_DECISION`，再进入对应提示词流程。
