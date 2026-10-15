# W0-2 Profile 隔离方案与身份路由协议草案

- 任务：W0-2（Profile 隔离方案与身份路由协议）
- 分支：task/W0-2-profile-isolation
- 输入：`docs/research/W0-1-weixin-ilink.md`（微信渠道能力与约束，本任务以其为前置约束）
- 范围：只读调研本机 Hermes 安装目录（`/home/agents/.hermes/hermes-agent`，只读参考）的源码、测试与随附文档，并做本机资源基线实测。
- 安全声明：本任务未读取任何凭据值，未新建/启动/停止任何 Profile，未执行任何写状态的 `hermes` 子命令；报告中一律使用占位符（如 `<ACCOUNT_ID>`、`<CHANNEL_USER_ID>`、`<EMPLOYEE_ID>`、`<PROFILE>`）。
- 证据声明：本文严格区分「代码与文档级证据」与「本机实测证据」；凡标注「须生产主机实测」的项不得在本机冒充最终结论。

## 1. 结论摘要

- **Hermes 原生隔离的是「数据与凭据作用域」，不是「操作系统边界」。** 每个 Profile 是一个独立的 Hermes home（默认 Profile 即 `~/.hermes` 本身，命名 Profile 在 `~/.hermes/profiles/<name>/`），拥有各自的 `config.yaml`、`.env`、`SOUL.md`、记忆、会话、技能、cron 与 `state.db`（`profiles.md:7`、`profiles.md:11`、`profiles.md:433`）。进程内以 `HERMES_HOME` 与 `agent/secret_scope.py` 的 context-local 凭据作用域为边界，跨 Profile 读取凭据会 fail-closed（`secret_scope.py:1-11`、`secret_scope.py:219-247`）。
- **Profile 不做沙箱。** 官方文档明确：Profile 只给 Hermes 自己的状态目录，**不限制文件系统访问**；在默认 `local` 终端后端下，Agent 拥有与运行账号相同的文件系统权限（`profiles.md:228-252`）。因此「员工之间不得互相读到对方文件/进程/网络」这一层**只能由部署层补充**（独立 systemd 服务账户 `User=`、容器、namespace、独立机器/VM）。
- **配置 / 会话 / 记忆 / 技能 / 凭据 / 会话命名空间 / 日志 / cron 属于 Hermes 原生隔离**，且在多路复用（一个网关进程服务多个 Profile）下逐项解析为「该 Profile 自己的值」，缺失时 fail-closed 而非借用默认 Profile（`multi-profile-gateways.md:658-689`、`multi-profile-gateways.md:578-628`、`test_weixin_secret_scope.py:36-45`、`test_weixin_secret_scope.py:135-149`）。
- **工作目录与临时目录、内部 API 身份、进程/网络/文件系统边界属于部署层补充。** 工作目录由 `terminal.cwd` 单独控制，与 `HERMES_HOME` 无关（`profiles.md:233`、`profiles.md:405`）；临时目录在默认 `local` 后端沿用进程 `TMPDIR`，未按 Profile 隔离。`terminal.home_mode: profile` 可让工具子进程的 `HOME` 指向 `{HERMES_HOME}/home`，从而隔离 `~/.ssh`/`~/.gitconfig` 等 CLI 身份，但这是 opt-in 且仍不构成沙箱（`profiles.md:407-431`）。
- **身份路由必须自建、且必须是拒绝式授权。** `gateway.profile_routes` 是**选路**（未命中落默认 Profile），官方文档明确它「is not deny-by-default authorization」（`multi-profile-gateways.md:814-818`）；`user_id` 的可靠性取决于接入端是否鉴权（`multi-profile-gateways.md:803-808`）。故「未绑定/已解绑身份默认拒绝」必须由 `WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS`（真正的拒绝式入站过滤）叠加**业务层 (渠道, 渠道用户 ID) → 内部员工 ID 绑定表**实现，不得把 `profile_routes` 当授权（与 W0-1 §6.5 一致）。
- **本机容量：实测单 Profile 常驻网关进程 RSS 约 405 MB（区间 405–409 MB），本机（2 vCPU / 1.9 GiB）可支撑约 2–3 个 Profile 常驻，不足以承载 20 Profile 并发或 100 用户。** 20 Profile 并发与 100 用户容量结论**必须在生产主机取得**（章程 A4；开放决策 O1）。
- **须实测项**：单 Profile 内存的稳定区间、多 Profile 并发下网关 RSS 是否线性增长、多路复用下路由端到端行为、以及生产主机上的 20/100 容量——本机只能给出外推起点，不能作为验收证据。

## 2. 证据基础

### 2.1 Hermes 版本与安装路径（只读）

- 命令：`hermes --version` → `Hermes Agent v0.22.1+72.g450ad50 (2026.9.24) · stable · local 450ad5055eb3`；`Install directory: /home/agents/.hermes/hermes-agent`；`Install method: git`；`Python: 3.14.7`。
- 安装目录（只读参考，未做任何修改）：`/home/agents/.hermes/hermes-agent`。

### 2.2 Profile 机制相关文件（路径 + 行号）

