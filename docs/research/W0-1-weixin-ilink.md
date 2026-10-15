# W0-1 微信 iLink 接入能力与 Go/No-Go 评估

- 任务：W0-1（weixin / iLink 接入能力评估）
- 分支：task/W0-1-weixin-ilink
- 范围：仅基于本机 Hermes 安装目录的源码、测试与随附文档做只读调研；不含任何真实账号验证。
- 声明：本结论是**代码与文档级证据的初步结论**，凡标注“待实测”的项必须在真实账号与真实环境中验证后才能升格为最终结论。

## 1. 结论摘要

**总体建议：有条件 Go（Go with conditions），面向“私聊 + 文本/图片/文件/语音入站”的最小垂直切片；对“群聊”“无前置入站的主动推送”“把外部用户 ID 直接当授权”三项按 No-Go 处理。**

- **Go（私聊单渠道）**：本机 Hermes 的微信适配器通过腾讯 iLink Bot API 以长轮询方式接入个人微信，单个 iLink bot 身份即可接收多个不同员工的私聊消息，并可用 `gateway.profile_routes` 按发送者 `user_id` 精确路由到不同 Hermes Profile。这正是本项目“共享渠道接入 + 身份路由”（章程 D2）所需的机制，属于一等公民能力（源码 + 官方文档双重证据）。
- **Go（入站富媒体）**：图片、文件、语音、视频的**入站**下载与解密由适配器内建（AES-128-ECB 加密 CDN），图片/文件/视频**出站**上传也已实现。语音出站仅以文件附件形式发送，原生语音气泡未被上游证实可用。
- **No-Go（群聊）**：QR 登录得到的是 iLink bot 身份（形如 `...@im.bot`），通常无法被拉入普通微信群，iLink 通常也不向此类账号投递普通群事件；群策略默认 `disabled`。代码与文档均明确说明“群消息可能永远到不了 Hermes，且限制在 iLink 侧”。因此**不得把群聊作为首期可用渠道**。
- **No-Go（无前置入站的主动推送）**：主动推送依赖对端 `context_token`；当该 token 缺失或过期（例如新配对的 bot、用户从未给 bot 发过消息）时，iLink 返回 `prepare failed` / `unknown error`，适配器会回退为“无 token 发送”一次，仍失败则报 `session not ready`，**必须由用户先给 bot 发一条消息**才能恢复。因此“员工从未与 bot 交互就收到主动通知”这一假定不成立。
- **No-Go（把外部用户 ID 直接当授权）**：`profile_routes` 的 `user_id` 路由是**选路而非拒绝式授权**；未命中任何路由的消息会落到默认 Profile。官方文档明确警告 `user_id` 的可靠性取决于接入端是否对发送者做了鉴权。身份鉴权必须额外依赖 `WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS`（这是真正的拒绝式入站过滤），并在业务层做 (渠道, 渠道用户 ID) → 内部员工 ID 的映射与绑定校验。
- **切换成本可控**：企业微信/钉钉/飞书/邮件/网页上传均可置于同一“渠道适配层”之后，替换只影响适配器与身份映射，不影响业务域，符合章程 R1 的缓解策略。

## 2. 证据基础

- **本机 Hermes 版本**：`v0.22.1+72.g450ad50 (2026.9.24) · stable · local`；安装目录 `~/.hermes/hermes-agent`（只读参考，未做任何修改）；安装提交 `450ad5055eb3c4852d96676ee99287d618bd283a`（`git describe` 显示 `v0.22.1+canary.20261015T070051Z`）。命令：`hermes --version`。
- **核心实现**：`~/.hermes/hermes-agent/gateway/platforms/weixin.py`（71862 字节）。
  - 模块级能力常量与端点：`weixin.py:44-90`（`ILINK_BASE_URL`、CDN、端点、超时/重试常量、`SESSION_EXPIRED_ERRCODE=-14`、`RATE_LIMIT_ERRCODE=-2`）。
  - 适配器类与出站能力入口：`weixin.py:692`（`class WeixinAdapter`）。
