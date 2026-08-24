# 多用户、组织权限与用户自带 AI 改造方案

## 1. 目标与边界

将当前“部署管理员通过 `.env` 配置一套共享 AI”的单租户系统，升级为支持注册、多用户、多组织、RBAC 权限、模板分级可见性，以及用户自带 AI Key（BYOK，Bring Your Own Key）的平台。

改造范围是身份、资源隔离、模型凭据归属与审计；不改变以下既有评分边界：

- 已发布 `RubricVersion`、AtomicRule 和冻结 `ScoringPolicy` 的不可变与授权边界；
- 评分内核的确定性优先、证据校验与人工复核机制；
- `SCORING_ENGINE_MODE=legacy` 仍为默认值，未经既有 GATE-03 发布批准不得切换默认 Core；
- 历史批次、评分报告和模型信息须继续可追溯。

当前仓库已有 `users`、`owner_id` 和 `password_hash` 等基础字段，但鉴权依赖仍将请求统一映射到 `DEFAULT_DEV_USER_ID`，尚未实现真实用户身份或资源隔离。现有 `.env` 中的 LLM 配置仍是整个服务进程共享的管理员配置。

## 2. 目标架构

```text
平台
└── 组织（学校 / 学院 / 部门 / 团队）
    ├── 成员与组织角色
    ├── 组织共享模板、批次与可选共享 AI 连接
    └── 用户
        ├── 私有模板、论文、报告
        └── 私有 AI 连接与 API Key
```

用户自带 AI 的调用链：

```text
用户登录
  → 选择自己的 AI 连接
  → 创建或提交评分任务
  → 后端读取该任务绑定的连接配置
  → 临时解密 API Key 并调用 AI 厂商
  → 保存评分、模型快照与用量
```

API Key 不下发给浏览器，不由浏览器直接调用 AI 厂商，也不出现在评分记录、导出、日志或错误响应中。

## 3. 账户、组织与会话

### 3.1 真实账户体系

将现有单租户简单登录升级为：

- 管理员邀请注册、登录、登出、管理员签发重置令牌后的密码重置；
- 密码使用 Argon2id（或 bcrypt）哈希；
- 服务端持久化会话，前端使用 `HttpOnly`、`Secure`、`SameSite` Cookie；
- 不再将 Bearer Token 保存到浏览器 `localStorage`；
- `AUTH_USERNAME` / `AUTH_PASSWORD` 仅用于首次部署时创建 Bootstrap Admin，而不是日常共享管理员账号。

注册仅允许 `invite_only`：管理员为指定邮箱和组织角色创建一次性邀请链接；注册请求中的邮箱必须与该邀请记录完全匹配，其他邮箱一律拒绝。邀请码在注册成功后即失效，不依赖邮件验证服务。

### 3.2 数据模型

新增核心表：

- `organizations`：组织；
- `organization_members`：用户与组织的多对多成员关系及组织角色；
- `auth_sessions`：可撤销的服务端会话；
- `password_reset_tokens`：管理员签发、一次性、带过期时间的密码重置令牌；
- `audit_logs`：重要安全和业务操作审计。

保留现有 `users.password_hash`，但将平台权限与组织权限分离：

- 平台权限：普通用户、`platform_admin`；
- 组织权限：`org_admin`、`teacher`、`member`；
- 同一用户可加入多个组织，并在不同组织拥有不同角色。

### 3.3 最小权限矩阵

| 操作 | `platform_admin` | `org_admin` | `teacher` | `member` |
| --- | --- | --- | --- | --- |
| 管理平台与系统模板 | 是 | 否 | 否 | 否 |
| 管理组织成员 | 否 | 是 | 否 | 否 |
| 创建批次、上传论文、发起评分 | 是 | 是 | 是 | 否 |
| 创建个人私有模板 | 是 | 是 | 是 | 否 |
| 管理个人 AI Key | 是 | 是 | 是 | 是 |
| 查看个人报告 | 是 | 按组织授权 | 按归属授权 | 按归属授权 |

## 4. 资源隔离与对象级授权

### 4.1 组织上下文

将现有固定返回默认用户的 `current_user_id()` 改为解析已验证会话，返回：

```text
CurrentPrincipal {
  user_id,
  organization_id,
  organization_role,
  platform_role
}
```

当前组织应由用户显式选择或由请求头/子域名确定；服务端必须验证该用户确实是组织成员，不能信任客户端传来的任意组织 ID。

### 4.2 数据边界

主要业务资源应新增或明确维护 `organization_id`：