| 关注点 | 文件与行号 | 关键事实 |
|---|---|---|
| Profile 定义与身份标记 | `website/docs/user-guide/profiles.md:7,11,433` | Profile = 独立 Hermes home；默认 Profile 即 `~/.hermes`；目录须带身份标记才算 Profile |
| 身份标记清单 | `hermes_constants.py:366-397` | `_PROFILE_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")`；`named_profile_has_identity` / `named_profile_has_servable_identity` |
| Profile 与沙箱的区别 | `profiles.md:228-252` | Profile 不沙箱 Agent；`local` 后端下与运行账号同权限；`SOUL.md` 不构成工作区边界 |
| `HERMES_HOME` 边界与 `HOME` | `profiles.md:403-433` | 状态按 `HERMES_HOME` 隔离；`HOME` 默认保持真实用户主目录，`terminal.home_mode: profile` 为 opt-in |
| 独立网关进程 / 服务名 | `profiles.md:254-290`；`hermes_cli/gateway.py:2310-2315` | 每 Profile 独立进程与独立 bot token；systemd 名 `hermes-gateway` / `hermes-gateway-<profile>` |
| 服务账户与 unit 绑定 | `hermes_cli/gateway_service_owner.py:1-60` | systemd unit 绑定 `HERMES_HOME` 与 `User=`；写入者须代表该 home |
| 路由数据类与匹配 | `gateway/profile_routing.py:55-101` | `ProfileRoute.matches` 合取匹配；`specificity`（`:69-72`）= 16·user_id + 8·thread_id + 4·chat_id + 2·guild_id |
| 路由解析与查找 | `gateway/profile_routing.py:133-185` | `parse_profile_routes`（按 specificity 降序）、`match_profile_route`（返回首个最具体命中或 None） |
| 路由拒绝异常 | `gateway/profile_routing.py:51-52` | `ProfileRouteRejected`：显式路由命中但目标 Profile 未被服务 |
| 路由调用与 served-set 校验 | `gateway/run.py:4206-4235` | 命中后校验目标在 `_multiplex_profile_homes` 内，否则抛 `ProfileRouteRejected` |
| 路由文档 | `multi-profile-gateways.md:749-870` | `profile_routes` 需 `multiplex_profiles: true`；未命中落默认 Profile；`user_id` 非拒绝式授权 |
| 逐项隔离清单 | `multi-profile-gateways.md:658-689` | 凭据/授权/slash 门禁/HTTP 端点/MCP 子进程/会话命名空间/日志/终端沙箱/命令审批等逐项按 Profile 解析，缺失 fail-closed |
| 出站身份 | `multi-profile-gateways.md:622-628` | 出站用该 Profile 自己的 bot，无则明确失败，绝不回退默认 bot |
| 会话命名空间与分库 | `multi-profile-gateways.md:550-567` | 默认 Profile 用 `agent:main:…`，命名 Profile 用 `agent:<profile>:…`；各行落各自 `profiles/<name>/state.db` |
| 凭据作用域实现 | `agent/secret_scope.py:1-11,115-155,219-247` | context-local 秘密作用域；多路复用下无作用域读取抛 `UnscopedSecretError`（fail-closed） |
| 微信凭据/授权作用域测试 | `tests/gateway/test_weixin_secret_scope.py:36-45,77-88,135-149` | 二级 Profile 无 `WEIXIN_TOKEN`/`WEIXIN_ALLOWED_USERS` 时不得借用默认 Profile 的环境值 |
| 微信 env → extra 映射 | `gateway/config_env.py:604-613` | `WEIXIN_TOKEN`/`WEIXIN_ACCOUNT_ID`/`WEIXIN_DM_POLICY`/`WEIXIN_ALLOWED_USERS`/`WEIXIN_GROUP_*` 映射 |
| 统一入站事件 | `gateway/platforms/event.py:24-103` | `MessageType`（TEXT/PHOTO/VIDEO/AUDIO/VOICE/DOCUMENT/…）与 `MessageEvent` 字段 |
| 会话来源 | `gateway/session.py:68-103` | `SessionSource` 字段，含 `profile`、`profile_route_rejected`、`scope_id`、`chat_type` |
| 身份解析 fail-closed | `gateway/session_identity.py:240-275` | 路由被拒 → `IdentityUnresolved`，事件被丢弃，不误投默认 Profile |
| 入站路由设计 | `website/docs/developer-guide/multiplexing-gateway.md:176-275` | 路由仅在多路复用下运行；目标不在 served 集合则丢弃；intake/delivery 两套 seam 分离 |

### 2.3 本机 `~/.hermes` 布局实测结果

- 命令与输出（如实记录）：
  - `ls -la /home/agents/.hermes/` → 顶层包含 `config.yaml`、`.env`、`auth.json`、`SOUL.md`、`state.db`、`sessions/`、`memories/`、`skills/`、`cron/`、`gateway.pid`、`gateway.sock`、`gateway_state.json` 等（即默认 Profile 的全部状态）。
  - `ls -la /home/agents/.hermes/profiles` → `ls: cannot access '/home/agents/.hermes/profiles': No such file or directory`（退出码 2）。
  - `stat /home/agents/.hermes/profiles` → `stat: cannot statx '/home/agents/.hermes/profiles': No such file or directory`（退出码 2）。
  - `ls -la /home/agents/.local/bin/` → 仅 `hermes`、`hermes-acp`、`hermes-agent` 三个包装脚本，**无任何 Profile 命令别名**。
  - `ls -la /home/agents/.config/systemd/user/ | grep -i hermes` → 仅 `hermes-gateway.service`（默认 Profile 的网关 unit），无 `hermes-gateway-<profile>.service`。
