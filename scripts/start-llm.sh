#!/usr/bin/env bash
# 本地大模型启动脚本：llama.cpp 跑 Qwen3-30B-A3B-Instruct-2507（OpenAI 兼容端点 /v1）
#
# ⚠️ 手动前台启动，自己盯实时日志（项目约定：LLM 不自助拉起）。
# 起完用 `pgs check` 验证 network=local + pong。
#
# 配套 .env（app 侧连接）：
#   LLM_PROVIDER=openai_compatible
#   OPENAI_COMPATIBLE_BASE_URL=http://localhost:18434/v1
#   OPENAI_COMPATIBLE_MODEL=qwen3-30b-a3b-instruct-2507
#   OPENAI_COMPATIBLE_PROVIDER_NAME=llamacpp
#   LLM_FALLBACK_TO_MOCK=false
#   OPENAI_COMPATIBLE_TIMEOUT_SECONDS=180   # 跑批关键，默认 60/300 都会放大卡死
#   OPENAI_COMPATIBLE_MAX_RETRIES=2
set -euo pipefail

# gguf 路径（HuggingFace 缓存 unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF）。
# 换模型/版本时改这里，或用环境变量覆盖：MODEL=/path/to.gguf ./scripts/start-llm.sh
HF_SNAPSHOT="$HOME/.cache/huggingface/hub/models--unsloth--Qwen3-30B-A3B-Instruct-2507-GGUF/snapshots/eea7b2be5805a5f151f8847ede8e5f9a9284bf77"
MODEL="${MODEL:-$HF_SNAPSHOT/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf}"
PORT="${PORT:-18434}"

if [[ ! -f "$MODEL" ]]; then
  echo "找不到模型文件：$MODEL" >&2
  echo "请改脚本里的 MODEL 或：MODEL=/path/to.gguf $0" >&2
  exit 1
fi

# 参数取自 QWK 攻坚定下的可复用配置（详见 memory: qwk-baseline-campaign）：
#   --parallel 4 : 4 slots 才稳；单 slot 长跑会被弃单生成占住→重试雪崩（死亡螺旋）
#   -c 16384     : 整体判分 holistic 提示词最大 ~5k token，16k 留足余量不溢出
#   --jinja      : 用模型自带 chat template
#   -ngl 999     : 全部层上 GPU（Metal）
#
# ⚠️ 这台 48GB Mac 上 30B 不能无人值守过夜长跑（macOS 后台抢内存→生成塌速→
#    客户端超时弃单→雪崩）。跑批前先关 Chrome 等释放内存。
exec llama-server \
  -m "$MODEL" \
  --host 127.0.0.1 \
  --port "$PORT" \
  -c 16384 \
  --parallel 4 \
  --jinja \
  -ngl 999
