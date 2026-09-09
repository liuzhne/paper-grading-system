# 架构与工程决策

> 当前决策索引更新于 2026-09-09。Accepted ADR 的原始语义优先于本文摘要；已实现事实以当前代码、Alembic head 和 CI 为准。详细 Core 基础决策见 `docs/adr/0001-general-scoring-core-foundations.md`。

## 1. 记录规则

每个修复方案都要在本文件追加或修订一个决策条目，至少包含：背景、选择、原因、放弃方案、代价/约束。单纯记录“改了什么”不够；如果修复没有形成新决策，也要在维护记录中注明复用了哪项既有决策及为何无需新增 ADR。

状态含义：

- `Accepted`：当前实现和后续变更应遵守。
- `Superseded`：被新条目替代，但保留历史原因。
- `Proposed`：仅供讨论，不能当作项目规则或已实现事实。

## 2. 决策索引

| ID | 状态 | 决策 |
|---|---|---|
| D-001 | Accepted | 评分标准模板驱动，业务分值不写死在代码中 |
| D-002 | Accepted | 采用通用 Core + Profile + Adapter 分层 |
| D-003 | Accepted | v1 兼容链路与 v2 通用链路渐进并存 |
| D-004 | Accepted | 确定性优先，LLM 只处理判断题并强制证据 |
| D-005 | Accepted | 用冻结快照、内容 hash、幂等键和完整运行身份保证可重放 |
| D-006 | Accepted | 评分标准严格审核发布，结果保留人工复核闭环 |
| D-007 | Accepted | PostgreSQL + Alembic 是生产真相，SQLite 用于本地和自包含测试 |
| D-008 | Accepted | LLM 可插拔且默认 Mock；多用户生产优先私有 BYOK |
| D-009 | Accepted | LLM 计算脱离数据库事务，并使用带完整身份的 L0 缓存 |
| D-010 | Accepted | Core 默认切换受 compare/观察/GATE-03/人工批准共同约束 |
| D-011 | Accepted | 生产只通过 main 的 GitHub Actions 门禁发布 Vercel |
| D-012 | Accepted | 存储抽象支持本地与 Supabase 私有直传，数据库保存引用和 hash |
| D-013 | Accepted | 资源以组织隔离，Cookie 会话和角色能力控制敏感操作 |
| D-014 | Accepted | OpenTelemetry + 自托管 Langfuse 只做脱敏诊断投影，业务库保持权威 |
| D-015 | Accepted | PromptEnvelopeV4 先采用可重放词法选择与双重 Token preflight，向量检索以 Recall 证据后置 |
| D-016 | Accepted | 单规则失败隔离并持久化检查点；阻塞失败必须进入证据化人工复核且总分为空 |
| D-017 | Accepted | 先用按连接的进程内并发/熔断保护，未完成分布式调度前保持单论文规则顺序执行 |
| D-018 | Accepted | Groq service tier 显式、可审计、默认不切换；Batch API 作为独立后续通道 |
| D-019 | Accepted | 前端 v2 补齐八项合同并逐阶段验收（阶段 0–6B 已实施，见 ARCHITECTURE §7.2） |
| D-020 | Accepted | 批量采纳端点定名 `/review-queue/accept` 而非计划写的 `accept-all` |
| D-021 | Accepted | 决策 9 的 LLM 侧拆出阶段 2，先用确定性原因交付复核闭环 |
| D-022 | Accepted | 0024–0028 有数据时一律拒绝降级，空库仍可回滚 |
| D-023 | Accepted | 不为 V08 造 Supabase 桩，直传/TUS 证据来自真实存储冒烟 |
| D-024 | Accepted | 旧 SPA 直接下线，不保留 `/legacy/` 回退窗口 |
| D-025 | Accepted | 生产迁移改为需审批的工作流，凭据不经手个人 |
| D-026 | Accepted | 评分标准只能由导入产生，不保留空白新建 |
| D-027 | Accepted | AI 起草与评分都必须绑定调用者自己的 BYOK 连接 |
| D-028 | Accepted | 平台默认模型改为管理员配置的单例表，初始为未配置 |
| D-029 | Accepted | 分享范围在发布时一次选定，发布后与版本一起冻结 |
| D-030 | Accepted | 平台模型用量按实际调用人归属 |
| D-031 | Accepted | 评分标准并发保护复用编译层，不加 rubric 级版本号 |

## 3. 决策详情

### D-001：评分标准模板驱动，业务分值不写死

