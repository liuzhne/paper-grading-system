# P0 集成配置

本项目的三条 P0 能力都已经有正式实现：

- 真实 LLM Adapter：`OpenAICompatibleChatScorer`，默认推荐智谱 GLM-4.7-Flash
- 真实 Google Sheets/在线表格写入：`GoogleAppsScriptSheetWriter`
- 正式产品前端：FastAPI 托管的 `frontend/web`

总览页的 `P0 集成状态` 会显示当前运行时是否真正启用了这些能力。该状态只显示布尔配置，不会返回 API Key 或密钥明文。

## 1. 启用真实 LLM Adapter：智谱 GLM-4.7-Flash

复制 `.env.example` 为 `.env`，并设置：

```bash
LLM_PROVIDER=openai_compatible
LLM_FALLBACK_TO_MOCK=true
LLM_DEBUG_LOG_ENABLED=true
LLM_DEBUG_LOG_MAX_CHARS=12000
LLM_RATE_LIMIT_SLEEP_SECONDS=1
LLM_RETRY_BASE_DELAY_SECONDS=1
LLM_RETRY_MAX_DELAY_SECONDS=30
LLM_429_RETRY_DELAY_SECONDS=12
OPENAI_COMPATIBLE_API_KEY=replace-with-zhipu-api-key
OPENAI_COMPATIBLE_BASE_URL=https://open.bigmodel.cn/api/paas/v4
OPENAI_COMPATIBLE_MODEL=glm-4.7-flash
OPENAI_COMPATIBLE_PROVIDER_NAME=zhipu
OPENAI_COMPATIBLE_TIMEOUT_SECONDS=60
OPENAI_COMPATIBLE_MAX_TOKENS=1200
OPENAI_COMPATIBLE_MAX_RETRIES=2
OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON=true
OPENAI_COMPATIBLE_THINKING_TYPE=disabled
SCORING_CHUNK_EVAL_TOP_K=3
# 以下两项仅供 legacy_unversioned compatibility；正式 Core 不读取它们授权分值
SCORING_STANDARD_CAP_RATIO=0.8
SCORING_EXCEPTIONAL_RATIO=0.92
```

`glm-4.7-flash` 支持思考模式。论文评分接口只需要稳定返回结构化 JSON，所以默认开启 JSON 模式并关闭思考模式，避免模型把输出预算消耗在 `reasoning_content` 上而最终 `message.content` 为空。

评分引擎按评分项逐块评测证据：每次只给 LLM 一个证据块，后端再汇总已验证证据。正式 published RubricVersion 的分值、档位、舍入与复核边界全部来自冻结 ScoringPolicy、AtomicRule 和 RuleLevel，不存在代码级固定 80% 封顶。`SCORING_STANDARD_CAP_RATIO` 与 `SCORING_EXCEPTIONAL_RATIO` 只保留给 `legacy_unversioned compatibility`，不能授权或覆盖 Core 规则。

真实 LLM 调用默认会在后端日志输出 `[LLM request]`、`[LLM response]` 和 `[LLM exception]`，包含请求 URL、模型 payload、响应状态码和响应体，`Authorization` 与 API Key 会自动打码。若论文内容不希望进入日志，可设置 `LLM_DEBUG_LOG_ENABLED=false`；如响应体较长，可调大 `LLM_DEBUG_LOG_MAX_CHARS`。

若智谱返回 `429 Too Many Requests`，说明触发了频率或额度限制。系统会尊重响应里的 `Retry-After`，没有该响应头时按 `LLM_429_RETRY_DELAY_SECONDS` 做更长退避，并在每次真实 LLM 调用前按 `LLM_RATE_LIMIT_SLEEP_SECONDS` 主动限速。免费额度调试建议从 `LLM_RATE_LIMIT_SLEEP_SECONDS=2`、`LLM_429_RETRY_DELAY_SECONDS=20` 开始；如果仍频繁 429，把 `SCORING_CHUNK_EVAL_TOP_K` 从 `3` 降到 `1` 或 `2`。

可以用下面的诊断命令单独测试真实 LLM，不会打印 API Key：

```bash
uv run python -m backend.app.scripts.diagnose_llm
```

诊断结果含义：

- `stage=transport`：请求没有进入 HTTP 响应阶段，优先检查代理、DNS、TLS 拦截或网络出口。
- `content_chars=0` 且 `reasoning_content_chars>0`：模型有思考内容但没有最终回答，确认 `OPENAI_COMPATIBLE_THINKING_TYPE=disabled` 后重启后端。
- `status_code=401/403`：API Key、账户权限或模型授权问题。
- `status_code=404`：Base URL 或模型名称不匹配。

重启 FastAPI 后，总览页应显示：

- `真实 LLM Adapter`
- `国内兼容模型已启用`
- `OpenAICompatibleChatScorer / glm-4.7-flash / thinking disabled / JSON mode`

评分链路仍由后端程序计算总分、等级和复核标记。LLM 只输出单项评分、原因、证据、建议和置信度。

### 切换到阿里云百炼 Qwen

如果智谱效果不满足要求，可以只改环境变量切换到阿里云百炼：

```bash
LLM_PROVIDER=openai_compatible
OPENAI_COMPATIBLE_API_KEY=replace-with-dashscope-api-key
OPENAI_COMPATIBLE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OPENAI_COMPATIBLE_MODEL=qwen-plus
OPENAI_COMPATIBLE_PROVIDER_NAME=dashscope
```

## 2. 启用真实 Google Sheets 写入

1. 在 Google Sheets 创建目标表格。
2. 打开 `扩展程序 -> Apps Script`。
3. 将 `docs/google_apps_script_webapp.gs` 的内容复制进去。
4. 在 Apps Script 的 `项目设置 -> 脚本属性` 中添加：

```text
PAPER_GRADING_SECRET=<your-shared-secret>
```

5. 部署为 Web App：

```text
执行身份：我
访问权限：任何知道链接的人
```

6. 在 `.env` 设置：

```bash
SHEET_WRITER_PROVIDER=google_sheets
SHEET_FALLBACK_TO_MOCK=false
GOOGLE_SHEETS_WEBAPP_URL=https://script.google.com/macros/s/.../exec
GOOGLE_SHEETS_WEBAPP_SECRET=<your-shared-secret>
```

重启 FastAPI 后，总览页应显示：

- `在线表格写入`
- `Google Sheets 已启用`
- `GoogleAppsScriptSheetWriter`

在前端 `导出写表` 页填写目标表格 ID，点击 `写入在线表格`。系统会写入两张表：

- `总分表`
- `评分明细表`

每次写入都会记录到 `写表记录`。

## 3. 使用正式产品前端

正式前端不需要单独启动 Streamlit。启动 FastAPI 后直接访问：

```text
http://localhost:8000/
```

Streamlit 只作为备用操作台保留：

```text
http://localhost:8501
```

正式前端已经覆盖核心闭环：

- 总览与 P0 集成状态
- 评分标准创建、Word/Excel 导入、草稿编辑、发布、复制版本
- 批次创建、论文上传、论文信息校正、持久化批量评分（用户提交观察策略、进度/错误/门禁信号、取消与定向重试）
- 评分证据查看、单项分数调整、人工复核
- Excel 导出、HTML 报告、在线写表和写表记录
