# 微信 B1 Bridge 运行手册

- 状态：B1（消息 / 身份 / 去重 / 通知）本地服务运行手册。
- 关联：Issue #104、`docs/integrations/clients/wechat.md`（设计 v2）、#83 / #97。
- 适用：测试机上的**单账号、单实例**微信 iLink Bot。生产部署、容量、跨主机恢复
  不在 B1 范围内。
- 本手册是操作步骤，不代表实施者已经执行；QR 扫码只能由所有者在测试机交互式
  会话完成。

## 1 B1 能力边界

已实现：

- 服务端 Task Intake + 闭集动作注册表（`app/tasks/intake.py`）；
- 微信身份解析（只读复用 `employee_channel_bindings` / `resolve_principal`）；
- 稳定事件键去重（`from_user_id` + `create_time_ms` -> 幂等任务键）；
- 只读 `/help`、`/whoami`；
- SDK 适配、本地进程入口、systemd 模板。

**没有**实现：advisory 回答、模型调用、governed/managed 执行、审批回调、受信审批
页面、进销存写入、公开 HTTP API、内网 token 校验。B1 生产动作注册表**没有任何
启用的建任务动作**；除 `/help`、`/whoami` 外的文本统一回复“B1 未开放”。

## 2 部署前提

- 专用测试微信账号 / 租户；所有者已接受 iLink 条款与账号风险。
- 测试机已具备仓库、Python 环境与测试数据库；Bridge 只连**测试/试点**库。
- 凭据文件路径不在仓库内，例如 `/var/lib/ajaimgr-wechat/credentials.json`；
  目录 0700、文件 0600（或 0400），属主为运行 Bridge 的用户。systemd 单元只给
  **凭据目录** `ReadWritePaths`（SDK 在 `-14` 时需要清理并重写凭据），不把整个
  `/var/lib` 或仓库设为可写。
- `AJAIMGR_ENV` 必须是 `pilot` / `test` / `development`；Bridge 启动时拒绝
  未标注或 production 环境。数据库必须是副本/测试库。
- 该本地受信服务**不需要** `AJAIMGR_INTERNAL_TOKEN`；不要把管理 API token 放进
  Bridge 进程。身份哈希使用独立的 `AJAIMGR_IDENTITY_HASH_SALT`。
- 单账号只跑一个 Bridge 进程；systemd 模板用 `flock -n` 做进程级 lease。
- 不要把 token、QR、`context_token`、消息正文写入仓库、Issue、PR 或日志。

## 3 首次扫码登录（所有者执行）

QR 只在**交互式 TTY** 输出；非 TTY（systemd/journal/重定向）会失败关闭，避免
QR 进入日志。

1. 停止可能存在的旧 Bridge（避免两个进程抢同一账号）：

   ```bash
   sudo systemctl stop ajaimgr-wechat-bridge.service
   ```

2. 以运行 Bridge 的用户、在真实终端执行登录：

   ```bash
   cd <repo-root>
   sudo -u <bridge-user> env \
     AJAIMGR_WECHAT_CREDENTIAL_PATH=/var/lib/ajaimgr-wechat/credentials.json \
     AJAIMGR_WECHAT_COMPANY_ID=<company-id> \
     <test-host-venv>/bin/python -m app.integrations.wechat login
   ```

3. 终端会显示一个登录 URL/二维码内容。所有者用手机微信扫码并在微信内确认；
   如果服务器要求 pairing code，按终端提示输入。
4. 登录成功后 SDK 写入凭据文件。确认权限：

   ```bash
   sudo stat -c '%a %U:%G %n' /var/lib/ajaimgr-wechat/credentials.json
   # 期望：600（或 400） 属主为 <bridge-user>
   ```

5. 若终端提示二维码过期/被拒绝，重新执行第 2 步；不要在聊天、工单或日志里粘贴
   QR 内容。

> QR URL 不是长期 token，但仍按敏感信息处理：不写 journal、不截图、不外发。

## 4 日常启动 / 停止 / 状态

```bash
sudo systemctl start ajaimgr-wechat-bridge.service
sudo systemctl status ajaimgr-wechat-bridge.service
sudo journalctl -u ajaimgr-wechat-bridge.service -n 50 --no-pager
```

启动前检查：

- 环境文件存在且 root-only（0600），包含 `AJAIMGR_DB_URL`、
  `AJAIMGR_WECHAT_CREDENTIAL_PATH`、`AJAIMGR_WECHAT_COMPANY_ID`；
- 凭据文件存在且权限不比 0600 宽；Bridge 启动时会对 group/other 可读的凭据
  直接失败关闭；
- 没有第二个 Bridge 进程（`flock` 占用时启动会立即失败，属预期）。

## 5 单实例 / lease