- 结论：**本机当前只存在默认 Profile（`~/.hermes` 本身），`profiles/` 子目录不存在，无命名 Profile、无 Profile 别名、无 Profile 级 systemd unit。** 这与 `profiles.md:433`（默认 Profile 就是 `~/.hermes`）一致。
- 运行中的 Hermes 进程（实测）：一个 `hermes gateway run`（默认 Profile 的网关）、一个 `hermes … -c --tui`（TUI 客户端）、一个 `node … ui-tui/dist/entry.js`（TUI 后端）。均属默认 Profile，未发现任何命名 Profile 进程。

### 2.4 资源基线采集（命令 + 时间戳 + 原始输出）

采集时间戳（CST，UTC+08:00）：

- 第一次：`2026-10-15T22:37:32+0800`
- 第二次：`2026-10-15T22:38:16+0800`
- 第三次：`2026-10-15T22:38:21+0800`

命令与输出：

```
$ date '+%Y-%m-%dT%H:%M:%S%z (%Z)'
2026-10-15T22:37:32+0800 (CST)

$ nproc
2

$ free -h
               total        used        free      shared  buff/cache   available
Mem:           1.9Gi       1.0Gi       376Mi       6.5Mi       726Mi       913Mi
Swap:          3.0Gi       1.7Mi       3.0Gi

$ grep -E 'MemTotal|MemAvailable|SwapTotal' /proc/meminfo
MemTotal:        2020948 kB
MemAvailable:     935556 kB
SwapTotal:       3107320 kB

$ ps -eo pid,rss,comm --sort=-rss | head -20   # 22:37:32
    PID   RSS COMMAND
 472549 408624 hermes
 473955 197692 node-MainThread
 473904 96316 hermes
   5532 65276 YDService
   5146 26808 barad_agent
 491723 20864 systemd-journal
   4333 13692 tat_agent
   5573 13420 YDLive
 492498 12940 sshd-session
 472525 12908 sshd-session
 481046 12736 sshd-session
 472536 12336 systemd
   5145 11876 barad_agent
      1 10804 systemd
   5138 10020 barad_agent
  34918  9972 sh
 492512  7464 sshd-session
 481053  7448 sshd-session
 472554  7444 sshd-session
```

```
$ ps -eo pid,rss,comm --sort=-rss | head -8    # 22:38:16
    PID   RSS COMMAND
 472549 405352 hermes
 473955 184736 node-MainThread
 473904 96316 hermes
   5532 65220 YDService
   5146 26808 barad_agent
 491723 20864 systemd-journal
   4333 13692 tat_agent

$ ps -eo pid,rss,comm --sort=-rss | head -8    # 22:38:21（5s 后）
    PID   RSS COMMAND
 472549 405364 hermes
 473955 185368 node-MainThread
 473904 96316 hermes
   5532 65264 YDService
   5146 26804 barad_agent
 491723 20864 systemd-journal
   4333 13692 tat_agent

$ ps -eo pid,rss,comm,args | grep -E 'hermes|node' | grep -v grep | awk '{print $1, $2, $3}'
472549 405364 hermes            # gateway run（默认 Profile 网关，多路复用宿主进程）
473904 96316  hermes            # TUI 客户端（-c --tui）
473955 185368 node-MainThread   # TUI 后端（ui-tui/dist/entry.js）

$ free -h                                       # 22:38:21
               total        used        free      shared  buff/cache   available
Mem:           1.9Gi       1.0Gi       387Mi       6.5Mi       727Mi       924Mi
Swap:          3.0Gi       1.7Mi       3.0Gi
```

进程计数与 RSS 汇总（实测）：

- `hermes` 进程数：2（`gateway run` ≈ 405–409 MB RSS；`-c --tui` ≈ 96 MB RSS）。
- `node` TUI 后端进程数：1（≈ 185–198 MB RSS）。
- **单 Profile 常驻网关进程 RSS 实测区间：约 405–409 MB**（三次采样：408624 / 405352 / 405364 kB）。该进程即默认 Profile 的网关，在多路复用下同时是宿主进程；由于本机无命名 Profile，该值等价于「单 Profile 网关基线」。
- 与章程 A5 的对照：A5 记为「单 Profile 常驻内存约 250 MB（实测 338/243/236 MB）」。**本机本次实测网关 RSS 明显高于 A5 的历史值（约 405 MB），A5 需按本次实测更新或标注采样时点差异。**
- **swap 事实澄清**：任务背景记为「无 swap」，但实测 `free`/`/proc/swaps` 显示本机存在 swap：`/dev/zram0`（partition，1010172 kB，priority 100）+ `/swapfile`（file，2097148 kB，priority 10），SwapTotal ≈ 3.0 GiB，已用仅 1.7 MiB。该项为**背景描述与实测的不一致**，须登记（见 §6）。
- 负载：`uptime` → `up 1 day, 9:45, 3 users, load average: 0.03, 0.12, 0.14`；CPU：`AMD EPYC 7K62 48-Core Processor`（宿主 CPU，本机配额 2 vCPU）。

