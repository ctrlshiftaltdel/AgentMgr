# 备份与恢复（数据库 / 附件 / 任务库）

- 日期：2026-10-16
- 任务：W1-25（阶段 1「审计、备份和恢复」）
- 依据：`DEPLOYMENT-REQUIREMENTS.md` §5（存储与备份）、`PROJECT-CHARTER.md` 成功标准「数据库与附件备份可在生产主机之外恢复，并完成真实恢复演练」、`AGENTS.md` §7（安全与隐私）
- 工具：`ops/backup.py`（备份）、`ops/restore.py`（恢复 + 逐项校验）
- 测试：`app/tests/ops/test_backup_restore.py`

## 1 结论摘要

- 已建立**一个归档**同时承载三类数据：数据库 `pg_dump`、附件内容寻址目录（`AJAIMGR_STORAGE_ROOT`）、任务库 `.orchestration/`（`state.sqlite3` + `config.json`），并附 `manifest.json`（逐成员 sha256/字节数 + 关键表行数）与 `<归档>.sha256` 校验和旁车。
- 恢复工具把归档恢复到**干净目标**并做 13 项逐项校验（含行数比对与附件 sha256 逐个重算）；任一项不符即**非零退出码**并逐项列出差异。
- 归档**排除凭据类文件**（`.env` / `auth.json` / `*.key` / `*.pem` / `credentials*` 等），排除项记入 manifest，归档完成后再次扫描成员名与私钥头；命中即删除归档并非零退出。
- 已在**本机真实数据**上完成一次完整恢复演练：备份退出码 0，恢复到专用库 `ajaimgr_restore_check` 退出码 0，13 项校验全部 PASS（见 §8）。
- **尚未满足生产验收**：本次演练为**同机**演练，备份未离开生产主机之外，也未做跨主机恢复；生产数据库角色分离（§2）与口令/证书认证（§2）尚未建立。详见 §7、§9。

## 2 归档格式

顶层布局（tar.gz，成员权限归一化为 0600、uid/gid 归零，便于跨机恢复）：

```
manifest.json                  # 校验根：格式版本、行数、逐成员散列、排除项、告警
db/database.sql                # pg_dump --format=plain --no-owner
storage/<aa>/<bb>/<sha256>     # AJAIMGR_STORAGE_ROOT 下全部内容寻址文件（路径原样保留）
orchestration/state.sqlite3    # 任务库快照（SQLite 在线备份 API 产物）
orchestration/config.json      # 任务库配置
```

`manifest.json` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `format` / `format_version` | `ajaimgr-backup` / `1`；恢复端版本不符即拒绝 |
| `created_at` / `host` / `pg_dump_version` | 取证信息（UTC 时间、生成主机、pg_dump 版本） |
| `database.dsn_redacted` | **脱敏**连接串（口令永不写入） |
| `database.table_names` | 备份时源库 `public` 模式表清单（恢复后逐一比对存在性） |
| `database.row_counts` | 关键表行数：`companies` / `employees` / `expense_claims` / `expense_lines` / `audit_events` / `invoice_verifications` / `source_files` |
| `storage.files[]` | 每个附件成员的 `path` / `sha256` / `size_bytes` |
| `orchestration.state_table_count` / `state_integrity` | 任务库表数与 `PRAGMA integrity_check` 结果 |
| `members[]` | 除 `manifest.json` 自身外的全部成员散列（manifest 是校验根，无法自证） |
| `excluded_credential_files[]` | 因命中凭据模式而被排除的文件（留痕，便于审计） |

**校验和**：备份完成后生成 `<归档>.sha256`（`sha256sum` 格式）。恢复端默认要求该旁车存在且与归档实际 sha256 一致，否则拒绝（`--allow-missing-checksum` 才能放行）。

## 3 工具用法

备份（默认输出 `~/ajaimgr-backups/ajaimgr-backup-<UTC>.tar.gz`，**不得落在仓库内**）：

```bash
uv run python ops/backup.py \
  --db-url postgresql+psycopg:///ajaimgr_dev \
  --storage-root var/storage \
  --orchestration-dir .orchestration \
  --output /var/backups/ajaimgr/ajaimgr-backup.tar.gz
```

连接串解析顺序：`--db-url` → `AJAIMGR_DB_URL` → `app.core.config` 默认值；附件根：`--storage-root` → `AJAIMGR_STORAGE_ROOT` → `var/storage`（相对 `--repo-root` 解析）。

