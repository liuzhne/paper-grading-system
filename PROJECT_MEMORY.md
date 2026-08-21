# 项目记忆：生产环境

> 最后核验：2026-08-20（Asia/Shanghai）。本文件只记录可共享的运行状态与操作约定，**严禁写入密码、token、连接串或服务角色密钥的值**。

## 当前生产部署

- 公开入口：[https://paper-grading-system-kohl.vercel.app/](https://paper-grading-system-kohl.vercel.app/)
- Vercel 团队/项目：`derrick-s-projects5/paper-grading-system`
- 当前生产部署：`dpl_Drh5X6gAzAH3TGb2mbyuhJo8t8ze`，状态 `Ready`；区域 `iad1`；FastAPI 函数约 80 MB。
- 当前源码分支：`Convert2RubricScalePlatform`；最近提交：`1b95455d0f369279dcc063a56eac1dde22e060ce`（Refine sidebar service status and account controls）。
- 运行时：Python 3.12；静态 Web 前端由 FastAPI 在 `/` 提供，Streamlit 仅保留为备用入口。

## 数据与集成

- 数据库：Supabase PostgreSQL。迁移基线为 Alembic `0017_batch_scoring_jobs`；数据库恢复后已执行到该 head。
- Supabase Vercel 集成资源：`supabase-cinereous-ocean`（Available）。
- 文件存储：`STORAGE_PROVIDER=supabase`，私有桶 `paper-grading-private`；上传、解析物和报告应留在此桶，不依赖 Vercel 本地磁盘。
- 鉴权：生产启用 `AUTH_ENABLED=true`；`/api/auth/status` 返回 `auth_required=true`；未认证访问 `/api/rubrics`、`/api/batches` 应返回 `401`。
- 前端侧栏通过 `/api/auth/me` 显示实际登录用户名、`已登录` 状态和首字母头像；未登录显示中性访客状态。不要重新引入硬编码 `dev-user / 本地工作区`。
- LLM：当前为 `mock`、离线、不触网；真实模型尚未配置。`SCORING_ENGINE_MODE` 必须保持 `legacy`，除非完成 GATE-03 并获得明确发布批准。
- 在线表格：当前为 `MockSheetWriter`；真实 Google Sheets 写入尚未配置。

## 已验证的生产冒烟检查

- 首页、工作台、评分任务、结果复核、模板中心、输出中心均可加载和切换。
- 生产登录会话可用，浏览器控制台未发现错误或警告。
- `/api/system/integrations` 显示：静态前端已启用、Supabase 存储已配置、LLM 和在线表格均为 Mock。
- Mock 模型连通性检查成功；这不代表真实 LLM 已可用。
- 生产库目前没有经用户授权导入的正式评分模板/批次。不得为了演示或测试而擅自 seed 评分规则、上传论文或创建评分任务。

## 发布与复核流程

1. 先运行 `.venv/bin/python -m pytest -q`；涉及前端时至少在本地验证主导航、登录状态和浏览器控制台。
2. JavaScript 静态资源有改动时，更新 `frontend/web/index.html` 中 `app.js` 的查询版本号，避免浏览器缓存旧脚本。
3. 发布命令：`pnpm dlx vercel@latest deploy --prod --yes --scope derrick-s-projects5`。必须带 `--scope derrick-s-projects5`，否则可能出现 `Not authorized`。
4. 发布后执行：
   - `pnpm dlx vercel@latest inspect https://paper-grading-system-kohl.vercel.app --scope derrick-s-projects5`
   - `curl -sS -o /dev/null -w '%{http_code}\n' https://paper-grading-system-kohl.vercel.app/`（期望 `200`）
   - 未认证检查：`/api/rubrics` 与 `/api/batches` 期望 `401`。
5. 不要将 `.env*`、`.vercel/`、本地 storage、测试材料或任何密钥打包/提交；`vercel.json` 与 `.vercelignore` 已负责生产发布边界。

## 安全待办（P0）

Vercel 环境变量清单显示以下**密钥类变量**被标为 `Non-sensitive`：`SUPABASE_SERVICE_ROLE_KEY`、`SUPABASE_SECRET_KEY`、`SUPABASE_JWT_SECRET`、`POSTGRES_PASSWORD`、`POSTGRES_URL`、`POSTGRES_URL_NON_POOLING` 等。应在 Vercel 中将所有私密数据库/服务角色变量改为 **Sensitive**；若其值曾被不应访问的人看到，需先轮换再重新部署。

Supabase 的公开 URL、匿名/可发布 key 可按前端需求保留为非敏感；服务角色 key、数据库密码和连接串绝不能公开。