- 背景：不同学校、论文类型和非论文 Profile 的标准不同，且扣分依据必须能追溯到获授权材料。
- 选择：Excel/Word/手工 JSON 进入来源图和 compilation，形成可审核 AtomicRule、RuleLevel、TemplateLink 与冻结 ScoringPolicy；checker 只执行授权规则。
- 原因：把变化留在数据和审核流程中，代码负责稳定的执行语义；可解释、可审计，也能扩展到非论文 Profile。
- 放弃：在代码、prompt 或 Profile 中为 T01-T06 等具体项写固定扣分；让模型自行决定扣分规则；导入时自动补齐缺失授权。
- 代价/约束：导入材料不完整时会产生 blocker，必须由评分标准维护者补齐、送审、批准和发布。

### D-002：通用 Core + Profile + Adapter 分层

- 背景：评分语义应跨业务复用，但论文和技术方案的结构、元数据与 checker 不同。
- 选择：Core 只依赖版本化 DTO、policy、evidence 和 ports；Profile 负责业务解释与 registry；Adapter 负责 ORM、存储、legacy 转换和持久化。
- 原因：避免 Core 被论文模型、SQLAlchemy 或中文字段名绑死，同时保留可测试的纯执行边界。
- 放弃：为每种文档复制一套评分引擎；让 Core 直接查询数据库；按可变 `criterion.name` 调度 checker。
- 代价/约束：新增 Profile 必须提供稳定 key/version、文档解释器、checker registry、运行 identity 和端到端测试。

### D-003：v1 与 v2 渐进并存

- 背景：已有论文 Web/API/CLI 和历史数据不能一次性中断，通用 Submission/Core 又需要更严格合同。
- 选择：保留 `score_paper()` 兼容入口，新增 `score_generic_submission()`；正式 v1 batch 锁定版本后同样强制 Core，未版本化路径才受 rollout mode 控制。
- 原因：用 strangler 式迁移保留现有能力，同时逐步把权威运行收敛到 Core。
- 放弃：大爆炸替换 v1；把正式 RubricVersion 重新投影到 arbitrary-points legacy；伪造历史运行的正式版本身份。
- 代价/约束：一段时期内需要维护两套 API 投影、报告入口和兼容测试，文档必须明确哪条路径是权威。

### D-004：确定性优先并强制证据

- 背景：结构、字数、引用、格式等判定适合代码；质量判断适合 LLM，但容易幻觉或越权。
- 选择：deterministic/findings checker 优先；LLM 输出经过结构化校验、注入检测和 evidence 校验；deductive/banded/llm_direct 的分数由冻结 policy 约束。
- 原因：降低成本和随机性，使每次扣分/选档都能定位证据，并把不确定性暴露给人工复核。
- 放弃：让模型自报最终总分；把所有 evidence 都伪装为 quote；把检索不到内容当成全文缺失；保留固定 80% 常规封顶。
- 代价/约束：严格校验会增加 `needs_review` 和阻断率，这是安全结果，不能用静默给分消除。

### D-005：冻结快照与完整运行身份

- 背景：评分标准、文档、模型、prompt、checker 或锚点任何一个变化都可能改变结果。
- 选择：发布 `RubricVersion`、`DocumentSnapshot`、plan/policy/prompt/model/checker/anchor/revision identity；对内容做 canonical hash；用 rescore generation 和身份投影计算幂等键。
- 原因：重复请求可安全复用，历史结果可审计，比较和发布门禁能证明比较的是同一输入合同。
- 放弃：只保存展示用版本字符串；重跑时读取当前可变 Rubric；覆盖旧 `ScoringRun`；按文件名或本地绝对路径充当身份。
- 代价/约束：存储字段和迁移更多；任何身份变化都需要新 generation、版本或候选批准，不能“就地修正历史”。

### D-006：严格发布与人工复核闭环

- 背景：评分涉及授权、误判和最终责任，自动管线不能替代业务审批。
- 选择：规则逐条送审/批准，模板映射逐条确认，Rubric 整体审核后显式绑定 compilation 发布；评分后用 `ScoreItem.final_score` 和 `ReviewLog` 留痕。
- 原因：把标准授权和个案裁量分开，保留问责与纠错路径。
- 放弃：导入即发布；自动批准规则；修改 AI 原始分覆盖历史；自动清除 blocker。
- 代价/约束：操作步骤更多，前端、CLI 和 Runbook 必须提供清晰的 blocker 与恢复指引。

### D-007：PostgreSQL/Alembic 为生产真相