恢复 + 校验（演练示例；`*_restore_check` 这类库名才允许 `--recreate-db`）：

```bash
uv run python ops/restore.py /path/ajaimgr-backup.tar.gz \
  --target-db-url postgresql+psycopg:///ajaimgr_restore_check \
  --recreate-db \
  --storage-target /tmp/restore-storage \
  --orchestration-target /tmp/restore-orchestration \
  --report /tmp/restore-report.json --verbose
```

退出码语义：`0` 全部校验通过；`1` 有校验项不符（逐项列出差异）；`2` 用法/IO/环境错误（归档不存在、缺校验和、目标库被安全守卫拒绝等）。

安全守卫：

- `--recreate-db`（会 `DROP` 目标库）要求库名含 `restore`，否则必须显式 `--force-target`；
- 目标库为 `ajaimgr_dev` / `ajaimgr_test` / `postgres` / `template*` 时必须 `--force-target`；
- 备份输出路径若位于仓库内直接拒绝（防止把备份提交进版本库）；
- DSN 口令只经环境变量 `PGPASSWORD` 传给 libpq，绝不进入 argv、报告或日志。

## 4 校验口径（13 项）

| 检查 | 判定 |
| --- | --- |
| `archive_checksum` | 归档 sha256 与旁车一致 |
| `archive_members` | 成员名安全：无绝对路径、无 `..`、无符号链接/硬链接/设备文件 |
| `manifest` | 格式标识与版本可识别、成员清单非空 |
| `member_hashes` | 逐成员 sha256 与字节数与 manifest 一致 |
| `db_restore` | `psql -v ON_ERROR_STOP=1 -f db/database.sql` 回放成功 |
| `db_tables` | manifest 记录的表全部存在 |
| `db_row_counts` | 7 张关键表行数与 manifest 一致 |
| `storage_restore` | 附件逐个落盘 |
| `storage_hashes` | 恢复后附件逐个 sha256/字节数与 manifest 一致 |
| `storage_content_address` | 内容寻址 blob 的文件名等于其内容 sha256 |
| `orchestration_restore` | 任务库文件落盘 |
| `orchestration_sqlite` | `state.sqlite3` 可打开、`integrity_check=ok`、表数与 manifest 一致 |
| `orchestration_config` | `config.json` 可解析为 JSON |

附件与任务库的校验在**恢复后的副本**上重算散列，而不是只信归档内记录；数据库行数在恢复后的库上重新 `count(*)`。

## 5 凭据排除策略

- 排除模式（大小写不敏感，逐路径部件匹配）：`.env`、`.env.*`、`auth.json`、`credentials`/`credentials.json`/`credentials.*`、`secret(s)*.json`、`*.key`、`*.pem`、`*.p12`、`*.pfx`、`*.jks`、`*.keystore`、`*.kdbx`、`id_rsa`/`id_ed25519`/`id_ecdsa`、`.netrc`、`.pgpass`、`.htpasswd`、`token.json`、`service-account*.json`。
- 命中即**不写入归档**，并把相对路径记入 `manifest.excluded_credential_files`（留痕可审计）。
- 归档完成后做**扫描断言**：遍历成员名再判一次模式，并扫描成员内容中的私钥头（`-----BEGIN ... PRIVATE KEY-----`）；命中即删除归档并非零退出（fail closed）。
- 已知边界：扫描只覆盖**文件名模式与私钥头**，不做通用密钥熵检测；业务数据里若出现形如密钥的字符串不会被拦。真正的保证来自「凭据根本不放在被备份目录里」——生产上 `.env`/`auth.json` 位于服务账户的配置目录（见 §7），不进入 `AJAIMGR_STORAGE_ROOT` 与 `.orchestration/`。
- 口令不落盘：DSN 口令只经 `PGPASSWORD` 传给 `pg_dump`/`psql`；报告与 stdout 只打印脱敏连接串。

## 6 备份周期与保留代数（建议）

| 对象 | 频率 | 保留 | 存放位置 |
| --- | --- | --- | --- |
| 数据库 + 附件 + 任务库（单归档） | 每日 1 次（业务低峰，建议 02:00–04:00 本地时间） | 日备 14 份 | **生产主机之外**（异地或另一台受控主机，见 §7） |
| 同上 | 每周 1 次（周日） | 周备 8 份 | 同上 |
| 同上 | 每月 1 次（1 日） | 月备 12 份 | 同上 |
| 迁移前/发布前 | 事件触发 | 保留至该次变更验收后 | 同上 |
| 任务库 `.orchestration/` | 随每日归档一并备份 | 同每日 | 同上 |

