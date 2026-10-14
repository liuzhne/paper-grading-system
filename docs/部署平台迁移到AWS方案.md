# 部署平台迁移方案：从 Vercel 迁到 AWS

状态：**已确认**（2026-10-13，第二版）。第 10 节的问题已全部答复或按建议执行。

背景：2026-10-12 讨论过换平台，结论是用常驻进程、不用 Lambda / Cloud Functions，服务必须放在新加坡。2026-10-13 维护者确认：使用者面向全球，平台选 AWS，**可接受的预算是每季度 120 美元（约 40 美元/月）**。

第一版方案是 ECS Fargate + CloudFront，约 120–140 美元/月，超出预算三倍以上。第二版改为**一台 Lightsail 实例跑 docker compose**，约 10–12 美元/月。第一版放弃的理由见第 9 节。

---

## 1. 要解决什么、不动什么

| Vercel 的限制 | 现在的迁就 | 迁移后 |
|---|---|---|
| 冷启动 7–12 秒 | `App.vue` 的“正在连接服务器…”提示 | 进程常驻，没有冷启动 |
| 单次函数最长 300 秒 | 超过 300 秒的单篇靠多次执行续评；起草时间预算 260 秒 | worker 是常驻进程，没有执行时限；web 请求也不再有 300 秒上限 |
| 没有常驻进程 | 用 Vercel Queues（beta）发叫醒消息；巡检靠 2 分钟一条的自续期消息，链断了要补投 | worker 循环自己领取、自己巡检 |
| 生产与内网叫醒方式不同 | 两条路径都要测；CI 冒烟测的是 worker 路径，不是生产路径 | 生产与内网用同一套 docker compose 结构 |

不动的：

- 数据库与文件存储仍是 Supabase（`ap-southeast-1` 新加坡，Free 套餐），连接串不变。
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
| Vercel Production 环境变量（Sensitive） | 运行配置与密钥 | 实例上一个权限 `0600` 的 `.env` 文件（4.8） |

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
| 容器 | 新增 `deploy/production/compose.yml`：`caddy`、`app`、`worker` 三个服务，不含数据库 | 与内网 compose 结构相同，只是数据库与存储换成 Supabase |
| 防火墙 | Lightsail 防火墙只开 22、80、443；SSH 只允许密钥登录 | |
| 日志 | Docker 日志按大小轮转（单文件 20 MB，保留 5 个），通过 SSH 查看 | 不花钱；代价是没有集中检索 |
| 告警 | 免费的外部可用性监测（如 UptimeRobot）每 5 分钟请求 `/api/system/health`，失败发邮件；Lightsail 实例告警（CPU、状态检查）；AWS Budgets 月度预算告警 | 见 4.9 |

## 4. 关键设计

### 4.1 后台执行：worker 循环替代 Vercel 叫醒

执行模型不用改。统一执行模型（0035）之后，Vercel 与 worker 共用 `claim_next_item`，差别只在谁来叫醒。`wake_enabled()` 为假时，所有叫醒都是空操作；巡检由 `run_worker_loop` 的主线程每 120 秒做一次。

切换期 Vercel 与实例两边同时领取是安全的：领取用 `SKIP LOCKED`，每个来源的名额、心跳租约、连续无进展计数都记在数据库里。唯一的影响是全局并发暂时相加（8 + 8）；每个连接自己的“同时请求数”仍然有效。所以可以先让实例上的 worker 上线干活，再切流量，不需要停机。

### 4.2 发版时服务怎么停

**worker。** 现在收到 SIGTERM 后主循环退出；领取线程是守护线程，进程直接结束。正在评的条目要等 120 秒租约过期，再由巡检重置续评。那次执行如果没写入新检查点，就记一次“无进展”，连续 3 次标为失败。每发一次版，正在评的条目就可能被记一次无进展。

改成：

1. 收到 SIGTERM 后不再领取新条目；
2. 等正在执行的条目做完，最多等 `--drain-seconds` 秒。生产 compose 传 100，`stop_grace_period` 设为 110 秒；命令行默认 0，内网与本地的行为不变（内网 compose 的停止宽限只有 Docker 默认的 10 秒，等也等不到）；
3. 到时还没做完的，照旧交给租约和巡检。

一个 AI 任务条目通常就是一次模型调用，多数能在 100 秒内做完。评分条目按规则写检查点，没做完的部分续评时复用已判完的规则，不重复计费。代价是发版最多要多等约 2 分钟。

**web。** 单台机器上换容器有几秒空档。Caddy 的 `reverse_proxy` 设 `lb_try_duration 30s`：空档期间新请求先等着，后端起来后再转发，用户看到的是几秒延迟而不是报错。`app` 的 `stop_grace_period` 设 60 秒，让进行中的请求做完。

### 4.3 请求时长

Caddy 反向代理默认不限制响应时长，web 请求不再有 Vercel 的 300 秒上限。第一版担心的 CloudFront 60 秒回源超时，在这一版里不存在。

### 4.4 安全头、缓存头、压缩

新增生产用的 `deploy/production/Caddyfile`：

