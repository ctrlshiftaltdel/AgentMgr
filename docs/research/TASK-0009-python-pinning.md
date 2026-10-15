# TASK-0009 Python 解释器固定与 .venv 策略报告

任务：在 worktree `.worktrees/TASK-0009`（分支 `task/TASK-0009-pinning`）上用仓库根 `.python-version` 把 Python 解释器固定在 3.13，修正 `ORCHESTRATION-PROJECT.md` 中的版本陈述并写明 `.venv` 策略，并在主工作树建立可用的 `.venv`。

## 1 结论摘要

- 根因确认：仓库此前没有 `.python-version`，裸跑 uv 会解析到 uv 自管的 CPython 3.14，而不是 TASK-0008 报告的 3.13.5。
- 修复：新增仓库根 `.python-version`（内容为 `3.13`），使 uv 在任何工作树内都解析到 3.13（本机为系统解释器 `/usr/bin/python3.13`，Python 3.13.5），不再落到 3.14。
- 实测结果：加固定后，worktree 内 `uv python find` 返回 3.13 路径（生成 `.venv` 前为 `/usr/bin/python3.13`，生成后为工作树 `.venv/bin/python`，退出码 0），`uv run python -V` 返回 `Python 3.13.5`（退出码 0）。
- 主工作树 `/home/agents/ajaimgr` 已建立 `.venv/`，`uv run python -V` 为 `Python 3.13.5`，`import fastapi, sqlalchemy, alembic, psycopg, pydantic` 退出码 0。
- `ORCHESTRATION-PROJECT.md` 已按任务要求改一处、增一条，其余内容逐字不变。
- 全部改动只涉及三个入库文件：新增 `.python-version`、新增本报告、修改 `ORCHESTRATION-PROJECT.md`；主工作树的 `.venv/` 不入库（已被 `.gitignore` 覆盖）。
- 未修改 `pyproject.toml` / `uv.lock`，未安装新依赖，未使用 pip。

本报告不含任何口令或凭据。

## 2 现状与问题

TASK-0008 环境报告（`docs/research/TASK-0008-dev-env.md`）记录的解释器与虚拟环境版本为 Python 3.13.5，来源是系统解释器 `/usr/bin/python3.13`，并说明当时用 `uv sync --python /usr/bin/python3.13` 显式指定了解释器。

问题在于：那次同步把解释器版本作为一次性命令行参数传入，仓库里没有任何持久化约束。于是任何未显式传 `--python` 的新工作树，uv 会按自身默认策略选择解释器，落到 uv 自管的 CPython 3.14，与文档陈述和 `uv.lock` 的可复现性不一致。

本次在未加固定的状态下实测复现了该偏差：

| 场景 | 命令 | 实测输出 | 退出码 |
| --- | --- | --- | --- |
| 新 worktree（无 `.python-version`） | `uv python find` | `/home/agents/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/bin/python3.14` | 0 |
| 主工作树（无 `.python-version`） | `uv sync` | `Using CPython 3.14.7` / `Creating virtual environment at: .venv` | 0 |
| 主工作树（无 `.python-version`） | `uv run python -V` | `Python 3.14.7` | 0 |
| 主工作树 `.venv/bin/python -V` | `.venv/bin/python -V` | `Python 3.14.7` | 0 |

对比 TASK-0008 报告记录的 3.13.5，可见差异确实存在：文档陈述的是 3.13，而裸跑 uv 得到的是 3.14.7。差异的后果是：`.venv` 里编译进二进制轮子的扩展（如 `pydantic-core`、`uvloop`）会按 3.14 的 ABI 选取，与文档、与以 3.13 为基座的既有环境不一致，锁文件虽然锁住了包版本，却锁不住解释器版本。

## 3 固定方式与落地

采用 uv 官方支持的版本固定文件 `.python-version`，放在仓库根，内容仅一行：

```text
3.13
```

选择 `3.13` 而不是精确到 `3.13.5` 的原因：`pyproject.toml` 的 `requires-python` 为 `>=3.13`，语义上要求的是 3.13 大版本；写成 `3.13` 允许补丁级差异（如另一台机器上是 3.13.7），同时严格排除 3.14。若写成 `3.13.5`，在只有 3.13.7 的机器上 uv 会尝试下载自管 3.13.5，反而增加换机成本。