- 遵循 **3-2-1** 原则：至少 3 份副本、2 种介质、1 份离开生产主机（`DEPLOYMENT-REQUIREMENTS.md` §5 的硬要求）。
- 附件与数据库都在中国大陆可控环境（章程数据处理约束），异地副本同样受此约束。
- 归档一律携带 `.sha256` 旁车；长期保存时建议同时保留 manifest 的校验和记录。
- 频率与保留代数目前是**建议值**：尚无真实数据量与恢复时长（RTO）实测，需在生产主机按实际规模复核后定稿。

## 7 恢复演练频率与责任人

| 事项 | 频率 | 责任人 | 说明 |
| --- | --- | --- | --- |
| 自动备份执行与失败告警 | 每日 | 系统管理员（当前即项目操作者） | 备份失败必须有人处理，不能静默 |
| **恢复演练**（备份 → 干净目标 → 逐项校验） | 每月 1 次 + 每次迁移/发布前 | 系统管理员执行，项目所有者验收 | 必须用真实数据，交回真实命令、退出码与比对结果 |
| 跨主机恢复演练（备份离开生产主机后恢复） | 每季度 1 次 | 系统管理员 + 项目所有者 | **尚未做过**，是生产验收的必需项（§5） |
| 保留策略与异地存储决定 | 一次 + 年度复核 | 项目所有者 | 涉及成本与合规（数据出境约束） |

- `AGENTS.md` §1 假设**单用户操作者**：上表角色名称是职责划分，当前可能由同一人承担；一旦引入第二个操作者，执行与验收必须分离（执行人不得自我验收）。
- 每次演练记录：归档路径与 sha256、命令、退出码、行数比对表、附件 sha256 比对结果、失败项与处置。

## 8 本机演练记录（2026-10-16，W1-25）

环境：本机 Debian 13，PostgreSQL 17.11（peer 认证，角色 `agents` 有 CREATEDB），`pg_dump` 17.11，Python 3.13.5。

演练数据（真实数据，非构造的空库）：

- 源库 `ajaimgr_dev` 迁移到 head（`0010_invoice_verifications`，23 张表），并写入真实业务行：1 公司 / 2 员工 / 1 报销单 / 2 明细 / 1 审计事件 / 2 条 `source_files` 元数据 + 权限目录 7 角色 / 20 权限 / 32 映射；
- 附件：3 个真实 blob 经产品代码 `app.documents.storage.store_file` 落盘（PNG 66 B / PDF 193 B / TXT 55 B，内容寻址路径 `aa/bb/<sha256>`），落盘后立即 `verify_file` 校验；
- 待备份目录中另外放置了**假凭据文件**（`.env` / `auth.json` / `server.key`，内容为明显的占位串，非真实口令），用于验证排除策略；
- 任务库使用仓库内真实 `.orchestration/`（只读：备份前 `state.sqlite3` 为 253952 B / mtime 08:52，备份后完全一致，且未在目录内新增 `-shm`/`-wal` 旁车文件）。

结果：

| 步骤 | 命令（要点） | 退出码 |
| --- | --- | --- |
| 备份 | `uv run python ops/backup.py --db-url postgresql+psycopg:///ajaimgr_dev --storage-root <drill>/storage --orchestration-dir .orchestration --output ~/ajaimgr-backups/w1-25-drill.tar.gz` | 0 |
| 恢复 | `uv run python ops/restore.py ~/ajaimgr-backups/w1-25-drill.tar.gz --target-db-url postgresql+psycopg:///ajaimgr_restore_check --recreate-db --storage-target <tmp> --orchestration-target <tmp> --report <json> --verbose` | 0 |
| 篡改归档字节（旁车未更新） | 同上，归档为被改动副本 | 1（`archive_checksum` FAIL） |
| 篡改附件字节（旁车已刷新） | 同上 | 1（`member_hashes` / `storage_hashes` FAIL，指出具体 blob 路径） |
| 篡改 manifest 行数（旁车已刷新） | 同上 | 1（`db_row_counts` FAIL，`companies: manifest=99 restored=1`） |

