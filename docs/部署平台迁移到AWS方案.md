# 部署平台迁移方案：从 Vercel 迁到 AWS

状态：**已确认**（2026-10-13，第三版：全新环境）。第 10 节的问题已全部答复或按建议执行。

背景：2026-10-12 讨论过换平台，结论是用常驻进程、不用 Lambda / Cloud Functions，服务必须放在新加坡。2026-10-13 维护者确认：使用者面向全球，平台选 AWS，**可接受的预算是每季度 120 美元（约 40 美元/月）**。

第一版方案是 ECS Fargate + CloudFront，约 120–140 美元/月，超出预算三倍以上。第二版改为**一台 Lightsail 实例跑 docker compose**，约 10–12 美元/月。第一版放弃的理由见第 9 节。

第三版（同日）：维护者已联系用户，**生产数据不再保留**，改为全新环境——新建 Supabase 项目 `grading-system`（`mfkmbwkuiimhaytkazvk`，新加坡），用初始化脚本建库（4.13），所有密钥重新生成并存进 GitHub `production` 环境（4.8）；不迁移数据，也不再新旧并行。旧项目在切流量后由维护者删除。

**2026-10-27 更新**：Supabase 组织被 Fair Use 限制（上传返回 402），维护者决定不再使用 Supabase：数据库改为本实例上的 db 容器、文件存实例磁盘，只迁移账号与固定配置。本文里“数据库与存储在 Supabase”的部分以 [迁出Supabase到单机Docker方案.md](迁出Supabase到单机Docker方案.md) 为准。

---

## 1. 要解决什么、不动什么

| Vercel 的限制 | 现在的迁就 | 迁移后 |
|---|---|---|
| 冷启动 7–12 秒 | `App.vue` 的“正在连接服务器…”提示 | 进程常驻，没有冷启动 |
| 单次函数最长 300 秒 | 超过 300 秒的单篇靠多次执行续评；起草时间预算 260 秒 | worker 是常驻进程，没有执行时限；web 请求也不再有 300 秒上限 |
| 没有常驻进程 | 用 Vercel Queues（beta）发叫醒消息；巡检靠 2 分钟一条的自续期消息，链断了要补投 | worker 循环自己领取、自己巡检 |
| 生产与内网叫醒方式不同 | 两条路径都要测；CI 冒烟测的是 worker 路径，不是生产路径 | 生产与内网用同一套 docker compose 结构 |

不动的：

- 数据库与文件存储仍用 Supabase（新加坡，Free 套餐），但换成新建的项目，旧数据不迁移（第三版）。
- 模型仍走 Bedrock，连接配置不变。
- 生产迁移仍是手动触发、需要审批的 `production-migrate.yml`。
- 镜像沿用现有 `Dockerfile`，前端沿用已提交的 `public/`。
- 内网 `docker-compose.yml` 与 `deploy/Caddyfile` 不改。

本期不做：

- 全球 CDN。静态资源从新加坡下发，全部压缩后约 260 KB（首屏只加载其中一部分），浏览器缓存后只在发版时重新下载。以后可以在前面加 CloudFront 免费套餐（第 9 节）。
- 多台机器、高可用。
- 放宽由 300 秒推出来的时间预算（如 `RUBRIC_AI_DRAFT_TIME_BUDGET_SECONDS=260`）。这会改变评分行为，另开方案。
- 删除 Vercel 相关代码。等切换稳定后在收尾 PR 里删（第 8 节 S5）。

## 2. 现在哪些地方依赖 Vercel

