# W0-7 Profile 隔离与边际内存实测报告

- 任务：W0-7（在本机真实创建命名 Profile，实测资源边际内存与隔离边界）
- 分支 / Worktree：`task/W0-7-profile-experiment` / `/home/agents/ajaimgr/.worktrees/W0-7`
- 本任务唯一写入的仓库文件：`docs/research/W0-7-profile-experiment.md`
- 前置阅读：`docs/research/W0-2-profile-isolation.md`（本报告用真实实验证实/推翻其结论）
- 安全声明：未读取、未写入、未打印任何凭据值或真实账号标识；报告内一律用占位符或「键名」表示。未安装软件包，未连接或登录任何真实渠道账号，未重启/停止/重配默认 Profile 网关与 TUI。
- 证据声明：所有数字来自本机真实命令输出（含时间戳与退出码）；外推项均显式标注假设。

## 1. 结论摘要

- **内存随 Profile 数的增长不是「每 Profile 405 MB」，而是「单进程共享基线 + 每 Profile 约 14 MB 的边际增量」。** 实测：默认 Profile 网关进程基线 `RSS 426952 kB / Pss 421931 kB`；在同一多路复用网关内依次新增 profile-a / profile-b / profile-c 后，`RSS` 边际增量为 `+13248 / +13100 / +15836 kB`（均值 **14061 kB ≈ 13.73 MB**），`Pss` 边际增量与之几乎完全一致（`+13245 / +13100 / +15839 kB`）。**这推翻了 W0-2 §5.2「N_max = (MemTotal − B) / 405 MB ≈ 3」的线性假设**：多路复用拓扑下，第 N 个被服务 Profile 的边际内存远小于每进程基线，因为 Python 解释器、模块与内置技能代码在**同一个地址空间内共享**。
- **RSS 与 Pss 在本次拓扑下几乎相等（差值恒为约 5021 kB）。** 原因是只有一个网关进程在消费这些页，跨进程共享效应不存在；差值即网关与其他进程（libc 等只读代码）共享的少量页。**「共享页导致边际内存非线性」这一效应在本拓扑中表现为「进程内共享」而非「跨进程共享」**，故边际增量小且近似恒定。
- **Hermes 的 Profile 隔离确为「数据与凭据作用域」，不是「操作系统边界」——本机实测证实。** 以当前 `agents` 用户身份 `cat /home/agents/.hermes/profiles/profile-b/config.yaml` **成功**（退出码 0）；`profiles/profile-a` 亦被同一用户读取成功。同一 OS 用户下所有 Profile 目录可互读（目录 700、文件 600 仅限制**其他用户**，不限制属主）。
- **创建命名 Profile 不改写默认 Profile 的 `config.yaml`。** 基线 sha256 `e817ae65…c1fa`，创建 3 个 Profile 后 sha256 **完全不变**，顶层键名集合也完全不变。唯一被 Hermes 自动写入的默认 Profile 状态是 `gateway_state.json`（记录 `served_profiles`），见 §6。
- **凭据不继承（文件级已验证）：** profile-a/b/c 各自的 `.env` 均为空模板（无任何 `KEY=` 行）；默认 Profile 的 `.env` 含 13 个键（含模型服务密钥名）。**多路复用运行时 fail-closed 未能在不影响默认网关的前提下验证**，如实标注为「未验证」并给出后续方案（§5.3、§7）。
- **本机容量（实测外推，须标注假设）：** 在「单进程多路复用 + 空闲（0 适配器、无消息流量）」口径下，网关内存 ≈ `426952 + 14061×(N−1)` kB；N=20 ≈ 678 MB，N=30 ≈ 815 MB，加 OS 基线约 600 MB 后总计约 1.28–1.42 GB，理论上可装入 1.9 GiB。**但该口径不含任何活动负载**（会话、模型调用、记忆检索、附件），且本机仅 2 vCPU，真实 20–30 并发用户在本机不可支撑。**生产容量结论必须在生产主机以真实负载取得（与 W0-2 A4/O1 一致）。**