- **相关测试文件**（位于 `~/.hermes/hermes-agent/tests/gateway/`）：
  - `test_weixin.py`（34229 字节，覆盖格式化/分块/去重/重试/限流熔断/媒体/轮询游标/语音）
  - `test_weixin_secret_scope.py`（多 Profile 凭据作用域，防止跨 Profile 借用环境变量）
  - `test_weixin_typing.py`（typing ticket 刷新）
  - `test_weixin_voice_warning_policy.py`（语音作为附件的告警策略）
- **路由与配置**：`gateway/profile_routing.py`（`ProfileRoute.matches` 在 `:74`，`match_profile_route` 在 `:174`）；`gateway/config_env.py:604-613`（微信环境变量到 `extra` 的映射）；`gateway/platforms/event.py`（统一入站事件 `MessageEvent`）；`gateway/platforms/access_policy_mixin.py`（DM/群访问策略）；`gateway/platforms/helpers.py:20`（`MessageDeduplicator`）。
- **官方/随附文档**：
  - 微信渠道用户指南（英文）：`website/docs/user-guide/messaging/weixin.md`
  - 多 Profile 网关与 `profile_routes`：`website/docs/user-guide/multi-profile-gateways.md`（“Routing shared-bot chats to profiles”一节，`:749-870`）
  - 环境变量参考：`website/docs/reference/environment-variables.md:479-487`
  - Cron 投递渠道：`website/docs/user-guide/features/cron.md:602`
  - 公开 URL：`https://hermes-agent.nousresearch.com/docs/user-guide/messaging/weixin/`（亦见 `hermes_cli/web_server_messaging.py:123` 引用的 docs_url）。
- **未执行的验证（诚实记录）**：本机 `/usr/bin/python3` 为 3.13.5，但**未安装 pytest**（`python3 -m pytest` 返回 `No module named pytest`），且本任务禁止安装软件包，因此上述测试文件**未实际运行**；本文对测试的引用仅为其覆盖范围（测试名）层面的静态证据。
- **文档与代码的一处不一致（风险提示）**：文档 `weixin.md` 的“Message Chunking”称最大消息长度为 **4000 字符**，而代码 `weixin.py:696` 为 `MAX_MESSAGE_LENGTH = 2000`、`weixin.py:698` 为 `_SPLIT_THRESHOLD = 1800`。以**代码为准**（2000/1800），文档数字偏大，属需上游澄清的不一致点。

## 3. 逐项能力评估表