| 位置 | 现在做什么 | 迁移后 |
|---|---|---|
| `vercel.json` | 区域 `sin1`、函数时限 300 秒、`/workbench` 改写、静态资源缓存头、5 个安全头 | 安全头、缓存头、压缩都由生产用的 Caddyfile 负责（4.4）；改写已由 FastAPI 路由承担 |
| `api/index.py` | Vercel 的 Python 入口 | 不用；容器直接起 uvicorn |
| `services/batch_scoring/vercel_queue.py`、`services/work_queue/wake.py` | 订阅 `pgs-work`、发叫醒、维持巡检链 | 不设 `VERCEL` 与 `BATCH_SCORING_DISPATCH` 时全部是空操作，由 worker 循环领取与巡检——内网已经这样跑 |
| `db/session.py` | 有 `VERCEL` 时用 `NullPool` | 生产连 6543 事务池，按端口本来就走 `NullPool`，行为不变 |
| `api/routes/batch_jobs.py` 的 `run_job` | 有 `VERCEL` 时拒绝在请求里跑整个批任务 | 改成显式配置项，生产同样拒绝。漏了这一步，一个请求就会把整批评分跑在 web 进程里 |
| `Dockerfile` 的 `CMD` | 启动先 `alembic upgrade head` | 生产运行角色 `pgs_app` 没有 DDL；生产 compose 覆盖启动命令，启动时不迁移（4.6） |
| `ci.yml` 的 `deploy-vercel-production` | 推到 main 后部署 Vercel | 新增 `deploy-production`，通过 SSH 部署到实例（4.7）；切换后删掉 Vercel 任务 |
| 公开入口 `paper-grading-system-kohl.vercel.app` | 用户访问地址 | 改为免费子域名；旧地址 308 跳转（4.10） |
| Vercel Production 环境变量（Sensitive） | 运行配置与密钥 | 全部重新生成，存在 GitHub `production` 环境；部署任务据此生成实例上权限 `0600` 的 `.env`（4.8） |

## 3. 目标架构

```
浏览器（全球）
  │ HTTPS
  ▼
Lightsail 实例（ap-southeast-1，静态 IP）
  ├─ caddy   ：Let's Encrypt 证书、zstd/gzip 压缩、安全头、静态资源缓存头
  ├─ app     ：uvicorn，1 个进程
  └─ worker  ：run_batch_worker，8 个线程
        │
        ▼
  Supabase 事务池 6543（同区域） · Supabase Storage · Bedrock

浏览器 ──签名直传──▶ Supabase Storage（不变）
```

| 项目 | 选择 | 说明 |
|---|---|---|
| 实例 | Lightsail Linux，2 GB 内存 / 2 vCPU / 60 GB SSD，Ubuntu 24.04 LTS，新加坡 | 套餐内含公网 IPv4 和流量额度；静态 IP 绑定实例时免费 |
| 内存 | web 1 个进程、worker 1 个进程，另开 2 GB swap 兜底 | 本机实测（2026-10-13，macOS）：加载全部模块后单进程常驻约 160 MB。加上 Caddy、Docker、系统和解析大文件时的峰值，预计 1–1.2 GB |
| 容器 | 新增 `deploy/production/compose.yml`：`caddy`、`app`、`worker` 三个服务，不含数据库 | 与内网 compose 结构相同，只是数据库与存储换成 Supabase。app 与 worker 共用卷 `/app/storage`，放 L0 缓存（`llm_cache.sqlite`），发版换容器不丢 |
| 防火墙 | Lightsail 防火墙只开 22、80、443；SSH 只允许密钥登录 | |
| 日志 | Docker 日志按大小轮转（单文件 20 MB，保留 5 个），通过 SSH 查看 | 不花钱；代价是没有集中检索 |
| 告警 | 免费的外部可用性监测（如 UptimeRobot）每 5 分钟请求 `/api/system/health`，失败发邮件；Lightsail 实例告警（CPU、状态检查）；AWS Budgets 月度预算告警 | 见 4.9 |

## 4. 关键设计

### 4.1 后台执行：worker 循环替代 Vercel 叫醒

执行模型不用改。统一执行模型（0035）之后，Vercel 与 worker 共用 `claim_next_item`，差别只在谁来叫醒。`wake_enabled()` 为假时，所有叫醒都是空操作；巡检由 `run_worker_loop` 的主线程每 120 秒做一次。

