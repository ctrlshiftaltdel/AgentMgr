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
- Issue #22 新增跨主机/异地恢复的 Runbook 与自动化封装（§11）：`ops/offsite_restore.py` 提供 `preflight`（只读预检）与 `rehearse`（单机 dry-run）。它**不建立任何网络连接**，数据库恢复步骤在 dry-run 中明确记为 `NOT_EXECUTED`；**真实跨主机/异地恢复演练仍未执行**（§11.8）。

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
| `missing_components[]` | 缺失且被 `--allow-missing-*` 显式放行的必需组成部分（取值 `storage` / `orchestration`；正常场景为空 list，见 §3） |

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

连接串解析顺序：`--db-url` → `AJAIMGR_DB_URL` → `app.core.config` 默认值；附件根：`--storage-root` → `AJAIMGR_STORAGE_ROOT` → `var/storage`（相对 `--repo-root` 解析）；任务库目录：`--orchestration-dir` → 默认 `<repo-root>/.orchestration`（同样按 `--repo-root` 解析，不随脚本所在位置变化）。

**必需组成部分缺失即失败**：解析后的附件存储根与任务库目录必须存在且为目录；任一缺失时备份直接失败（退出码 2）且**不写出归档或旁车文件**。只有显式给出放行开关才允许继续：

```bash
uv run python ops/backup.py ... \
  --allow-missing-storage \          # 仅当部署确实没有附件存储
  --allow-missing-orchestration      # 仅当部署确实未启用任务库
```

放行时归档照常生成，但 manifest 顶部 `missing_components` 记下缺失清单（取值 `storage` / `orchestration`；正常场景为空 list），stdout 汇总同样打印 `missing parts` 一行。已知边界：目录存在但内容为空、或缺少 `state.sqlite3` / `config.json` 的情况**不触发**该守卫，保持原行为（备份成功并记入 `warnings`）。

恢复 + 校验（演练示例；`*_restore_check` 这类库名才允许 `--recreate-db`）：

```bash
uv run python ops/restore.py /path/ajaimgr-backup.tar.gz \
  --target-db-url postgresql+psycopg:///ajaimgr_restore_check \
  --recreate-db \
  --storage-target /tmp/restore-storage \
  --orchestration-target /tmp/restore-orchestration \
  --report /tmp/restore-report.json --verbose
```

退出码语义（备份 / 恢复通用）：`0` 成功（恢复侧表示 13 项校验全部通过）；`1` 恢复时有校验项不符（逐项列出差异）；`2` 用法/IO/环境错误——恢复侧如归档不存在、缺校验和、目标库被安全守卫拒绝等，备份侧如**必需组成部分缺失且未给对应 `--allow-missing-*` 开关**、输出路径位于仓库内（退出码 2 时备份不会写出任何归档/旁车文件）。

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

## 8 本机演练记录（2026-10-16，W1-25，第 1 轮修复后重跑）

环境：本机 Debian 13，PostgreSQL 17.11（peer 认证，角色 `agents` 有 CREATEDB），`pg_dump`/`psql` 17.11，Python 3.13.5（仓库 `.venv`）。

演练数据（真实数据，非构造的空库）：

- 源库 `ajaimgr_dev` 迁移到 head（23 张表），真实业务行：1 公司 / 2 员工 / 1 报销单 / 2 明细 / 1 审计事件 / 2 条 `source_files` 元数据 + 权限目录 7 角色 / 20 权限 / 32 映射；
- 附件：3 个真实 blob 经产品代码 `app.documents.storage.store_file` 落盘并 `verify_file` 校验（PNG 66 B / PDF 69 B / TXT 50 B，内容寻址路径 `<aa>/<bb>/<sha256>`）；
- 待备份目录中另放**假凭据文件**（`.env` / `auth.json` / `server.key`，明显占位串，非真实口令），验证排除策略；
- 任务库使用仓库内真实 `.orchestration/`（只读）：备份前后 `state.sqlite3` 均为 258048 B / mtime 09:28:03，目录内无新增文件、无 `-shm`/`-wal` 旁车；演练结束复核同样干净。（经验教训：对任务库做独立只读检查必须打开**复制本**——直接对原文件 `sqlite3` 打开即使 `mode=ro` 也会在目录内生成 `-shm`/`-wal`，本工具内部正是因此先复制再打开。）

结果：

