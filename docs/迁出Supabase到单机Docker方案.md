# 迁出 Supabase：数据库与文件改到 Lightsail 实例上

状态：草案，待维护者确认（2026-10-27）
关联：`docs/部署平台迁移到AWS方案.md`（单机拓扑）、RUNBOOK 9.4、PR #61（worker 空闲退避）

## 1. 背景

2026-10-27，评分助手在生产上传不了文件。直传申请 `POST /api/papers/direct-upload-intents` 返回 502 `DIRECT_UPLOAD_SIGNING_FAILED`。

- **真正原因**：Supabase 对签名上传请求返回 **402**。组织用的是 Free 套餐，超出免费额度后触发了 Fair Use 限制。
- **影响范围**：限制作用于组织里的所有项目，而且还可能升级为“数据库只读”或“暂停项目”。宽限期已经用过，再超会直接被限制。
- **项目自身用量**：数据库 22 MB、存储 2.7 MB，远低于额度。
- **疑似原因**：连接池的出流量。14 天执行了 1219 万条语句，绝大部分来自空闲 worker 的轮询。

维护者当天决定：**不再使用 Supabase，数据库和文件都改到现有的 Lightsail 实例上**。

## 2. 已定的决定（维护者 2026-10-27）

| 编号 | 决定 |
|---|---|
| D1 | 不再使用 Supabase（数据库与存储都不用）。 |
| D2 | PostgreSQL 用 Docker 跑在现有实例上；文件存实例磁盘（`STORAGE_PROVIDER=local`），不跑 S3 兼容服务。每月不增加费用。 |
| D3 | 只迁移固定配置与人员账号（含密码）；其余生产数据都不迁移。 |
| D4 | worker 空闲退避（PR #61）先行，单独发布。 |

被否掉的方案及理由见第 12 节。

## 3. 目标拓扑

```
            ┌──────────── Lightsail 实例（2 GB，新加坡）────────────┐
 浏览器 ──▶ │ caddy ──▶ app ──┐                                      │
            │                 ├──▶ db（postgres:16，卷 pg_data）     │
            │          worker ┘                                      │
            │   app、worker 共用卷 app_storage（上传文件、L0 缓存）    │
            │   /opt/pgs/backups（每晚逻辑备份，保留 7 份）           │
            └────────────────────────────────────────────────────────┘
                    实例每日自动快照（AWS 保存，保留 7 份）
```

### 3.1 数据库容器 `db`

- **镜像**：`postgres:16-bookworm`。生产镜像里的客户端（`POSTGRES_MAJOR=16`）、CI 的迁移门禁和本地 compose 都是 16；用 Debian 镜像，排序规则与 CI 一致（不用 alpine 的 musl）。
- **不暴露端口**：只在 compose 内网可达。app 与 worker 用主机名 `db:5432` 连接；GitHub 的运行器连不到它（见 5.2）。
- **卷**：`pg_data`，删容器不丢数据。
- **超级用户密码**：`POSTGRES_PASSWORD` 写在单独的 `/opt/pgs/db.env`（0600），只给 `db` 服务。app 与 worker 的 `.env` 里没有这个密码。
- **内存参数**（实例 2 GB，现在用了约 860 MB，另有 2 GB swap）：

  | 参数 | 取值 |
  |---|---|
  | `shared_buffers` | 128MB |
  | `effective_cache_size` | 512MB |
  | `work_mem` | 4MB |
  | `max_connections` | 50 |

  以上用 `command: postgres -c …` 写在 compose 里，随仓库评审。
- **健康检查与启动顺序**：健康检查用 `pg_isready`；app 与 worker 设 `depends_on: db: condition: service_healthy`。
- **连接池**：直连后 `db/session.py` 不再走 `NullPool`（那只对 6543 事务池），改用 SQLAlchemy 默认的 QueuePool，每个进程最多 15 条连接。worker 有 8 条领取线程，每个执行中的条目还有一条心跳线程。实施时要按并发显式设 `pool_size` / `max_overflow`，并保证 app 与 worker 的上限之和小于 `max_connections`。
- **角色**：
  - `postgres`：超级用户，只用于初始化和迁移。
  - `pgs_app`：运行角色，沿用现在的做法——只有 DML 权限，全表开 RLS 并建 `pgs_app_dml` 策略，由 `init_database.py` 建好。

