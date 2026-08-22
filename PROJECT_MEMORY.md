# 项目记忆：生产环境

> 最后核验：2026-08-22（Asia/Shanghai）。本文件只记录可共享的运行状态与操作约定，**严禁写入密码、token、连接串或服务角色密钥的值**。

## 当前生产部署

- 公开入口：[https://paper-grading-system-kohl.vercel.app/](https://paper-grading-system-kohl.vercel.app/)
- Vercel 团队/项目：`derrick-s-projects5/paper-grading-system`
- 当前生产部署：`dpl_8GPV2quDdMnefVonhpUKSr3LcooL`，状态 `Ready`，于 2026-08-22 20:16（Asia/Shanghai）创建；区域 `iad1`；FastAPI 函数约 80 MB。
- 当前源码分支：`main`；部署代码提交：`3b544a670066ceec9519c0d3f90bf8cd253eba93`（`feat: add multi-tenant users and private AI connections`）。`develop` 与 `main` 在该代码提交上保持一致。
- 运行时：Python 3.12；静态 Web 前端由 FastAPI 在 `/` 提供，Streamlit 仅保留为备用入口。

## 数据、鉴权与集成

- 数据库：Supabase PostgreSQL 17.6，项目 ref `euizniwqisnhfhxwiqsg`。生产库为空库初始化后已到 Alembic `0022_legacy_tenant_backfill`。
- 运行数据库角色：`pgs_app`。它只拥有 `public` 现有表的应用读写与序列使用权限；不具备超级用户、建库、建角色、`BYPASSRLS`、DDL、`auth` schema 或 `storage` schema 权限。Supabase RLS 已为 38 张应用表配置此角色的应用策略；未来新增表或序列后，必须由迁移管理员显式补充授权与策略。
- 文件存储：`STORAGE_PROVIDER=supabase`，私有桶 `paper-grading-private`。2026-08-22 已完成随机临时对象的写入、读取、删除往返验证；不依赖 Vercel 持久本地磁盘。
- 鉴权：生产启用 `AUTH_ENABLED=true`、`REGISTRATION_MODE=invite_only`、安全 Cookie。Bootstrap Admin 已成功首次登录并绑定 `Default Organization`；未认证数据端点返回 `401`。
- 多租户与 BYOK：组织端点和私有 AI connection 路由均已生产验证；`BYOK_MASTER_KEY` 与版本号由受管 Production 变量提供，API 响应不暴露 API Key 或密文。
- LLM：平台托管模型关闭，默认 `mock`；租户在明确授权后可使用私有 BYOK。`OFFLINE_MODE=false` 以支持 BYOK 外呼；`SCORING_ENGINE_MODE=legacy`，在 GATE-03 获明确发布批准前不得默认切换 Core。
- 在线表格：当前为 `MockSheetWriter`；真实 Google Sheets 写入尚未配置。

## 已验证的生产冒烟检查（2026-08-22）

- Vercel 部署状态 `Ready`，生产别名已指向当前部署；首页 `/` 返回 `200`。
- `/api/auth/status`、`/api/system/integrations` 返回 `200`；静态前端和 Supabase 私有存储均显示已配置。
- 未认证的 `/api/auth/me`、`/api/organizations`、`/api/ai-connections`、`/api/system/ops-readiness` 均返回 `401`。
- Bootstrap Admin 登录返回 `204`；认证后的 `/api/auth/me`、`/api/organizations`、`/api/ai-connections`、`/api/system/ops-readiness` 均返回 `200`，随后已注销临时会话。
- Ops readiness 显示 PostgreSQL、鉴权安全检查通过，并保持 `production_default_switch_authorized=false`。
- 直接数据库复核：`alembic_version=0022_legacy_tenant_backfill`、1 个组织、2 个用户（含 Bootstrap Admin）、38 条 `pgs_app` RLS 策略；会话池与事务池读写探测均已回滚。
- 未导入正式评分模板、批次、论文或评分任务；不得为了演示而擅自 seed 生产业务数据。

## 发布与复核流程

1. 先运行 `.venv/bin/python -m pytest -q`；涉及前端时至少在本地验证主导航、登录状态和浏览器控制台。
2. JavaScript 静态资源有改动时，更新 `frontend/web/index.html` 中 `app.js` 的查询版本号，避免浏览器缓存旧脚本。
3. 发布命令：`vercel deploy --prod --yes --scope derrick-s-projects5`。必须带 `--scope derrick-s-projects5`。
4. 发布后执行：
   - `vercel inspect https://paper-grading-system-kohl.vercel.app --scope derrick-s-projects5`
   - 首页 `200`、未认证数据端点 `401`、管理员登录 `204`、认证后的组织与 AI connection 端点 `200`。
   - 复核 `alembic_version`、`/api/system/ops-readiness`、私有 Storage 写读删往返。
5. 不要将 `.env*`、`.vercel/`、`supabase/.temp/`、本地 storage、测试材料或任何密钥打包/提交。`.env.local` 是按部署授权维护的本机凭据副本，权限必须为 `0600` 且保持 Git 忽略；Vercel Production 变量必须全部标记为 `Sensitive`。

## 安全状态与后续事项

- 2026-08-22 已将本次数据库、Supabase、鉴权、BYOK 和运行时 Production 变量同步为 Vercel `Sensitive`，并保存到受 Git 忽略、权限 `0600` 的 `.env.local`；未记录任何值。
- 已移除代码未使用、指向旧项目的 Production `SUPABASE_JWT_SECRET`，避免保留过期凭据。
- 生产函数在 `iad1`，数据库与 Storage 在新加坡；大批同步评分可能受跨区域延迟和 Vercel 300 秒函数时限影响，应通过持久化批任务和运维监控处理，不应以默认 Core 切换作为缓解手段。
- 任何历史上曾暴露于终端输出、日志或本机 shell 配置的第三方 API Key，仍应按其提供商流程轮换；当前生产不使用平台托管真实 LLM Key。