## 2. 实验环境与基线快照

### 2.1 环境

- 主机：Debian trixie（Linux 6.12.107+deb13-amd64），`nproc = 2`，`MemTotal = 2020948 kB ≈ 1.927 GiB`，swap `3107320 kB ≈ 3.0 GiB`（zram + swapfile）。
- Hermes：`v0.22.1+72.g450ad50 (2026.9.24) · stable · local 450ad5055eb3`，安装目录 `/home/agents/.hermes/hermes-agent`。
- Profile 布局：默认 Profile 即 `/home/agents/.hermes` 本身；命名 Profile 位于 `/home/agents/.hermes/profiles/<name>/`。实验前 `profiles/` 不存在（与 W0-2 §2.3 一致）。
- 运行中的 Hermes 进程（实验前）：网关 `gateway run`（PID 472549）、TUI 客户端 `-c --tui`（PID 473904）、TUI 后端 `node … ui-tui/dist/entry.js`（PID 473955）。**默认网关即为多路复用宿主进程**（见 §3.3、§6）。

### 2.2 基线快照（时间戳 `2026-10-15T23:26:09+0800`）

```
$ free -m
               total        used        free      shared  buff/cache   available
Mem:            1973        1175         139          36         878         797
Swap:           3034           6        3027

$ grep -E 'MemTotal|MemAvailable|SwapTotal|SwapFree' /proc/meminfo
MemTotal:        2020948 kB
MemAvailable:     816956 kB
SwapTotal:       3107320 kB
SwapFree:        3100248 kB

$ ps -eo pid,rss,etime,comm | grep -E 'hermes|node'
 472549 426952   02:07:49 hermes        # gateway run（默认 Profile 网关 = 多路复用宿主）
 473904  86220   02:04:21 hermes        # TUI 客户端
 473955 199280   02:04:19 node-MainThread  # TUI 后端

$ cat /proc/472549/smaps_rollup | grep -E '^(Rss|Pss|Pss_Anon|Pss_File|Shared_Clean|Shared_Dirty|Private_Clean|Private_Dirty):'
Rss:              426952 kB
Pss:              421931 kB
Pss_Anon:         408860 kB
Pss_File:          13071 kB
Shared_Clean:       7576 kB
Shared_Dirty:          0 kB
Private_Clean:     10484 kB
Private_Dirty:    408892 kB

$ sha256sum /home/agents/.hermes/config.yaml
e817ae6592282b4cd11cd890dfd1782e2e45235cc43db777493114018f54c1fa  /home/agents/.hermes/config.yaml
（2412 行）

$ grep -E '^[A-Za-z_]+:' /home/agents/.hermes/config.yaml | sed 's/:.*/:/' | sort   # 仅键名
agent:  attachments:  auth:  browser:  code_execution:  compression:  _config_version:
cron:  database:  delegation:  display:  gateway:  group_sessions_per_user:  kanban:
known_builtin_toolsets:  known_plugin_toolsets:  max_concurrent_sessions:  memory:
model:  onboarding:  platform_toolsets:  plugins:  prompt_caching:  runtime:  skills:
streaming:  stt:  telemetry:  terminal:  tool_loop_guardrails:  updates:  web:
```

### 2.3 关键基线值（后续增量以此为准）

| 指标 | 基线值 |
|---|---|
| 时间戳 | 2026-10-15T23:26:09+0800 |
| MemAvailable | 816956 kB |
| SwapFree | 3100248 kB |
| 网关 PID | 472549 |
| 网关 RSS | 426952 kB |
| 网关 Pss | 421931 kB |
| 默认 config.yaml sha256 | e817ae6592282b4cd11cd890dfd1782e2e45235cc43db777493114018f54c1fa |
| 命名 Profile 数 | 0 |

