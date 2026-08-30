# Supabase 直传与可恢复解析改造计划

## 1. 目标与用户流程

本次改造只调整“评分任务 → 上传待评材料 → 单文件解析”链路：

1. 浏览器先向业务 API 申请一个与当前组织、评分任务、文件名和文件大小绑定的短期上传凭证。
2. 文件不经过 Vercel Function：不大于 6 MiB 时默认使用 Supabase 私有桶的标准签名直传；大于 6 MiB 时直接使用 TUS；小文件标准直传发生网络错误时自动切换 TUS。
3. 浏览器直传完成后，业务 API 校验私有桶中的对象确实存在且大小一致，随后把材料状态持久化为 `uploaded`。
4. 每个文件分别触发解析。状态持久化为 `parsing`，成功为 `parsed`，失败为 `failed`；失败任务和超过租约时间的 `parsing` 任务可以重新执行。
5. 前端逐文件显示“准备上传、上传中、确认归档、解析中、完成、可重试”，不再把接口错误仅留在 Network 面板。

验收主路径：

`选择文件 → 申请凭证 → 私有桶归档 → 服务端确认对象 → 单文件解析 → 显示可恢复状态 → 可进入批量评分`

## 2. P0 改造任务（按执行顺序）

### P0-1 上传意图与私有对象确认

- 新增上传意图请求/响应和完成确认模型。
- 后端校验组织权限、评分任务、扩展名、MIME、文件大小及最大上传限制。
- 创建 `uploading` 状态的 Paper，占用唯一对象路径后生成 Supabase 签名上传 URL/Token；任何响应不得返回 Supabase Secret Key。
- 以 6 MiB 为分界返回推荐模式：`standard` 或 `tus`，同时返回 TUS 直连地址供网络失败回退。
- 完成确认时通过服务端 Storage API 校验对象存在、对象大小与声明大小一致，再把状态更新为 `uploaded`；确认操作保持幂等。

### P0-2 可恢复的单文件解析状态

- 解析前先持久化 `parsing` 并提交，使函数超时或进程中断后仍可识别未完成任务。
- `parsed` 请求幂等返回；`failed` 可立即重试；未超过租约的 `parsing` 拒绝重复执行；超过租约后允许接管重试。
- 保留已有“解析成功后才替换 chunks”的数据安全语义。
- 上传未确认、对象不存在或状态不允许时，返回包含用户可执行中文提示的结构化错误。

### P0-3 浏览器标准签名直传与 TUS 自动切换

- 每个文件单独申请上传意图并单独上传，避免一个失败拖垮整批文件。
- 不大于 6 MiB 默认使用标准签名直传，并显示真实上传进度。
- 大于 6 MiB 直接使用 TUS；标准直传发生断网、连接重置或超时等网络错误时，自动改用 TUS。
- TUS 使用 Supabase 规定的 6 MiB 分块、`x-signature`、直连 Storage Host、断点 URL/offset 恢复；业务 4xx 不误判为网络不稳定。
- 私有桶确认成功后才触发对应 Paper 的解析；解析失败只影响当前文件并显示“重新解析”。

### P0-4 测试先行与回归

- 先增加失败测试，再写生产实现。
- 后端覆盖权限、类型/大小边界、模式选择、签名信息脱敏、对象缺失/大小不符、完成幂等、解析状态持久化及超时接管。
- 前端契约覆盖不再调用批量 multipart、6 MiB/TUS 自动选择、网络错误回退、状态呈现及重试入口。
- 运行定向测试、JavaScript 语法检查、静态构建测试及全量 pytest。

## 3. P1 交互与运维任务

### P1-1 用户友好的状态与错误

- 上传区显示每个文件的传输方式、百分比和当前动作。
- API、Supabase 标准上传、TUS、归档确认和解析错误统一转换成中文说明和下一步操作。
- 上传/解析部分成功时保留成功项，不清空文件级结果；失败项提供重试入口。
- 未全部解析完成时，批量评分入口给出明确阻断原因，而不是笼统提示“请先上传”。

### P1-2 可观察性与发布检查

- 保留旧 `/papers/upload` 与 `/papers/bulk-upload` 供本地/离线兼容，Web 生产链路不再使用它们。
- 系统集成状态继续以 Supabase 私有桶为前提；配置缺失时上传意图接口明确提示管理员配置存储。
- 在上线清单中增加私有桶、CORS、签名 URL、TUS、6 MiB 边界、对象确认、失败恢复和 Vercel 4.5 MB 旁路验证。

## 4. 文件级范围

允许修改的生产源文件仅为：