## 3. 多 Profile 隔离方案

逐项给出隔离手段、强制方式、以及「Hermes 原生支持」还是「必须由部署层补充」。证据列给出源码/文档行号。

| 隔离项 | 隔离手段 | 强制方式 | 原生 / 部署层 | 证据 |
|---|---|---|---|---|
| 配置（`config.yaml`、`SOUL.md`、`profile.yaml`） | 每个 Profile 独立 home 目录，按 `HERMES_HOME` 解析；多路复用下每轮按被路由 Profile 重新解析，缺失用文档默认值而非宿主值 | 文件系统（各 Profile 私有目录）+ 进程内 `HERMES_HOME` 覆盖；目录权限 | **原生** | `profiles.md:7,11,403-433`；`multi-profile-gateways.md:658-689` |
| 会话（历史、transcript、`state.db`） | 会话命名空间 `agent:<profile>:…`（默认 `agent:main:…`）；各行落各自 `profiles/<name>/state.db` | 进程内命名空间 + 每 Profile 独立 SQLite 文件 | **原生** | `multi-profile-gateways.md:550-567` |
| 记忆（`memories/`、外部记忆 provider） | 每 Profile 独立 `memories/`；外部 provider 身份（如 `MEM0_USER_ID`、`HINDSIGHT_BANK_ID`）从该 Profile `.env` 读取，缺失用 provider 的 per-profile 默认 | 文件系统 + context-local 秘密作用域 | **原生** | `profiles.md:11,83`；`multi-profile-gateways.md:630-643` |
| 技能（`skills/`、插件） | 每 Profile 独立 `skills/`；`hermes update` 同步内置技能但**不覆盖用户修改**；插件安装记录随 Profile | 文件系统 | **原生** | `profiles.md:11,327-335` |
| 凭据（`.env`、`auth.json`、`${VAR}` 引用、MCP token） | context-local 秘密作用域：多路复用下无作用域读取抛 `UnscopedSecretError`；有作用域但未命中返回默认值而**绝不**借用默认 Profile 的 `os.environ` | 进程内 contextvar + fail-closed 读取；文件系统权限（`auth.json`/`.env` 为 `-rw-------`） | **原生** | `agent/secret_scope.py:1-11,219-247`；`test_weixin_secret_scope.py:36-45,77-88`；`multi-profile-gateways.md:665` |
| 凭据（OS 级共享） | 默认 `local` 后端下工具子进程 `HOME` 仍是真实用户主目录，故 `~/.ssh`/`~/.gitconfig`/`~/.config/gh` 等**跨 Profile 共享**；`terminal.home_mode: profile` 可让 `HOME={HERMES_HOME}/home` | 需显式配置 `terminal.home_mode: profile` 并各自初始化 CLI 凭据；仍非沙箱 | **部署层补充**（opt-in） | `profiles.md:407-431` |
| 工作目录 | 由 `terminal.cwd` 独立控制，与 `HERMES_HOME` 无关；未设时 `local` 后端用 `$HOME`；`cwd: "."` 表示启动目录而非 Profile 目录 | 每 Profile `config.yaml` 显式设绝对 `terminal.cwd` | **部署层补充** | `profiles.md:233,238-246,405`；`multi-profile-gateways.md:677` |
| 临时目录 | 默认沿用进程 `TMPDIR`，**不按 Profile 隔离**；容器后端用 `{HERMES_HOME}/home` 持久化工具状态 | 部署层为每 Profile 设置独立 `TMPDIR`/独立容器/namespace | **部署层补充** | `profiles.md:407-411`；`multi-profile-gateways.md:677` |
| 内部 API 身份（HTTP 端点） | 每 Profile 自有 `API_SERVER_KEY`、`profile:` 绑定 webhook 路由与 `/p/<profile>/` 前缀；错误密钥 `401/403`，无适配器 `404/502/503`，**绝不回退默认 Profile 的 bot** | 每 Profile 独立监听配置 + served-set 校验 | **原生**（HTTP 层） | `multi-profile-gateways.md:668-669` |
| 内部 API 身份（进程/系统账号） | Hermes 不提供跨 Profile 的进程/账号隔离；同一 OS 用户下所有 Profile 进程可互访文件、socket、端口 | 独立 systemd 服务账户（`User=`）、独立 UID、容器、namespace、独立主机 | **部署层补充** | `hermes_cli/gateway_service_owner.py:44-60`；`profiles.md:228-252` |
| 文件系统边界（沙箱） | Profile **不沙箱**；`local` 后端与运行账号同权限，可访问 Profile 目录之外的路径 | 容器/namespace/独立账户/独立主机 | **部署层补充** | `profiles.md:228-252` |
| 进程边界 | 每 Profile 独立网关进程与服务名 `hermes-gateway-<profile>`；多路复用下则为一个宿主进程服务多 Profile | systemd/launchd/s6 独立 unit；多路复用为单进程多租户 | **原生**（进程编排）；**部署层**（账号隔离） | `profiles.md:254-290`；`gateway.py:2310-2315` |
| 日志 | `agent.log`/`errors.log`/`gateway.log` 落各 Profile 自己的 home | 文件系统 | **原生** | `multi-profile-gateways.md:675` |
| cron | 每 Profile 独立 ticker；多路复用下共享宿主但按 Profile 分别 tick；cron 投递按位置路由限定 | 进程内 ticker + `chat_id`/`thread_id` 路由校验 | **原生** | `multi-profile-gateways.md:572,861-870` |