## 3. 创建过程与命令

### 3.1 使用的子命令与帮助输出（真实片段）

```
$ hermes profile --help
usage: hermes profile [-h]
                      {list,use,create,delete,describe,show,alias,rename,purge-identity,migrate-identity,export,import,install,update,info} ...
    create              Create a new profile
    ...

$ hermes profile create --help
usage: hermes profile create [-h] [--clone] [--clone-all]
                             [--clone-from SOURCE] [--clone-channels]
                             [--sync-imports] [--no-alias] [--no-skills]
                             [--description DESCRIPTION]
                             profile_name

positional arguments:
  profile_name          Profile name (lowercase, alphanumeric)

options:
  --clone               Copy config.yaml, .env, SOUL.md, and skills from active profile ...
  --clone-all           Full copy of active profile ...
  --no-alias            Skip wrapper script creation
  --no-skills           Create an empty profile with no bundled skills ...
```

实际使用命令：`hermes profile create profile-a`（不传任何 `--clone*` 选项，即创建**空配置**的新 Profile，不复制默认 Profile 的配置/凭据/技能集）。三次创建输出均为：

```
Profile 'profile-a' created at /home/agents/.hermes/profiles/profile-a
58 bundled skills synced.
Wrapper created: /home/agents/.local/bin/profile-a

Next steps:
  profile-a setup              Configure API keys and model
  profile-a chat               Start chatting
  (served now by the running multiplexed gateway — add its bot token and it connects)

  ⚠ This profile has no API keys yet. Run 'profile-a setup' first,
    or it will inherit keys from your shell environment
```

（`profile-b`、`profile-c` 输出同构，仅名称不同；三次创建退出码均为 `0`。）

### 3.2 创建后目录结构（profile-a 为例）

```
$ ls -la /home/agents/.hermes/profiles/profile-a/
drwx------  19 agents agents 4096  .
drwxrwxr-x   3 agents agents 4096  ..
drwx------   2 agents agents 4096  audio_cache
drwxrwxr-x   3 agents agents 4096  backups
drwxrwxr-x   2 agents agents 4096  cache
-rw-------   1 agents agents  163  config.yaml
drwx------   3 agents agents 4096  cron
-rw-------   1 agents agents  165  .env
-rw-rw-r--   1 agents agents    0  .env.lock
-rw-------   1 agents agents  207  gateway.lock
drwxrwxr-x   2 agents agents 4096  home
drwx------   2 agents agents 4096  hooks
drwx------   2 agents agents 4096  image_cache
drwx------   3 agents agents 4096  logs
drwx------   2 agents agents 4096  memories
drwx------   2 agents agents 4096  pairing
drwxrwxr-x   2 agents agents 4096  plans
drwxrwxr-x   3 agents agents 4096  platforms
drwxrwxr-x   3 agents agents 4096  runtime
drwx------   2 agents agents 4096  sessions
drwx------  14 agents agents 4096  skills
drwxrwxr-x   2 agents agents 4096  skins
-rw-rw-r--   1 agents agents  667  SOUL.md
-rw-------   1 agents agents 315392 state.db
...（state.db-shm / state.db-wal / *.lock 等）
drwxrwxr-x   2 agents agents 4096  workspace
```

目录总大小 `du -sh` = **5.3 MB**（三个 Profile 相同，主要是 58 个内置技能）。profile-a 的 `config.yaml` 仅含 `model:` 与 `_config_version:` 两个顶层键（163 字节），**不含任何凭据**；`.env` 为 165 字节的空模板（无 `KEY=` 行）。

### 3.3 服务方式：单进程多路复用（关键发现）

创建输出直接提示 `(served now by the running multiplexed gateway — add its bot token and it connects)`。网关日志与状态文件证实：