| 能力 | 结论 | 证据（文件路径+行号 或 文档链接） | 风险或限制 |
|------|------|-----------------------------------|-----------|
| 多员工私聊（一个适配器服务多个不同员工） | 支持 | `weixin.py:692`（单个 `WeixinAdapter` 持有单 token）；`weixin.py:744` `connect()` 对 `weixin-bot-token` 加锁；`weixin.py:871` `_process_message` 按 `from_user_id` 分发；`profile_routing.py:74-101`（`user_id` 路由，权重 16）；文档 `multi-profile-gateways.md:749-870` | 一个 bot token 同一时刻只能被一个进程轮询（多 Profile 文档 `:1223`）；多员工路由需 `gateway.multiplex_profiles: true`，否则 `profile_routes` 被忽略 |
| 稳定外部用户 ID（能否作为身份绑定键） | 待实测（有保留地支持） | `weixin.py:875` `sender_id = message.get("from_user_id")`；`weixin.py:256-266` `_guess_chat_type` 在 DM 下以 `from_user_id` 作为会话 ID；`profile_routing.py:74-101` 以其作精确匹配 | 官方文档 `multi-profile-gateways.md:749-870` 警告：`user_id` 的可靠性取决于接入端是否对发送者鉴权；跨会话/重新登录后是否稳定、能否抵抗伪造，须真机验证 |
| 主动通知（非用户发起时能否推送） | 部分支持（待实测） | `weixin.py:1029` `send()` 可在无入站时发送；`weixin.py:969-1027` `_send_text_chunk` 含“无 token 回退重发”；`config_env.py:612` `WEIXIN_HOME_CHANNEL`；`cron.md:602` 列出 weixin 为 cron 投递渠道 | 对端 `context_token` 缺失/过期时 iLink 返回 `prepare failed`/`unknown error`，回退仍失败则 `session not ready`，需用户先给 bot 发消息（文档 `weixin.md` 故障排查表）；`user_id` 单独路由不满足 cron 投递，需另配 `chat_id`/`thread_id` 位置路由（`multi-profile-gateways.md:749-870`） |
| 图片消息 | 支持 | 入站：`weixin.py:676-681` `_INBOUND_MEDIA`（`ITEM_IMAGE`）、`weixin.py:918-941` `_download_media`；出站：`weixin.py:1087` `send_image`、`weixin.py:1099` `send_image_file`、`weixin.py:1134` `_send_file` | 依赖 `cryptography` 与 CDN 可达（`novac2c.cdn.weixin.qq.com`）；出站 URL 受 SSRF 校验限制（`weixin.py:1124-1132`） |
| 文件消息 | 支持 | 入站：`weixin.py:678`（`ITEM_FILE`，保留原始 `file_name`）；出站：`weixin.py:1111` `send_document`、`weixin.py:668-670` `_file_item` | 大文件经加密 CDN 上传，失败会整体报错；附件应视为不可信输入，需业务层再做类型/大小校验 |
| 语音消息 | 部分支持 | 入站：`weixin.py:679`（`ITEM_VOICE`，始终下载 `.silk` 交中央 STT 重转写，注释 `#27300`）；出站：`weixin.py:1120-1122` `send_voice` **以文件附件发送**，注释称原生语音气泡“未被上游证实可用” | 出站无原生语音气泡；入站语音依赖配置好的 STT 后端（mlx-whisper/whisper.cpp/faster-whisper），无 STT 则只有原始 `.silk` |
| 限流 | 支持（适配器内建熔断） | `weixin.py:57` `RATE_LIMIT_ERRCODE=-2`；`weixin.py:700-712` 熔断参数（阈值/窗口/打开时长）；`weixin.py:957-967` `_record_rate_limit_event`/冷却；`weixin.py:969-1027` 3 倍退避重试 | 仅对 iLink 返回的 `-2` 生效；`-2` 伴随 `prepare failed`/`unknown error` 会被识别为“会话过期”而非限流（`weixin.py:69-75`）；真实配额与并发上限未知，须真机压测 |
| 断线恢复与重连 | 支持（会话过期除外） | `weixin.py:797-852` `_poll_loop`（连续失败退避，`MAX_CONSECUTIVE_FAILURES=3`、2s/30s）；`weixin.py:854` `_recycle_poll_session` 换新会话回收套接字；`weixin.py:565-576`+`:833-837` 游标 `sync_buf` 落盘续传 | `-14` 会话过期时暂停 600s，且**可能需重新扫码登录**（文档故障排查：重新执行 `hermes gateway setup`），无法自动恢复；`connect()` 的 token 锁使重复轮询直接失败 |
| 群聊限制 | 不支持 | `weixin.py:763-772` `connect()` 中群策略告警；文档 `weixin.md` 顶部 iLink bot 身份警告与群策略说明；`access_policy_mixin.py` 群策略默认不放行 | QR 登录得到 `...@im.bot` 身份，通常无法被拉入普通微信群，iLink 通常不投递普通群事件；群策略即便设为 `open`/`allowlist` 也可能完全无效，限制在 iLink 侧 |

## 4. 待实测项清单

以下各项**只能在真实微信账号与真实运行环境中验证**，逐条给出验证方法与预期观察点。禁止在本任务内登录真实账号。