| 步骤 | 命令（要点） | 退出码 |
| --- | --- | --- |
| 备份 | `uv run python ops/backup.py --db-url postgresql+psycopg:///ajaimgr_dev --repo-root <repo-root> --storage-root <drill>/storage --output <drill>/w1-25-drill-r2.tar.gz`（**不给** `--orchestration-dir`，验证默认值按 `--repo-root` 解析） | 0 |
| 恢复 | `uv run python ops/restore.py <drill>/w1-25-drill-r2.tar.gz --target-db-url postgresql+psycopg:///ajaimgr_restore_check --recreate-db --storage-target <drill>/restored-storage --orchestration-target <drill>/restored-orchestration --report <drill>/restore-report.json --verbose` | 0（13/13 PASS） |
| 负向：任务库缺失且未放行 | 同上备份，`--repo-root` 指向无 `.orchestration` 的目录 | 2（报 `required component 'orchestration' is missing: …` 并提示 `--allow-missing-orchestration`；不写归档） |
| 负向：存储根缺失且未放行 | 同上备份，`--storage-root` 指向不存在路径 | 2（同口径提示，含解析路径与 `--allow-missing-storage`；不写归档） |
| 篡改归档字节（旁车未更新） | 同上恢复，归档为被改动副本 | 1（`archive_checksum` FAIL） |
| 篡改附件字节（旁车已刷新） | 同上 | 1（`member_hashes` / `storage_restore` / `storage_hashes` FAIL，指出具体 blob 路径） |
| 篡改 manifest 行数（旁车已刷新） | 同上 | 1（`db_row_counts` FAIL，`companies: manifest=99 restored=1`） |

- 归档：`w1-25-drill-r2.tar.gz`，64705 B，sha256 `d52b8753a7c9e7af4faac492c741ab61b37041d1984b348d3cb4bd06ba404cbd`，7 个成员（manifest + dump + 3 附件 + 任务库 2 文件），凭据扫描 0 violation；3 个假凭据全部被排除并记入 `excluded_credential_files`。
- 行数比对（manifest vs 恢复后库；恢复侧工具与独立 `psql` 查询两条路径分别核对）：`companies` 1/1、`employees` 2/2、`expense_claims` 1/1、`expense_lines` 2/2、`audit_events` 1/1、`invoice_verifications` 0/0、`source_files` 2/2，全部一致。
- 附件 sha256：3/3 与 manifest 一致；恢复目录与源目录 `diff -r` 除被排除的 3 个假凭据外无任何差异。
- 任务库：恢复后可打开，`integrity_check=ok`，4 张表（`tasks`/`locks`/`events`/`sqlite_sequence`），43 条任务、414 条事件，与源库一致；恢复出的 `state.sqlite3` / `config.json` sha256 与 manifest 记录（归档内快照）一致。
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
2. **未做跨主机恢复**：本次是同机演练，网络传输、目标机权限、目标机 PostgreSQL 版本差异均未验证。Runbook 与预检 / dry-run 封装见 §11；截至 2026-10-18 仍**没有**真实跨主机演练记录。
3. **pg_dump 版本一致性**：本机 `pg_dump` 17.11 与服务器同主版本；生产若为其它主版本，需以同主版本客户端备份（否则可能出现 dump/restore 失败或降级）。
4. **角色分离未落地**：应用运行期角色目前仍是表属主、有 DDL 权限，`audit_events` 的只增不改触发器可被 `ALTER TABLE ... DISABLE TRIGGER` 绕过（§2 已记录）；备份侧影响是归档不含所有权/GRANT，恢复后必须由角色/授权脚本重建。
5. **恢复时长（RTO）未测**：本机数据量极小（本次归档 64705 B ≈ 63 KB），真实规模下的备份窗口与恢复时间未知。
6. **保留代数与自动调度未实现**：§6 是建议值，尚无 cron/systemd timer 与失败告警；`.orchestration/backups/` 是离线版规则包时期任务库的本地副本（该规则包已于 2026-10-16 切换为 GitHub 版并退役，此目录仅作历史归档），不作为生产备份手段。
7. **内容扫描边界**：凭据排除只覆盖文件名模式与私钥头（§5），不做通用密钥熵检测。
8. **`--no-owner` 的取舍**：为让归档可跨角色恢复而放弃所有权信息；若生产要求归档自带所有权，需要改为同角色恢复或补充授权脚本（与第 4 项一并解决）。
9. **`--allow-missing-*` 的误用边界**：放行开关会把「残缺归档」从失败降级为成功；虽有 manifest `missing_components` 与 stdout `missing parts` 留痕，日常巡检仍应断言 `missing_components` 为空，只在部署事实（确实未启用任务库 / 确实没有附件存储）成立时才使用该开关。
10. **组成部分守卫的检查深度**：只检查「存在且为目录」——目录存在但为空、或缺少 `state.sqlite3` / `config.json` 时备份仍成功（仅记入 `warnings`）；恢复侧对缺失的任务库文件会在 `orchestration_sqlite` / `orchestration_config` 报 FAIL，但「附件存储为空目录」与「零附件」在恢复侧不可区分，生产巡检需另行核对来源清单。
11. **跨主机 Runbook 已就绪但未演练**：§11 的步骤、预检与 dry-run 封装都只在单机验证过；网络传输、目标机权限、目标机 PostgreSQL 版本、真实恢复时长均**未实测**，不能把 dry-run 结果当作生产验收结论（§11.8）。