```
$ grep -i 'profile-a|profile-b|profile-c|multiplex' /home/agents/.hermes/logs/gateway.log | tail
2026-10-15 23:26:19,695 INFO gateway.run_profile_reconcile: [MULTIPLEX] Now serving profile 'profile-a' (0 adapter(s) connected; control-socket)
2026-10-15 23:27:00,615 INFO gateway.run_profile_reconcile: [MULTIPLEX] Now serving profile 'profile-b' (0 adapter(s) connected; control-socket)
2026-10-15 23:28:19,347 INFO gateway.run_profile_reconcile: [MULTIPLEX] Now serving profile 'profile-c' (0 adapter(s) connected; control-socket)
```

默认 Profile 的 `config.yaml` 的 `gateway:` 段在**基线时已包含** `multiplex_profiles`、`auto_multiplex_migration`、`profile_routes` 三个键（sha256 未变即证明为基线既有）。因此默认网关在创建 Profile 后**自动**把这些命名 Profile 纳入同一进程的服务集合，**未产生任何新的 hermes 进程**（见 §4）。

## 4. 资源实测

### 4.1 每步测量原始值

| 步骤 | 时间戳 | MemAvailable (kB) | SwapFree (kB) | 网关 RSS (kB) | 网关 Pss (kB) | 新进程 | Profile 目录 |
|---|---|---|---|---|---|---|---|
| 基线（仅 default） | 23:26:09 | 816956 | 3100248 | 426952 | 421931 | — | — |
| +profile-a | 23:26:20 / 23:26:32 | 800048 | 3100248 | 440200 | 435176 | 无 | 5.3 MB |
| +profile-b | 23:27:01 / 23:27:07 | 780196 | 3099480 | 453300 | 448276 | 无 | 5.2 MB |
| +profile-c | 23:28:19 | 807596 | 3098456 | 469136 | 464115 | 无 | 5.2 MB |
| 稳态（3 次采样） | 23:28:52–23:29:00 | — | — | 470488 / 466276 / 467984 | 465473 / 461261 / 462969 | 无 | — |
| 收尾 | 23:29:54 | 796336 | 3098456 | 470680 | 465658 | 无 | — |

### 4.2 边际内存增量（关键结论）

以网关进程 RSS/Pss 为准（`MemAvailable` 受本机其它活动干扰，仅作辅助）：

| 步骤 | ΔRSS (kB) | ΔPss (kB) |
|---|---|---|
| default → +profile-a | **+13248** | **+13245** |
| +profile-a → +profile-b | **+13100** | **+13100** |
| +profile-b → +profile-c | **+15836** | **+15839** |
| 3 个 Profile 合计 | **+42184 (41.20 MB)** | **+42184 (41.20 MB)** |
| **每 Profile 均值** | **14061 kB ≈ 13.73 MB** | **14061 kB ≈ 13.73 MB** |

**判定：本机实测下，多路复用网关内每新增一个（空闲被服务）Profile 的边际内存约 14 MB，近似线性但斜率远小于「每进程 405 MB」。** 三个增量分别为 13.2 / 13.1 / 15.8 MB，波动约 ±2 MB（JIT/分配器/技能元数据加载抖动），无随 N 增大的加速上升趋势。**因此 W0-2 §5.2 把「每 Profile ≈ 405 MB」当作边际成本并按此线性外推是错的（对多路复用拓扑）；405 MB 是该**单进程共享基线**，不是边际值。**

### 4.3 RSS 与 Pss 的差异与共享页影响