- 背景：测试需要轻量自包含，生产需要并发、约束、备份恢复和可验证迁移。
- 选择：生产使用 PostgreSQL 16 和逐版本 Alembic；本地/pytest 使用 SQLite，连接时强制外键；CI 单独验证 Postgres 迁移、约束、排序和恢复。
- 原因：兼顾开发速度与生产真实性，并显式承认两种方言的差异。
- 放弃：仅用 `Base.metadata.create_all()` 验证生产 schema；把本机 SQLite 通过当作发布证据；允许有损 downgrade 静默删除数据。
- 代价/约束：新增字段必须同时有迁移和测试；关键生产变更必须保留 Postgres artifact，不能只跑单元测试。

### D-008：可插拔 LLM、默认 Mock、私有 BYOK

- 背景：项目要支持离线测试、本地模型、OpenAI 与兼容厂商，同时隔离多用户的 Key、费用和数据边界。
- 选择：factory + adapter 统一运行接口；默认 `mock`；受保护部署默认不启用平台共享模型，用户私有连接使用独立 master key 加密并在批次冻结配置/key version。
- 原因：测试无需网络或 Secret，部署者能选择数据边界，Key 轮换不会悄悄改变既有任务身份。
- 放弃：测试依赖真实 API；把 Key 写入数据库明文、日志或前端；从旧 `.env` 自动复制成用户连接；允许任意私网 URL 造成 SSRF。
- 代价/约束：真实模型诊断和 QWK 必须显式配置；连接变化后旧批任务需要重建；生产原文 debug 必须关闭。

### D-009：collect -> compute -> persist 与身份化缓存

- 背景：LLM 延迟长，把外呼放在数据库事务中会造成 SQLite 锁、Postgres 连接占用和批量串行。
- 选择：评分先预取输入并释放事务，纯计算阶段不访问数据库，最后短事务保存；L0 缓存 key 覆盖模型、prompt 版本、输入、RubricVersion、采样和 anchors。
- 原因：提升并发和故障隔离，同时保证缓存不会跨评分合同错误复用。
- 放弃：在长事务中逐项请求 LLM；只按 prompt 文本或模型名缓存；缓存响应绕过当前 evidence/policy 校验。
- 代价/约束：collect 后的输入必须自包含；prompt/输入构造变化必须 bump `PROMPT_VERSION`；persist 冲突要按幂等身份处理。

### D-010：Core 切换必须门禁化

- 背景：新 Core 的正确性、性能和兼容性不能由单元测试或一次 Mock 演练证明。
- 选择：默认 `SCORING_ENGINE_MODE=legacy`；compare 只生成非权威候选；批任务保存观察策略与指标；真实 GATE-03 冻结外部 holdout、模型和完整身份，并由维护者批准。
- 原因：把工程可运行、操作就绪、评分质量和发布授权分成独立证据。
- 放弃：测试通过即切默认；由代码内置业务阈值；OPS readiness 或 PGS-6 报告自动授权；生产 compare 双倍调用并产生两个权威结果。
- 代价/约束：切换较慢且需要外部私有数据/人工角色；触发回退时只改变未来未版本化入口，不改写历史权威 run。

### D-011：生产发布只走 GitHub Actions 门禁

- 背景：Vercel Git 直部署和人工本地提升会绕过测试、迁移与恢复证据，产生重复或不可追溯部署。
- 选择：关闭 Vercel Git deployment；只有 `main` push 且 unit、Postgres migration/restore、Docker smoke 全绿后，GitHub `production` Environment 才运行 prebuilt Vercel deploy。
- 原因：每个生产版本都能关联 commit、CI artifact、部署 URL 和责任审批。
- 放弃：本地脏工作区直接发布；Preview 成功替代生产门禁；删除 `.vercelignore` 绕过 CLI 回归。
- 代价/约束：Vercel CLI 暂时固定 `58.4.0`，升级需独立验证；数据库迁移仍需生产前备份和受控执行。

### D-012：本地/Supabase 存储抽象与私有直传

- 背景：本地/内网适合文件系统，Vercel 大文件经 Function 会受 payload 和超时限制。
- 选择：存储服务返回受控引用；本地保存到 storage root，生产可用 Supabase 私有桶签名直传，小文件标准上传、大文件 TUS；服务端确认大小、归属与租约后解析。
- 原因：同一业务服务适配两种部署，避免大文件穿过 serverless function，并保留恢复能力。
- 放弃：公开桶；浏览器持有 service key；数据库保存不可移植绝对路径；解析失败时回滚其他成功文件。
- 代价/约束：必须正确配置 CORS、私有桶和 Secret；备份/恢复需覆盖对象和引用一致性。