## 11 跨主机/异地恢复 Runbook 与自动化（dry-run）

- 日期：2026-10-18
- 任务：Issue #22「跨主机/异地恢复 Runbook 与自动化（dry-run，不含真实异地执行）」
- 工具：`ops/offsite_restore.py`（`preflight` 预检 / `rehearse` 本机 dry-run）；恢复本体仍是 `ops/restore.py`（§3、§4 的 13 项校验）
- 测试：`app/tests/ops/test_offsite_restore.py`
- **结论先说**：本节交付的是**可复用 Runbook + 单机 dry-run 封装**；**没有执行任何真实跨主机/异地恢复**（§11.8）。生产验收仍需要一次真实的跨主机演练（§7）。

### 11.1 前置条件

1. **完整归档 + 校验和旁车**：`ops/backup.py` 产物（`<archive>` 与 `<archive>.sha256`），且已离开生产主机（3-2-1 原则，§6）。
2. **归档必须是完整的**：`manifest.missing_components` 必须为空。用 `--allow-missing-storage` / `--allow-missing-orchestration` 生成的归档、以及缺少 `.sha256` 旁车（只能靠 `--allow-missing-checksum` 放行）的归档，**不得用于生产验收**（§10 第 9 项）。`preflight` 会把 `missing_components` 非空直接判 FAIL。
3. **归档先放在 staging 目录（中转），不要放进目标目录**：`<staging-dir>` 是仓库之外、执行账户可读的目录（可与 `<target-dir>` 同级，例如 `/srv/ajaimgr-restore-staging`）。`preflight` 与 `rehearse` **只从 `<staging-dir>` 读归档**（`--archive <staging-dir>/<archive>`）；`<target-dir>` 在这两步执行期间必须**保持为空**。
   - 这是工具的硬性守卫：`--target-dir` 非空即 `rc=2`，报 `--target-dir must be empty: ... holds N entry(ies)`；把归档放进 `<target-dir>/incoming/` 再跑 `preflight` 会**必然失败**。
4. **目标机可登录、可传输**：账户与传输通道（如 `rsync` over 受控网络）由 DBA 提供；本 Issue 不涉及凭据分发与网络配置。
5. **目标库是新的演练库**：建议命名 `<app>_restore_check`；目标库名不得是 `postgres` / `template*` / `ajaimgr_dev` / `ajaimgr_test` / `ajaimgr_prod` 等受保护或生产名。
6. **目标目录已存在且为空**：`<target-dir>` 由 DBA 创建（属主为执行账户，建议 `0700`），本工具**不会自动创建**，也不会覆盖已有文件。它只承载**恢复产物**：`preflight` 不写它；`rehearse`（可选，步骤 0.3）会写入 `incoming/`、`work/`、`restored-storage/`、`restored-orchestration/`、`evidence/`；真实传输（步骤 1）与恢复（步骤 4）随后写入 `incoming/`、`run/`、`evidence/`。因此 `preflight` / `rehearse` 必须在**传输之前、目录仍为空**时执行（顺序见 §11.3）：
   - 执行过 `rehearse` 之后该目录已非空，**不能**再对它跑 `preflight` / `rehearse`；重试要么换一个新的空目录（推荐，如 `<target-dir>-r2`），要么在**保留证据、确认失败原因已修复**后清理该目录再跑（不得用清理掩盖失败）。