- 本拓扑中 `RSS − Pss` 恒为 **约 5021 kB**（基线 426952−421931=5021；收尾 470680−465658=5022）。该差值即网关进程与其他进程（libc、系统库等只读代码页）共享的部分（`Shared_Clean ≈ 7576 kB`）。
- **为什么 RSS ≈ Pss：** 全部 4 个 Profile 由**同一个进程**服务，只存在一个页消费者，不存在「多进程各计入一遍共享页」的重复计数。故本拓扑下 **RSS 与 Pss 均可直接用作内存预算，二者近似等价**。
- **共享页的真实来源是「进程内共享」**：Python 解释器、已加载模块、58 个内置技能的代码与元数据在同一地址空间被所有 Profile 复用，这正是边际增量只有约 14 MB 的原因。若改为「每 Profile 一个独立网关进程」，则每个进程各有一份解释器与模块（RSS 约 417 MB/进程），此时跨进程共享页才会让「简单累加 RSS」高估实际占用、需用 Pss 校正。
- **边际增量主要构成（定性）：** 每个新 Profile 触发网关加载其 `config.yaml`（163 字节级）、`state.db`（315 KB 级）、58 个内置技能的元数据与 `SOUL.md`，并建立会话/路由句柄；这些增量以匿名私有页为主（`Private_Dirty` 从 408892 → 452228 kB）。

### 4.4 内存增长是否线性（用实测数据回答）

- **近似线性，但斜率是约 14 MB/Profile，不是约 405 MB/Profile。** 实测三点增量 13.2 / 13.1 / 15.8 MB 基本恒定，未见明显超线性上升。外推式（default 计为第 1 个 Profile）：`网关 RSS(N) ≈ 426952 + 14061×(N−1) kB`。
- **该线性结论的有效边界（重要）：** 仅适用于「**空闲**、0 适配器、无消息流量、单进程多路复用」口径。一旦 Profile 接入真实渠道并产生会话/模型调用/记忆检索/附件，其内存将由**活动负载**主导，远高于 14 MB 骨架值——该部分本机未测（§7 限制）。

### 4.5 本机可支撑 Profile 数量区间（外推，标注假设）

- **内存口径（空闲、单进程多路复用）：** 假设 OS/非 Profile 基线约 600 MB（沿用 W0-2 §5.1 估算），则
  - N=10 → 网关 ≈ 541 MB，总计 ≈ 1.14 GB；
  - N=20 → 网关 ≈ 678 MB，总计 ≈ 1.28 GB；
  - N=30 → 网关 ≈ 815 MB，总计 ≈ 1.42 GB。
  - 单就内存而言，本机（1.9 GiB）在**空闲口径**下可装入约 20–30 个被服务 Profile 并留约 0.5 GB 余量。**但这是「被服务但空闲」的骨架成本，不是「可并发服务 20–30 个活跃用户」。**
- **CPU / 共享命运口径（更现实的上限）：** 本机仅 **2 vCPU**；所有 Profile 挤在**一个网关进程**内，是单点故障与 CPU 争用的共享命运体。真实并发（有会话与模型调用）时，**2 vCPU 才是绑定约束**。故**本机实际仍只适合个位数 Profile 的冒烟/开发验证**，与章程「本地并行执行实例上限为 1」的工程约束一致。
- **对照 W0-2：** W0-2 §5.2 得「本机 ≈ 2–3 个 Profile」是建立在「每 Profile 405 MB」的**错误线性假设**上；本实测证明该假设在**多路复用拓扑**下不成立，但其「本机不足以承载 20 Profile 并发/100 用户」的**最终结论仍然成立**——原因从「内存不足」转为「活动负载未测 + 2 vCPU + 单进程共享命运」。

### 4.6 首期 20–30 人规模的内存外推（实测依据 + 显式假设）

**实测依据：** 单进程多路复用网关基线 426952 kB；每被服务（空闲）Profile 边际 14061 kB。

**外推假设（逐条标注）：**
1. 每人一个 Profile（章程 D1）；
2. 采用**单网关多 Profile 多路复用**（章程 D2；本机默认网关即此模式）；
3. 边际内存取实测空闲值 **14 MB/Profile**——**这是下界**；
4. 活动负载（会话、模型调用、记忆、附件、并发）的**每 Profile 增量未测**，不计入下界；
5. 计入 OS/其它常驻约 600 MB；
6. 不使用 swap（本机虽存在 3.0 GiB swap，常驻 Agent 换入换出会严重抖动，不计入「可支撑」）。