- `.env.example`：记录直传独立大小上限、TUS 阈值和解析租约配置；旧 multipart 上限不再限制直传。
- `backend/app/core/config.py`：增加直传阈值和解析租约设置。
- `backend/app/schemas/paper.py`：增加上传意图、完成确认和直传响应模型。
- `backend/app/services/storage/local.py`：增加 Supabase 签名上传、直连 TUS 地址、对象信息校验辅助函数；不改变其他 artifact 读写协议。
- `backend/app/services/papers/ingestion.py`：增加解析任务领取/超时恢复语义，保留既有解析与 chunk 替换逻辑。
- `backend/app/api/routes/papers.py`：增加上传意图、完成确认和可恢复解析 API；保留旧接口兼容。
- `frontend/web/index.html`：增加文件级上传状态容器和用户提示。
- `frontend/web/assets/app.js`：实现标准签名直传、原生 TUS 分块恢复、自动回退、逐文件解析与重试。
- `frontend/web/assets/styles.css`：只增加上传进度与错误状态样式。
- `public/**`：由 `scripts/build_web_static.py` 从上述前端源文件重新生成的部署产物，不手工维护。

允许新增/修改的测试和文档仅为：

- `backend/app/tests/test_paper_direct_upload.py`：新增本次链路的后端和 Web 契约测试。
- `docs/Supabase直传与可恢复解析改造计划.md`：本计划与完成记录。
- `docs/上线清单.md`：增加本次生产验收项。

明确不在范围内：

- Rubric/AtomicRule、评分引擎、LLM、批量评分检查点、导出、认证和组织模型。
- Alembic 迁移和数据库表结构。
- 旧上传端点的协议删除或离线导入流程重写。
- Supabase 桶的公开访问策略；源文件始终使用私有桶和短期签名凭证。

## 5. 数据与接口设计

### `POST /api/papers/direct-upload-intents`

输入：`batch_id`、`file_name`、`content_type`、`byte_size`。

输出：`paper`、`mode`、`signed_url`、`token`、`tus_endpoint`、`bucket_name`、`object_path`、`threshold_bytes`。`mode` 仅为推荐方式，同一 token 可供标准上传和 TUS 使用。

### `POST /api/papers/{paper_id}/complete-upload`

输入：`byte_size`。

行为：校验对象存在及大小；成功后从 `uploading` 转为 `uploaded`，重复确认返回当前 Paper。

### `POST /api/papers/{paper_id}/parse`

行为：持久化领取解析任务后执行单文件解析；支持 `failed` 和过期 `parsing` 的恢复；`parsed` 幂等返回。

## 6. 不变量与安全约束

- 文件内容不进入 Vercel Function 请求体。
- 签名只能写入服务端生成的唯一对象路径，并具有短有效期。
- Paper、Batch、对象路径均绑定同一组织；所有变更接口要求 `org_admin` 或 `teacher`。
- 完成确认不能信任浏览器声明，必须查询 Storage 对象元数据。
- 不覆盖同路径对象；每次上传使用新的 Paper ID 路径。
- Supabase Secret Key 不进入 HTML、JavaScript、API 响应或日志。
- 单文件失败不回滚同批次已成功文件。

## 7. 完成定义

- 新增测试在实现前可复现失败，实现后全部通过。
- 小文件标准直传，大文件与网络错误自动 TUS 的契约均被测试锁定。
- 生产 Web 不再调用 `/papers/bulk-upload`。
- 解析中断后可通过同一 Paper 恢复，不产生重复 Paper。
- 全量 pytest 与静态构建通过。
- `git diff --name-only` 只包含第 4 节列出的文件；任何范围外文件必须恢复为无改动状态。

## 8. 实施结果（2026-08-30）

- 已在分支 `codex/supabase-direct-upload-recoverable-parse` 完成实现；未新增 Alembic 迁移，未修改评分、LLM、Rubric、认证或批任务内核。
- 测试先行红灯：新增契约初次执行为 `6 failed`，分别命中新接口、对象确认、解析领取和前端直传缺失。
- 定向回归：直传、既有核心流程、ingestion、Supabase 部署契约和静态构建共 `15 passed`。
- 全量回归：`.venv/bin/python -m pytest -q` 为 `1417 passed, 46 warnings`；warnings 均为既有依赖/SQLite Python 3.12 弃用提示。
- JavaScript 语法：`node --check frontend/web/assets/app.js` 通过；`git diff --check` 通过。
- 静态产物：源码与 `public/assets` 的 SHA-256 一致，当前文件名为 `app.d6bca96d81f0.js`、`styles.4b0e1f1ffc8a.css`。
- 安全兼容：TUS 恢复地址使用 `sessionStorage`，满足既有“前端不使用 localStorage 保存会话/敏感数据”门禁；API/静态资源未包含 Supabase Secret Key。
- 范围审计：所有改动均位于第 4 节允许范围；其中 `backend/app/services/papers/ingestion.py` 无需修改，既有成功后替换 chunks 的语义直接复用。