第二版曾计划新旧两边共用一个库、同时领取，再切流量。第三版换了新库，两边不共用数据，也就不并行：新环境上线验证后直接切流量。（同一个库上多处同时领取仍然是安全的——领取用 `SKIP LOCKED`，名额、租约、无进展计数都在数据库里——这在单机上多开 worker 时仍然成立。）

### 4.2 发版时服务怎么停

**worker。** 现在收到 SIGTERM 后主循环退出，每个领取线程最多再等一个轮询周期多一点（约 4 秒），进程就结束。正在评的条目要等 120 秒租约过期，再由巡检重置续评。那次执行如果没写入新检查点，就记一次“无进展”，连续 3 次标为失败。每发一次版，正在评的条目就可能被记一次无进展。

改成：

1. 收到 SIGTERM 后不再领取新条目；
2. 等正在执行的条目做完，最多等 `--drain-seconds` 秒。生产 compose 传 100，`stop_grace_period` 设为 110 秒；命令行默认 0，内网与本地的行为不变（内网 compose 的停止宽限只有 Docker 默认的 10 秒，等也等不到）；
3. 到时还没做完的，照旧交给租约和巡检。

一个 AI 任务条目通常就是一次模型调用，多数能在 100 秒内做完。评分条目按规则写检查点，没做完的部分续评时复用已判完的规则，不重复计费。代价是发版最多要多等约 2 分钟。

**web。** 单台机器上换容器有几秒空档。Caddy 的 `reverse_proxy` 设 `lb_try_duration 30s`：空档期间新请求先等着，后端起来后再转发，用户看到的是几秒延迟而不是报错。`app` 的 `stop_grace_period` 设 60 秒，让进行中的请求做完。

### 4.3 请求时长

Caddy 反向代理默认不限制响应时长，web 请求不再有 Vercel 的 300 秒上限。第一版担心的 CloudFront 60 秒回源超时，在这一版里不存在。

### 4.4 安全头、缓存头、压缩

新增生产用的 `deploy/production/caddy/Caddyfile`（compose 挂整个目录：部署时文件被替换成新的 inode，单文件挂载看不到新内容）：

- 证书：Let's Encrypt 自动签发与续期（内网版用的是 `tls internal`）；
- 安全头：照抄 `vercel.json` 的 5 个，HSTS 带 `includeSubDomains`；
- 缓存头：`/workbench/assets/*` 为 `public, max-age=31536000, immutable`，入口 HTML 为 `public, max-age=0, must-revalidate`；
- 压缩：`encode zstd gzip`，与内网版相同；
- 上传大小：`request_body max_size 50MB`，与 `MAX_UPLOAD_SIZE_MB` 一致。

应用代码不用为此改动。代价是安全头现在有三处定义（`vercel.json`、内网 Caddyfile、生产 Caddyfile）；收尾时删掉 `vercel.json`，剩两处，RUNBOOK 写明要一起改。

### 4.5 数据库连接

新项目同样走 Supabase 事务池（端口 6543）+ `NullPool`，用户是 `pgs_app.<项目 ref>`。实例与 Supabase 在同一区域，每次建连只要几毫秒。Supabase 直连地址只有 IPv6，所以不用直连。部署时校验端口必须是 6543。

### 4.6 启动命令与迁移顺序

`Dockerfile` 不改，内网仍靠它在启动时迁移。生产 compose 覆盖启动命令：

- app：`uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'`。app 端口不对外，只有 Caddy 能连到，所以信任转发头是安全的。
- worker：`python -m backend.app.scripts.run_batch_worker --drain-seconds 100`。

迁移顺序与现在相同：需要迁移的版本，先跑 `production-migrate.yml`，再合并部署。

### 4.7 部署流水线

不用镜像仓库，GitHub 和实例上都不放 AWS 凭据：