### D-013：组织隔离与角色能力

- 背景：论文、评分、标准和私有模型连接属于敏感租户资源，简单“登录即全局可见”不满足多用户部署。
- 选择：所有主要资源持有 organization/owner identity；路由查询按当前主体过滤，越界资源返回 404；管理员/教师等角色控制写操作；Cookie 使用 HttpOnly/Secure/SameSite。
- 原因：在数据查询和能力检查两层实施隔离，减少枚举泄漏并支持审计。
- 放弃：只在前端隐藏按钮；用客户端传入的 owner_id 决定权限；AUTH 开启后仍允许弱 Secret 或原文调试。
- 代价/约束：迁移 0022 必须把 legacy 数据安全回填默认组织；有真实归属数据时 downgrade fail closed。

### D-014：OpenTelemetry + 自托管 Langfuse 诊断投影

- 背景：Groq/OpenAI-compatible 失败原先以裸 `httpx.HTTPStatusError` 传播，上下文超限、认证、限流和容量故障难以稳定区分，也无法还原规则级模型请求链。
- 选择：应用以 `provider-error@1` 投影白名单错误字段；用 Langfuse v4 的 OpenTelemetry 实现记录 scoring/rule/generation/retry Observation，默认 `metadata_only`，支持经审批的脱敏内容模式。
- 原因：稳定错误合同能驱动重试、UI 和后续规则状态机；OpenTelemetry 保留观测后端可替换性；源头脱敏比依赖下游设置更安全。
- 放弃：继续从异常文本猜错误；默认上传完整 Prompt/论文；让 Langfuse 成为分数或任务状态的唯一存储；因 Exporter 故障使评分失败。
- 代价/约束：需要独立 Langfuse 运行环境、凭据、RBAC、保留/备份策略；默认不能在观测界面直接查看原文；导出失败只能记内部告警，不能改变分数或任务状态。

### D-015：V4 先保证上下文安全，再演进混合检索

- 背景：整篇正文与每条规则拼接会触发 Provider 400/context 错误；直接引入向量库又会增加数据驻留、embedding 版本和召回验证成本。
- 选择：PromptEnvelopeV4 封闭 selection/budget identity；按规则和章节做确定性词法 Top-K，保留完整 EvidenceUnit，并在构造后与网络发送前各做一次 Token preflight。向量召回/reranker 只有在授权 Recall@K 数据集证明收益后才加入。
- 原因：先以最小依赖消除必然超长请求，保持可重放和证据可追溯；避免把未经校准的语义检索当成正确性保证。
- 放弃：继续发送全文；按字符硬截断 quote；当前立即引入独立向量数据库；把 Top-K 未命中解释为全文不存在。
- 代价/约束：词法基线可能漏掉语义同义证据；缺失/全局规则必须使用 exhaustive/hierarchical 或人工复核，发布前仍需真实 Recall/QWK。

### D-016：失败隔离、规则检查点与阻塞式人工复核

- 背景：一个 Groq 请求失败会终止整篇评分，失败项也没有正式领取、证据和并发控制。
- 选择：Core 把安全分类后的 Provider/预算异常转换为单规则 invalid；旁支继续。0023 持久化 `RuleScoringTask`，blocking invalid 唯一映射 `ManualReviewTask`；open/claimed 时总分和等级为空，人工解决必须持有任务、匹配乐观版本并引用冻结原文。
- 原因：保留已完成结果、阻止虚假完整总分，并让责任、依据和覆盖动作可审计。
- 放弃：整篇 fail-fast；失败项默认为 0 或满分；仅在日志/便签中人工处理；允许普通整体复核绕过阻塞项。
- 代价/约束：本轮任务在 Core 结束后持久化，尚不支持进程中断后的单规则续跑或自动定向重试；人工队列会增加运营工作量。0023 在 PostgreSQL 检测到既有 `pgs_app` 时同步最小 DML/RLS，避免生产授权成为未版本化手工步骤。

### D-017：Provider 保护层渐进实施

