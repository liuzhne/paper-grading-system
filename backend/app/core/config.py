from pathlib import Path
from typing import Literal, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "Paper Grading System"
    API_PREFIX: str = "/api"
    DATABASE_URL: str = "postgresql+psycopg://paper:paper@localhost:5432/paper_grading"
    STORAGE_ROOT: Path = Path("storage")
    MAX_UPLOAD_SIZE_MB: int = 50
    OFFLINE_MODE: bool = False  # True=硬禁止一切外呼（网络型导出报错提示改用 Excel）；CLI 可经 --offline 开启
    DEFAULT_DEV_USER_ID: str = "00000000-0000-0000-0000-000000000001"
    DEFAULT_DEV_USERNAME: str = "dev-user"
    # 单租户简单登录（opt-in）：默认 False=本地/可信网络免登录（CLI 亦免）；
    # Web 公网部署时设 AUTH_ENABLED=true + AUTH_PASSWORD 才真正要求登录。
    AUTH_ENABLED: bool = False
    AUTH_USERNAME: str = "admin"
    AUTH_PASSWORD: Optional[str] = None
    AUTH_SECRET: str = "change-me-in-prod"  # 签发会话 token 的 HMAC 密钥，生产务必改
    AUTH_TOKEN_TTL_SECONDS: int = 86400
    LLM_PROVIDER: str = "mock"
    # M3 rollout switch.  ``legacy`` remains the production-safe default;
    # ``compare`` executes a non-authoritative Core candidate and ``core`` is
    # reserved for explicitly isolated vertical validation until M8.
    SCORING_ENGINE_MODE: Literal["legacy", "compare", "core"] = "legacy"
    LLM_FALLBACK_TO_MOCK: bool = True
    LLM_CACHE_ENABLED: bool = True  # L0 缓存/账本（设计§7）：按输入哈希复用 LLM 评分结果
    COHERENCE_SEMANTIC_ENABLED: bool = False  # §8 语义一致性核验（研究问题↔结论等）：每篇额外一次 LLM 调用，故默认 opt-in（设计「LLM 按需」）
    SCORING_DRIFT_BIAS_THRESHOLD: float = 1.0  # L2 漂移检测：|AI分−人工终分| 的人均偏移超过此值即标记（设计§7/§15.2）
    MONITORING_REVIEW_SAMPLE_RATIO: float = 0.2  # 上线抽样复核（§15.2）：每批抽取此比例的论文做人工抽检
    MONITORING_MIN_REVIEW_COVERAGE: float = 0.1  # 漂移监控可信门槛：人工复核覆盖率低于此值则漂移信号暂不可信
    # 警告：调试日志会落入论文原文/评分内容/学生 PII，生产或处理真实学生数据时务必关闭。
    LLM_DEBUG_LOG_ENABLED: bool = True
    LLM_DEBUG_LOG_MAX_CHARS: int = 12000
    LLM_RATE_LIMIT_SLEEP_SECONDS: float = 1.0
    LLM_RETRY_BASE_DELAY_SECONDS: float = 1.0
    LLM_RETRY_MAX_DELAY_SECONDS: float = 30.0
    LLM_429_RETRY_DELAY_SECONDS: float = 12.0
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"
    OPENAI_MODEL: str = "gpt-4.1-mini"
    OPENAI_TIMEOUT_SECONDS: float = 60
    OPENAI_MAX_OUTPUT_TOKENS: int = 1200
    OPENAI_MAX_RETRIES: int = 2
    OPENAI_TEMPERATURE: float = 0.0
    OPENAI_COMPATIBLE_API_KEY: Optional[str] = None
    OPENAI_COMPATIBLE_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
    OPENAI_COMPATIBLE_MODEL: str = "glm-4.7-flash"
    OPENAI_COMPATIBLE_PROVIDER_NAME: str = "zhipu"
    OPENAI_COMPATIBLE_TIMEOUT_SECONDS: float = 60
    OPENAI_COMPATIBLE_MAX_TOKENS: int = 1200
    OPENAI_COMPATIBLE_MAX_RETRIES: int = 2
    OPENAI_COMPATIBLE_RESPONSE_FORMAT_JSON: bool = False
    OPENAI_COMPATIBLE_THINKING_TYPE: Optional[str] = "disabled"
    OPENAI_COMPATIBLE_TEMPERATURE: float = 0.0
    # 本地私有模型：LLM_PROVIDER=local（或 llama/ollama/vllm）即用本块，走 OpenAI 兼容协议连本地端口，零外呼。
    LOCAL_LLM_BASE_URL: str = "http://localhost:8080/v1"  # llama.cpp llama-server 默认端口；Ollama 用 11434
    LOCAL_LLM_MODEL: str = "local-model"  # llama.cpp 忽略请求名用已加载模型；Ollama/vLLM 需填实际模型名
    LOCAL_LLM_API_KEY: Optional[str] = None  # 本地多数无需鉴权，留空即可
    SCORING_CHUNK_EVAL_TOP_K: int = 3
    # llm_direct 整体判分（opt-in）：True=把召回的多块证据一次性喂给模型整体判一次（~1 调用/项，整体上下文）；
    # False(默认)=逐块各判一次再加权汇总（top_k 调用/项）。默认 off 保持既有行为/测试不变；实验用 env 开启。
    SCORING_LLM_DIRECT_SINGLE_CALL: bool = False
    SCORING_STANDARD_CAP_RATIO: float = 0.8  # 已弃用：被证据门槛取代（见下），保留以兼容旧 .env
    SCORING_EXCEPTIONAL_RATIO: float = 0.92  # 已弃用
    # 证据门槛（取代 0.8 常规封顶）：证据不足时的得分上限、需复核的置信度/满分阈值。
    SCORING_INSUFFICIENT_EVIDENCE_CAP_RATIO: float = 0.6
    SCORING_CONFIDENCE_REVIEW_THRESHOLD: float = 0.72
    SCORING_FULL_SCORE_REVIEW_RATIO: float = 0.95
    SCORING_HIGH_CONFIDENCE: float = 0.85
    SHEET_WRITER_PROVIDER: str = "mock"
    SHEET_FALLBACK_TO_MOCK: bool = True
    GOOGLE_SHEETS_WEBAPP_URL: Optional[str] = None
    GOOGLE_SHEETS_WEBAPP_SECRET: Optional[str] = None
    GOOGLE_SHEETS_TIMEOUT_SECONDS: float = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def uploads_dir(self):
        return self.STORAGE_ROOT / "uploads"

    @property
    def parsed_dir(self):
        return self.STORAGE_ROOT / "parsed"

    @property
    def reports_dir(self):
        return self.STORAGE_ROOT / "reports"

    @property
    def exports_dir(self):
        return self.STORAGE_ROOT / "exports"


settings = Settings()