1. `deploy-production` 任务等现有门禁（`docker-compose-smoke`、`frontend-workbench`）通过，并经 `production` 环境审批；
2. 在 GitHub 上构建镜像，标签用提交 SHA；
3. `docker save | gzip` 后经 SSH 传到实例，`docker load`；
4. 同步 `deploy/production/` 下的 compose 文件、Caddyfile 与 `deploy.sh`，并用 GitHub 上的密钥生成实例的 `.env`（4.8）；
5. 把这个镜像另打一个 `current` 标签，compose 只认 `current`；先重建 app，等它健康后再重建 worker（worker 要等在执行的条目做完），最后让 Caddy 热加载配置；镜像只保留最近 3 个；
6. 冒烟：请求 `https://<域名>/api/system/health`，失败则任务失败。

回滚：在实例上运行 `deploy/production/deploy.sh <上一个 SHA>`，把 `current` 指回旧镜像再重启，不需要重新构建。`deploy.sh --list` 列出实例上还留着的版本。

GitHub `production` 环境新增 3 个部署密钥：`PRODUCTION_SSH_KEY`（部署用的 SSH 私钥）、`PRODUCTION_SSH_HOST`（实例地址）、`PRODUCTION_SSH_KNOWN_HOSTS`（核对过的主机指纹，防止中间人）；运行时密钥见 4.8。仓库变量（必须是仓库级：任务的 `if` 读不到环境级变量）：`PRODUCTION_SITE_URL`（冒烟地址）、`LIGHTSAIL_DEPLOY_ENABLED`（设为 `true` 才部署）、`VERCEL_DEPLOY_ENABLED`（切流量前设为 `false`，否则下一次合并会把整个应用重新部署回 Vercel、盖掉跳转）。

### 4.8 密钥

- 全新环境，所有密钥重新生成，不需要找回 Vercel 上的旧值（Vercel 的 Sensitive 变量本来也读不回来）。
- **生产密钥只存 GitHub `production` 环境**：`RUNTIME_DATABASE_URL`、`BYOK_MASTER_KEY`、`AUTH_SECRET`、`AUTH_PASSWORD`、`SUPABASE_SECRET_KEY`；非密钥的 `SUPABASE_URL`、`AUTH_USERNAME`、`PRODUCTION_SITE_URL` 是仓库变量。
- 部署任务每次用它们生成实例上的 `/opt/pgs/.env`（`scripts/production_env.py --from-environment`，只取名单里的变量，只打印变量名），经 SSH 标准输入写入，权限 `0600`。改密钥 = 改 GitHub 上的值再部署一次；不在实例上手改。
- 值由维护者在 GitHub 网页或管道里填写（如 `openssl rand -hex 32 | gh secret set …`），Claude 不读取、不经手任何密钥值。
- `AUTH_PASSWORD` 在新库上是**首个平台管理员的登录密码**：第一次用 `AUTH_USERNAME` 登录时用它建账号。
- 手工部署时，同一个脚本也能从一份本机变量文件生成 `.env`（`--source`）。

### 4.9 健康检查与告警

新增 `GET /api/system/health`，不需要登录，返回 200 或 503，响应里只有检查项名称与状态，没有任何业务数据：

- 数据库能否连通；
- worker 是否在巡检：超过两个巡检周期（240 秒）没有巡检记录，就判为不健康。worker 不论有没有活都每个周期巡检一次，所以**空闲时也判失败**，免得等有人提交任务才发现 worker 挂了。现有运维页的巡检信号没活时不判失败，因为它还服务没有 worker 的部署，所以两者不合并（实施时由“复用运维页的判断”改为此规则）。

外部监测请求这个地址，所以“web 挂了”“数据库连不上”“worker 卡住了”三种情况都会发邮件。Lightsail 实例告警管机器层面（CPU 持续过高、状态检查失败）。费用告警见 4.12。

### 4.10 域名与旧地址