- 证书：Let's Encrypt 自动签发与续期（内网版用的是 `tls internal`）；
- 安全头：照抄 `vercel.json` 的 5 个，HSTS 带 `includeSubDomains`；
- 缓存头：`/workbench/assets/*` 为 `public, max-age=31536000, immutable`，入口 HTML 为 `public, max-age=0, must-revalidate`；
- 压缩：`encode zstd gzip`，与内网版相同；
- 上传大小：`request_body max_size 50MB`，与 `MAX_UPLOAD_SIZE_MB` 一致。

应用代码不用为此改动。代价是安全头现在有三处定义（`vercel.json`、内网 Caddyfile、生产 Caddyfile）；收尾时删掉 `vercel.json`，剩两处，RUNBOOK 写明要一起改。

### 4.5 数据库连接

保持现在的连接串：Supabase 事务池（端口 6543）+ `NullPool`。实例与 Supabase 在同一区域，每次建连只要几毫秒。Supabase 直连地址只有 IPv6，所以不改用直连。

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
4. 同步 `deploy/production/` 下的 compose 文件与 Caddyfile；
5. 把这个镜像另打一个 `current` 标签，compose 只认 `current`；先重建 app，等它健康后再重建 worker（worker 要等在执行的条目做完），最后让 Caddy 热加载配置；镜像只保留最近 3 个；
6. 冒烟：请求 `https://<域名>/api/system/health`，失败则任务失败。

回滚：在实例上运行 `deploy/production/deploy.sh <上一个 SHA>`，把 `current` 指回旧镜像再重启，不需要重新构建。`deploy.sh --list` 列出实例上还留着的版本。

GitHub `production` 环境新增 3 个密钥：部署用的 SSH 私钥、实例地址、实例的 SSH 主机指纹（防止中间人）。

### 4.8 密钥

- 实例上的 `/opt/pgs/.env`，属主是部署用户，权限 `0600`。
- 由维护者在本机运行一个脚本生成并上传：脚本读 `.env.local`，只取名单里的变量，不打印任何值。Claude 不读取、不经手任何密钥值。
- 变量名与 Vercel 上一致，应用代码不用改。

### 4.9 健康检查与告警

新增 `GET /api/system/health`，不需要登录，返回 200 或 503，响应里只有检查项名称与状态，没有任何业务数据：

- 数据库能否连通；
- worker 是否在巡检：有排队或执行中的任务、并且超过两个巡检周期（240 秒）没有巡检记录时，判为不健康。现有运维页的巡检信号用的就是这条规则，这里复用同一个判断。

外部监测请求这个地址，所以“web 挂了”“数据库连不上”“worker 卡住了”三种情况都会发邮件。Lightsail 实例告警管机器层面（CPU 持续过高、状态检查失败）。费用告警见 4.12。

### 4.10 域名与旧地址

- 用免费子域名（Q2，2026-10-13 维护者决定）：DuckDNS 的 `<名字>.duckdns.org`，A 记录指向静态 IP，Caddy 按这个名字向 Let's Encrypt 签证书。静态 IP 不变，所以只设一次，不需要动态更新。
- 可以同时申请 eu.org 的免费域名（人工审批，可能要几周）。**S3 切流量之前**批下来就改用它：用户只认 S3 时公布的地址，此前换名字没有代价；S3 之后再换，就要把跳转、重新登录再来一遍。
- 风险：地址归 DuckDNS 管，它的 DNS 出故障或改规则，网站就打不开；部分学校与企业网络会拦截动态域名。外部可用性监测（4.9）能发现前者；后者要靠用户反馈，遇到时再考虑换付费域名。
- 切换后，旧地址 `paper-grading-system-kohl.vercel.app` 改成**只有跳转规则**的 Vercel 部署：所有路径 308 跳到新域名，保留路径和查询参数。已发出的邀请链接（7 天有效）与重置链接（1 小时有效）照样能用，满足 CLAUDE.md 里“这三条不能只剩 404”的要求。
- 登录 Cookie 绑定域名，切换后每个用户要重新登录一次；切换时还开着旧页面的用户要刷新一次。

### 4.11 服务器维护

单台机器要自己维护，这是省钱的代价。尽量做成脚本：

- `deploy/production/bootstrap.sh`：装 Docker、建部署用户、开 2 GB swap、设置 Docker 日志轮转、开启 `unattended-upgrades`（只装安全更新，不自动重启）。新建或重建实例时运行一次。
- 实例上没有需要备份的数据：数据库与文件都在 Supabase，密钥以本机 `.env.local` 为准，Caddy 证书可以重新签发。实例坏了就新建一台，跑 `bootstrap.sh` 再部署一次。
- 每月挑低峰手动重启一次，让内核更新生效。RUNBOOK 写步骤。

### 4.12 AWS 账号

维护者确认（2026-10-13）：账号是 **Free 账户计划**，用的是新户赠送的额度。这个计划的规则：