**强制方式小结**：Hermes 原生强制依赖「文件系统目录权限 + 进程内 `HERMES_HOME`/秘密作用域 contextvar + fail-closed 读取」；**它不提供内核级隔离**。凡要求「一个员工无法读取另一个员工的文件、进程、网络或临时数据」的验收项，**必须**由部署层以独立 systemd 服务账户（`User=`）、独立 UID、容器或 namespace 强制，且须以负面测试证明（见 §6）。

## 4. 身份路由协议草案

本节对齐 `gateway/platforms/event.py` 的 `MessageEvent`/`MessageType` 与 `gateway/session.py` 的 `SessionSource`，并给出业务层归一化后的自有事件模型与路由规则。所有渠道标识以占位符表示。

### 4.1 统一消息事件 schema

**渠道层字段（对齐 `MessageEvent`，`event.py:44-103`）**：

- `text`：文本内容。
- `message_type`：`MessageType` 枚举（`TEXT`/`LOCATION`/`PHOTO`/`VIDEO`/`AUDIO`/`VOICE`/`DOCUMENT`/`STICKER`/`COMMAND`）。
- `user_id` / `user_name`：**渠道侧**发送者标识，**不是**内部员工 ID。
- `message_id`：渠道侧消息标识（可能缺失）；`reply_to_message_id` / `reply_to_text` / `reply_to_author_id` / `reply_to_author_name` / `reply_to_is_own_message`：被引用消息信息。
- `media_urls[]`（本地缓存路径）、`media_types[]`（MIME）、`media_text_inlined[]`：附件引用。
- `timestamp`：时间戳。
- `source`（`SessionSource`，`session.py:68-103`）：`platform`、`chat_id`、`chat_name`、`chat_type`（`dm`/`group`/`channel`/`thread`）、`user_id`、`user_name`、`thread_id`、`scope_id`（平台中立作用域，Discord guild/Slack workspace/Matrix server）、`guild_id`（`scope_id` 的兼容别名）、`parent_chat_id`、`message_id`、`profile`、`profile_route_rejected`。
- `metadata`：自由元数据（插件须 `.get()`）。

**业务层归一化后字段（建议）**：

| 字段 | 类型 | 说明 | 来源 |
|---|---|---|---|
| `event_id` | string | 本系统为该入站事件分配的唯一 ID | 生成 |
| `channel` | enum | 渠道标识（首期 `weixin`） | `source.platform` |
| `channel_account_id` | string（占位 `<ACCOUNT_ID>`） | 接入账号标识 | 适配器配置 |
| `channel_user_id` | string（占位 `<CHANNEL_USER_ID>`） | 渠道侧发送者 ID，**跨渠道不复用** | `source.user_id` |
| `channel_chat_id` | string | 渠道会话 ID（微信 DM 下等于 `channel_user_id`） | `source.chat_id` |
| `chat_type` | enum | `dm`/`group`/`channel`/`thread` | `source.chat_type` |
| `message_id` | string? | 渠道侧消息 ID（可能缺失） | `event.message_id` |
| `text` | string | 文本内容 | `event.text` |
| `message_type` | enum | 归一化消息类型 | `event.message_type` |
| `media_refs[]` | list | 附件引用：`{local_path, mime, sha256}` | `media_urls`/`media_types` + 计算摘要 |
| `reply_to` | object? | 被引用消息的 `{message_id, text, author_id}` | `reply_to_*` |
| `timestamp` | datetime | 事件时间戳 | `event.timestamp` |
| `dedup_key` | string | 入站去重键（见 4.4） | 由渠道字段派生 |
| `employee_id` | string?（占位 `<EMPLOYEE_ID>`） | **解析后**的内部员工 ID；未绑定为 null | 绑定表解析 |
| `target_profile` | string?（占位 `<PROFILE>`） | **解析后**的目标 Hermes Profile；未绑定/未授权为 null | 绑定表解析 |
| `binding_state` | enum | `bound` / `unbound` / `revoked` / `ambiguous` | 绑定表解析 |
| `authorized` | bool | 是否通过拒绝式授权（allowlist + 绑定校验） | 业务层 |

### 4.2 路由规则：`(渠道, 渠道用户 ID) → 内部员工 ID → 目标 Profile`

路由在**渠道适配层与业务后端之间**执行，分四步，全部为**先解析身份、后处理业务**：