- 归档：`w1-25-drill.tar.gz`，63463 B，sha256 `68053e0b0ca20ebc48093611ca60b7fe7b31a4b43901ee32cd96c89a20bba33e`，7 个成员，凭据扫描 0 violation。
- 行数比对（manifest vs 恢复后库）：`companies` 1/1、`employees` 2/2、`expense_claims` 1/1、`expense_lines` 2/2、`audit_events` 1/1、`invoice_verifications` 0/0、`source_files` 2/2，全部一致。
- 附件 sha256：3/3 与 manifest 一致，恢复目录与源目录逐字节 `diff -r` 无差异。
- 任务库：恢复后可打开，`integrity_check=ok`，4 张表（`tasks`/`locks`/`events`/`sqlite_sequence`），43 条任务、410 条事件，与源库一致。
- 演练用恢复库 `ajaimgr_restore_check` 演练后删除。

## 9 本机演练与生产环境的差异（**不得混同**）

| 维度 | 本机（本次演练） | 生产要求 | 当前状态 |
| --- | --- | --- | --- |
| 备份存放位置 | `~/ajaimgr-backups/`，与源库**同一台主机** | 必须存放于**生产主机之外**（`DEPLOYMENT-REQUIREMENTS.md` §5） | **未满足**：异地/他机存储与传输未建立 |
| 恢复演练范围 | 同机恢复（新建 `ajaimgr_restore_check`） | 需在**另一台主机**上从备份恢复并验收 | **未做**：跨主机恢复未验证 |
| 数据库认证 | unix socket + peer（无口令落盘） | 口令或证书认证；口令不入仓库/日志/提示词（§2） | 生产做法**未建立** |
| 数据库角色 | 单一角色 `agents`（有 CREATEDB，可建演练库） | 运行期角色**无 DDL 权限**；迁移由独立 DDL 角色执行（§2） | 角色/授权脚本**尚未建立** |
| 所有权与授权 | `pg_dump --no-owner`，同角色恢复无差异 | 恢复后需由角色/授权脚本重建所有权与 `GRANT` | 归档**不含**所有权/GRANT 重建信息，须靠脚本补齐 |
| 任务库 | 本机 `.orchestration/`（单用户） | 生产同主机、同服务账户下；备份随归档离开主机 | 传输环节未建立 |
| 服务身份/进程隔离 | 同一 OS 用户 | 每 Profile 独立 systemd 账户、独立 UID/TMPDIR（§3） | 与本任务无关但同属部署前提 |
| pg_dump 版本 | 17.11（与本机服务器 17.11 同主版本） | 必须使用与**生产服务器同主版本**的 `pg_dump` | **待核对**：生产 PG 版本未定 |
| 时钟/时区 | 本地 CST，库内 `timestamptz` 存 UTC | 多机需 NTP 同步（§6） | 单机无影响 |

结论：本次演练证明**工具与校验口径可用**（归档完整性、附件散列、行数比对、任务库可恢复性、凭据排除），但**不等于**通过 `PROJECT-CHARTER.md` 的成功标准——「可在生产主机之外恢复」需要跨主机、异地存储的独立演练。

## 10 已知风险与未完成项

1. **未在生产主机外存储**：`~/ajaimgr-backups/` 与源库同机，主机损毁即同时失去两份；需建立异地/他机副本与传输校验（并保持大陆可控环境）。
2. **未做跨主机恢复**：本次是同机演练，网络传输、目标机权限、目标机 PostgreSQL 版本差异均未验证。
3. **pg_dump 版本一致性**：本机 `pg_dump` 17.11 与服务器同主版本；生产若为其它主版本，需以同主版本客户端备份（否则可能出现 dump/restore 失败或降级）。
4. **角色分离未落地**：应用运行期角色目前仍是表属主、有 DDL 权限，`audit_events` 的只增不改触发器可被 `ALTER TABLE ... DISABLE TRIGGER` 绕过（§2 已记录）；备份侧影响是归档不含所有权/GRANT，恢复后必须由角色/授权脚本重建。
5. **恢复时长（RTO）未测**：本机数据量极小（归档 63 KB），真实规模下的备份窗口与恢复时间未知。
6. **保留代数与自动调度未实现**：§6 是建议值，尚无 cron/systemd timer 与失败告警；`.orchestration/backups/` 目前仍是任务库的本地副本（由 `scripts/orchestrator.py backup` 生成），不作为生产备份手段。
7. **内容扫描边界**：凭据排除只覆盖文件名模式与私钥头（§5），不做通用密钥熵检测。
8. **`--no-owner` 的取舍**：为让归档可跨角色恢复而放弃所有权信息；若生产要求归档自带所有权，需要改为同角色恢复或补充授权脚本（与第 4 项一并解决）。
