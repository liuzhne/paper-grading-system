from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    APP_NAME: str = "Paper Grading System"
    API_PREFIX: str = "/api"
    DATABASE_URL: str = "postgresql+psycopg://paper:paper@localhost:5432/paper_grading"
    STORAGE_ROOT: Path = Path("storage")
    MAX_UPLOAD_SIZE_MB: int = 50
    DEFAULT_DEV_USER_ID: str = "00000000-0000-0000-0000-000000000001"
    DEFAULT_DEV_USERNAME: str = "dev-user"
    LLM_PROVIDER: str = "mock"
    LLM_FALLBACK_TO_MOCK: bool = True
    LLM_CACHE_ENABLED: bool = True  # L0 缓存/账本（设计§7）：按输入哈希复用 LLM 评分结果
    COHERENCE_SEMANTIC_ENABLED: bool = True  # §8 语义一致性核验（研究问题↔结论等），每篇额外一次 LLM 调用
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
    SCORING_CHUNK_EVAL_TOP_K: int = 3
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