- 背景：批内并发叠加规则并发会放大 429/498/5xx；Vercel 多实例又使纯进程内计数不是真正全局额度。
- 选择：先按 AI connection/base URL 使用进程内 semaphore 与 closed/open/half-open circuit，401/403 需重认证后恢复；在集中配额、租约和幂等扣费证明完成前，单论文规则保持顺序执行。
- 原因：立即抑制单实例错误风暴，同时不虚构跨实例一致性，也不在恢复能力不足时扩大并发面。
- 放弃：无限并发；每篇论文独立 semaphore；现在直接开启 DAG 并发；把本地 circuit 宣称为全局限流。
- 代价/约束：横向扩容后总并发仍可能超过账户额度；实例重启会清空 circuit，生产需保守配置外层批并发并监控 Provider 指标。

### D-018：Groq service tier 显式选择

- 背景：Groq 实时 service tier 的容量/延迟/错误语义不同，Batch API 又是独立异步协议；静默 fallback 会改变身份和可比性。
- 选择：OpenAI-compatible 连接只接受批准枚举，显式发送 `service_tier` 并将其计入 provider artifact identity；不默认启用、不跨 tier 静默切换。Batch API 后续以独立任务状态机实施。
- 原因：费用、容量和重放身份可审核，实时失败处理不会与异步批处理混淆。
- 放弃：收到 498 后静默换模型/tier；把 Chat Completions 参数直接用于 Batch；根据公共套餐表自动设置生产并发。
- 代价/约束：运营方必须提供账户真实限额和批准 tier；修改 tier 会产生新的运行身份并需要重新校准。

### D-019：前端 v2 计划的实施前合同补齐

- 状态：`Accepted`；2026-09-07 完成计划修订，2026-09-08 完成阶段 0–6B 实施（见 ARCHITECTURE §7.2）；仍不构成生产发布或默认 Core 切换授权。
- 背景：前端初稿把 legacy `chunk_id`、置信度和普通改分视为正式评分的完整合同，同时低估了直传、现有 Excel 日志、部署入口和运维数据范围的影响。
- 选择：正式 Core 查看运行绑定的冻结正文，legacy 另行投影 chunk；无法验证原 quote 时展示上下文，不伪造定位。复核区承接普通确认与现有阻塞任务，批量采纳使用明确集合、共享 revision/锁和持久化幂等回执。原因分别覆盖 legacy/Core 模型输出、确定性检查及 Provider/预算错误，保留缓存版本与真实 QWK 要求。
- 配套选择：Supabase 保留签名直传/TUS/归档/解析恢复，local 使用文件级入口；导出保留旧表和四种类型，新增事件后幂等补录，未知操作人和历史逐 run 粒度如实保留。七阶段结合 job 状态，所有写入口共用守卫。平台和组织运维服务区分，教师日常任务操作保留。阶段 0 建设 Vercel/本地/Docker 的同源产物、双入口和浏览器测试，阶段 6 才切默认前端并保留回退窗口。
- 原因：优先保证正式 Core 评分能被完整查看和人工处理，避免前端重写造成已交付能力退化或历史数据错误分类。
- 放弃：仅增加页码即宣称证据可定位；以低置信度筛选覆盖全部复核；通过写 ReviewLog 清除阻塞；把已有日志统一回填为 Sheets；仅生成 `public/` 就视为各部署形态已迁移；靠静态源码字符串断言验收 Vue 页面。
- 代价/约束：阶段 0～3 增加服务端与并发工作，撤销固定三个迁移的估算；首版保留旧日志表和兼容 UI。`docs/前端v2改造计划.md` §13 将 R1–R8 关联到阶段和 V01–V13 验收，§12 要求前端 CI 成为生产 deploy 的依赖。复用 D-003/D-005/D-007/D-011/D-012/D-013/D-016，保持冻结身份、组织隔离、阻塞总分为空、真实 CI 与 QWK 门禁要求。

### D-020：批量采纳端点定名 `accept` 而非 `accept-all`

- 状态：`Accepted`；2026-09-08 实施时确定，与计划 §6 的字面写法有意不一致。
- 背景：§6 的端点表写 `POST /batches/{id}/review-queue/accept-all`，而同一份计划的 §5-B 要求它「作用于用户预览并提交的有限集合，每次最多 100 项，跨页操作需要重新选择」。
- 选择：路径定为 `/review-queue/accept`，语义按 §5-B 约束实现（显式 ID 集合 + 各自 review revision + 幂等键）。
- 原因：`accept-all` 会让调用方以为它处理全批次，而它恰恰不会——名字与约束冲突时，错的是名字。
- 放弃：保留 `accept-all` 的名字再靠文档解释它其实不是「全部」。
- 代价/约束：与计划正文存在一处命名偏离，已记入 `docs/前端v2改造计划.md §9.1「与计划的已知偏离」`。