7. **连接参数只放环境变量**：执行账户的 shell 里设置 `PGHOST` / `PGUSER`（口令由 libpq 经 `PGPASSWORD`、`PGPASSFILE` 或客户端证书读取）。本工具只检查"是否设置"，**不读取、不回显、不落盘其值**；命令行不接受 DSN，也不接受任何口令参数。
8. **演练窗口**：冻结写入（或对只读快照操作），并预留回滚时间（§11.5）。

### 11.2 目标机要求

| 项目 | 要求 | 校验方式 |
| --- | --- | --- |
| PostgreSQL 服务器主版本 | **≥ 归档 `pg_dump` 主版本**（`manifest.pg_dump_version`，如 17）；更旧的服务器无法加载较新 `pg_dump` 的输出 | `preflight` 的 `pg_target_major` 检查（`--target-pg-major` 显式声明） |
| 客户端工具 | `psql` / `pg_dump` 与**目标服务器同主版本**（`psql --version`） | 目标机人工执行；本工具在证据里记录本机客户端版本 |
| 目录 | **`<staging-dir>`（归档中转，仓库外）**：存放 `<archive>` 与 `<archive>.sha256`，供 `preflight` / `rehearse` 读取；<br>**`<target-dir>`（恢复目标）** 存在、为空、属主为执行账户、建议 `0700`；`incoming/` 放（真实传输的）归档，`restored-storage/` 放附件，`restored-orchestration/` 放任务库，`work/` 放解包产物，`run/` 放 `restore.py` 产物，`evidence/` 放证据 | `preflight` 检查 `<target-dir>` 存在/**为空**/可写（非空即 `rc=2`）；上述子目录由 `rehearse` 与步骤 1/4/7 创建，`preflight` 不写任何文件 |
| 磁盘 | `<target-dir>` 所在分区 ≥ 归档体积 ×3（`incoming/` 副本 + `work/` 解包 + `run/` 与 `restored-*/` 恢复产物）；`<staging-dir>` 另需 1 份归档体积（与 `<target-dir>` 同盘时合计 ×4） | 目标机 `df -h <staging-dir> <target-dir>` 人工确认 |
| 数据库 | 目标实例可连、执行账户有 `CREATEDB`（或由 DBA 预建空库） | `sudo -u postgres createdb -O <app-role> <target-db>` 或 DBA 预建 |
| 角色 | 恢复后由角色/授权脚本重建所有权与 `GRANT`（归档是 `--no-owner`，§9） | 见 §9「所有权与授权」行 |
| 时钟 | NTP 同步（库内 `timestamptz` 存 UTC） | 目标机 `timedatectl` 人工确认 |
| 服务账户 | 应用以独立 UID 运行，`<target-dir>` 与 `AJAIMGR_STORAGE_ROOT` 均为该账户可写 | 部署清单（§3 of `DEPLOYMENT-REQUIREMENTS.md`） |

### 11.3 分步命令

> 占位符约定：`<archive>`、`<staging-dir>`、`<target-host>`、`<target-dir>`、`<target-db>`、`<account>`、`<app-role>` 全部由操作者替换；仓库与文档中**不得**出现真实主机名、IP、账号或口令。
> **执行顺序（务必自上而下）**：`0.1 → 0.2 → 0.3（可选）→ 1 → 2 → 3 → 4 → 5 → 6 → 7`。
> 关键约束：`0.2` / `0.3` 都要求 `<target-dir>` **为空**，且两者都从 `<staging-dir>` 读归档；归档**不得**提前放进 `<target-dir>/incoming/`（否则 `preflight` 直接 `rc=2` 报 `must be empty`）。真实传输（步骤 1）在预检/演练之后执行，之后的写盘都在同一个 `<target-dir>` 内，互不冲突（步骤 1 只是把 `incoming/` 里的 dry-run 副本换成真实传输件）。
> `ops/offsite_restore.py` 输出的 `plan` 与下面**步骤 1–7 一一对应**，工具只渲染命令、**不执行**。

**步骤 0（准备 + 预检，在目标机执行）**

