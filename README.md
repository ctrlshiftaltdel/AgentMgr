# Agent-Orchestration — Offline Edition

单机、单用户、无托管平台版 Agent 开发编排规则。可用于维护已有仓库，也可从空目录启动新项目。
**不依赖 GitHub/GitLab、Issue/PR 服务或远程 CI**；可使用本地 Git 与 Worktree。模型本身是否能完全断网运行，取决于模型运行时，不由本规则包保证。

## 内容

- `AGENTS.md`：项目内所有 Agent 的协作契约与权限边界。
- `MASTER.md`：Master 的日常手册；每次接手先恢复本地状态。
- `AGENT-ORCHESTRATION.md`：状态机、原子文件锁、失败恢复、审查和合并规则。
- `AGENT-ORCHESTRATION-ACCEPTANCE.md`：成功路径与失败路径验收清单。
- `ORCHESTRATION-PROJECT.md`：需要针对每个项目填写的配置。
- `AGENT-BOOTSTRAP.md`：会话自动引导入口；每次会话开始时恢复本地状态并自动路由。
- `prompts/`：新项目启动、已有项目接手、Master 单/多会话、Execution 和 Reviewer 提示词。
- `prompts/START-CHARTER.md`：章程引导启动，把模糊想法澄清为各方确认的项目章程。
- `scripts/orchestrator.py`：基于 SQLite 的本地任务状态与原子路径锁 CLI。
- `scripts/validate_kit.py`：规则包静态检查。
- `templates/TASK-PACKET.md`：Execution Agent 的单任务交接模板。
- `templates/DESIGN.md`：设计文档模板。
- `templates/PLAN.md`：实现计划模板。
- `templates/REVIEW-CHECKLIST.md`：两阶段审查清单。
- `templates/ORCHESTRATION-PROJECT.template.md`：项目适配层模板；发布包内的 `ORCHESTRATION-PROJECT.md` 即按此模板给出，首次使用时填写。
- `templates/gitignore.snippet`：应将其中规则合并进仓库根的 `.gitignore`（无论仓库是否已初始化 Git），避免提交机器本地任务数据库；尤其静态检查 `python -m py_compile ...` 会生成 `__pycache__`，不忽略会让 `git status` 变脏。
- `tests/`：本地任务库脚本测试。

## 安装到一个项目

1. 将本包内容复制进目标仓库根目录。已有 `AGENTS.md` 时先人工合并，不能盲目覆盖原规则。
2. 编辑 `ORCHESTRATION-PROJECT.md`，填写主分支、测试/静态检查命令、关键路径和风险约束。发布包内的 `ORCHESTRATION-PROJECT.md` 为待填模板（或直接参考 `templates/ORCHESTRATION-PROJECT.template.md`）。
3. 从项目根目录运行：

   ```bash
   python scripts/orchestrator.py init
   python scripts/validate_kit.py
   python scripts/orchestrator.py --help
   ```

4. 首次启动已有项目：使用 `prompts/START-MASTER-EXISTING-PROJECT.md`。
5. 从零启动新项目：使用 `prompts/START-MASTER-NEW-PROJECT.md`。先产出项目章程和首批任务，再由 Execution Agent 执行；Master 不越权直接实现。
6. 此后每次会话开始时，以 `AGENT-BOOTSTRAP.md` 为入口：自动恢复本地状态，并判断走章程引导、新项目启动还是已有项目恢复。

## 工作流

1. **会话引导**：会话开始时执行 `AGENT-BOOTSTRAP.md`，读取权威文件、用任务库与 Git 的真实输出恢复状态，并自动路由到对应流程。
2. **章程引导**：按 `prompts/START-CHARTER.md` 澄清用户意图，产出项目章程与填好的 `ORCHESTRATION-PROJECT.md`。
3. **编写计划**：按 `templates/PLAN.md` 把章程拆成实现计划，每个任务块对应一条 `task add`，粒度不合格的块先拆分再登记。
4. **注册任务**：用 `orchestrator.py task add` 逐块登记任务，写清目标、允许路径、验收与依赖。
5. **逐任务派发**：优先把自包含任务包派给独立上下文的子代理执行；不具备 spawn 能力时改用多会话交接，不假装已启动。
6. **两阶段审查**：Master 按 `templates/REVIEW-CHECKLIST.md` 先查规格符合性，再查质量；Stage 1 有失败项即退回，不进入 Stage 2。
7. **合并与验证**：审查批准并绑定 head SHA 后由 Master 合并，标记 `MERGED`，合并后重新运行验证再标记 `VERIFIED`。

## 本地任务库

```bash
python scripts/orchestrator.py task add --id TASK-0001 --title "建立项目骨架" --paths README.md src/ --acceptance "有最小可运行入口" "测试命令可执行"
python scripts/orchestrator.py task ready TASK-0001
python scripts/orchestrator.py task dispatch TASK-0001
python scripts/orchestrator.py task status TASK-0001 IN_PROGRESS --note "Execution 已开始"
python scripts/orchestrator.py task workspace TASK-0001 --branch task/TASK-0001-init --worktree .worktrees/TASK-0001
python scripts/orchestrator.py task status TASK-0001 REVIEW_READY --head-sha <commit-sha> --note "实现已交回"
python scripts/orchestrator.py task status TASK-0001 MASTER_REVIEW --note "Master 开始独立审查"
python scripts/orchestrator.py task status TASK-0001 APPROVED --head-sha <commit-sha> --note "验收全部通过"
python scripts/orchestrator.py task status TASK-0001 MERGED --merge-sha <merge-commit-sha> --note "本地合并完成"
python scripts/orchestrator.py task status TASK-0001 VERIFIED --note "合并后检查通过"
```

`dispatch` 会在 SQLite 事务中原子地检查和占用路径锁，防止两个本地任务同时改相交路径。依赖必须到 `VERIFIED` 才可就绪/派发。`APPROVED` 绑定当前 head SHA；head 变化后必须重新审查。`VERIFIED` / `CANCELED` 会释放锁；异常退出或状态不明时不会默默释放锁。

任务状态保存在 `.orchestration/state.sqlite3`，事件写在数据库中，备份使用 `python scripts/orchestrator.py backup`。`init` 会把 `.orchestration/` 加入当前克隆的 `.git/info/exclude`（若存在 Git），不会覆盖项目的 `.gitignore`。如果项目尚未初始化 Git，之后执行 `git init` 时需把 `.orchestration/` 加入 `.gitignore`。请定期备份该目录；它是本地编排的事实来源。

## 运行边界

- 此版本假设同一位操作者在一台机器上工作；不设计多人同步、远端 Issue/PR、服务器调度或共享锁服务。
- 多个本地 Agent 进程可以并行，但只能通过此任务库登记活动任务；不要绕过锁表直接派发冲突文件任务。
- `orchestrator.py` 只管理本地任务状态和路径锁，不会启动模型、编写代码、运行项目测试或自动合并 Git 分支。那些操作仍由 Agent 按提示词执行，合并由 Master 决策。
- 若模型运行时不具备真实 spawn/wait/collect 能力，必须使用单用户的多会话交接：Master 产出任务包，操作者在本机另开会话启动 Execution；不得假装已启动。

## 自检

```bash
python scripts/validate_kit.py
python scripts/validate_kit.py --project-ready  # after filling project config
python -m py_compile scripts/orchestrator.py scripts/validate_kit.py
python -m unittest discover -s tests -v
```