- 用免费子域名（Q2，2026-10-13 维护者决定）：DuckDNS 的 `<名字>.duckdns.org`，A 记录指向静态 IP，Caddy 按这个名字向 Let's Encrypt 签证书。静态 IP 不变，所以只设一次，不需要动态更新。
- 可以同时申请 eu.org 的免费域名（人工审批，可能要几周）。**S3 切流量之前**批下来就改用它：用户只认 S3 时公布的地址，此前换名字没有代价；S3 之后再换，就要把跳转、重新登录再来一遍。
- 风险：地址归 DuckDNS 管，它的 DNS 出故障或改规则，网站就打不开；部分学校与企业网络会拦截动态域名。外部可用性监测（4.9）能发现前者；后者要靠用户反馈，遇到时再考虑换付费域名。
- 切换后，旧地址 `paper-grading-system-kohl.vercel.app` 改成**只有跳转规则**的 Vercel 部署：所有路径 308 跳到新域名，保留路径和查询参数，`/login`、`/register`、`/reset-password` 不会变成 404。旧库不迁移，所以旧邀请与重置链接里的令牌在新库里无效，跳转只为老书签还能打开。
- 登录 Cookie 绑定域名，切换后每个用户要重新登录一次；切换时还开着旧页面的用户要刷新一次。

### 4.11 服务器维护

单台机器要自己维护，这是省钱的代价。尽量做成脚本：

- `deploy/production/bootstrap.sh`：装 Docker、建部署用户、开 2 GB swap、设置 Docker 日志轮转、开启 `unattended-upgrades`（只装安全更新，不自动重启）。新建或重建实例时运行一次。
- 实例上没有需要备份的数据：数据库与文件都在 Supabase，密钥在 GitHub `production` 环境里，Caddy 证书可以重新签发。实例坏了就新建一台，跑 `bootstrap.sh` 再部署一次。
- 每月挑低峰手动重启一次，让内核更新生效。RUNBOOK 写步骤。

### 4.12 AWS 账号

维护者确认（2026-10-13）：账号是 **Free 账户计划**，用的是新户赠送的额度。这个计划的规则：

- 开户满 6 个月或额度用完，**以先到者为准**，计划结束，账号无法访问，资源停止运行；结束后 90 天内不升级，账号永久关闭、资源删除。
- 升级为付费计划后，没用完的额度在**开户 12 个月内**仍可用于符合条件的服务；额度用完或过期后按标准价付费。
- 这条现在就影响生产：Bedrock 的模型费用也在扣这份额度。额度一旦用完，账号立即到期，**不论是否已经迁移，评分都会停**。

维护者决定（2026-10-13）：**先用 Free 计划，6 个月后再定**。对应的做法：

1. 在 Billing 控制台看清剩余额度与 Free 计划到期日，到期前两周决定是否升级；
2. AWS Budgets 两条告警（维护者已设）：账号总额每月 40 美元，Bedrock 每月 28 美元（预算 40 减服务器约 12）。告警要按额度抵扣前的用量计算，否则额度用完前一直显示 0；
3. Lightsail 对新账号有“部分套餐前 3 个月免费”的优惠（每个账号限一个套餐）。不同来源列出的套餐不一致，创建实例时以控制台标注为准；如果 2 GB 套餐在列，前 3 个月服务器不花钱。

### 4.13 新库初始化（第三版）

新项目用一次性脚本 `backend/app/scripts/init_database.py` 初始化，不新增迁移（维护者决定）：

1. 运行角色 `pgs_app` 不存在时创建（LOGIN，不能建库、建角色、绕过行级安全）；已存在时不改密码。密码只能是 24 位以上字母数字：要原样放进连接串，Alembic 的配置解析又会把 `%` 当插值符号。
2. 用 owner 连接串跑**现有**迁移到 head。
3. 给 `public` 下所有表授权（`alembic_version` 只给 SELECT）、开启行级安全、建 `pgs_app_dml` 策略，与 0023 起各迁移的做法一致；序列给 USAGE 与 SELECT。
4. 建私有存储桶 `paper-grading-private`；已存在但是公开的，直接报错。
5. 逐表核对授权、行级安全与策略，以及迁移版本。

为什么需要：0023 之后的迁移会在 `pgs_app` 存在时给新表授权；更早那批表（截至 0022 共 38 张）当初是在 SQL 编辑器里手工授权的，迁移里没有。新库只跑迁移，迁移成功、应用却 permission denied。脚本把那次手工操作固定下来。