1. **iLink 是否投递群事件**：以真实 bot 身份尝试被拉入一个普通微信群，并在群内 @bot 与直接发言；观察 gateway 日志是否出现任何原始群入站事件（`from=%s type=group`）。预期：若日志无群事件，则确证群聊不可用（与文档一致）。
2. **`from_user_id` 的稳定性**：记录同一员工在不同天、断线重连、重新扫码登录后上报的 `from_user_id`；预期：同一员工 ID 稳定；若随会话变化，则不能直接作为长期绑定键，需退回业务层显式绑定。
3. **多员工私聊是否都到达同一 bot**：让至少 3 个不同员工分别给 bot 发私聊；观察是否都到达同一适配器且 `from_user_id` 互不相同；预期：均到达且 ID 不同（否则“一个 bot 服务多员工”不成立）。
4. **`user_id` 路由端到端**：配置 `gateway.multiplex_profiles: true` 与按 `user_id` 的 `profile_routes`，让不同员工发消息；观察是否分别进入各自 Profile（会话命名空间、记忆隔离）。预期：按 `user_id` 精确路由；未命中的发送者落到默认 Profile。
5. **发送者鉴权强度**：评估 iLink 是否对 `from_user_id` 做服务端鉴权（能否被伪造）；预期：若不可验证来源真实性，则身份必须叠加 `WEIXIN_ALLOWED_USERS` 白名单与业务层绑定校验。
6. **无前置入站的主动推送**：在“用户从未给 bot 发消息”的新配对状态下，触发一次 cron/通知发送；观察是否返回 `session not ready`。再让该用户先发一条消息后重试，观察是否成功。预期：新配对无 token 时失败，用户先发言后可成功。
7. **主动推送在重启后的持续性**：重启 gateway 后立即触发通知；观察 `context_token` 落盘恢复（`weixin.py:180` `ContextTokenStore`）是否使推送仍成功。预期：已交互过的对端重启后仍可推送。
8. **真实限流阈值与并发**：以受控速率连续发送，记录触发 `-2` 的速率与冷却表现；预期：达到某速率后触发退避/熔断，记录真实阈值。
9. **媒体往返**：真机发送图片/文件/语音/视频，观察入站下载解密与出站上传渲染；预期：图片/文件/视频可用，语音出站为文件附件而非语音气泡。
10. **会话过期频率**：长期运行观察 `errcode=-14` 出现频率与重新扫码的必要性；预期：出现过期即需人工重新登录。
11. **消息去重与内容指纹**：让上游重发相同文本（新 `message_id`），观察是否被内容指纹去重（`weixin.py:877-889`，TTL 300s）；预期：同内容不同 ID 被去重。

## 5. 替代渠道与切换代价

所有候选渠道都可置于同一“渠道适配层 + 身份路由”之后；切换只影响适配器实现与身份映射，不触及业务域（符合章程 R1 缓解策略与 D2 决策）。

| 渠道 | 本机是否已有适配器/证据 | 身份键 | 主动推送 | 对现有架构的影响与代价 |
|------|------------------------|--------|----------|------------------------|
| 企业微信（WeCom） | 有：`website/docs/user-guide/messaging/wecom.md`、`wecom-callback.md`；`config_env.py:590` `WECOM_HOME_CHANNEL` | 企业成员 userid（企业内稳定、由企业鉴权） | 支持（应用消息/回调） | 需企业注册与管理员配置；身份可信度高于个人微信；群默认 `open`（与微信相反）。代价：中（管理员协调 + 凭据） |
| 钉钉（DingTalk） | 有：`config_env.py:575` `DINGTALK_HOME_CHANNEL` | 员工 staffId | 支持（工作通知） | 需企业组织与管理员配置；代价：中 |
| 飞书（Feishu） | 有：`config_env.py:584` `FEISHU_HOME_CHANNEL` | open_id/union_id（应用内稳定） | 支持（应用消息/卡片） | 需企业自建应用与管理员配置；代价：中 |
| 邮件 | 有：仓库技能 `himalaya`（IMAP/SMTP 终端收发） | 邮箱地址 | 支持（异步发送） | 无“打字中”等实时语义，会话体验弱；身份易伪造（需 DKIM/白名单）；代价：低—中 |
| 网页上传（自建后台/上传页） | 有：`website/docs/user-guide/features/web-dashboard.md`；后端已有 FastAPI 计划（章程 D3） | 登录会话 + 内部员工 ID | 支持（页面/站内通知） | 需自建登录与身份绑定，但身份完全可控、附件原生；代价：高（但可作兜底主渠道） |

## 6. 对架构的约束

渠道适配层接口必须满足以下要求，且多员工路由上不得做如下假定。