- 开户满 6 个月或额度用完，**以先到者为准**，计划结束，账号无法访问，资源停止运行；结束后 90 天内不升级，账号永久关闭、资源删除。
- 升级为付费计划后，没用完的额度在**开户 12 个月内**仍可用于符合条件的服务；额度用完或过期后按标准价付费。
- 这条现在就影响生产：Bedrock 的模型费用也在扣这份额度。额度一旦用完，账号立即到期，**不论是否已经迁移，评分都会停**。

所以：

1. S0 就升级为付费计划，不等到上线；
2. 同时在 AWS Budgets 设两条告警：账号总额每月 40 美元，Bedrock 每月 28 美元（预算 40 减服务器约 12）；
3. Lightsail 对新账号有“部分套餐前 3 个月免费”的优惠（每个账号限一个套餐）。不同来源列出的套餐不一致，创建实例时以控制台标注为准；如果 2 GB 套餐在列，前 3 个月服务器不花钱。

## 5. 代码改动清单

| 改动 | 文件 |
|---|---|
| `run_job` 里的 `os.getenv("VERCEL")` 改成配置项 `INLINE_JOB_RUN_ENABLED`（默认 true，生产 false） | `api/routes/batch_jobs.py`、`core/config.py` |
| worker 优雅退出：停止领取、等待在执行的条目、新增 `--drain-seconds` | `services/work_queue/runner.py`、`scripts/run_batch_worker.py` |
| 健康检查接口 | `api/routes/system.py`（复用 `deployment/readiness.py` 的巡检判断） |
| 生产 compose、Caddyfile、`bootstrap.sh`、`deploy.sh`（兼做回滚） | `deploy/production/`（新增） |
| 生成并上传 `.env` 的脚本（只读名单、不回显） | `scripts/`（新增） |
| `deploy-production` 任务；生产 compose 与 Caddyfile 的语法校验 | `.github/workflows/ci.yml` |
| 三文档，以及 `PROJECT_MEMORY.md`、`CLAUDE.md`、`README.md`、`docs/上线清单.md` 中关于 Vercel 的描述 | 文档；`test_m8_documentation_contract.py` 按需更新 |

没有数据库迁移，没有前端改动（冷启动提示留到收尾时删）。

## 6. 测试与验收（不调用真实模型）

- 单元测试：健康检查的各种状态（数据库不可用、有活动任务且巡检过期、没有活动任务）；`run_job` 按配置拒绝；worker 收到停止信号后不再领取，并等在执行的条目做完。
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
| S0 准备 | 维护者 | 账号升级为付费计划并设两条预算告警（4.12）；在 DuckDNS 注册子域名（可同时申请 eu.org）；在新加坡创建 Lightsail 实例并绑定静态 IP、配置防火墙；子域名 A 记录指向静态 IP；运行 `bootstrap.sh`；上传 `.env`；在 GitHub 配置 3 个部署密钥 |
| S1 代码 | Claude | 第 5 节的改动，一个 PR；部署密钥配好之前，部署任务不执行 |
| S2 并行运行 | 维护者批准部署 | 首次部署后，实例上的 worker 与 Vercel 一起干活（4.1）；在新域名上验收；确认能收到告警邮件 |
| S3 切流量 | 维护者 | Vercel 改为只跳转的部署；通知用户新地址；观察 48 小时 |
| S4 回滚（需要时） | 维护者 | 在 Vercel 控制台用 Instant Rollback 回到切换前的部署。数据是共用的，不需要回滚数据；实例可以留着，也可以 `docker compose stop` |
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
- **留在 Vercel**：Hobby 套餐不花钱，但第 1 节的四个限制都还在。迁移每季度约 30–36 美元，换掉这四个限制。
- **付费域名**：Cloudflare Registrar 的 .com 每年约 10–11 美元，地址归自己、不易被拦截。维护者考虑经费，选了免费子域名（Q2）。

## 10. 问题与答复

| # | 问题 | 选项 | 建议 |
|---|---|---|---|
| Q1 | 每季度 120 美元是否也要付 Bedrock 的模型费用 | **已定（2026-10-13）**：包括模型费用；部署预算优先，模型费用不够时换到维护者的 Google Cloud 额度（第 7 节） | — |
| Q2 | 域名 | **已定（2026-10-13）**：用免费子域名 DuckDNS，同时可申请 eu.org，S3 前批下来就改用 | — |
| Q3 | 实例规格 | A. 2 GB（10–12 美元/月）；B. 4 GB（约 24 美元/月） | **按建议选 A**。实测单进程约 160 MB，2 GB 加 swap 够用；不够时按第 7 节升级 |
| Q4 | AWS 账号是不是 Free 账户计划 | **已定（2026-10-13）**：是。S0 就升级为付费计划，并设两条预算告警（4.12） | — |
| Q5 | 旧 `vercel.app` 地址的跳转保留多久 | A. 长期保留（Hobby 免费，只剩跳转规则）；B. 30 天后删除 | **按建议选 A**。不花钱，旧书签和在途链接都不会失效 |
| Q6 | 交付方式 | A. 本文档一个 PR → S1 一个 PR → S5 一个 PR；B. S1 拆成代码与部署脚本两个 PR | **按建议选 A**。S1 的代码改动小，和部署脚本放在一起更容易对照评审 |

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