### 3.2 文件存储

- `STORAGE_PROVIDER=local`，`STORAGE_ROOT=/app/storage`。这个卷现在已经由 app 与 worker 共用，L0 缓存就在里面。
- **上传路径**：能力接口返回 `provider: local` 时，前端本来就走经过 app 的普通上传（`POST /papers/upload`，multipart）。所以**前端不用改**，浏览器也不会直接连存储，不涉及签名地址、CORS 和 CSP。
- **大小上限**：Caddy 的 `request_body max_size 50MB` 与 `MAX_UPLOAD_SIZE_MB=50` 已经对齐。Lightsail 是常驻进程，没有 Vercel 那种请求体限制，原来需要直传正是因为 Vercel。
- **磁盘**：实例 60 GB，空闲约 53 GB；单个文件最多 50 MB。磁盘监控见第 11 节。

## 4. 代码与配置改动

| 位置 | 改动 |
|---|---|
| `deploy/production/compose.yml` | 新增 `db` 服务与卷 `pg_data`；`STORAGE_PROVIDER: local`；app/worker 依赖 `db` 健康。 |
| `scripts/production_env.py` | 必需变量去掉 `SUPABASE_URL` / `SUPABASE_SECRET_KEY`；`DATABASE_URL` 的校验从“端口必须 6543”改为“主机 `db`、端口 5432”；另写 `db.env`（`POSTGRES_PASSWORD`）。 |
| `.github/workflows/ci.yml`（部署任务） | 渲染并上传 `db.env`；部署前检查实例磁盘空闲空间。 |
| `.github/workflows/production-migrate.yml` | 改为经 SSH 在实例上跑一次性容器执行迁移（见 5.2），保留手动触发、审批、`expected_head` 与 `dry_run`。 |
| 新增 `backend/app/scripts/copy_fixed_config.py` | 一次性把固定配置与账号从旧库复制到新库（见第 6 节）。 |
| 新增 `.github/workflows/production-cutover.yml` | 手动触发并审批：初始化新库 → 复制配置 → 切换连接 → 核对（见第 8 节）。 |
| `deploy/production/compose.yml` 的 `backup` 服务 | 同一镜像，每天 03:00（UTC+8）调用 `ops_backup create`，保留 7 份，空间不足时拒绝并记日志（见第 7 节）。随 compose 部署：部署用户 `deploy` 没有 root，装不了 systemd 定时器。 |
| `backend/app/scripts/ops_backup.py` | `--migration-head` 默认值还停在 `0028`，改为读取当前 Alembic head。 |
| `backend/app/db/session.py` | 直连时按配置设连接池大小（见 3.1）。 |
| 测试 | `test_production_deployment.py` 里“compose 只有 caddy/app/worker、没有数据库”的断言改为四个服务，并校验 `db` 不暴露端口、不读 `.env`；补 `production_env` 新校验、`copy_fixed_config`（SQLite → SQLite，以及 CI 上 Postgres → Postgres）的测试。 |
| 文档 | CLAUDE.md、PROJECT_MEMORY.md、ARCHITECTURE / DECISIONS / RUNBOOK、`docs/上线清单.md`、`docs/部署平台迁移到AWS方案.md`；`test_m8_documentation_contract.py` 里与 Supabase 相关的边界事实。 |
| 清理（切换稳定后另开 PR，见 Q5） | 删除 Supabase 存储实现、直传接口、`supabase` 依赖、`init_database` 的建桶步骤、`production_env` 的 6543 分支，以及 Vercel 专用路径里依赖 Supabase 的部分。 |

不改的部分：评分逻辑、提示词、Alembic 迁移（不新增迁移，head 仍为 `0041_assistant_agent_state`）、前端。

## 5. 数据库初始化与迁移

### 5.1 首次初始化

在实例上起一个一次性容器，接入 compose 内网，运行现有的 `init_database`：