连接串与密码从环境变量读，没有就在终端提示输入（不回显）；脚本只打印步骤与计数。每一步先检查再做，可以重跑。CI 的 Postgres 任务在空库上跑两遍，再以 `pgs_app` 身份运行 `verify_postgres_ops --exercise-ci-fixture` 证明能读写——这是 CI 里第一次真的建 `pgs_app`。以后新增表仍然要在迁移里授权，脚本只管新环境。

## 5. 代码改动清单

| 改动 | 文件 |
|---|---|
| `run_job` 里的 `os.getenv("VERCEL")` 改成配置项 `INLINE_JOB_RUN_ENABLED`（默认 true，生产 false） | `api/routes/batch_jobs.py`、`core/config.py` |
| worker 优雅退出：停止领取、等待在执行的条目、新增 `--drain-seconds` | `services/work_queue/runner.py`、`scripts/run_batch_worker.py` |
| 健康检查接口 | `api/routes/system.py`（复用 `deployment/readiness.py` 的巡检判断） |
| 生产 compose、Caddyfile、`bootstrap.sh`、`deploy.sh`（兼做回滚） | `deploy/production/`（新增） |
| 生成 `.env` 的脚本：部署任务从 GitHub 密钥生成（`--from-environment`），也可从本机文件生成；只读名单、不回显 | `scripts/production_env.py`（新增） |
| 新库初始化脚本（4.13） | `backend/app/scripts/init_database.py`（新增） |
| `deploy-lightsail-production` 任务（含生成 `.env`）；`production-config` 校验 compose、Caddyfile 与脚本；Postgres 任务在空库上验证初始化脚本 | `.github/workflows/ci.yml` |
| 三文档，以及 `PROJECT_MEMORY.md`、`CLAUDE.md`、`README.md`、`docs/上线清单.md` 中关于 Vercel 的描述 | 文档；`test_m8_documentation_contract.py` 按需更新 |

没有数据库迁移，没有前端改动（冷启动提示留到收尾时删）。

## 6. 测试与验收（不调用真实模型）

- 单元测试：健康检查的各种状态（数据库不可用、有活动任务且巡检过期、没有活动任务）；`run_job` 按配置拒绝；worker 收到停止信号后不再领取，并等在执行的条目做完。
- 初始化脚本：建角色只建一次且不打印密码、拒绝会弄坏连接串的密码、授权语句与 0023 一致且重跑不重复建策略、存储桶必须私有；CI 在真实 PostgreSQL 16 空库上跑两遍，再以 `pgs_app` 读写。
- CI：`docker compose -f deploy/production/compose.yml config` 与 `caddy validate` 校验生产配置；现有 `docker-compose-smoke` 不变（它跑的就是 worker 路径）。
- 部署后冒烟：请求健康检查，并检查安全头。
- 线上验收（S2）：登录、上传、解析、查看已有结果。真实评分要花钱，跑不跑、跑多少由维护者决定。

## 7. 费用估算

| 项目 | 每月约 |
|---|---|
| Lightsail 2 GB 实例（含公网 IPv4 与流量额度） | 10–12 美元（来源不一致，以控制台为准） |
| 域名（DuckDNS 免费子域名） | 0 |
| 静态 IP（绑定实例时）、Let's Encrypt、外部监测、AWS Budgets | 0 |
| **合计** | **约 10–12 美元/月，每季度约 30–36 美元** |

**模型费用也算在每季度 120 美元里**（Q1）。服务器约 30–36 美元/季度，剩下约 85 美元留给模型。维护者另有 Google Cloud 的模型额度：如果服务器开销超出预期，或模型费用不够，就把模型换到 Google Cloud，部署预算优先。换模型是另一件事：现有的 `openai_compatible` 协议能直接接 Gemini API 的 OpenAI 兼容地址（API Key 认证）；Vertex AI 用的是短期 OAuth 令牌，要改适配器；无论换到哪个模型，都要按 §15 重新锚定 QWK 基线，并且需要维护者授权真实调用与预算。

额度能抵扣的这段时间（开户 12 个月内、额度用完前），以上开销实际不用付现金。