**6.1 统一消息事件字段**（对齐 `gateway/platforms/event.py` 的 `MessageEvent` / `SessionSource`，业务层应归一化为自有事件模型）：
- 文本：`text`；类型：`message_type`（枚举 TEXT/PHOTO/VIDEO/VOICE/DOCUMENT/…）。
- 发送者：`user_id` / `user_name`（渠道侧标识，**不是**内部员工 ID）。
- 会话来源：`source`（含 `platform`、`chat_id`、`chat_type`、`user_id`、`thread_id`、`scope_id`）。
- 消息标识：`message_id`（渠道侧，可能缺失）。
- 附件：`media_urls[]`（本地缓存路径）与 `media_types[]`（MIME），及 `reply_to_*`（被引用消息的文本/作者）。
- 时间戳：`timestamp`。

**6.2 去重键**：
- 主键：`(platform, account_id, message_id)`；副键：内容指纹 `(sender_id, md5(text))`，因上游会用新 `message_id` 重发相同文本（`weixin.py:877-889`）。
- 去重窗口须有 TTL（微信侧 300s，`weixin.py:58`）与容量上限（`helpers.py:20-44`）；业务层的幂等键必须**至少覆盖入站去重键**，保证重复消息不重复创建报销单/审批任务（章程验收标准）。

**6.3 附件引用**：
- 统一以“本地缓存路径 + MIME + 内容摘要（hash）”为引用，不依赖渠道 URL 的持久性（微信媒体经加密 CDN 传输）。
- 附件一律视为**不可信输入**：入库前做类型/大小/病毒校验；加密 CDN 的密钥与地址属运行时敏感信息，不得落日志或写入业务记录。

**6.4 回执与重试语义**：
- 出站发送返回 `SendResult(success, message_id, error)`（`weixin.py:1029`）；适配器已内建退避重试与“无 token 回退”（`weixin.py:969-1027`）。
- 业务层必须按**至少一次（at-least-once）**语义设计：以入站去重键为幂等键，容忍重复投递与重试；出站用 `client_id` 幂等标识（`weixin.py:1029` 生成 `hermes-weixin-<uuid>`）。
- 主动推送失败（`session not ready`）不得阻塞业务事务：走事务性 Outbox（章程 D5）+ 后台 Worker 重试，失败可见可审计。

**6.5 多员工路由上不得假定的地方**：
- **不得假定 `user_id` 即授权**：`profile_routes` 的 `user_id` 是选路而非拒绝式授权，未命中路由会落到默认 Profile（`multi-profile-gateways.md:749-870`）。必须叠加 `WEIXIN_DM_POLICY=allowlist` + `WEIXIN_ALLOWED_USERS` 实现“未绑定/已解绑身份默认拒绝”，并在业务层做 (渠道, 渠道用户 ID) → 内部员工 ID 的绑定校验。
- **不得假定同一 bot 可被多进程并发轮询**：一个 token 只能一个 poller（`weixin.py:744` 锁 + 多 Profile 文档 `:1223`）；多员工必须走单网关多 Profile 多路复用，而非每 Profile 各自登录同一 bot。
- **不得假定群聊可用**：群事件可能根本不投递（`weixin.py:763-772`、`weixin.md` 警告）。
- **不得假定主动推送总是成功**：无前置入站时可能 `session not ready`（`weixin.md` 故障排查）。
- **不得假定渠道用户 ID 跨渠道一致**：微信 `from_user_id` ≠ 企业微信 userid ≠ 钉钉 staffId ≠ 飞书 open_id ≠ 邮箱；身份层必须以 (渠道, 渠道用户 ID) 复合键映射到内部员工 ID，绝不能跨渠道复用。
- **不得假定 `chat_id` 与 `user_id` 的关系在所有渠道一致**：微信 DM 下二者相同（`weixin.py:256-266`），群聊下不同；cron 投递只认 `chat_id`/`thread_id` 位置路由，`user_id` 单独路由不满足（`multi-profile-gateways.md:749-870`）。
- **不得把 `account_id`/`from_user_id` 的真实值写入仓库或日志**：报告与代码中一律使用占位符（如 `<ACCOUNT_ID>`），凭据只存于 Profile 私有目录。