1. 建 `pgs_app`；
2. 跑全部迁移；
3. 全表授权、开 RLS；
4. 逐表核对（没有 `storage` 模式时跳过建桶）。

`MIGRATION_DATABASE_URL`（`postgres@db:5432`）与 `PGS_APP_PASSWORD` 经 SSH 的标准输入写进临时文件，用完即删，不出现在命令行和日志里。

### 5.2 之后的迁移

数据库不对外，GitHub 运行器连不到。`production-migrate.yml` 改为：

1. SSH 到实例；
2. 用当前已部署的镜像跑一次性容器 `alembic upgrade head`；
3. 仍然手动触发、production 环境审批，先 `dry_run` 打印待执行的迁移。

owner 连接串只在这次运行里经标准输入传入，不写进实例上长期存在的文件。

## 6. 一次性数据迁移

### 6.1 迁移范围

生产库当前的行数（只统计行数，没有读取内容）：

| 表 | 行数 | 建议 | 说明 |
|---|---|---|---|
| `users` | 2 | **迁移** | 账号与密码哈希原样复制，老师用原密码登录。 |
| `organizations` | 1 | **迁移** | 账号所属组织。 |
| `organization_members` | 2 | **迁移** | 成员与角色。 |
| `platform_llm_config` | 1 | **迁移** | 平台模型配置（密文，见 6.3）。 |
| `ai_connections` | 1 | **迁移** | 模型连接（密文）。保留 ID，评测工作流的 `--connection-id` 照常可用。 |
| `assistant_preferences` | 1 | **迁移** | 评分助手的模型选择。 |
| `organization_invitations` | 2 | 待定（Q1） | 已发出、未接受的邀请。不迁移的话，邮件里的邀请链接会失效。 |
| `site_leads` | 2 | 待定（Q1） | 官网试用申请，是业务线索，不是学生数据。 |
| `audit_logs` | 51 | 待定（Q1） | 操作审计记录。 |
| 其余表 | — | 不迁移 | 评分标准、批次、论文、评分结果、助手会话、用量账本、登录会话等，按 D3 丢弃；所有人需要重新登录。 |

### 6.2 做法：`copy_fixed_config.py`

- **源库**：旧 Supabase 库，经 Session pooler（IPv4、5432）连接，在只读事务（`begin read only … rollback`）里读取。只读事务即使在 Fair Use 把库切成只读之后也能用。
- **目标库**：新库，用 owner 连接写入。
  - 按外键顺序复制：`users` → `organizations` → `organization_members` → 其余各表。
  - 只复制两边都有的列，保留原 ID。
  - 全部在一个事务里完成，任一步失败整体回滚。
- **前置检查**：两边的 Alembic head 必须相同（都是 `0041`）；目标表必须为空，否则拒绝执行，重跑前要先清空新库。
- **输出**：只打印表名和行数，从不打印内容、连接串或密钥。
- **在哪里跑**：作为 `production-cutover.yml` 的一步，在实例上用一次性容器执行，不在任何人的电脑上跑。源库连接串用 GitHub production 环境里现有的 `RUNTIME_DATABASE_URL`：`pgs_app` 对全表有读权限。

### 6.3 加密字段

`ai_connections` 与 `platform_llm_config` 里的密钥是用 `BYOK_MASTER_KEY` 加密的密文，原样复制。只要这个密钥不换，新库就能解密。

切换后由维护者在运维页点一次“测试连接”核对。这一步会真实调用一次模型，费用可以忽略。

## 7. 备份与恢复

两层备份，防的不是同一类问题：

| 层 | 内容 | 防什么 | 费用 |
|---|---|---|---|
| 实例每日自动快照（Lightsail 控制台开启，保留 7 份） | 整块磁盘：数据库卷、文件卷、配置 | 实例损坏、整机误操作；AWS 在实例之外保存 | 按快照实际占用 0.05 美元/GB·月，预计每月不到 1 美元 |
| 每晚逻辑备份（`ops_backup create`，存 `/opt/pgs/backups`，保留 7 份） | `pg_dump` + 存储目录打包 + 校验清单 | 误删数据、迁移出错，需要单独恢复数据库的情况；比整盘恢复快 | 0（占实例磁盘） |