1. **归一化与账号校验**：适配器把渠道消息归一化为 4.1 的业务事件；校验 `channel_account_id` 属于已登记的接入账号，否则丢弃。
2. **入站拒绝式过滤（第一道）**：由 `WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS` 实现——未列入白名单的发送者在**适配器层**即被拒绝，不进入业务层（这是真正的 deny-by-default，`multi-profile-gateways.md:816-818`；与 W0-1 §6.5 一致）。群策略默认 `disabled`（W0-1 §3）。
3. **绑定查找**：以复合键 `(channel, channel_user_id)` 在**业务绑定表**中查 `employee_id`（`employee_id` 由系统管理员在最小管理后台维护，章程用户角色含「系统管理员：管理员工、角色、身份绑定与 Profile 生命周期」）。**绝不跨渠道复用** `channel_user_id`：微信 `from_user_id` ≠ 企业微信 userid ≠ 钉钉 staffId ≠ 飞书 open_id ≠ 邮箱（W0-1 §6.5）。
4. **Profile 解析**：由 `employee_id` 解析出该员工的 `target_profile`（章程 D1「每人独立 Hermes Profile」）。校验目标 Profile **已安装且被服务**；不满足则**拒绝**（对齐 `gateway/run.py:4216-4233` 与 `session_identity.py:249-261` 的 `ProfileRouteRejected` → 丢弃事件语义），**绝不**回退默认 Profile。

**与 Hermes 原生路由的衔接**：`gateway.profile_routes` 仍可用作**二级选路**（把共享 bot 的某个发送者路由到目标 Profile），但它**不是**授权来源；业务绑定表才是授权真源。`profile_routes` 需 `gateway.multiplex_profiles: true`，否则被忽略（`multi-profile-gateways.md:855-859`）。多路复用下，未命中任何 route 的发送者会落到默认 Profile——因此**默认 Profile 必须保持「无业务权限」的空壳配置**，任何落入默认 Profile 的未绑定消息都应在业务层被再次拒绝。

### 4.3 未绑定 / 已解绑身份的拒绝行为（默认拒绝）

- **默认拒绝（deny-by-default）**：`binding_state ∈ {unbound, revoked, ambiguous}` 或 `authorized = false` 时，业务层**拒绝**该事件：不创建业务记录、不调用模型、不投递任何业务回复，只记录一条审计事件（含 `(channel, channel_user_id)` 的**哈希**而非明文）。
- **不得依赖 `profile_routes` 的选择性路由当授权**：官方明确 `user_id` 路由「is not deny-by-default authorization」，未命中会落到默认/活动 Profile（`multi-profile-gateways.md:803-818`）。因此授权判定**必须**由 `WEIXIN_ALLOWED_USERS` 白名单 + 业务绑定表共同给出，且两者都必须 fail-closed。
- **解绑即时生效**：管理后台解除绑定时，须同时从 `WEIXIN_ALLOWED_USERS` 与业务绑定表移除，使后续消息在**第一道适配器过滤**即被拒绝；已绑定但被解绑身份的在途消息以 `binding_state = revoked` 拒绝。
- **歧义拒绝**：同一 `(channel, channel_user_id)` 映射到多个 `employee_id`（数据异常）时按 `ambiguous` 拒绝，不猜测。
- **审计**：所有拒绝写审计（拒绝原因、时间、渠道、身份哈希），满足章程「未绑定或已解绑的渠道身份默认拒绝」的验收项。
- **失败即拒绝**：绑定表/Profile 解析服务不可用或超时时，一律拒绝，不降级为放行。

### 4.4 去重键与幂等键与路由的绑定

- **入站去重键**（对齐 W0-1 §6.2）：主键 `(channel, channel_account_id, message_id)`；副键内容指纹 `(channel, channel_user_id, md5(text))`，因上游会用**新 `message_id` 重发相同文本**（`weixin.py:877-889`，去重窗口 TTL 300s，`weixin.py:58`）。
- **业务幂等键必须至少覆盖入站去重键**：`idempotency_key = hash(channel, channel_account_id, message_id)`（`message_id` 缺失时退回内容指纹键）。重复投递不得重复创建报销单/审批任务（章程验收标准）。
- **与路由的绑定关系**：
  - **先路由、后幂等**：先解析 `employee_id`/`target_profile`，再用 `idempotency_key` 去重；这样「同一 `message_id` 但发送者不同」的异常可被发现。
  - **冲突即拒绝**：若同一 `idempotency_key` 再次到达时携带**不同的** `channel_user_id`/`employee_id`，判定为**路由冲突**并拒绝（记审计），**不得**在第二个身份下静默处理——防止跨身份串单。
  - **幂等键不含 Profile**：`idempotency_key` 由渠道字段派生，不掺入 `target_profile`；否则 Profile 变更会导致同一消息被重复处理。Profile 归属由绑定表在解析阶段决定，并随事件落审计。
  - **出站幂等**：出站用适配器生成的 `client_id`（如 `hermes-weixin-<uuid>`，W0-1 §6.4），重试不产生重复消息。
- **至少一次（at-least-once）语义**：业务层以 `idempotency_key` 为幂等锚点，容忍重复投递与重试；主动推送失败（`session not ready`，W0-1 §6.4）走事务性 Outbox（章程 D5）+ 后台 Worker 重试，不阻塞业务事务。