**外推结果（仅空闲口径，作为下界）：**
- 20 人：网关 ≈ 426952 + 14061×19 = **694117 kB ≈ 678 MB**；合计 ≈ **1.28 GB**。
- 30 人：网关 ≈ 426952 + 14061×29 = **834731 kB ≈ 815 MB**；合计 ≈ **1.42 GB**。

**结论与告诫：** 上表只是「20–30 个 Profile 被一个进程**空闲**服务」的内存下界；**它不是「20–30 个活跃用户」的容量结论**。生产主机规格必须由**真实负载测试**（接入渠道、产生会话与模型调用、观察 RSS/Pss 随并发增长曲线与峰值）确定，并计入 2 vCPU→生产 vCPU 的 CPU 约束。这与 W0-2 §5.3（章程 A4 / 开放决策 O1）一致。

## 5. 隔离边界实测结果

### 5.1 跨 Profile 文件互读（结论：无 OS 级隔离）

```
$ whoami
agents

$ cat /home/agents/.hermes/profiles/profile-b/config.yaml
model:
  default: deepseek-v4.1-flash
  provider: opencode-go
  base_url: https://opencode.ai/inference/openai/v1
  api_mode: chat_completions
_config_version: 51
EXIT=0

$ head -c 60 /home/agents/.hermes/config.yaml >/dev/null && echo "read default config OK"
read default config OK EXIT=0
```

- **结果：同一 `agents` 用户可读取任意其它 Profile 的 `config.yaml`（退出码 0）。** 这证实 W0-2 §3「Profile 不沙箱、`local` 后端下与运行账号同权限」的结论：**Profile 目录权限只限制其他 OS 用户，不限制属主账号**；同一账号下所有 Profile 互为可读。凡要求「员工 A 无法读取员工 B 的文件」的验收项，**必须**由部署层（独立 systemd `User=`、独立 UID、容器/namespace）补充。

### 5.2 目录与文件权限位（实测）

```
$ stat -c '%a %U %n' ...        # 默认 Profile
700 agents /home/agents/.hermes
600 agents /home/agents/.hermes/config.yaml
600 agents /home/agents/.hermes/.env
600 agents /home/agents/.hermes/auth.json
600 agents /home/agents/.hermes/SOUL.md
600 agents /home/agents/.hermes/state.db

$ stat -c '%a %U %n' ...        # 命名 Profile（a/b/c 一致）
700 agents /home/agents/.hermes/profiles/profile-a
600 agents /home/agents/.hermes/profiles/profile-a/config.yaml
600 agents /home/agents/.hermes/profiles/profile-a/.env
600 agents /home/agents/.hermes/profiles/profile-a/state.db
664 agents /home/agents/.hermes/profiles/profile-a/SOUL.md

775 agents /home/agents/.hermes/profiles     # 父目录
```

- 命名 Profile 目录 `700`、`config.yaml`/`.env`/`state.db` 均 `600`（仅属主可读写），与默认 Profile 的 `600` 一致；属主均为 `agents`。
- 差异点：命名 Profile 的 `SOUL.md` 为 `664`（组/其他可读），默认 Profile 的 `SOUL.md` 为 `600`。`SOUL.md` 非凭据，但这是**唯一**一处命名 Profile 比默认 Profile 更宽松的权限位，登记为观察项。
- 父目录 `profiles/` 为 `775`（其他用户可列出目录名），但各 Profile 子目录为 `700`，其他用户**无法进入**。综上：**权限位提供了「防其他 OS 用户」的弱边界，但不构成同账号内的隔离**（§5.1）。

### 5.3 凭据作用域（部分验证）

- **文件级（已验证）：** profile-a/b/c 的 `.env` **均无任何 `KEY=` 行**（空模板），即创建时**未复制**默认 Profile 的凭据。默认 Profile 的 `.env`（`600`，28276 字节）含 13 个键（含模型服务密钥名），这些键**未出现在**任何命名 Profile 中。