- `rubrics`、`rubric_versions`；
- `grading_batches`、`papers`、`scoring_runs`；
- 导出记录、校准锚点、上传文件与报告元数据；
- LLM 缓存、调用账本与审计记录。

所有列表与详情查询均需在 SQLAlchemy 查询中增加组织条件。对于私有资源，还需同时限制 `owner_id`。任何按 ID 的读取、编辑、删除、下载、重试、导出或评分接口，都要先执行对象级授权检查；不能仅以对象存在作为许可。

## 5. Rubric 模板可见性与版本管理

### 5.1 模板范围

`rubrics` 新增：

- `organization_id`：系统模板为 `NULL`；
- `visibility`：`system`、`organization`、`private`；
- `owner_id`：明确关联创建者；
- `published_by`、`archived_by` 等审计字段。

模板查询规则：

```text
visibility = system
或 visibility = organization 且 organization_id = 当前组织
或 visibility = private 且 owner_id = 当前用户
```

创建批次绑定 `rubric_id` 时，后端必须再次验证当前用户拥有该模板的读取权限，防止跨组织引用或猜测私有模板 ID。

### 5.2 唯一约束

当前 `rubrics(name, version)` 是全局唯一。多组织后，不能直接以普通 `organization_id + name + version` 唯一约束替代，因为 PostgreSQL 中 `NULL` 值不会彼此冲突，系统级模板可能产生重复。

应改为按范围建立部分唯一索引：

- 系统模板：`name + version` 唯一；
- 组织模板：`organization_id + name + version` 唯一；
- 私有模板：`owner_id + name + version` 唯一。

### 5.3 不可变性

已发布且被评分批次引用的模板仍禁止原位修改。调整模板只能通过 Clone 派生新草稿或新版本，修改、验证、审核后再发布。该原则保证评分报告、证据、扣分和模型调用上下文可复现。

## 6. 用户自带 AI（BYOK）

### 6.1 AI 连接模型

新增 `ai_connections` 表：

- `id`、`organization_id`、`owner_id`；
- `name`、`scope`；
- `provider_type`：第一期仅支持 `openai_responses`、`openai_compatible`；
- `base_url`、`model_name`；
- `provider_options`：JSON mode、thinking、超时等非敏感参数；
- `api_key_ciphertext`、`key_version`、`key_last4`；
- `status`、`last_verified_at`、`last_error_code`；
- 创建、更新、停用、删除时间。

第一期连接范围仅允许 `private`，即用户使用自己的厂商账号并自行承担费用。组织共享连接是后续功能，必须由 `org_admin` 显式创建、授权，并提供额外的预算与审计控制。

### 6.2 密钥管理

API Key 使用信封加密：

- 业务数据库仅保存密文、随机 nonce/认证标签与密钥版本；
- 主密钥由 KMS、密钥服务或独立的部署密钥配置托管；
- 服务端仅在发起厂商请求前短暂解密；
- API 仅显示如 `sk-…7H2K` 的掩码；
- 密钥绝不进入任务表、评分记录、导出、浏览器存储、应用日志或异常详情；
- 备份必须采用加密与访问控制。

### 6.3 评分运行时改造

现有模型工厂从读取全局 `settings` 改为接收本次任务绑定的运行时连接配置：

```python
get_llm_scorer(connection_runtime)
```

评分任务创建时固定 `ai_connection_id`、连接版本、模型名、厂商和非敏感参数快照。评分记录保存该快照及 token 用量，绝不保存密钥。

密钥更换或连接停用仅影响新任务。排队任务若发现绑定连接已停用或版本不一致，应明确失败并要求重新选择连接；不得静默切换到平台 Key 或 Mock 评分器。

### 6.4 前端交互

新增“账户设置 → 我的 AI 连接”页面：

- 厂商预设：OpenAI、智谱、阿里云百炼、Gemini OpenAI-compatible；
- 自定义兼容端点、模型名及高级参数；
- 测试未保存连接与已保存连接；
- 保存、替换 Key、停用、删除；
- 显示掩码、状态、最后测试时间和调用量；
- 明示论文内容将发往用户选择的厂商，费用由该用户的厂商账户结算。

创建评分任务时，用户从自己有权限使用的连接中选择一个。浏览器只将 Key 通过 HTTPS 发送到后端保存或测试，绝不直接请求模型厂商。

## 7. 安全、缓存与隐私