### 4.5 与 W0-1 §6.2 / §6.5 的一致性

- **§6.2 去重键**：本协议 4.4 直接采用 W0-1 §6.2 的主/副键与 TTL，并追加「幂等键必须覆盖去重键」「路由冲突即拒绝」两条路由侧约束。
- **§6.5 多员工路由不得假定的地方**：本协议逐条对齐——
  - 不得假定 `user_id` 即授权 → 4.2 步骤 2/3 以 allowlist + 绑定表为授权真源，`profile_routes` 仅选路（4.3）。
  - 不得假定同一 bot 可被多进程并发轮询 → 共享渠道接入走**单网关多 Profile 多路复用**，而非每 Profile 各自登录同一 bot（章程 D2；W0-1 §6.5）。
  - 不得假定群聊可用 → 群策略默认 `disabled`，首期仅私聊（W0-1 §3）。
  - 不得假定主动推送总是成功 → 4.4 的 Outbox 重试路径。
  - 不得假定渠道用户 ID 跨渠道一致 → 4.2 步骤 3 以 `(channel, channel_user_id)` 复合键映射，绝不跨渠道复用。
  - 不得假定 `chat_id` 与 `user_id` 关系跨渠道一致 → 4.1 保留 `chat_type`，cron 投递按 `chat_id`/`thread_id` 位置路由（`multi-profile-gateways.md:861-870`）。
  - 不得把真实 `account_id`/`from_user_id` 写入仓库或日志 → 本协议与审计一律用哈希/占位符。

## 5. 本机容量结论

### 5.1 实测基线（见 §2.4 原始输出）

- 内存总量 `MemTotal = 2020948 kB ≈ 1.927 GiB`；`MemAvailable` 采样 913–935 MiB。
- **单 Profile 常驻网关进程 RSS 实测区间：405–409 MB**（三次采样 408624 / 405352 / 405364 kB）。
- 辅助进程：`node` TUI 后端 185–198 MB；`hermes -c --tui` 客户端 96 MB（非每 Profile 常驻，属交互式桌面/TUI 成本）。
- CPU：`nproc = 2`。
- 非 Profile 基线占用（系统服务 + 已运行默认 Profile 之外的部分）估算：`used ≈ 1.0 GiB` 减去网关自身 `≈ 405 MB`，得**非 Profile 基线 ≈ 600 MB**（含 OS、系统代理进程、日志、buff 之外的常驻项）。

### 5.2 可支撑 Profile 数上限（算式与假设）

**算式**：设单 Profile 常驻网关 RSS 为 `P = 405 MB`，非 Profile 基线保留 `B = 600 MB`，桌面/TUI 后端按需占用 `T = 190 MB`（仅在使用桌面/TUI 时计入），则

```
N_max = (MemTotal − B [− T]) / P
```

- 不带桌面/TUI：`N_max = (2020948 kB − 600×1024 kB) / (405×1024 kB) = 1406548 / 414720 ≈ 3.39` → **≈ 3 个 Profile**。
- 带桌面/TUI：`N_max = (2020948 − 614400 − 194560) / 414720 = 1211988 / 414720 ≈ 2.92` → **≈ 2 个 Profile**。

**假设**：

1. 单 Profile 网关 RSS 取本次实测均值 405 MB；多 Profile 并发下 RSS **线性增长**这一假设**尚未实测**（多路复用共享一个进程时，额外 Profile 的边际内存**可能低于**每进程基线，也**可能**因加载各自技能/记忆/适配器而更高）。
2. 非 Profile 基线 600 MB 为估算，随系统服务与日志波动。
3. 不使用 swap（本机虽有 zram + swapfile 共 3.0 GiB，但用 swap 会让常驻 Agent 出现严重抖动，不计入「可支撑」）。
4. 未计入模型调用、附件缓存、向量检索等按负载增长的内存。

**结论**：**本机（2 vCPU / 1.9 GiB）可支撑约 2–3 个 Profile 常驻**，仅够「编排与开发 + 少量冒烟验证」，与章程「本地并行执行实例上限为 1」的工程约束一致。**不足以承载 20 Profile 并发，更不足以做 100 用户容量结论。**

### 5.3 必须由生产主机取得的结论（引用章程 A4 与开放决策 O1）

- 章程 **A4**：「本机无法承担 20/100 Profile 负载验收，容量结论必须在生产主机取得。」
- 章程 **开放决策 O1**：「生产 Profile 主机规格与数量（影响阶段 0 负载验收与阶段 1 验收标准）」——建议阶段 0 前给出最小可行规格（内存优先），选项含 (a) 自建物理/虚拟服务器（大陆机房，32–64 GB 起）、(b) 大陆区域云主机按 Profile 数弹性扩容、(c) 先 10–20 Profile 验证再扩容。
- 章程成功标准明列「在生产主机上完成 20 Profile 并发与 100 用户容量结论」。
- 因此：**本机的 2–3 个 Profile 外推只作为容量规划的起点**，20 Profile 并发与 100 用户验收**必须在生产主机**、以真实多 Profile 多路复用负载实测取得（含每 Profile RSS 随并发增长曲线、峰值内存、CPU、网关稳定性与路由正确性）。

