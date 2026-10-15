# TASK-0008 开发环境安装与验证报告

任务：在 worktree `.worktrees/TASK-0008`（分支 `task/TASK-0008-dev-env`）上用 uv 管理 Python 环境与依赖，安装 PostgreSQL 并建立 `ajaimgr_dev` / `ajaimgr_test` 两个数据库，产出依赖清单与安装报告。

## 1 结论摘要

开发环境已在本机（Debian 13 trixie，2 vCPU / 1.9 GiB 内存）安装并逐条验证通过：

- Python 环境与依赖全部由 uv 管理（uv 0.13.1），未安装、未使用 pip。
- 系统解释器 `/usr/bin/python3.13`（Python 3.13.5）作为虚拟环境基座，`.venv` 由 uv 创建，未额外下载 Python 解释器。
- PostgreSQL 17.11 已安装，服务 `active`，集群 `17/main` 在线（端口 5432），采用本地 peer 认证。
- 数据库角色 `agents` 及数据库 `ajaimgr_dev`、`ajaimgr_test` 均已建立并实测可连接、可读写。
- 依赖锁定文件 `uv.lock` 已生成；`.gitignore` 追加 `.venv/`；`.venv/` 与缓存均未进入版本库。
- 本任务范围内仅改动仓库内四个文件：`.gitignore`、`pyproject.toml`、`uv.lock`、`docs/research/TASK-0008-dev-env.md`。

所有口令类凭据均未落盘；本报告不含任何密码或凭据。

## 2 安装清单与版本

| 组件 | 版本 | 路径 / 说明 |
| --- | --- | --- |
| uv | 0.13.1 (x86_64-unknown-linux-gnu) | `/home/agents/.local/bin/uv`（官方脚本安装，用户目录） |
| PostgreSQL | 17.11 (Debian 17.11-0+deb13u1) | 系统级 apt 安装，集群 `17/main` |
| psql 客户端 | PostgreSQL 17.11 | `/usr/bin/psql` |
| Python 解释器 | 3.13.5 | `/usr/bin/python3.13`（系统解释器） |
| 虚拟环境 Python | 3.13.5 | `.worktrees/TASK-0008/.venv/bin/python`（uv 创建） |

直接依赖（来自 `pyproject.toml`）：

| 包 | 版本 |
| --- | --- |
| fastapi | 0.143.2 |
| uvicorn[standard] | 0.54.0 |
| sqlalchemy | 2.1.4 |
| alembic | 1.20.0 |
| psycopg[binary] | 3.3.6（psycopg-binary 3.3.6） |
| pydantic | 2.14.1 |
| python-multipart | 0.0.32 |
| pytest（dev） | 9.1.1 |
| httpx（dev） | 0.28.1 |

`uvicorn[standard]` 带入的常见附加包：uvloop 0.23.0、httptools 0.9.0、watchfiles 1.3.0、websockets 17.2、python-dotenv 1.2.4、pyyaml 6.0.3。传递依赖另含 starlette 1.8.0、anyio 4.15.1、mako 1.4.3 等，共 35 个包，版本以 `uv.lock` 为准。

## 3 数据库与认证方式

- 认证方式：本地 **peer 认证**（`pg_hba.conf` 由安装脚本初始化为 `--auth-local peer --auth-host scram-sha-256`）。操作系统用户 `agents` 通过 Unix socket 连接时，PostgreSQL 依据同名数据库角色 `agents` 放行，无需口令。
- 角色：`agents`（`LOGIN CREATEDB`）。
- 数据库：`ajaimgr_dev`（所有者 agents）、`ajaimgr_test`（所有者 agents），编码 UTF8，排序规则 en_US.utf8。
- 为何不落口令：本机为单用户开发机，peer 认证下无需口令即可完成本地连接；把口令写进文件（配置、脚本、报告）会引入泄露风险，且与项目“不提交密钥/凭据”的安全约定冲突。因此安装与文档中一律不出现口令；如需远程或口令认证，应在生产主机上单独配置并交由凭据管理，而非写入仓库（见第 7 节）。

## 4 复现步骤

以下命令序列可直接重跑（假设已具备 sudo 免密权限）：

```bash
# 1) 建立隔离工作区
cd /home/agents/ajaimgr
git worktree add .worktrees/TASK-0008 -b task/TASK-0008-dev-env

# 2) 安装 uv（官方脚本，装到 ~/.local/bin）
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv --version

# 3) 安装 PostgreSQL 并启动
sudo apt-get update
sudo apt-get install -y postgresql
sudo systemctl enable --now postgresql
systemctl is-active postgresql

# 4) 建角色与数据库（peer 认证，不写口令）
sudo -u postgres psql -c "CREATE ROLE agents LOGIN CREATEDB;"   # 已存在则忽略报错
sudo -u postgres createdb -O agents ajaimgr_dev
sudo -u postgres createdb -O agents ajaimgr_test

# 5) 连接与往返自测
psql -d ajaimgr_dev -c "select current_user, current_database();"
psql -d ajaimgr_dev -c "create table t_probe(i int); insert into t_probe values (1); select * from t_probe; drop table t_probe;"

# 6) 建立 Python 项目并同步依赖（在 worktree 内；先写 pyproject.toml，勿用 uv init）
cd /home/agents/ajaimgr/.worktrees/TASK-0008
uv sync --python /usr/bin/python3.13

# 7) 验证
uv run python -c "import fastapi, sqlalchemy, alembic, psycopg, pydantic; print('imports ok')"
uv run pytest --version

# 8) 提交（仅四个允许文件）
git add .gitignore pyproject.toml uv.lock docs/research/TASK-0008-dev-env.md
git commit -m "chore(TASK-0008): uv 开发环境、PostgreSQL 与依赖清单"
```