```bash
# 0.0 归档只放在 staging（仓库外），目标目录随后保持为空
ls -l <staging-dir>/<archive> <staging-dir>/<archive>.sha256

# 0.1 目标机自检：客户端/服务器主版本、磁盘、目录属性
psql --version
df -h <target-dir>
ls -ld <staging-dir> <target-dir>
test -z "$(ls -A <target-dir>)" && echo "target-dir is empty"

# 0.2 只读预检：参数、归档完整性、目标目录、PG 主版本（不写目标目录）
#     注意 --archive 指向 staging，--target-dir 仍是空目录
PGHOST=<target-host> PGUSER=<account> \
uv run python ops/offsite_restore.py preflight \
  --archive <staging-dir>/<archive> \
  --target-host <target-host> \
  --target-db <target-db> \
  --target-dir <target-dir> \
  --target-pg-major 17
```

**步骤 0.3（可选，本机 dry-run；必须在步骤 1 之前、`<target-dir>` 仍为空时执行）** —— 复现目标机的落盘路径，**不执行数据库恢复**：

```bash
PGHOST=<target-host> PGUSER=<account> \
uv run python ops/offsite_restore.py rehearse \
  --archive <staging-dir>/<archive> \
  --target-host <target-host> \
  --target-db <target-db> \
  --target-dir <target-dir> \
  --target-pg-major 17 \
  --evidence <target-dir>/evidence/offsite-restore.json --verbose
```

- `rehearse` 会**真实写入** `<target-dir>` 的子目录：`incoming/`（归档副本 + 旁车）、`work/extracted/`（解包）、`restored-storage/`（附件）、`restored-orchestration/`（任务库）、`evidence/`（证据）。这正是它要验证的落盘路径。
- 因此它**只能在空目录上跑一次**：跑完 `<target-dir>` 已非空，再跑 `preflight` / `rehearse` 会报 `must be empty`。重试要么换新的空目录（推荐，如 `<target-dir>-r2`），要么在保留证据、确认原因已修复后清理该目录再跑。
- 因为它在传输之前执行，**不冲突**：步骤 1 的 `rsync` 会把同名的真实传输件写进已存在的 `incoming/`（`--checksum` 下内容一致则跳过），步骤 2 再独立复核旁车；步骤 4 的 `restore.py` 只写 `run/`，与 `restored-*` 不重名。
- 若不需要 dry-run，可**跳过 0.3**，直接进入步骤 1；此时 `incoming/` 由 `rsync` 创建。

**步骤 1（传输）** —— 归档与旁车**必须一起**传输：

```bash
rsync -a --checksum <staging-dir>/<archive> <staging-dir>/<archive>.sha256 \
  <account>@<target-host>:<target-dir>/incoming/
```

**步骤 2（传输校验，在目标机执行）**：

```bash
cd <target-dir>/incoming && sha256sum -c <archive>.sha256
```

`preflight` 本身不校验传输副本；`rehearse` 的 `transfer_checksum` 项在 dry-run 中复现同一判定（源归档 → `<target-dir>/incoming/` 的副本字节散列必须等于源归档）。真实传输的判定以本步骤的 `sha256sum -c` 为准。从步骤 1 起 `<target-dir>` 不再为空，`0.2` / `0.3` 不可重跑。

**步骤 3（建库；先确认目标库不存在）**：

```bash
# 本工具不执行 DROP、不提供 --recreate-db 等价开关
psql -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '<target-db>'"
sudo -u postgres createdb -O <app-role> <target-db>
```

**步骤 4（恢复 + 逐项校验，由 `ops/restore.py` 执行）**：

```bash
PGHOST=<target-host> PGUSER=<account> \
uv run python ops/restore.py <target-dir>/incoming/<archive> \
  --target-db-url postgresql+psycopg:///<target-db> \
  --storage-target <target-dir>/run/storage \
  --orchestration-target <target-dir>/run/orchestration \
  --report <target-dir>/evidence/restore-report.json --verbose
```

- **禁止**追加 `--allow-missing-checksum` / `--allow-missing-storage` / `--allow-missing-orchestration`（这三个开关仅供本地演练，**禁止用于生产验收**，§10 第 9 项）；`ops/offsite_restore.py` 会直接拒绝这三个参数。
- **禁止**追加 `--recreate-db`：目标必须是新建的演练库，不能对既有库执行 `DROP`。
- 退出码语义同 §3：`0` = 13 项全 PASS，`1` = 有校验项不符，`2` = 用法/IO/环境错误。

**步骤 5（校验）**：按 §11.4 清单逐条核对，任一项不满足即按 §11.5 回滚。

**步骤 6（切换服务）**：