不够用时的升级路径：换 4 GB 实例（约 24 美元/月，每季度约 75 美元）。实例上没有数据，升级就是新建一台、跑 `bootstrap.sh`、改 DNS。

## 8. 切换步骤

| 步骤 | 谁做 | 内容 |
|---|---|---|
| S0 准备 | 维护者 | Free 计划先用 6 个月、看清额度与到期日，两条预算告警（4.12，已设）；Lightsail 实例 `grading-sg` + 静态 IP + 防火墙（已建）；新 Supabase 项目 `grading-system`（已建）并运行 `init_database`；DuckDNS 子域名；`bootstrap.sh`；GitHub 密钥与仓库变量（RUNBOOK 9.4） |
| S1 代码 | Claude | 第 5 节的改动，一个 PR；`LIGHTSAIL_DEPLOY_ENABLED` 打开之前，部署任务不执行 |
| S2 上线验证 | 维护者批准部署 | 首次部署；用 `AUTH_USERNAME` / `AUTH_PASSWORD` 登录建平台管理员，填平台模型的 API Key；在新域名上验收；确认能收到告警邮件。Vercel 仍连旧库照常服务 |
| S3 切流量 | 维护者 | 先设 `VERCEL_DEPLOY_ENABLED=false`，再把 Vercel 改为只跳转；通知用户新地址并重新邀请；观察 48 小时后，在 Supabase 后台删除旧项目（不可恢复，数据已确认不保留） |
| S4 回滚（需要时） | 维护者 | 删除旧项目之前：在 Vercel 控制台用 Instant Rollback 回到切换前的部署，并把 `VERCEL_DEPLOY_ENABLED` 改回；删除之后就没有回退目标，所以删除放在最后 |
| S5 收尾 | Claude | 切换稳定 2 周后一个 PR：删 `vercel_queue.py`、`vercel` 依赖、`api/index.py`、`VERCEL` 判断、Vercel 部署任务；删冷启动提示；更新文档 |

## 9. 放弃了什么（实施时写进 DECISIONS）

- **ECS Fargate + CloudFront（第一版）**：两个常驻服务、自动扩缩、发版不中断、多可用区，约 120–140 美元/月，是预算的 3–4 倍。代价：单台机器要自己维护，没有多可用区，发版时请求会等几秒。
- **EC2（如 t4g）+ docker compose**：每 vCPU 更便宜，也能用 IAM 实例角色和 SSM；但磁盘、公网 IPv4、出站流量要分开计费，合计不比 Lightsail 便宜；t4g 是 ARM，还要改为构建 arm64 镜像。
- **Lightsail 容器服务**：同等价位只有 0.25–0.5 vCPU，解析与评分不够用。
- **ECR 镜像仓库**：实例上要放 AWS 访问密钥才能拉镜像，还有存储与流量费用；经 SSH 直接传镜像都省了。
- **在实例上从源码构建镜像**：会占用生产机器的 CPU 和内存，实例上还要有仓库的读权限。
- **一期就上 CDN**：CloudFront 有免费的 flat-rate 套餐，但会带回 60 秒回源超时的限制，还要处理回源证书。全部静态资源压缩后约 260 KB，先不上；需要时再加。
- **Google Cloud Run**：数据库（Supabase 在 AWS 新加坡）和模型（Bedrock）都在 AWS。2026-10-13 已定。
- **ECS Express Mode、App Runner、Lambda**：Express Mode 放不了 worker，负载均衡配置也改不了；App Runner 从 2026-04-30 起不再接受新客户；Lambda 与 Vercel 函数一样有冷启动和执行时限。
- **启动时自动迁移**：生产运行角色没有 DDL；迁移保留手动审批。
- **迁移旧数据、新旧并行（第二版做法）**：维护者已联系用户，数据不再保留；新库从空开始，所有旧凭据随旧项目一起作废，也不用找回 Vercel 上读不回来的 `BYOK_MASTER_KEY`。代价：用户要重新注册、重新配置模型连接。
- **补一个迁移 `0039` 给旧表授权**：能让任何环境跑完迁移就完整；维护者选择用初始化脚本（4.13）。代价：新环境必须记得跑脚本，漏跑时的症状写在 RUNBOOK 排错里。
- **新注册一个 Supabase 账号**：新项目已经带来全新的地址、密钥和数据库密码；新账号只会多一套登录要管，当前会话的 Supabase 连接也用不了。
- **把运行时密钥放在实例上手工维护**：要从本机上传、容易与 GitHub 里的两条数据库连接串不一致；改为部署任务每次从 GitHub 生成。
- **留在 Vercel**：Hobby 套餐不花钱，但第 1 节的四个限制都还在。迁移每季度约 30–36 美元，换掉这四个限制。
- **付费域名**：Cloudflare Registrar 的 .com 每年约 10–11 美元，地址归自己、不易被拦截。维护者考虑经费，选了免费子域名（Q2）。