## 5 验证结果

| 命令 | 结果 | 退出码 |
| --- | --- | --- |
| `git worktree add .worktrees/TASK-0008 -b task/TASK-0008-dev-env` | `HEAD is now at ecf6ced`，新分支创建成功 | 0 |
| `curl -LsSf https://astral.sh/uv/install.sh \| sh` | `installing to /home/agents/.local/bin`，`everything's installed!` | 0 |
| `/home/agents/.local/bin/uv --version` | `uv 0.13.1 (x86_64-unknown-linux-gnu)` | 0 |
| `uv python find 3.13` | `/usr/bin/python3.13` | 0 |
| `sudo systemctl enable --now postgresql` | 同步 SysV 脚本并启用 | 0 |
| `systemctl is-active postgresql` | `active` | 0 |
| `sudo -u postgres psql -c "CREATE ROLE agents LOGIN CREATEDB;"` | `CREATE ROLE` | 0 |
| `sudo -u postgres createdb -O agents ajaimgr_dev` | 无输出（成功） | 0 |
| `sudo -u postgres createdb -O agents ajaimgr_test` | 无输出（成功） | 0 |
| `psql -d ajaimgr_dev -c "select current_user, current_database();"` | `agents \| ajaimgr_dev` | 0 |
| `psql -d ajaimgr_dev -c "create table t_probe(i int); insert into t_probe values (1); select * from t_probe; drop table t_probe;"` | `CREATE TABLE` / `INSERT 0 1` / 查询返回 `1` / `DROP TABLE` | 0 |
| `psql -d ajaimgr_test -c "select current_user, current_database();"` | `agents \| ajaimgr_test` | 0 |
| `psql --version` | `psql (PostgreSQL) 17.11 (Debian 17.11-0+deb13u1)` | 0 |
| `uv sync --python /usr/bin/python3.13` | `Prepared 35 packages` / `Installed 35 packages`（支持 `[dependency-groups]`，无需回退到 `uv add --dev`） | 0 |
| `uv run python -c "import fastapi, sqlalchemy, alembic, psycopg, pydantic; print('imports ok')"` | `imports ok` | 0 |
| `uv run pytest --version` | `pytest 9.1.1` | 0 |
| `uv run python -V` | `Python 3.13.5` | 0 |
| `git status --short`（worktree，提交前） | 仅 `.gitignore`、`pyproject.toml`、`uv.lock`、`docs/research/TASK-0008-dev-env.md` | — |
| `git ls-files \| grep -c venv` | `0`（`.venv/` 未入库） | — |
| `git status --short`（主工作树 `/home/agents/ajaimgr`） | 空（无输出） | — |

注：`uv sync` 未报 `[dependency-groups]` 不支持，故未采用 `uv add --dev pytest httpx` 回退方案。

## 6 卸载与回滚

以下操作均可逆，且不影响仓库：

- 移除 uv：删除二进制与其缓存，`rm -rf ~/.local/bin/uv ~/.local/bin/uvx ~/.local/share/uv`；如曾写入 shell 配置（`~/.bashrc` / `~/.profile` 中的 `source $HOME/.local/bin/env`），一并删除对应行。
- 移除 Python 虚拟环境：在 worktree 内 `rm -rf .venv`（`.venv/` 已在 `.gitignore` 中，不入库，删除不影响 Git 状态）；如需重新生成，重跑 `uv sync --python /usr/bin/python3.13`。
- 移除 PostgreSQL：先停服 `sudo systemctl disable --now postgresql`，再卸载 `sudo apt-get remove --purge -y postgresql postgresql-17 postgresql-client-17`，最后按需清理数据目录 `sudo rm -rf /var/lib/postgresql/17`。注意：purge 会删除数据库数据，执行前确认不再需要 `ajaimgr_dev` / `ajaimgr_test` 中的数据。
- 删除工作区与分支（在仓库主目录执行）：`git worktree remove .worktrees/TASK-0008`，随后 `git branch -D task/TASK-0008-dev-env`。以上不会改动 `main` 上的任何文件。
- 回滚本次提交：`git revert <commit>` 或删除分支即可；因本任务只新增/追加四个文件，回滚无副作用。

## 7 已知限制与后续

- 认证方式仅适用于本机开发：当前为 peer 认证，生产主机（多用户 / 容器 / 远程）需要改为口令（scram-sha-256）或证书认证，并把凭据交由密钥管理，绝不能写入仓库。
- PostgreSQL 版本差异：本机为 17.11（Debian trixie 源）。其他环境可能是 15/16 等版本，`CREATE ROLE ... CREATEDB`、`createdb -O` 与 peer 认证在 12+ 行为一致，但若目标环境 `pg_hba.conf` 默认策略不同（如强制口令），需相应调整。
- Python 版本约束：`requires-python = ">=3.13"`。若部署机只有 3.12 及以下，uv 将尝试下载受管解释器或报错，需先满足解释器版本要求。
- 依赖版本未做上限约束：`pyproject.toml` 中直接依赖未固定版本，可复现性依赖 `uv.lock`；升级依赖时应重跑验证（imports、pytest）并检查锁文件差异。
- 本机资源有限（2 vCPU / 1.9 GiB 内存），同时运行 PostgreSQL 与较重的测试/构建时需留意内存占用。
- 后续可考虑：为数据库连接引入环境变量/配置层（不含口令，口令来自外部注入），在 CI 中固定 PostgreSQL 大版本，并为 alembic 迁移补齐初始化脚本与测试。