- AI 连接管理与连接测试必须要求登录、限流、审计；不能复用公开系统检测接口作为用户配置接口。
- 自定义 Base URL 必须防 SSRF：只允许 HTTPS，拦截 `localhost`、回环地址、私网 IP、链路本地地址和云元数据地址；同时在网络出口层实施允许列表或等效策略。
- 关闭原始 LLM 调试日志。论文输入、模型响应和缓存属于敏感教育数据，默认不得以原文方式落入可被其他租户读取的位置。
- LLM 缓存键必须加入 `organization_id`、`ai_connection_id`、连接版本或配置指纹。不得跨组织、跨用户、跨连接复用缓存。
- 对原始输入缓存采用加密、保留期限与访问范围控制；默认优先保存脱敏审计投影而非论文全文。
- 记录 token、请求数、失败率与本系统的估算费用；最终账单以用户在 AI 厂商账户中看到的金额为准。

## 8. API 与前端改造清单

### 8.1 认证与组织 API

- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `POST /api/auth/password-reset/confirm`
- `GET /api/auth/me`
- `GET /api/organizations`
- `POST /api/organizations/{id}/members`
- `POST /api/organizations/{id}/members/{user_id}/password-reset-token`
- `PATCH /api/organizations/{id}/members/{user_id}`

### 8.2 AI 连接 API

- `GET /api/ai-connections`
- `POST /api/ai-connections`
- `PATCH /api/ai-connections/{id}`
- `POST /api/ai-connections/{id}/test`
- `POST /api/ai-connections/test-draft`
- `POST /api/ai-connections/{id}/rotate-key`
- `POST /api/ai-connections/{id}/disable`
- `DELETE /api/ai-connections/{id}`

所有响应禁止返回 API Key 明文或可逆密文。

## 9. 数据库迁移与兼容策略

在当前 Alembic head 之后依次新增迁移，具体 revision 编号以实际合入时的 head 为准：

1. 身份与组织：创建组织、成员、会话、验证与审计表；创建默认组织；将原默认开发用户设为 Bootstrap Admin。
2. 资源归属：为现有业务资源增加 `organization_id`，回填为默认组织，再逐步加外键、索引和非空约束。
3. 模板范围：增加 Rubric `visibility`、组织字段、审计字段，并替换原全局唯一约束为范围唯一索引。
4. AI 连接：创建加密连接表、用量账本与任务模型快照字段。
5. 缓存隔离：对缓存/账本增加组织、连接和保留策略边界，迁移或废弃不满足隔离要求的旧缓存。

旧 `.env` LLM 配置保留为“平台托管模型”，仅用于迁移期、演示或组织明确授权的场景。不得将环境变量中的平台 API Key 自动复制到任何用户 AI 连接。历史评分保持原模型记录与结果不变。

## 10. 分阶段实施与验收

### 10.1 实施阶段

1. 账户、组织、会话与 Bootstrap Admin。
2. 所有资源的组织过滤、对象级授权与越权修复。
3. Rubric 三层可见性、范围唯一索引和发布权限。
4. 加密 AI 连接、动态 scorer 工厂、任务绑定与连接测试。
5. 注册、组织管理、模板范围和 AI 连接前端。
6. 邀请注册灰度、生产监控和公开注册评估。

### 10.2 验收标准

- 用户 A 无法通过前端、直接 API、文件下载、导出、报告或缓存访问用户 B 的资源；
- API Key 不出现在数据库明文、响应、日志、导出、浏览器存储、备份明文或错误信息中；
- 每个评分任务可追溯发起者、组织、连接版本、厂商、模型和用量，但无法恢复密钥；
- 连接失效、额度不足或模型不兼容时，任务状态明确，不静默使用其他用户、平台 Key 或 Mock；
- 历史批次、报告和旧 API 的过渡访问保持兼容；
- Alembic 升级/降级、PostgreSQL 约束、跨组织越权、SSRF、密钥加密、缓存隔离、批任务恢复均具备自动化测试；
- 涉及 LLM 调用构造的改动后，按项目要求重新运行 QWK 留出集与发布门禁；
- 不因本改造擅自切换默认评分引擎或改变正式规则授权逻辑。

## 11. 第一阶段交付范围

第一期建议限定为：邀请注册、组织成员、多组织资源隔离、Rubric 的 `system` / `organization` / `private` 可见性、私有 BYOK，以及 OpenAI/OpenAI-compatible 两类协议。

该范围能满足“可注册、多用户、用户自己配置并自行付费使用 AI”的核心目标，同时控制协议适配、共享计费和公开注册带来的安全与运维风险。