```
$ for p in profile-a profile-b profile-c; do echo -n "$p .env KEY names: "; grep -oE '^[A-Za-z_]+=' ~/.hermes/profiles/$p/.env; echo "(none)"; done
profile-a .env KEY names: (none)
profile-b .env KEY names: (none)
profile-c .env KEY names: (none)

$ grep -cE '^[A-Za-z_]+=' /home/agents/.hermes/.env    # 默认 .env 键数
13
```

- **运行时（未验证）：** 多路复用下二级 Profile 读取凭据时是否 fail-closed（不借用默认 Profile），本次**未能验证**。原因：网关日志显示各 Profile 均为 `0 adapter(s) connected`，无消息流量即不会触发按 Profile 的凭据解析路径；而要触发需接入真实渠道并投递消息，这既受「禁止连接真实渠道账号」约束，也会影响正在运行的默认网关。**因此该项如实标注为「未验证」，不得作为已通过结论。** 后续验证方案见 §7。
- 静态证据（W0-2 已列）：`agent/secret_scope.py` 的 context-local 秘密作用域 + `tests/gateway/test_weixin_secret_scope.py` 的 fail-closed 用例；本机未安装 pytest，未实际运行。

## 6. 默认 Profile 网关健康与副作用

### 6.1 网关健康对比

| 时点 | 网关 PID | 存活 | RSS (kB) | Pss (kB) |
|---|---|---|---|---|
| 基线 23:26:09 | 472549 | 是 | 426952 | 421931 |
| 收尾 23:29:54 | 472549 | 是 | 470680 | 465658 |

- **网关进程全程存活，PID 未变（472549），未重启、未停止。** 创建 3 个 Profile 期间**未产生任何新的 hermes 进程**；`ps` 中 hermes/node 进程数与基线一致（网关 1 + TUI 客户端 1 + TUI 后端 1）。
- 网关 RSS 从 426952 → 470680 kB（+42184 kB），即上述每 Profile 约 14 MB 的边际增量，属预期内。

### 6.2 对默认 Profile 的副作用

1. **`config.yaml`：无改动。** 基线 sha256 `e817ae6592282b4cd11cd890dfd1782e2e45235cc43db777493114018f54c1fa`，创建 3 个 Profile 后**逐字节一致**；顶层键名集合前后完全一致（对比 §2.2 与收尾输出）。
2. **`gateway_state.json`：被 Hermes 自动写入（唯一自动改动，已登记）。** 收尾时其内容为 `served_profiles = ['default','profile-a','profile-b','profile-c']`，`multiplex_standalone_reason = None`；mtime `2026-10-15 23:28:55`（在创建 profile-c 之后），大小 580 字节。这是网关在多路复用下**自动**记录服务集合，非人工写入。
3. **新增文件（Hermes 自动生成）：** 目录 `/home/agents/.hermes/profiles/{profile-a,profile-b,profile-c}/`；包装脚本 `/home/agents/.local/bin/{profile-a,profile-b,profile-c}`（`775`，内容为 `hermes -p <name>` 的转发）。
4. **未改动：** 默认 Profile 的 `.env`、`auth.json`、`SOUL.md`、`state.db`、`sessions/`、`memories/`、`skills/`、`cron/` 均未被创建 Profile 的动作改动（`.env` 键数仍为 13，`auth.json` 权限仍 `600`）。

### 6.3 结论

创建命名 Profile **未破坏、未重配、未重启默认 Profile 网关**；对默认 Profile 的唯一自动写入是 `gateway_state.json` 的 `served_profiles` 集合（Hermes 自身行为，符合任务授权例外，已如实记录）。

## 7. 清理命令、已知限制与后续

### 7.1 清理命令（**未执行**，保留给后续任务）