## 6. 对后续任务的约束与风险

### 6.1 Wave 1 必须遵守的隔离 / 路由约束

1. **每人一个 Profile、独立进程与独立配置/记忆/会话/技能上下文**，不同员工不得共用 Profile（章程 D1/强制原则）。首期若采用多路复用单进程服务多 Profile，则须逐项验证 §3 隔离表在真实负载下成立（尤其是凭据 fail-closed 与出站身份不回退默认 bot）。
2. **渠道与 Agent 进程解耦，共享渠道接入 + 身份路由**（章程 D2）：单 bot 单轮询，多员工走单网关多 Profile 多路复用；**禁止**每 Profile 各自登录同一微信 bot。
3. **身份路由必须默认拒绝**：`WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS` 为第一道拒绝式过滤；业务绑定表 `(channel, channel_user_id) → employee_id` 为授权真源；**不得**把 `profile_routes` 当授权（W0-1 §6.5）。
4. **默认 Profile 保持空壳无业务权限**：防止未命中 route 的未绑定消息落到默认 Profile 后获得业务能力。
5. **幂等键必须覆盖入站去重键**，且路由冲突（同键不同发送者）必须拒绝（§4.4）。
6. **凭据与身份标识不得落仓库/日志**：一律占位符或哈希；附件按不可信输入处理（W0-1 §6.3）。
7. **部署层须补充内核级隔离**：Wave 1 起就应为「跨员工不可互访文件/进程/网络/临时目录」的验收项准备独立 systemd 服务账户（`User=`）、独立 UID、容器或 namespace 方案；`terminal.cwd`、`TMPDIR`、`terminal.home_mode: profile` 须在每 Profile 显式配置。

### 6.2 待实测项（本机无法定论）

1. **单 Profile 内存稳定区间**：本次三次采样得 405–409 MB，须在更长时段与真实负载下复测，并更新章程 A5（历史值 250 MB 与本次实测不符）。
2. **多 Profile 并发内存曲线**：多路复用单进程下，第 N 个 Profile 的边际内存是否低于单进程基线；须真实创建（在授权环境中）多个 Profile 实测——**本任务禁止在本机创建 Profile，故未执行**。
3. **路由端到端**：`multiplex_profiles: true` + `profile_routes` 下，不同员工消息是否分别进入各自 Profile（会话命名空间、记忆隔离），未命中发送者的落点与拒绝行为（W0-1 §4.4 待实测）。
4. **身份路由拒绝链路**：未绑定/已解绑身份是否在适配器层即被拒绝、审计是否完整（W0-1 §6.5 待实测）。
5. **fail-closed 边界**：多路复用下二级 Profile 缺 `WEIXIN_TOKEN`/`WEIXIN_ALLOWED_USERS` 时确实不借用默认 Profile（代码与测试为静态证据，`test_weixin_secret_scope.py`；**本机未安装 pytest，未实际运行**，与 W0-1 §2 同一限制）。
6. **内存 vs swap 行为**：本机存在 swap（与背景描述「无 swap」不符），须确认生产主机是否配置 swap 及对常驻 Agent 的影响。

### 6.3 因本机限制而须外部环境验证的清单

- **20 Profile 并发容量**（章程 A4）：必须在生产主机实测（生产主机规格待 O1 决策）。
- **100 用户容量结论**（章程成功标准）：必须在生产主机实测。
- **跨员工隔离负面测试**（越权访问文件/进程/网络被拒绝）：须在具备独立服务账户/容器的生产环境执行。
- **真实微信账号下的路由与鉴权**（`from_user_id` 稳定性、发送者鉴权强度、白名单生效）：须真实账号环境（W0-1 §4 待实测清单），本任务与 W0-1 均禁止在本机登录真实账号。
- **多 Profile 服务编排**（systemd 独立 unit / 独立 `User=`）：本机仅有默认 Profile 的 `hermes-gateway.service`，多 Profile 服务拓扑须在目标主机验证。

### 6.4 风险登记

- **R-隔离错觉**：把「Profile 原生数据隔离」误当成「OS 级安全隔离」。缓解：§3 明确 Profile 不沙箱，内核级隔离列为部署层强制项，并以负面测试验收。
- **R-路由授权错位**：把 `profile_routes` 当选路之外的授权手段。缓解：§4.3 默认拒绝 + allowlist + 绑定表三重，`profile_routes` 仅选路。
- **R-容量误判**：用本机 2–3 Profile 外推生产容量。缓解：§5.3 明确 20/100 结论只能在生产主机取得（A4/O1）。
- **R-背景与实测不一致（swap）**：背景记为「无 swap」，实测存在 zram + swapfile。缓解：登记为待澄清项，容量结论按「不使用 swap」假设给出并注明。
- **R-A5 基线过时**：章程 A5 记 250 MB，本次实测网关 405 MB。缓解：建议更新 A5 并在生产主机复测。
- **R-凭据/身份泄露**：报告与日志中出现真实账号标识。缓解：§4.1/§4.3/§4.5 一律占位符与哈希，凭据只存 Profile 私有目录。