落地动作：

1. 在 worktree 内新增 `.python-version`（内容 `3.13`）。
2. 在该 worktree 内验证 uv 解析结果（见第 4 节）。
3. 修改 `ORCHESTRATION-PROJECT.md`：
   - 「项目身份」的「语言 / 框架 / 运行方式」条，把原句 `Python 3.13（uv 管理环境与依赖，`.venv/` 已建立，依赖清单见 `pyproject.toml` 与 `uv.lock`）` 改为 `Python 3.13（由仓库根 `.python-version` 固定；uv 管理环境与依赖，依赖清单见 `pyproject.toml` 与 `uv.lock`）`，该条其余内容逐字不变。
   - 「本地工作流」节末尾新增一条：`- Python 环境：解释器由仓库根 `.python-version` 固定（3.13）；每个 Worktree 首次 uv sync/uv run 时各自生成 `.venv/`（已被 gitignore 忽略），依赖版本由 `uv.lock` 固定；统一用 uv，不得使用 pip`。
   - 其余章节与条目逐字不变。
4. 在主工作树 `/home/agents/ajaimgr` 建立 `.venv/`（唯一的主工作树写入例外，不入库）。

未采用的做法：不改 `pyproject.toml`（如加 `[tool.uv]` 段或收紧 `requires-python`），因为该文件不在本任务允许路径内，且收紧 `requires-python` 到 `>=3.13,<3.14` 会改变依赖解析语义，属于超范围改动。

## 4 验证结果

以下命令均在真实环境执行，退出码为实测值。

加固定后的 worktree（`.worktrees/TASK-0009`）：

| 命令 | 实测输出 | 退出码 |
| --- | --- | --- |
| `cat -A .python-version` | `3.13$`（一行，以换行结尾） | 0 |
| `uv python find`（生成 `.venv` 之前） | `/usr/bin/python3.13` | 0 |
| `uv python find`（生成 `.venv` 之后） | `/home/agents/ajaimgr/.worktrees/TASK-0009/.venv/bin/python` | 0 |
| `uv run python -V` | `Using CPython 3.13.5 interpreter at: /usr/bin/python3.13` / `Creating virtual environment at: .venv` / `Installed 35 packages in 24ms` / `Python 3.13.5` | 0 |

注：`uv python find` 在 worktree 内首次查询（尚无 `.venv`）解析到基座解释器 `/usr/bin/python3.13`；`uv run` 生成 `.venv` 后再次查询，会优先返回该工作树自己的 `.venv/bin/python`。两者版本同为 3.13.5，均非 3.14。

主工作树（`/home/agents/ajaimgr`）：

| 命令 | 实测输出 | 退出码 |
| --- | --- | --- |
| `uv sync --python 3.13` | `Installed 35 packages`（尾行 `+ websockets==17.2`） | 0 |
| `uv run python -V` | `Python 3.13.5` | 0 |
| `uv run python -c "import fastapi, sqlalchemy, alembic, psycopg, pydantic; print('imports ok')"` | `imports ok` | 0 |
| `ls -d .venv` | `.venv` | 0 |
| `.venv/bin/python -V` | `Python 3.13.5` | 0 |
| `readlink -f .venv/bin/python` | `/usr/bin/python3.13` | 0 |

说明：主工作树当前尚未包含 `.python-version`（该文件在本任务分支上，待合并后才会出现在 `main`），因此主工作树这次同步用 `uv sync --python 3.13` 显式指定了 3.13；一旦本分支合并，`main` 上直接 `uv sync` 即会由 `.python-version` 解析到 3.13，无需再传 `--python`。这一点是本次交付中的一个已记录的偏差（见第 7 节）。

仓库级检查：

| 命令 | 期望 | 实测 |
| --- | --- | --- |
| 占位符计数（对未填充占位符字面量执行 `grep -c`） | `0` | `0` |
| `python3 scripts/validate_kit.py --project-ready` | 退出码 0 | 退出码 0 |
| worktree 内 `git status --short` | 不出现 `.venv` / `__pycache__` | 不出现（仅三个入库文件） |

## 5 .venv 位置策略

两种可选方案：

- 方案 A：每个 Worktree 各自一份 `.venv/`（uv 默认行为，相对项目根）。
- 方案 B：共享一份环境，通过 `UV_PROJECT_ENVIRONMENT` 指向固定路径（例如 `~/.venvs/ajaimgr`），所有工作树复用。