```bash
# 1) 先备份现网配置（含 DB_URL / STORAGE_ROOT，口令不写进配置文件明文）
# 2) 停服务 → 修改 AJAIMGR_DB_URL / AJAIMGR_STORAGE_ROOT 指向新库/新目录 → 启动
# 3) 先跑健康检查，再开放写入
```

**步骤 7（证据留存）**：把 `offsite-restore.json`、`restore-report.json` 与命令输出归档到 `<target-dir>/evidence/`，复制一份到受控存储（§11.6）。

### 11.4 校验清单

逐条勾选，**不得以 SKIP 项充当通过**：

| # | 校验项 | 判定 | 证据来源 |
| --- | --- | --- | --- |
| 1 | 归档 sha256 与旁车一致 | 一致 | `preflight` / `rehearse` 的 `archive_checksum` |
| 2 | 归档成员安全（无绝对路径 / `..` / 链接 / 设备文件） | 无危险成员 | `archive_members` |
| 3 | manifest 格式与版本可识别、成员清单非空 | `ajaimgr-backup` / `1` | `manifest` |
| 3.1 | `manifest.missing_components` 为空（归档完整） | 空 list | `manifest_completeness`（非空即 FAIL：该归档来自 `--allow-missing-*`，禁止用于验收） |
| 4 | 逐成员 sha256 + 字节数与 manifest 一致 | 全部一致 | `member_hashes` |
| 5 | 目标机 PG 主版本 ≥ 归档 `pg_dump` 主版本 | 成立 | `pg_target_major` |
| 6 | 传输副本与源归档字节一致 | 一致 | `rehearse`（步骤 0.3）的 `transfer_checksum`；真实传输用步骤 2 的 `sha256sum -c`（`preflight` 不做传输校验） |
| 7 | 目标目录内解包成功 | 成功 | `rehearsal_extract`（步骤 0.3） |
| 8 | 附件逐个落盘且 sha256/字节数一致 | 全部一致 | `storage_restore` / `storage_hashes` |
| 9 | 内容寻址 blob 文件名等于其内容 sha256 | 成立 | `storage_content_address` |
| 10 | 任务库文件落盘 | 全部落盘 | `orchestration_restore` |
| 11 | `state.sqlite3` 可打开、`integrity_check=ok`、表数一致 | 成立 | `orchestration_sqlite` |
| 12 | `config.json` 可解析 | 成立 | `orchestration_config` |
| 13 | **数据库恢复 13 项校验（§4）** | 全 PASS | `restore-report.json`（步骤 4） |
| 14 | 关键表行数与 manifest 一致 | 7 张关键表全部一致 | 报告 `db_row_counts` + 独立 `psql count(*)` 复核 |
| 15 | 恢复目录与源目录一致（排除凭据文件） | 无差异 | `diff -r` 人工复核 |
| 16 | 目标库、目标目录没有**清单外**的遗留文件 | 演练库按需删除；目录内容仅为下述已知产物 | `dropdb <target-db>` + `ls -A <target-dir>` |
| 17 | 证据文件齐全（含 `dry_run_only` / `database_step` 字段） | 齐全 | §11.6 |

第 13–15 项需要数据库，**只能在目标机执行**；`rehearse` 的 `db_restore` 会显示 `SKIP / NOT EXECUTED`，这不是通过。

`<target-dir>` 的**已知产物**（不属于第 16 项的"遗留"）：`incoming/`（步骤 0.3 的 dry-run 副本 + 步骤 1 的真实传输件）、`work/`（步骤 0.3 解包）、`restored-storage/` / `restored-orchestration/`（步骤 0.3 落盘）、`run/`（步骤 4 恢复产物）、`evidence/`（步骤 0.3 / 4 / 7 证据）。除此之外的未知文件（例如上一次演练的残留、误放进来的归档）一律视为遗留，验收前清掉；演练库 `dropdb` 后按需整体清理 `<target-dir>`。

### 11.5 失败回滚