- systemd 模板的 `ExecStart` 使用
  `/usr/bin/flock -n /run/ajaimgr-wechat/bridge.lock ...`；第二个实例拿不到锁会
  退出。
- 不要在同一微信账号上并行启动两个 Bridge，也不要在另一台机器上同时扫码登录
  同一账号。
- 迁移到另一台测试机前先停止旧实例，并预期可能需要重新扫码/重新绑定。

## 6 常见状态与处置

| 现象 | 含义 | 处置 |
|---|---|---|
| 启动退出并记录 `credential_missing` | 凭据文件不存在 | 由所有者执行第 3 节扫码登录 |
| 启动退出并记录 `credential_permissions_unsafe` | 凭据对 group/other 可读 | `chmod 600`，属主改为 `<bridge-user>` |
| 日志出现 `qr_required` / `qr_login_requires_interactive_terminal` | 需要重新扫码，且当前不是交互式终端 | 停止服务，按第 3 节在真实终端扫码；不要期待无人值守自动恢复 |
| 日志出现 `sdk_failure code=sdk_error` | 网络/API/发送失败 | 检查网络与账号状态；必要时重启服务 |
| 收到 `-14`（session expired） | SDK 已清空凭据并要求重新登录 | 同 `qr_required`：所有者重新扫码；重新登录后 `from_user_id` 可能变化，需要重新绑定 |
| 冷用户 `NoContextError` | SDK 的 `send()` 不能主动为冷用户创建 context；B1 只回复入站消息，不主动 `send()` | 未来审批/通知需要 Workbuddy 或其它通道；不要假设微信随时可推送 |
| 日志出现 `reply_failed code=reply_send_failed` | 入站回复发送失败（网络/API） | 检查网络/账号；Bridge 不重试、不把失败当成功；用户可能未收到回复 |

`-14` 与冷用户限制都是 SDK v0.3.0 的已知行为，B1 不声称 at-least-once 或随时可推送。

## 7 日志与隐私

允许记录：稳定事件码、渠道事件哈希（SHA-256 前缀）、任务号、动作 ID、内部拒绝
原因、`reply_*` 稳定码、`qr_required`。

禁止记录：token / key / QR / `context_token` / 消息正文 / 渠道用户明文 / 员工
姓名或 ID 明文。适配器会丢弃 SDK v0.3.0 直接写 stderr 的内部日志（该日志含
`user_id`），因此不要绕过适配器直接调用 SDK。

拒绝审计由身份解析服务写入，身份只存加盐哈希；盐未配置时省略哈希而绝不写明文。

## 8 故障排查命令

```bash
# 服务状态与最近日志（不含正文）
sudo systemctl status ajaimgr-wechat-bridge.service
sudo journalctl -u ajaimgr-wechat-bridge.service --since '30 min ago' --no-pager

# 确认只有一个实例持有锁
sudo fuser -v /run/ajaimgr-wechat/bridge.lock || true

# 凭据权限
sudo stat -c '%a %U:%G %n' /var/lib/ajaimgr-wechat/credentials.json

# 语法/配置检查（不联网、不扫码）
cd <repo-root>
<test-host-venv>/bin/python -m py_compile app/integrations/wechat/*.py app/tasks/intake.py
<test-host-venv>/bin/python -m app.integrations.wechat --help
```

配置缺失/非法时 `python -m app.integrations.wechat` 以非零状态退出并记录稳定
`code`；没有默认生产凭据路径，也没有默认公开监听。

## 9 B1 验证（Master / 所有者在测试机）

```bash
# DB-free 契约测试（不要把 test_intake_db.py 放进 --noconftest 运行）
<test-host-venv>/bin/python -m pytest \
  app/tests/integrations/wechat/test_action_registry.py \
  app/tests/integrations/wechat/test_bridge.py \
  app/tests/integrations/wechat/test_wechat_config.py \
  -q -o addopts='' --noconftest

# 真实 PostgreSQL 测试（需要 AJAIMGR_TEST_DB_URL）
<test-host-venv>/bin/python -m pytest app/tests/integrations/wechat/test_intake_db.py -q

# 隐私与 diff 检查
python3 scripts/check_privacy.py --ci
git diff --check
```

真实微信扫码/收发 E2E 由 Master/所有者执行，B1 先以假 SDK 覆盖消息、身份、去重、
`NoContextError` 与日志隐私。不得把“测试通过”表述为“真实微信已验证”。

## 10 后续增量（不在 B1）

- B2：只读 advisory 执行器（有来源/引用、无写工具）；
- B3：governed 文件审批（受信页面 + `record_approval`）；
- B4：managed 进销存 applier（副本 DSN、唯一约束、读回校验）。

B1 不把 `policy_lookup` 入队，也不回答“已受理 = 已回答”。