## 10. 问题与答复

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| Q1 | 每季度 120 美元是否也要付 Bedrock 的模型费用 | **已定（2026-10-13）**：包括模型费用；部署预算优先，模型费用不够时换到维护者的 Google Cloud 额度（第 7 节） | — |
| Q2 | 域名 | **已定（2026-10-13）**：用免费子域名 DuckDNS，同时可申请 eu.org，S3 前批下来就改用 | — |
| Q3 | 实例规格 | A. 2 GB（10–12 美元/月）；B. 4 GB（约 24 美元/月） | **按建议选 A**。实测单进程约 160 MB，2 GB 加 swap 够用；不够时按第 7 节升级 |
| Q4 | AWS 账号是不是 Free 账户计划 | **已定（2026-10-13）**：是；**先用 Free 计划 6 个月，到时再定**，看清额度与到期日（4.12） | — |
| Q5 | 旧 `vercel.app` 地址的跳转保留多久 | A. 长期保留（Hobby 免费，只剩跳转规则）；B. 30 天后删除 | **按建议选 A**。不花钱，旧书签和在途链接都不会失效 |
| Q6 | 交付方式 | A. 本文档一个 PR → S1 一个 PR → S5 一个 PR；B. S1 拆成代码与部署脚本两个 PR | **按建议选 A**。S1 的代码改动小，和部署脚本放在一起更容易对照评审 |
| Q7 | 生产数据是否保留 | **已定（2026-10-13）**：不保留（已联系用户），新建 Supabase 项目 `grading-system`，旧项目切流量后删除 | — |
| Q8 | 新库先建后删旧，还是先删旧再建 | **先建后删**（维护者已先建好新项目） | — |
| Q9 | 旧表的授权：补迁移 `0039`，还是初始化脚本 | **已定（2026-10-13）**：初始化脚本，不加迁移（4.13） | — |
| Q10 | 运行时密钥放哪里 | **按建议**：GitHub `production` 环境，部署任务生成实例 `.env`（4.8） | — |

## 参考

- [AWS Free Tier：$200 额度与 6 个月免费计划](https://aws.amazon.com/about-aws/whats-new/2025/07/aws-free-tier-credits-month-free-plan/)
- [AWS Free Tier FAQs](https://aws.amazon.com/free/faqs/)
- [Free 账户计划何时到期（AWS re:Post）](https://www.repost.aws/knowledge-center/aws-free-tier-account-start-expire)
- [Lightsail 价格与新账号优惠](https://aws.amazon.com/lightsail/pricing)
- [Amazon Lightsail 价格（第三方汇总）](https://cloudburn.io/blog/amazon-lightsail-pricing)
- [AWS App Runner availability change](https://docs.aws.amazon.com/ja_jp/apprunner/latest/dg/apprunner-availability-change.html)
- [Resources created by Amazon ECS Express Mode services](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/express-service-work.md)
- [CloudFront custom origin response timeout](https://www.repost.aws/knowledge-center/cloudfront-custom-origin-response)
- [CloudFront flat-rate pricing plans](https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/flat-rate-pricing-plan.md)
