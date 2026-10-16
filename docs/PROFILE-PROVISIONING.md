# 每员工 Profile 供给：`app/runtime/` 领域说明（W1-15）

- 任务：W1-15（每员工 Hermes Profile 的配置生成器与目录校验器）
- 交付物：`app/runtime/`（第 12 个领域目录）+ `app/tests/runtime/` + 本文档
- 状态：**生成器与校验器已实现并测试通过；Profile 尚未实际部署**（见 §4）

## 1 为什么新增 `app/runtime/` 领域目录

`templates/PLAN.md` §5.1「平台公共模块」把 **「Hermes Profile 生命周期及状态监控」**
列为平台公共能力之一。在此之前仓库已有 11 个领域目录（`identity` `intake` `expense`
`workflow` `documents` `inventory` `integrations` `audit` `api` `db` `core`），但没有
承载 Profile 生命周期与状态监控的归属。W1-15 据此新增 `app/runtime/` 作为第 12 个
领域目录，用于：

- 生成每员工 Profile 的目录布局、权限要求、服务账户与凭据**引用**、隔离要素清单；
- 校验已有 Profile 目录是否符合要求（供部署后巡检与 CI 使用）；
- 后续扩展 Profile 的**状态监控**（本任务只交付生成器与校验器）。

`app/runtime/__init__.py` 的模块 docstring 说明了职责；实现位于
`app/runtime/profile_provisioning.py`。

## 2 与 `app/api/` 下工具代理路径的关系

两者是**同一条信任链上的上下游**，不重叠：

```
渠道适配层 → (channel, external_user_id) → employee_id        （app/identity/channel_routing.py）
          → 受信任的本地工具代理（PLAN §2.1 / §5.1「统一业务 API 和工具代理」）
          → 注入 X-Employee-Id + 内部令牌 → app/api（app/api/deps.py）
          → 业务域服务
```

- `app/api/deps.py` 的 `get_principal` 定义了**运行期**的主体来源：`employee_id`
  只能来自受信任工具代理注入的 `X-Employee-Id`，内部令牌用 `AJAIMGR_INTERNAL_TOKEN`
  做常量时间比较、缺失即 fail-closed。
- `app/runtime/profile_provisioning.py` 定义的是**部署期**的每员工 Profile 计划：它
  产出该 Profile 的工作目录、临时目录、独立 systemd 服务账户与**内部 API 凭据引用**
  （`AJAIMGR_INTERNAL_TOKEN` / `AJAIMGR_PROFILE_<PROFILE>_API_TOKEN` 等**环境变量名**，
  绝非凭据值）。
- 因此：`app/runtime/` 负责「把每个员工的 Profile 与它的独立内部 API 身份规划出来」，
  `app/api/` 负责「在请求到达时按该身份做确定性授权」。二者共享同一套凭据**引用名**
  约定，但都不在仓库中保存任何凭据值。

## 3 本任务交付了什么（不实际部署）

`app/runtime/profile_provisioning.py` 提供：

1. `plan_profile(employee, *, base_dir, ...) -> ProfilePlan`：**纯函数**，返回某员工的
   Profile 目录布局、必填文件清单、目录（`0700`）/敏感文件（`0600`）权限要求、
   systemd 服务账户名（`User=` 形式）、内部 API 凭据**引用**、工作目录与临时目录路径、
   以及隔离要素清单。**不创建任何文件**。
2. `render_config(plan) -> Mapping` / `render_config_text(plan) -> str`：产出该 Profile
   的 config 片段（模型 / 技能 / 记忆 / 会话 / 工作目录 / 临时目录）。密钥一律以
   `${VAR}` **引用**出现，**不含任何明文凭据**。
3. `validate_profile_dir(path) -> list[Problem]`：校验已有目录的权限（目录 `0700`、敏感
   文件 `0600`）、必填文件与子目录是否齐备、是否出现明文凭据；逐项列出差异。**只读**。
4. CLI：`python -m app.runtime.profile_provisioning plan ...`（dry-run，只打印计划，
   不产生任何文件）与 `python -m app.runtime.profile_provisioning validate <path>`
   （不符合时以**非零退出码**退出并列出差异）。

权限与隔离要求来自本仓库既有实测结论：

- `docs/research/W0-2-profile-isolation.md` §3：Profile 只隔离「数据与凭据作用域」，
  **不是 OS 边界**；工作目录、临时目录、内部 API 身份（进程/系统账号）与文件系统边界
  必须由**部署层**补充（独立 systemd `User=`、独立 UID、容器/namespace）。
- `docs/research/W0-7-profile-experiment.md` §5.1：命名 Profile 目录实测 `700`、
  `config.yaml` / `.env` / `state.db` 为 `600`、`SOUL.md` 为 `664`；同一 OS 用户可互读
  任意 Profile（目录权限只防其他 OS 用户，不构成同账号隔离）。
- `docs/DEPLOYMENT-REQUIREMENTS.md` §3：服务身份与进程隔离为**硬要求**，须以负面测试
  证明。

## 4 尚未实际部署（重要）

本任务**不部署**任何 Profile：

- 未在本机新建、启动或停止任何真实 Hermes Profile；
- 未读写 `~/.hermes`（含 `~/.hermes/profiles`）下的任何内容；
- 未调用任何 `hermes` 子命令、未生成任何真实 `config.yaml` / `.env`；
- 生成器与校验器只产出**计划数据**与**校验结果**，不落地。

因此以下事项**仍未完成、须由后续部署任务在真实环境中验证**（与
`docs/DEPLOYMENT-REQUIREMENTS.md` §8 一致）：

- 真实 systemd 独立服务账户 / 独立 UID / `ProtectSystem`、`ProtectHome` 下的内核级
  隔离**负面测试**（「员工 A 无法读取员工 B 的文件/进程/网络/临时目录」）；
- 生产主机规格与 20 Profile 并发容量（章程 O1 / A4）；
- 多路复用运行时凭据 fail-closed 的端到端验证（W0-7 §7.3）。

在此之前，本文档与生成器只提供**规划与校验能力**，不构成「已部署」或「已通过隔离
验收」的任何声明。