方案 A 的取舍：

- 优点：隔离彻底，一个工作树的依赖安装不会影响另一个正在验证的分支；与「一个执行实例一个 Worktree、不共享可写工作区」的既有约定一致；无需额外配置，换机后行为一致。
- 缺点：磁盘与安装时间重复，本机 2 vCPU / 1.9 GiB 内存、`uv sync` 每次约需几十秒到一分钟量级，多工作树并存时占用叠加。

方案 B 的取舍：

- 优点：只装一次，节省磁盘与安装时间，适合依赖集合长期一致、工作树频繁创建的场景。
- 缺点：共享可写环境，一个工作树的 `uv sync` 会就地改写另一个工作树正在使用的环境，破坏隔离；版本冲突时难以定位；跨分支调试时环境与分支不再一一对应；需要一个仓库外的路径约定，换机后需重新设置，反而降低可复现性。

结论：采用方案 A，即每个 Worktree 各自生成 `.venv/`。理由是本仓库的核心纪律是隔离与真实证据（见 `AGENTS.md` 的「一个执行实例一个 Worktree；不共享可写工作区」），共享环境会直接违反该纪律，并让「在某分支上验证通过」的结论失去边界。隔离带来的重复成本在本项目规模下可接受，且 `.venv/` 已被 `.gitignore` 覆盖，不会污染版本库。本策略已写入 `ORCHESTRATION-PROJECT.md` 的「本地工作流」节。

## 6 换机复现步骤

在另一台 Debian Linux 机器上从零复现（假设已具备 uv、Git、PostgreSQL 17，且以单用户 `agents` 运行）：

```bash
# 1) 克隆仓库并进入
git clone <repo-url> ajaimgr && cd ajaimgr

# 2) 确认固定文件存在且内容为 3.13
cat .python-version        # 期望输出：3.13

# 3) 确认 uv 解析到的解释器（应为 3.13.x，不得是 3.14）
uv python find
uv run python -V

# 4) 建立本工作树的环境并安装依赖（版本由 uv.lock 固定）
uv sync

# 5) 验证依赖可用
uv run python -c "import fastapi, sqlalchemy, alembic, psycopg, pydantic; print('imports ok')"

# 6) 规则包检查
python3 scripts/validate_kit.py --project-ready
```

若机器上没有 3.13 解释器，uv 会自动下载自管 CPython 3.13.x 并记录实际路径；此时 `uv python find` 会返回 `~/.local/share/uv/python/` 下的 3.13 路径，版本仍为 3.13.x，同样满足固定要求。若 `uv python find` 返回 3.14，说明 `.python-version` 缺失或被覆盖，应停止并排查。

## 7 已知限制

- 主工作树本次用 `uv sync --python 3.13` 显式指定解释器，而非仅靠 `.python-version`。原因是 `.python-version` 位于本任务分支，`main` 尚未合并该文件；合并后主工作树将自动由 `.python-version` 固定。这是交付中的一处已记录偏差，不改变「主工作树 `.venv` 为 3.13.5」这一结果。
- `.python-version` 只固定大版本 `3.13`，不固定补丁级。不同机器可能落在 3.13.5 / 3.13.7 等不同补丁版本，属预期行为；若某天需要严格一致，可把文件内容改为精确补丁版本，但会引入自管解释器下载。
- 固定文件约束的是 uv 的解析行为，不约束绕过 uv 的调用。若有人直接用 `/usr/bin/python3.14` 或系统 `python3` 指向 3.14 运行代码，仍可能偏离；本仓库约定统一用 uv（见 `ORCHESTRATION-PROJECT.md` 附加说明与本地工作流）。
- `uv.lock` 锁的是包版本与哈希，不含解释器 ABI 维度；因此解释器固定（本任务）与依赖固定（`uv.lock`）是两件必须同时具备的事，缺一不可。
- 本机资源有限（2 vCPU / 1.9 GiB 内存），每个 Worktree 一份 `.venv/` 会叠加磁盘占用；工作树清理时可用 `git worktree remove` 连同其 `.venv` 一并回收。
- 本任务未验证 3.14 下运行既有代码是否会出错（不在允许路径与目标内）；仅验证了解释器被正确固定在 3.13。