### D-021：决策 9 的 LLM 侧拆出阶段 2

- 状态：`Accepted`；2026-09-08。计划 §11 首行已列出这条二选一，本次选择后者。
- 背景：「需要确认的原因」由评分时 LLM 顺带产出，会改判分输出契约 → 必须 bump `PROMPT_VERSION`（L0 缓存全量失效）→ 必须按 §15 用真实留出集重锚 QWK。而仓库唯一评估 `docs/baselines/m0-thesis-evaluation.json` 标记 `gating_eligible=false`、QWK −0.002，不可作自动门禁；建立可门禁基线须走 PGS-8 两阶段批准，当前未排期。
- 选择：只交付确定性原因管线（正常路径、validator 覆盖文案、0025 之前历史行的显示回退），数据形状预留 `source` 字段并有测试钉住模型来源条目可原样穿过队列投影。
- 原因：未经基线验证就改判分逻辑违反 CLAUDE.md 的硬约束；而复核闭环本身不必等它。
- 放弃：先上模型原因、事后补基线。
- 代价/约束：复核原因栏目前只有确定性来源。接入模型侧仍需 PGS-8 批准与 QWK 重锚，不因本次实施而免除。

### D-022：0024–0028 有数据时拒绝降级

- 状态：`Accepted`；2026-09-08。计划 §7 对每个迁移都要求验证这一条。
- 背景：0024 已 fail-closed，0025–0028 未做——降级会静默删掉复核原因、批量采纳幂等回执、导出审计事件与旧日志映射。
- 选择：四个迁移的 `downgrade()` 先查有无数据，有则抛出并说明会失去什么；空库仍允许回滚。CI 在真实 PostgreSQL 上再验证一次拒绝行为且 DDL 未受损。
- 原因：这些不是缓存，重建不回来；丢掉幂等回执还会让一次重放变成二次写入。
- 放弃：一律禁止降级（会逼人手工删表），或一律放行（等于把审计数据交给一次误操作）。
- 代价/约束：回滚到 0024 之前需要先由维护者显式导出并确认这些数据。

### D-023：不为 V08 造 Supabase 桩

- 状态：`Accepted`；2026-09-08。依据计划 §12.2「Mock 网络路由不冒充真实 Supabase 验证」。
- 背景：V08 要求覆盖签名直传、TUS、归档确认与恢复；一次性验收后端没有对象存储。
- 选择：浏览器验收只覆盖不依赖对象存储的部分（预检、逐文件状态、单文件失败隔离、草稿续传）；直传与 TUS 的证据来自真实存储冒烟并另行归档。
- 原因：桩会让用例看起来覆盖了 V08，实际验证的只是桩自己——签名过期、对象已存在、TUS 断点这些真实失败模式一个都碰不到。用一个绿灯换掉一个真问题比不覆盖更糟。
- 放弃：造桩并把结果记为 V08 通过。
- 代价/约束：发布记录不得写「V01–V13 全部通过」，须注明 V08 的直传/TUS 由真实存储冒烟单独覆盖。

### D-024：旧 SPA 直接下线，而不是保留回退窗口

- 状态：`Accepted`；2026-09-08（用户决定）。
- 背景：v2 计划 §8.3 原设计是 `WORKBENCH_DEFAULT_ENTRY` 开关 + 常驻 `/legacy/`，保留一个可回退发布窗口。
- 选择：直接把 `/` 换成工作台，删掉开关与 `/legacy/`，并新增邀请注册页与密码重置页。
- 原因：两套并存意味着两套守卫，迟早失配；用户已确认新页可用。
- 放弃：保留回退窗口。**代价是出问题只能靠重新发版回滚，不能改一个环境变量退回去**。
- 代价/约束：`/login` `/register` `/reset-password` 必须由工作台承接——邮件里已发出的链接指向后两条，
  收件人不会重新拿到新链接。入口要在 FastAPI 路由与 `public/index.html` **两处**同时改；Vercel 直接
  静态托管后者，只改前者在生产上无效。

### D-025：生产迁移改为需审批的工作流，而不是手工连库

- 状态：`Accepted`；2026-09-08。
- 背景：生产运行角色 `pgs_app` 按设计没有 DDL，迁移必须用 owner 连接串。此前是在某台笔记本上手工执行。
- 选择：`pgs-production-migrate` 工作流，手动触发 + `production` Environment，owner 串放 GitHub Secrets。
  `dry_run` 默认 true；输入的 head 必须与该 commit 一致；写入后**用运行角色**复验。