| 失败点 | 处置 | 注意 |
| --- | --- | --- |
| 步骤 0 预检失败（缺参、受保护库名、目录不存在/非空、主版本不兼容） | 修正参数或环境后重跑；目标目录保持原样 | 本工具在预检失败时**不写目标目录**。若报 `--target-dir must be empty: ... holds 1 entry(ies) (incoming)`，说明归档被放进了 `<target-dir>/incoming/`（或上一次 `rehearse` 已写入）：把归档移回 `<staging-dir>`，换一个新的空目录作为 `<target-dir>` 重跑，不要就地删除未知文件 |
| 步骤 0.3 dry-run 失败 | 保留证据；`<target-dir>` 里可能已有部分产物（`incoming/` 等） | 该目录已非空，重试必须换新的空目录（如 `<target-dir>-r2`）；不要用"清空后重跑"掩盖真实失败 |
| 步骤 1–2 传输失败/校验和不符 | 删除 `<target-dir>/incoming` 下的残片，重新传输；必要时重新生成归档 | 生产库未受影响（尚未建库）。执行过 0.3 时 `incoming/` 已有 dry-run 副本，`rsync --checksum` 会覆盖为真实传输件，步骤 2 仍独立复核算真 |
| 步骤 4 恢复失败（退出码 1/2） | 保留 `restore-report.json`；确认目标库是**演练库**后 `dropdb <target-db>`，清空 `<target-dir>/run` 重来 | 严禁对生产库执行 `dropdb` / `--recreate-db` |
| 步骤 6 切换后发现问题 | 停服务 → 把 `AJAIMGR_DB_URL` / `AJAIMGR_STORAGE_ROOT` 指回原库/原目录 → 启动 → 复跑健康检查 | 原库在切换窗口内必须保持可写或已有快照；回滚配置在切换前必须备份 |
| 任意阶段判定"不可继续" | 停在这里并记录证据；**不得**用 `--allow-missing-*` 把失败降级为成功 | 见 §10 第 9 项 |

回滚后必须重新执行 §11.4 全量校验；"进程能起来"不等于恢复通过。

### 11.6 证据留存格式

- 文件名：`<target-dir>/evidence/offsite-restore-<UTC 时间戳>.json`（`preflight` 默认不写文件，需显式 `--evidence`）。
  - 预检（步骤 0.2）若要留证据，写到 `<staging-dir>/evidence/` 或目标目录之外的路径：`preflight` 若把 `evidence/` 建在 `<target-dir>` 内，目录立即非空，紧接着的 `rehearse`（步骤 0.3）会报 `must be empty`。
  - `rehearse`（步骤 0.3）的 `evidence/` 写在 `<target-dir>` 内属于预期产物，与上述限制不冲突（它本身就是最后一步写盘）。
- 存放：**目标机本地 + 受控存储**各一份；**不得提交进仓库**（工具会拒绝把 `--evidence` 指向仓库内）。
- 关键字段：

| 字段 | 含义 |
| --- | --- |
| `tool` / `mode` / `scope` / `dry_run_only` | 工具名、`preflight`/`rehearse`、`dry-run/local`、恒为 `true` |
| `started_at` / `finished_at` | UTC 时间（取证用） |
| `result` | `PASS` / `FAIL`（**SKIP 不等于 PASS**，见 `database_step`） |
| `archive.path` / `.sha256` / `.size_bytes` / `.sidecar` | 归档身份与校验和 |
| `target.host_label` / `.database` / `.directory` / `.pg_major_declared` | 操作者声明的目标环境（主机只是标签，不解析、不连接） |
| `environment.required_keys` / `present_keys` / `values_recorded` | 只记录键名与"是否设置"，`values_recorded` 恒为 `false` |
| `database_step` | `PLANNED`（preflight）/ `NOT_EXECUTED`（rehearse）；真实演练应在 `restore-report.json` 看到 13 项 PASS |
| `checks[]` | 逐项 `name` / `status`（PASS/FAIL/SKIP）/ `detail` / `diffs` |
| `storage.files[]` | 恢复后逐个附件的 `path` / `sha256` / `size_bytes` / `ok` |
| `orchestration.state_table_count` / `.state_integrity` | 任务库表数与完整性 |
| `artifacts` | 传输副本、附件目录、任务库目录的实际落盘位置 |
| `plan[]` | 目标机 7 步命令（本工具只渲染，不执行） |
| `warnings[]` / `notes[]` | 告警与边界声明（含"未执行真实跨主机恢复""禁止 `--allow-missing-*`"） |

### 11.7 与 `ops/restore.py` 的关系（不改恢复语义）

`ops/offsite_restore.py` 只是一层**声明校验 + 单机演练**封装：