- 最坏情况：实例整机损坏时，从前一天的快照恢复，**最多丢一天数据**。
- 逻辑备份与数据库在同一块盘上，盘坏了要靠快照，它不能替代快照。
- 备份用超级用户连接（读 `db.env`）：`pg_dump` 默认关闭行级安全，`pgs_app` 没有绕过行级安全的权限，会直接报错。
- 备份含学生 PII：目录权限 0700、文件 0600，不进 CI artifact，不出实例（异地备份见 Q4）。
- **恢复演练**：切换完成前，在实例上把当晚的备份恢复到一个临时库（`ops_backup restore --confirm-database …`），核对行数后删除临时库。RUNBOOK 写明步骤。

## 8. 切换步骤

推荐分两步（Q2）。第一步先让上传恢复；第二步在维护窗口里切数据库。

### 第一步：文件改存实例磁盘（随代码 PR 部署，无停机）

- 合并代码 PR，部署时 `STORAGE_PROVIDER` 变为 `local`，`db` 容器随之启动（空库，暂不使用）。
- 数据库仍连 Supabase。上传立即恢复，因为不再调用 Supabase Storage。
- 这期间新上传的材料存在实例磁盘上、记录在 Supabase 库里。第二步按 D3 不迁移它们，过渡期只用于测试。

### 第二步：数据库切换（维护窗口，预计停机约 15 分钟）

**维护者准备**：

1. 在 Lightsail 控制台开启自动快照，并手动拍一份快照。
2. 在 GitHub production 环境设置：
   - `POSTGRES_PASSWORD`、`PGS_APP_PASSWORD`：只用字母和数字，24 位以上；
   - 新的 `MIGRATION_DATABASE_URL`（`postgres@db:5432`）；
   - 旧库连接串另存为 `SOURCE_DATABASE_URL`。
3. 通知老师停机时间，并说明除账号外的数据都不保留。

**运行 `production-cutover.yml`（手动触发 + 审批）**：

1. 停 worker，再停 app（这期间网站不可用，Caddy 返回 502）；
2. 对新库跑 `init_database`；
3. 跑 `copy_fixed_config`，打印每张表复制了多少行；
4. 把 `.env` 的 `DATABASE_URL` 换成 `pgs_app@db:5432`，启动 app 与 worker；
5. 核对：健康检查通过；用原账号能登录；平台模型配置可读；做一次 `ops_backup create` 并 `verify`。

**切换后，由维护者确认**：

1. 测试连接；
2. 上传一篇论文走完评分；
3. 把 GitHub 里的 `RUNTIME_DATABASE_URL` 改成新值，以后每次部署渲染的都是新库。

### 回滚

- **切换后、老师开始使用前**：把 `DATABASE_URL` 改回 Supabase 重新部署即可，新库原样保留，下次再切。
- **老师开始使用新库之后**：回滚会丢掉这期间的新数据，不再提供。之后的故障按第 7 节恢复。
- **Supabase 项目**：切换后保留 7 天（免费套餐不产生费用），确认无误后由维护者删除。删除不可撤销，是维护者自己的操作。

## 9. 费用

| 项 | 现在 | 迁移后 |
|---|---|---|
| Lightsail 实例（2 GB，含 IPv4） | 12 美元/月 | 12 美元/月 |
| 实例快照 | 0 | 预计不到 1 美元/月 |
| Supabase | 0（Free，但已被限制） | 0（删除） |
| 合计 | 12 美元/月 | 约 13 美元/月 |

季度预算 120 美元（含模型）里，每月留给模型的约 27 美元，与现在基本相同。

## 10. 不在这次范围内

- **LISTEN/NOTIFY 叫醒 worker**：直连后可以做到零轮询、零等待，但 PR #61 的退避已经够用，以后另议。
- **老师已上传数据的迁移**：按 D3 不迁移。
- **评分助手上传流程的体验修复**：显示每个文件的失败原因、新建任务页与助手互相清空上传状态、选文件时预检。另开 PR。
- **3 个空草稿批次**：随 Supabase 库一起丢弃，不用单独清理。