- 原因：凭据不进任何人的命令行与 shell 历史，每次执行有审批与日志，换人也能跑。
- 放弃：继续手工执行（凭据落盘、不可审计）；把迁移挂到 push 上自动跑（每次合并都动生产库）。
- 代价/约束：多一次人工触发。**用运行角色复验是关键**——owner 什么都读得到，证明不了应用能不能工作；
  `0026/0027` 的新表缺 `pgs_app` 授权，正是只有这样才暴露得出来。

### D-026：评分标准只能由导入产生，不保留空白新建

- 状态：`Accepted`；2026-09-09（用户决定，v3）。
- 背景：后端同时有 `POST /rubrics`（空白新建）与 `POST /rubrics/import-files`（导入 Excel + Word 批注）。
- 选择：界面只提供导入与克隆，空白新建退出界面（CLI/测试仍可用）。
- 原因：每份标准都带模板溯源。副作用是好的——`publish` 里「legacy draft 必须先显式升级溯源」那条
  分支在新界面上不再可能触发。
- 放弃：保留手填评分项的快捷路径。代价是没有 Excel 就建不了标准。

### D-027：AI 起草与评分都必须绑定调用者自己的 BYOK 连接

- 状态：`Accepted`；2026-09-09（用户决定，v3）。
- 背景：`engine.py:147` 在批次没绑连接时走 `get_llm_scorer()`；生产 `LLM_PROVIDER=mock`，
  于是**通过界面建的任务评出来的是 Mock 假分**，界面无任何提示。同一条路径让
  `draft-deduction-rules` 产出 Mock 编的扣分规则，却以「AI 起草 · 待确认」呈现，确认后进入正式标准。
- 选择：认证部署下 `POST /batches` 与 `draft-deduction-rules` 都要求连接；`get_llm_scorer()` 把
  「认证部署禁止平台模型」的判断提到 `provider == "mock"` 分支**之前**。
- 原因：假结果比明确失败危险得多——它会被当成真结论沿用下去。
- 放弃：保留「不选连接就用平台默认」的便利路径。
- 代价/约束：`AUTH_ENABLED=false` 仍走环境变量，否则本地开发与全套浏览器验收会立刻断掉。

### D-028：平台默认模型改为管理员在页面配置的单例，而不是环境变量

- 状态：`Accepted`；2026-09-09（用户决定，v3）。
- 背景：平台模型完全由 `LLM_PROVIDER` 等环境变量决定，没有配置表，也就没有「谁配的、配了什么」。
- 选择：独立单例表 `platform_llm_config`，沿用 BYOK 的信封加密，限平台管理员；
  `get_llm_scorer()` 解析顺序改为「绑定的 BYOK → 平台配置表 → 抛错」。**初始状态是未配置。**
- 原因：让「平台有没有模型」成为一次显式的、可追溯的管理动作，而不是某个 env 恰好设对。
- 放弃：复用 `ai_connections` 存平台连接——它的 `organization_id`/`owner_id` 都是 NOT NULL 且有
  `scope='private'` 约束，**为一个单例记录削弱一张多租户表的完整性约束代价太大**。
- 代价/约束：环境变量退化为开发与 CI 专用。生产进入「未配置」状态后，未绑 BYOK 的用户被引导去配置，
  不能评分——这是期望行为。
- 实施补充（2026-09-09，V3-0a）：
  - **`PLATFORM_MANAGED_LLM_ENABLED` 一并退休**，不再授予任何权限。原先它为 true 时可让环境变量里的
    模型在受保护部署生效；留着等于把刚堵上的洞用一个 env 重新打开，且谁设的、设了什么都没有记录。
    代价是自建/内网部署也必须走管理页配置，不能只靠 env 起来。
  - **会话由调用方传入**（`get_llm_scorer(session=...)`）。最初实现让工厂自己开 `SessionLocal`，
    那会脱离调用方事务，在测试里还指向另一个数据库。拿不到会话的入口（CLI 诊断）保留自建会话回落。
  - **加密用独立 AAD 域** `platform-llm|<config_id>|<version>`：与 BYOK 的
    `ai-connection|<org>|<owner>|<version>` 互不通用，任一方密文搬到另一方都解不开。

### D-029：分享范围在发布时一次选定，发布后冻结

- 状态：`Accepted`；2026-09-09（用户决定，v3）。
- 背景：导入默认「仅自己可见」，而可见范围**目前完全不可改**——`PATCH /rubrics` 在非草稿状态返回 409，
  且 `RubricUpdate` 里根本没有 `visibility` 字段。