- 归档校验和、成员安全、manifest、逐成员散列、附件/任务库落盘与散列复核，全部**调用** `ops/restore.py` 里既有的函数（`check_checksum` / `extract_archive` / `check_manifest` / `check_member_hashes` / `restore_storage` / `restore_orchestration`），没有第二套实现；
- 数据库恢复仍由 `ops/restore.py` 执行，本工具不连接任何数据库、不改 `restore.py` 的恢复语义；
- 差异只在**入口守卫**：目标库名、目标目录、PG 主版本、`PG*` 环境变量、`--allow-missing-*` 一律在这里先 fail closed（`ops/restore.py` 保留 `--force-target` 逃生口，本工具**没有**）。

### 11.8 本 Issue 的边界（**未做的事**）

1. **未执行真实跨主机/异地恢复**：本文与工具只覆盖"单机 dry-run"，没有任何网络连接、`ssh`/`scp` 调用或异地数据库连接。
2. **数据库恢复步骤未执行**：`rehearse` 的 `db_restore` 恒为 `SKIP / NOT_EXECUTED`；真正的恢复要在目标机跑 `ops/restore.py`（步骤 4）。
3. **网络与权限未验证**：传输带宽/中断恢复、目标机账户权限、防火墙、目标机 PostgreSQL 版本与扩展差异均未实测。
4. **§8 的 13/13 仍是同机记录**：那次演练在**同一台主机**上用真实数据完成（Debian + PG 17.11），不等于跨主机验收。
5. **保留/调度未变**：仍按 §6 的建议值，无自动调度与失败告警。
6. 因此：`PROJECT-CHARTER.md` 的「可在生产主机之外恢复」**仍未满足**，需按 §7 每季度做一次真实跨主机演练并留存 §11.6 证据。

### 11.9 本机 dry-run 实测记录（2026-10-18）

环境：Windows 10 + Python 3.13.14，**本机无 PostgreSQL、无 `psql`/`pg_dump`**；归档为测试夹具（`app/tests/ops/test_offsite_restore.py` 用 `tarfile` 构造，结构与 `ops/backup.py` 产物一致，`db/database.sql` 为占位内容且从未被 `psql` 消费）。

| 步骤 | 命令（要点） | 退出码 |
| --- | --- | --- |
| 预检 | `python ops/offsite_restore.py preflight --archive <fixture>.tar.gz --target-host target-host --target-db ajaimgr_restore_check --target-dir <tmp>/target --target-pg-major 17` | 0（6 项 PASS） |
| dry-run | 同上改 `rehearse --evidence <tmp>/evidence.json --verbose` | 0（14 项 PASS + `db_restore` SKIP，共 15 条） |
| 缺参 | `preflight`（只给 `--archive`） | 2（一次性列出 5 个缺失参数 + 缺 `PGHOST`/`PGUSER`） |
| 受保护库名 | `--target-db postgres` | 2（`refusing protected database name`，并说明本工具无 `--force-target`） |
| 目标目录不存在 | `--target-dir <不存在路径>` | 2（含解析后的绝对路径与"本工具不自动创建"） |
| 主版本不匹配 | `manifest pg_dump 17` + `--target-pg-major 13` | 1（`pg_target_major` FAIL，说明"不能被更旧的主版本加载"） |
| 拒绝放行开关 | 追加 `--allow-missing-checksum` | 2（`禁止用于生产验收`） |
| 篡改归档 | 改一个 blob 字节并刷新旁车 | 1（`member_hashes` FAIL，目标目录保持为空） |
| 目录约束（正向）：归档在目标目录之外 | `<fixture>/x.tar.gz` + `--target-dir <tmp>/target`（空目录） | 0（`preflight` 不写目标目录，故同一空目录可连续跑 `preflight` → `rehearse`；这是 §11.3 的 staging 约定） |
| 目录约束（反向）：归档被放进目标目录 | `--archive <tmp>/target/incoming/x.tar.gz` + `--target-dir <tmp>/target` | 2（`--target-dir must be empty: ... holds 1 entry(ies) (incoming)`；§11.3 已据此改为从 `<staging-dir>` 读取） |

边界：以上全部是**单机夹具**演练，**不是**真实数据、不是真实异地主机、**不是**生产验收。

补充（第 1 轮 master 复核）：`rehearse` 跑完后 `<target-dir>` 不再为空，此时再跑 `preflight` / `rehearse` 一律 `rc=2`（`must be empty`）。§11.3 因此把 `rehearse` 前移到步骤 0.3（传输之前、目录仍空时执行一次），并要求归档只放在 `<staging-dir>`。