## 11. 风险与对策

| 风险 | 对策 |
|---|---|
| 实例磁盘损坏或实例丢失 | 从每日快照恢复，最多丢一天；RUNBOOK 写恢复步骤。 |
| 内存不足（数据库与 app、worker 共用 2 GB） | 第 3.1 节的保守参数；部署后看 `docker stats`。长期不够时升级实例，而不是放宽参数。 |
| 磁盘写满（上传文件 + 备份） | `backup` 服务在空闲不足 5 GB 时拒绝执行并记日志；部署任务报告空闲空间；Lightsail 控制台设磁盘告警。 |
| 切换前 Supabase 把库切成只读或暂停 | 只读不影响复制（只读事务）。暂停的话需要维护者先在控制台恢复项目；恢复不了时，账号只有 2 个，可以手工重建，但模型连接要重新填写密钥。 |
| PostgreSQL 大版本升级 | 不自动升级：镜像固定在 16 的大版本，小版本随镜像更新。升级大版本另写方案（dump/restore）。 |
| 连接池耗尽 | 见 3.1：显式设置连接池大小，保证总数小于 `max_connections`。 |

## 12. 放弃了什么

- **Lightsail 托管 PostgreSQL + 存储桶**：每月多 16 美元，占掉季度预算里约 40% 的模型费用。它自带 7 天任意时间点恢复、数据库独立于实例，这正是自建要付出的代价：备份靠我们自己，最坏丢一天。
- **在实例上跑 S3 兼容服务（如 MinIO）**：单机上只是在磁盘外再包一层，多一个要维护和备份的服务；代码已有 `local` 存储。
- **只把文件搬走、数据库继续用 Supabase**：Fair Use 按组织计算，数据库的出流量照样计费，限制还可能升级为只读或暂停。
- **升级 Supabase Pro**：每月 25 美元，比托管数据库还贵。
- **用 `pg_dump` 整库恢复到新库再删数据**：要把全部生产数据（含学生 PII）搬一遍再删，与 D3 相反；按表复制只碰需要的那几张。
- **一次切完（文件与数据库同一个维护窗口）**：更简单，但上传要一直坏到维护窗口；分两步的话，代码一合并上传就恢复（Q2）。

## 13. 待确认问题

| 编号 | 问题 | 建议 |
|---|---|---|
| Q1 | 6.1 里三张待定的表是否迁移？ | `organization_invitations` 迁移（已发出的邀请链接继续有效）、`site_leads` 迁移（业务线索）、`audit_logs` 不迁移（与已丢弃的数据对应不上）。 |
| Q2 | 分两步（先文件、后数据库）还是一次切完？ | 分两步，上传先恢复。 |
| Q3 | 迁移仍然手动审批，还是部署时自动执行？ | 仍然手动审批，只是改到实例上执行，不改现有规矩。 |
| Q4 | 是否需要异地备份（例如每周把逻辑备份拉到维护者的电脑）？ | 先不做：快照已存在实例之外；备份含 PII，拉到个人电脑反而多一个泄露面。 |
| Q5 | Supabase 相关代码与依赖什么时候删？ | 切换稳定 7 天、Supabase 项目删除后，另开一个清理 PR。 |
| Q6 | PostgreSQL 用哪个大版本？ | 16，与镜像客户端、CI、本地一致；以后统一升级。 |

## 14. 实施顺序

1. **PR #61**：worker 空闲退避，独立合并。
2. **本方案确认后，代码 PR**：第 4 节除“清理”外的全部内容 + 三份文档 + 测试。合并即完成第一步（文件改存实例磁盘）。
3. **维护者准备**（第 8 节），然后运行 `production-cutover.yml`，完成第二步。
4. **观察 7 天**：维护者删除 Supabase 项目，再开清理 PR。

## 维护记录

| 日期 | 变化 |
|---|---|
| 2026-10-27 | 初稿：背景、已定决定 D1–D4、目标拓扑、迁移范围、备份、两步切换、费用、风险、放弃项、待确认 Q1–Q6。 |
