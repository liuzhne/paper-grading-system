from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def deployment_security_issues(config):
    """Return stable, non-secret issue codes for protected deployments."""

    if not config.AUTH_ENABLED:
        return ["AUTH_DISABLED"]
    issues = []
    username = (config.AUTH_USERNAME or "").strip().casefold()
    password = (config.AUTH_PASSWORD or "").strip()
    password_folded = password.casefold()
    weak_passwords = {
        "change-me",
        "change-me-in-prod",
        "password",
        "admin",
        "replace-with-strong-login-password",
    }
    if (
        len(password) < 12
        or len(set(password)) < 4
        or password_folded == username
        or password_folded in weak_passwords
        or password_folded.startswith("replace-with-")
    ):
        issues.append("AUTH_PASSWORD_WEAK")
    secret = (config.AUTH_SECRET or "").strip()
    secret_folded = secret.casefold()
    if (
        len(secret) < 32
        or len(set(secret)) < 8
        or secret_folded == "change-me-in-prod"
        or secret_folded.startswith("replace-with-")
    ):
        issues.append("AUTH_SECRET_WEAK")
    if config.LLM_DEBUG_LOG_ENABLED:
        issues.append("LLM_RAW_DEBUG_LOGGING_ENABLED")
    return issues


class Settings(BaseSettings):
    APP_NAME: str = "Paper Grading System"
    API_PREFIX: str = "/api"
    DATABASE_URL: str = Field(
        default="postgresql+psycopg://paper:paper@localhost:5432/paper_grading",
        validation_alias=AliasChoices("DATABASE_URL", "POSTGRES_URL"),
    )
    STORAGE_ROOT: Path = Path("storage")
    STORAGE_PROVIDER: Literal["local", "supabase"] = "local"
    SUPABASE_URL: Optional[str] = None
    SUPABASE_SECRET_KEY: Optional[str] = None
    SUPABASE_STORAGE_BUCKET: str = "paper-grading-private"
    MAX_UPLOAD_SIZE_MB: int = 50
    DIRECT_UPLOAD_MAX_SIZE_MB: int = 50
    DIRECT_UPLOAD_TUS_THRESHOLD_MB: int = 6
    PAPER_PARSE_LEASE_SECONDS: int = 600
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
    AUTH_COOKIE_NAME: str = "pgs_session"
    AUTH_COOKIE_SECURE: bool = True
    AUTH_COOKIE_SAMESITE: Literal["lax", "strict"] = "lax"
    REGISTRATION_MODE: Literal["invite_only"] = "invite_only"
    PASSWORD_RESET_TOKEN_TTL_SECONDS: int = 3600
    DEFAULT_ORGANIZATION_NAME: str = "Default Organization"
    # Deployment-owned key-encryption material.  It is deliberately separate
    # from AUTH_SECRET: rotating cookie signing keys must not make BYOK records
    # unreadable.  Protected deployments must set a managed/KMS-derived value.
    BYOK_MASTER_KEY: Optional[str] = None
    BYOK_KEY_VERSION: int = 1
    AI_CONNECTION_RATE_LIMIT_PER_MINUTE: int = 20
    LLM_PROVIDER: str = "mock"
    # A protected multi-user deployment must not accidentally bill/expose work
    # through the deployment-wide key.  Set this only for a reviewed migration,
    # demo, or explicitly authorized platform-managed model policy.
    PLATFORM_MANAGED_LLM_ENABLED: bool = False
    # M3 rollout switch.  ``legacy`` remains the production-safe default;
    # ``compare`` executes a non-authoritative Core candidate and ``core`` is
    # reserved for explicitly isolated vertical validation until M8.
    SCORING_ENGINE_MODE: Literal["legacy", "compare", "core"] = "legacy"
    # Core provider input rollout.  V3 remains replayable and is the explicit
    # emergency rollback; new production calls use rule-scoped V4 evidence.
    SCORING_PROMPT_ENVELOPE_VERSION: Literal["v3", "v4"] = "v4"
    SCORING_EVIDENCE_SELECTION_MODE: Literal["all", "scoped"] = "scoped"
    SCORING_EVIDENCE_TOP_K: int = Field(default=12, ge=1, le=100)
    SCORING_CONTEXT_WINDOW_TOKENS: int = Field(default=32768, ge=1024)
    SCORING_CONTEXT_SAFETY_MARGIN_TOKENS: int = Field(default=1024, ge=0)
    SCORING_RULE_TASKS_ENABLED: bool = True
    MANUAL_REVIEW_QUEUE_ENABLED: bool = True
    PROVIDER_CIRCUIT_BREAKER_ENABLED: bool = True
    PROVIDER_GLOBAL_CONCURRENCY: int = Field(default=4, ge=1, le=32)
    PROVIDER_CIRCUIT_FAILURE_THRESHOLD: int = Field(default=5, ge=1, le=100)
    PROVIDER_CIRCUIT_COOLDOWN_SECONDS: int = Field(default=30, ge=1, le=3600)
    LLM_FALLBACK_TO_MOCK: bool = True
    LLM_CACHE_ENABLED: bool = True  # L0 缓存/账本（设计§7）：按输入哈希复用 LLM 评分结果
    COHERENCE_SEMANTIC_ENABLED: bool = False  # §8 语义一致性核验（研究问题↔结论等）：每篇额外一次 LLM 调用，故默认 opt-in（设计「LLM 按需」）
    SCORING_DRIFT_BIAS_THRESHOLD: float = 1.0  # L2 漂移检测：|AI分−人工终分| 的人均偏移超过此值即标记（设计§7/§15.2）
    MONITORING_REVIEW_SAMPLE_RATIO: float = 0.2  # 上线抽样复核（§15.2）：每批抽取此比例的论文做人工抽检
    MONITORING_MIN_REVIEW_COVERAGE: float = 0.1  # 漂移监控可信门槛：人工复核覆盖率低于此值则漂移信号暂不可信
    # 警告：调试日志会落入论文原文/评分内容/学生 PII，生产或处理真实学生数据时务必关闭。
    # Raw model payloads can contain student work and vendor data.  Local
    # diagnostics must be explicitly opted into; protected deployments reject
    # the setting altogether.
    LLM_DEBUG_LOG_ENABLED: bool = False
    LLM_DEBUG_LOG_MAX_CHARS: int = 12000
    LLM_OBSERVABILITY_ENABLED: bool = False
    LLM_OBSERVABILITY_EXPORTER: Literal["none", "langfuse"] = "none"
    LLM_OBSERVABILITY_CONTENT_MODE: Literal["metadata_only", "redacted"] = (
        "metadata_only"
    )
    LLM_OBSERVABILITY_SUCCESS_SAMPLE_RATE: float = Field(default=1.0, ge=0, le=1)
    LLM_OBSERVABILITY_MAX_CONTENT_CHARS: int = Field(default=2000, ge=256, le=20000)
    LANGFUSE_PUBLIC_KEY: Optional[str] = None
    LANGFUSE_SECRET_KEY: Optional[str] = None
    LANGFUSE_BASE_URL: Optional[str] = None
    LANGFUSE_ENVIRONMENT: str = "development"
    LANGFUSE_RELEASE: Optional[str] = None
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
    OPENAI_COMPATIBLE_SERVICE_TIER: Optional[
        Literal["auto", "on_demand", "flex", "performance"]
    ] = None
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
    # PGS-12 operations policy.  These are safe development defaults only;
    # every production deployment must explicitly review/override them in its
    # environment and record the accepted values in the launch checklist.
    OPS_DISK_FREE_GB_MIN: float = 10.0
    OPS_DATABASE_SIZE_GB_MAX: float = 50.0
    OPS_BATCH_STALE_MINUTES: int = 30
    OPS_LLM_FAILURE_RATE_MAX: float = 0.05
    OPS_RTO_MINUTES: int = 120
    OPS_RPO_MINUTES: int = 1440

    # `.env.local` is an ignored developer override.  Process environment
    # variables still take precedence, so container/platform deployments keep
    # using their injected configuration rather than any checked-out files.
    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore")

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _use_psycopg_driver(cls, value):
        raw = str(value or "")
        if raw.startswith("postgres://"):
            raw = "postgresql+psycopg://" + raw[len("postgres://") :]
        elif raw.startswith("postgresql://"):
            raw = "postgresql+psycopg://" + raw[len("postgresql://") :]
        if raw.startswith("postgresql+psycopg://"):
            parts = urlsplit(raw)
            query = [
                (key, item)
                for key, item in parse_qsl(parts.query, keep_blank_values=True)
                if key.casefold() not in {"supa", "pgbouncer"}
            ]
            raw = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
            )
        return raw

    @model_validator(mode="after")
    def _validate_protected_deployment(self):
        """Fail closed when auth marks this process as a protected deployment."""

        if self.LLM_OBSERVABILITY_ENABLED:
            if self.LLM_OBSERVABILITY_EXPORTER != "langfuse":
                raise ValueError(
                    "LLM observability is enabled without the langfuse exporter"
                )
            missing = [
                name
                for name in (
                    "LANGFUSE_PUBLIC_KEY",
                    "LANGFUSE_SECRET_KEY",
                    "LANGFUSE_BASE_URL",
                )
                if not str(getattr(self, name) or "").strip()
            ]
            if missing:
                raise ValueError(
                    "Langfuse observability configuration missing: %s"
                    % ", ".join(missing)
                )
        if not self.AUTH_ENABLED:
            return self
        issues = deployment_security_issues(self)
        if issues:
            raise ValueError(
                "protected deployment security validation failed: %s"
                % ", ".join(issues)
            )
        return self

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