以下命令用于删除本次创建的测试 Profile；**本任务未执行**（保留现场给后续任务复用）：

```
# 方式一：用 Hermes 自带子命令（推荐，会同时清理 identity/session/路由残留与包装脚本）
hermes profile delete profile-a -y
hermes profile delete profile-b -y
hermes profile delete profile-c -y

# 方式二：若需手工清理（等价于方式一，谨慎使用）
rm -rf /home/agents/.hermes/profiles/profile-a
rm -rf /home/agents/.hermes/profiles/profile-b
rm -rf /home/agents/.hermes/profiles/profile-c
rm -f  /home/agents/.local/bin/profile-a
rm -f  /home/agents/.local/bin/profile-b
rm -f  /home/agents/.local/bin/profile-c

# 删除后复核：profiles/ 目录与 served_profiles 应不再包含测试 Profile
hermes profile list
```

（`hermes profile delete --help` 实测：`usage: hermes profile delete [-h] [-y] profile_name`，`-y/--yes` 跳过确认。删除属数据删除类操作，按 AGENTS.md §5 应经项目所有者决定后执行。）

### 7.2 已知限制

1. **活动负载未测：** 本实验各 Profile 均为 `0 adapter(s) connected`、无消息流量，所测 14 MB/Profile 是**空闲骨架**边际值，不含会话/模型调用/记忆/附件成本。真实并发容量不能由本实验直接给出。
2. **多路复用运行时 fail-closed 未验证：** 见 §5.3；需在不影响默认网关的前提下触发按 Profile 的凭据解析路径（§7.3）。
3. **样本量小：** 仅 3 个新增 Profile、单次实验、约 3 分钟窗口；边际增量存在约 ±2 MB 抖动。更长时段、更多 Profile 的复测可提高置信度。
4. **`MemAvailable` 噪声：** 受本机其它活动影响，基线 816956 kB 到收尾 796336 kB 的变化（约 20 MB）与网关 RSS 增量（约 41 MB）量级不同，故**以网关 RSS/Pss 为准**，`MemAvailable` 仅作辅助。
5. **单机单次：** 结论绑定本机（2 vCPU / 1.9 GiB / zram+swapfile）与 Hermes `v0.22.1+72.g450ad50`；换版本或主机须复测。
6. **CPU 未做压测：** 2 vCPU 是 20–30 并发用户的现实约束，本实验未对 CPU 做负载测试。

### 7.3 后续验证方案

1. **多路复用凭据 fail-closed 运行时验证（优先级最高，且须不触碰默认网关）：** 在**独立的临时网关实例**（`HERMES_HOME` 指向一个隔离的临时目录、使用独立端口/socket、不连接任何真实渠道）内，令某命名 Profile 缺失 `WEIXIN_TOKEN`/`WEIXIN_ALLOWED_USERS`，注入一条合成入站事件，断言其**不借用**默认 Profile 的环境值且 fail-closed（对齐 `test_weixin_secret_scope.py`）。此路径完全不触及生产默认网关。
2. **多 Profile 活动负载曲线（须生产主机）：** 在目标主机接入渠道，令 20/30 个 Profile 同时产生会话与模型调用，采样每 Profile 的 RSS/Pss 随并发增长曲线、峰值内存、CPU 与网关稳定性；据此确定生产规格（章程 A4 / O1）。
3. **跨 Profile 内核级隔离负面测试（须生产环境）：** 以独立 systemd `User=`/容器部署多 Profile，实测「员工 A 无法读取员工 B 的文件/进程/网络/临时目录」。
4. **权限位一致性修复建议：** 命名 Profile 的 `SOUL.md` 为 `664`（默认 Profile 为 `600`）；虽非凭据，建议在生产加固时统一收紧为 `600`。

---

**附：本报告未包含任何凭据、token 或真实账号标识；所有 Profile 名（profile-a/b/c）与键名均为实验占位或非敏感标识。**