- 选择：`publish` 请求同时带编译产物与分享范围，原子生效；发布后范围与版本一起冻结。
- 原因：与「已发布模板请先复制为新版本后再编辑」保持同一条语义，不引入第二种可变性。
- 放弃：发布后可随时改范围（会让「已发布即冻结」出现例外）。扩大范围的路径是克隆为新版本。
- 代价/约束：**放宽了一条现有规则**——组织可见此前需 `org_admin`，改为创建者本人即可。否则
  「默认仅自己可见」会让每份标准都要走一次审批才能给同事用。跨组织的「所有人」维持 `platform_admin`。

### D-030：平台模型用量按实际调用人归属

- 状态：`Accepted`；2026-09-09（用户决定，v3）。
- 背景：平台模型被所有未绑 BYOK 的用户共用，费用记在平台账上。
- 选择：`ai_usage_ledger` 记录**实际发起调用的用户**，而非平台或组织。用量管理页面排入下一期。
- 原因：归属字段这一期就要写对；页面可以后补，**记漏了的账回填不回来**。
- 放弃：先不记、等做页面时再补。

### D-031：评分标准的并发保护复用编译层，不加 rubric 级版本号

- 状态：`Accepted`；2026-09-09（v3 §4.3 的复核结论）。
- 背景：v3 方案原本要给 `Rubric` 加 `state_version`，理由是「两人同编草稿会后写覆盖先写」。
- 复核发现：编辑草稿的实际路径是 `recompile`，它已经要求显式的 `supersedes_compilation_id`，
  基于非活跃草稿提交会被 `RUBRIC_RECOMPILE_BLOCKED` 整体拒绝——这就是编译层面的乐观并发。
  而 `PATCH /rubrics/{id}` 在这条路径上基本不可达：`POST /rubrics` 会自动产生一份编译产物，
  之后 PATCH 一律返回 `RUBRIC_RECOMPILE_REQUIRED`。
- 选择：不加 `state_version`，改为用测试钉住既有机制，并把「创建即产生编译产物」这个前提
  单独断言——它一旦不成立，PATCH 会重新变成可达路径，那时才需要重新考虑。
- 原因：加一个没人读的列和一次迁移，只会让后来者以为有两套并发机制而不知道该信哪个。
- 放弃：按方案原文添加 rubric 级 `state_version`（**方案写了但核实后不成立**）。
- 代价/约束：并发保护的粒度是编译产物而不是 rubric 行。若将来出现绕过 recompile 的编辑
  入口，需要重新评估。

## 4. 维护记录

| 日期 | 主题 | 决策处理 |
|---|---|---|
| 2026-09-01 | 初始化三文档 | 从 Accepted ADR、现有实现、迁移和 CI 汇总 D-001 至 D-013；未引入新的评分业务规则或 Core 切换授权。 |
| 2026-09-03 | P0 Provider 错误与 Langfuse 可观测性 | 接受 D-014；保持 D-004/D-005/D-009 的证据、运行身份和 collect-compute-persist 语义不变。 |
| 2026-09-04 | 评分韧性、规则检查点与人工复核 | 接受 D-015 至 D-018；保持 D-010 的 GATE-03 与默认 legacy 边界，明确当前增量实现和后续分布式/向量阶段。 |
| 2026-09-07 | 前端 v2 计划审查 | 新增 Proposed D-019，记录计划遗漏和备选调整；不把审查建议转为 Accepted 决策，不改变既有评分与发布边界。 |
| 2026-09-09 | V3-0b 写端点门控与并发复核 | 13 个评分标准写端点补 `require_organization_role`；核实 §4.3 的 `state_version` 多余，接受 D-031 改为钉住既有的编译层并发。未改评分语义。 |
| 2026-09-09 | v3 决策与三文档同步规则 | 接受 D-024 至 D-030：旧 SPA 下线、生产迁移工作流、导入唯一入口、BYOK 强制绑定、平台模型单例配置、分享范围发布时冻结、用量按调用人归属。把三文档同步写入 CLAUDE.md。未改评分语义与 GATE-03 边界。 |
| 2026-09-07 | 前端 v2 八条审查意见落实 | 根据用户确认将 D-019 从 Proposed 转为 Accepted 的待实施设计，记录证据投影、复核幂等、直传保留、日志扩展、权限、三部署、状态守卫及浏览器门禁；未实施业务代码或授予发布权限。 |
