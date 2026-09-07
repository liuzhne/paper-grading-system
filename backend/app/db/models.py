import uuid
from datetime import datetime
from datetime import timezone

from sqlalchemy import Boolean
from sqlalchemy import CheckConstraint
from sqlalchemy import Column
from sqlalchemy import DateTime
from sqlalchemy import ForeignKey
from sqlalchemy import ForeignKeyConstraint
from sqlalchemy import Integer
from sqlalchemy import Index
from sqlalchemy import JSON
from sqlalchemy import Numeric
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import Text
from sqlalchemy import UniqueConstraint
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.ext.mutable import MutableList
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import Session
from sqlalchemy.orm import foreign
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import relationship


def new_id():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def _lower_hex_digest_check(column_name):
    """生成 SQLite/PostgreSQL 都可执行的小写十六进制摘要约束。"""

    stripped = column_name
    for character in "0123456789abcdef":
        stripped = "replace(%s, '%s', '')" % (stripped, character)
    return "length(%s) = 64 AND length(%s) = 0" % (column_name, stripped)


def _json_null_check(column_name):
    return "(%s IS NULL OR CAST(%s AS TEXT) = 'null')" % (
        column_name,
        column_name,
    )


def _json_present_check(column_name):
    return "(%s IS NOT NULL AND CAST(%s AS TEXT) <> 'null')" % (
        column_name,
        column_name,
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="developer")
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=True)
    email: Mapped[str] = mapped_column(String(320), nullable=True, unique=True)
    email_verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    platform_role: Mapped[str] = mapped_column(String(50), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    created_by: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id", name="uq_organization_membership"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="member")
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    token: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)
    organization_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=True, index=True, deferred=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class AIConnection(Base):
    """A private, server-side BYOK connection. Never expose cipher material."""

    __tablename__ = "ai_connections"
    __table_args__ = (
        CheckConstraint("scope = 'private'", name="ck_ai_connections_private_scope"),
        CheckConstraint(
            "provider_type IN ('openai_responses', 'openai_compatible')",
            name="ck_ai_connections_provider_type",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled', 'deleted')",
            name="ck_ai_connections_status",
        ),
        UniqueConstraint("owner_id", "name", name="uq_ai_connections_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    scope: Mapped[str] = mapped_column(String(20), nullable=False, default="private")
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_options: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False, default=dict
    )
    api_key_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_nonce: Mapped[str] = mapped_column(String(128), nullable=False)
    api_key_tag: Mapped[str] = mapped_column(String(128), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    last_error_code: Mapped[str] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )
    disabled_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)


class AIUsageLedger(Base):
    __tablename__ = "ai_usage_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    ai_connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_connections.id"), nullable=False, index=True
    )
    scoring_run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("scoring_runs.id"), nullable=True, index=True
    )
    provider_type: Mapped[str] = mapped_column(String(50), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class Rubric(Base):
    __tablename__ = "rubrics"
    __table_args__ = (
        Index(
            "uq_rubrics_system_name_version",
            "name",
            "version",
            unique=True,
            sqlite_where=sql_text("visibility = 'system'"),
            postgresql_where=sql_text("visibility = 'system'"),
        ),
        Index(
            "uq_rubrics_organization_name_version",
            "organization_id",
            "name",
            "version",
            unique=True,
            sqlite_where=sql_text("visibility = 'organization'"),
            postgresql_where=sql_text("visibility = 'organization'"),
        ),
        Index(
            "uq_rubrics_private_name_version",
            "owner_id",
            "name",
            "version",
            unique=True,
            sqlite_where=sql_text("visibility = 'private'"),
            postgresql_where=sql_text("visibility = 'private'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=True)  # P4.3 预留（单租户暂不强隔离）
    organization_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=True, index=True, deferred=True)
    # Kept deferred so the current model can still load historical schemas in
    # migration replay tests that intentionally stop before 0020.
    visibility: Mapped[str] = mapped_column(String(20), nullable=False, default="private", deferred=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False, default=100)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    description: Mapped[str] = mapped_column(Text, nullable=True)
    # 期望格式规格（设计§2 global_format）：从 Word 模板抽取，作格式检查器的基准。
    format_spec: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
    )
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, deferred=True
    )
    archived_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True, deferred=True
    )

    criteria: Mapped[list["RubricCriterion"]] = relationship(
        back_populates="rubric",
        cascade="all, delete-orphan",
        order_by="RubricCriterion.display_order",
    )
    # 建立 ORM flush 依赖顺序；只填写 created_by ID 且 User 同批新增时，
    # SQLite 外键开启后也必须先 INSERT users，再 INSERT rubrics。
    creator: Mapped["User | None"] = relationship(foreign_keys=[created_by])


class RubricCriterion(Base):
    __tablename__ = "rubric_criteria"
    __table_args__ = (UniqueConstraint("rubric_id", "code", name="uq_rubric_criteria_rubric_code"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    max_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    weight: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    evidence_hints: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    deduction_rules: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    display_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 设计§2 原子项语义。criterion_type: deterministic|llm_judgment|hybrid。
    # scoring_mode: deductive|banded|llm_direct（llm_direct 为过渡态=模型直接给分，目标是迁移到 deductive/banded）。
    criterion_type: Mapped[str] = mapped_column(String(20), nullable=False, default="llm_judgment")
    scoring_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="llm_direct")
    applies_to: Mapped[str] = mapped_column(String(100), nullable=False, default="global")
    rubric_levels: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    sub_checks: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    # 设计§2 维度（内容/结构/格式/创新性/规范性/逻辑…）：findings 按维度绑定到对应评分项。
    dimension: Mapped[str] = mapped_column(String(50), nullable=True)
    # 设计§3.2/§5 编译产物：结构化扣分规则 [{match, points, reason, source}]，供 findings→扣分使用。
    deduction_rules_structured: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    rubric: Mapped["Rubric"] = relationship(back_populates="criteria")
    score_items: Mapped[list["ScoreItem"]] = relationship(back_populates="criterion")
    atomic_rules: Mapped[list["AtomicRule"]] = relationship(back_populates="criterion")


class RubricCompilation(Base):
    """一次评分标准导入/编译的完整、可审计运行记录。"""

    __tablename__ = "rubric_compilations"
    __table_args__ = (
        # SQLite 的复合外键要求被引用列组具有显式唯一约束。
        UniqueConstraint("id", "rubric_id", name="uq_rubric_compilations_id_rubric"),
        UniqueConstraint("id", "final_version_hash", name="uq_rubric_compilations_id_final_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(100), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str] = mapped_column(String(100), nullable=True)
    sampling_params: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_parse_output: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    raw_model_output: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    validation_result: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    blockers: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
        server_default=sql_text("'[]'"),
    )
    warnings: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
        server_default=sql_text("'[]'"),
    )
    human_changes: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
        server_default=sql_text("'[]'"),
    )
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    final_version_hash: Mapped[str] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    rubric: Mapped["Rubric"] = relationship()
    artifacts: Mapped[list["SourceArtifact"]] = relationship(
        back_populates="compilation",
        cascade="all, delete-orphan",
        order_by="SourceArtifact.created_at",
    )


class SourceArtifact(Base):
    """一次编译消费的原始 Excel/DOCX 文件及其不可变元数据。"""

    __tablename__ = "source_artifacts"
    __table_args__ = (CheckConstraint("file_size_bytes >= 0", name="ck_source_artifacts_nonnegative_size"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    compilation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("rubric_compilations.id"),
        nullable=False,
        index=True,
    )
    artifact_type: Mapped[str] = mapped_column(String(50), nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    compilation: Mapped["RubricCompilation"] = relationship(back_populates="artifacts")
    source_rules: Mapped[list["SourceRule"]] = relationship(
        back_populates="artifact",
        cascade="all, delete-orphan",
        order_by="SourceRule.row_number",
    )
    template_items: Mapped[list["TemplateItem"]] = relationship(back_populates="artifact")


class SourceRule(Base):
    """从 Excel 原文提取的规则，保留工作表、行和单元格定位。"""

    __tablename__ = "source_rules"
    __table_args__ = (CheckConstraint("row_number >= 1", name="ck_source_rules_positive_row"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_artifact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("source_artifacts.id"),
        nullable=False,
        index=True,
    )
    source_rule_code: Mapped[str] = mapped_column(String(50), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(200), nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    cell_locator: Mapped[str] = mapped_column(Text, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    artifact: Mapped["SourceArtifact"] = relationship(back_populates="source_rules")
    atomic_rules: Mapped[list["AtomicRule"]] = relationship(
        secondary="atomic_rule_source_rules",
        back_populates="source_rules",
    )


class RubricVersion(Base):
    """由一次编译产生的内容寻址评分标准版本；仅父来源图发布后可执行。"""

    __tablename__ = "rubric_versions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["compilation_id", "rubric_id"],
            ["rubric_compilations.id", "rubric_compilations.rubric_id"],
            name="fk_rubric_versions_compilation_rubric",
        ),
        ForeignKeyConstraint(
            ["compilation_id", "version_hash"],
            ["rubric_compilations.id", "rubric_compilations.final_version_hash"],
            name="fk_rubric_versions_compilation_hash",
            onupdate="CASCADE",
        ),
        # M3 的 batch/run 外键同时约束版本必须属于同一 rubric，并冻结
        # version hash/hash scheme。SQLite 要求复合父键具有显式唯一约束。
        UniqueConstraint("id", "rubric_id", name="uq_rubric_versions_id_rubric"),
        UniqueConstraint(
            "id",
            "rubric_id",
            "version_hash",
            "hash_scheme",
            name="uq_rubric_versions_replay_identity",
        ),
        UniqueConstraint("compilation_id", name="uq_rubric_versions_compilation"),
        UniqueConstraint("rubric_id", "version", name="uq_rubric_versions_rubric_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_id: Mapped[str] = mapped_column(String(36), nullable=False)
    organization_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=True, index=True, deferred=True
    )
    compilation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    workflow_profile: Mapped[str] = mapped_column(String(100), nullable=False)
    global_policy: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Python defaults only preserve legacy callers that construct the ORM model
    # directly.  The migration deliberately installs no server defaults: every
    # future database write must state its profile/hash scheme explicitly.
    business_profile_key: Mapped[str] = mapped_column(
        String(100), nullable=False, default="thesis"
    )
    hash_scheme: Mapped[str] = mapped_column(
        String(100), nullable=False, default="rubric-content-v1"
    )
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    # 两个复合数据库外键共同冻结 compilation identity；此窄关系只负责
    # ORM UnitOfWork 的 INSERT 顺序，完整一致性仍由数据库复合外键裁决。
    compilation: Mapped["RubricCompilation"] = relationship(
        primaryjoin=foreign(compilation_id) == RubricCompilation.id,
        foreign_keys=[compilation_id],
    )
    atomic_rules: Mapped[list["AtomicRule"]] = relationship(back_populates="rubric_version")


atomic_rule_source_rules = Table(
    "atomic_rule_source_rules",
    Base.metadata,
    Column(
        "atomic_rule_id",
        String(36),
        ForeignKey("atomic_rules.id"),
        primary_key=True,
        nullable=False,
    ),
    Column(
        "source_rule_id",
        String(36),
        ForeignKey("source_rules.id"),
        primary_key=True,
        nullable=False,
    ),
)


class TemplateItem(Base):
    """从 Word 模板正文、结构或批注抽取的可定位条目。"""

    __tablename__ = "template_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('section', 'content', 'comment', 'structure', 'format')",
            name="ck_template_items_kind",
        ),
        CheckConstraint(
            "strictness IN ('required', 'preferred', 'unknown')",
            name="ck_template_items_strictness",
        ),
        CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name="ck_template_items_parse_confidence",
        ),
        UniqueConstraint(
            "source_artifact_id",
            "item_code",
            name="uq_template_items_artifact_code",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_artifact_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("source_artifacts.id"),
        nullable=False,
        index=True,
    )
    item_code: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    section_path: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
        server_default=sql_text("'[]'"),
    )
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_constraint: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=True,
    )
    strictness: Mapped[str] = mapped_column(String(50), nullable=False)
    source_locator: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    artifact: Mapped["SourceArtifact"] = relationship(back_populates="template_items")
    rule_links: Mapped[list["RuleTemplateLink"]] = relationship(
        back_populates="template_item",
    )


class AtomicRule(Base):
    """由来源规则编译出的最小可判断、可审计规则。"""

    __tablename__ = "atomic_rules"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('band', 'deduct', 'bonus', 'none')",
            name="ck_atomic_rules_direction",
        ),
        CheckConstraint(
            "effect_type IN ('score', 'review', 'block_submission', 'report_only')",
            name="ck_atomic_rules_effect_type",
        ),
        CheckConstraint(
            "repeat_policy IS NULL OR repeat_policy IN ('once', 'per_occurrence', 'capped')",
            name="ck_atomic_rules_repeat_policy",
        ),
        CheckConstraint(
            "judge_type IN ('deterministic', 'semantic')",
            name="ck_atomic_rules_judge_type",
        ),
        CheckConstraint(
            "strictness IN ('required', 'preferred', 'unknown')",
            name="ck_atomic_rules_strictness",
        ),
        CheckConstraint(
            "max_points IS NULL OR max_points >= 0",
            name="ck_atomic_rules_nonnegative_max_points",
        ),
        CheckConstraint(
            "cap_points IS NULL OR cap_points >= 0",
            name="ck_atomic_rules_nonnegative_cap_points",
        ),
        UniqueConstraint(
            "rubric_version_id",
            "rule_code",
            name="uq_atomic_rules_version_code",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_version_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("rubric_versions.id"),
        nullable=False,
        index=True,
    )
    criterion_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("rubric_criteria.id"),
        nullable=False,
        index=True,
    )
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rule_text: Mapped[str] = mapped_column(Text, nullable=False)
    direction: Mapped[str] = mapped_column(String(50), nullable=False)
    effect_type: Mapped[str] = mapped_column(String(50), nullable=False)
    max_points: Mapped[float] = mapped_column(Numeric(8, 2), nullable=True)
    repeat_policy: Mapped[str] = mapped_column(String(50), nullable=True)
    cap_points: Mapped[float] = mapped_column(Numeric(8, 2), nullable=True)
    judge_type: Mapped[str] = mapped_column(String(50), nullable=False)
    checker_key: Mapped[str] = mapped_column(String(200), nullable=True)
    checker_params: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    evidence_policy: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    positive_example: Mapped[str] = mapped_column(Text, nullable=True)
    negative_example: Mapped[str] = mapped_column(Text, nullable=True)
    boundary_example: Mapped[str] = mapped_column(Text, nullable=True)
    strictness: Mapped[str] = mapped_column(String(50), nullable=False)
    applies_to: Mapped[str] = mapped_column(Text, nullable=False)
    mutex_group: Mapped[str] = mapped_column(String(100), nullable=True)
    depends_on_rule_codes: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON),
        nullable=False,
        default=list,
        server_default=sql_text("'[]'"),
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    creation_method: Mapped[str] = mapped_column(String(50), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    rubric_version: Mapped["RubricVersion"] = relationship(back_populates="atomic_rules")
    criterion: Mapped["RubricCriterion"] = relationship(back_populates="atomic_rules")
    source_rules: Mapped[list["SourceRule"]] = relationship(
        secondary=atomic_rule_source_rules,
        back_populates="atomic_rules",
    )
    levels: Mapped[list["RuleLevel"]] = relationship(
        back_populates="atomic_rule",
        cascade="all, delete-orphan",
        order_by="RuleLevel.display_order",
    )
    template_links: Mapped[list["RuleTemplateLink"]] = relationship(
        back_populates="rule",
        cascade="all, delete-orphan",
    )


class RuleLevel(Base):
    """band 规则的一个可解释分档。"""

    __tablename__ = "rule_levels"
    __table_args__ = (
        CheckConstraint("points >= 0", name="ck_rule_levels_nonnegative_points"),
        CheckConstraint(
            "display_order >= 0",
            name="ck_rule_levels_nonnegative_display_order",
        ),
        UniqueConstraint(
            "atomic_rule_id",
            "level_code",
            name="uq_rule_levels_rule_code",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    atomic_rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("atomic_rules.id"),
        nullable=False,
        index=True,
    )
    level_code: Mapped[str] = mapped_column(String(100), nullable=False)
    points: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False)
    descriptor: Mapped[str] = mapped_column(Text, nullable=False)
    positive_example: Mapped[str] = mapped_column(Text, nullable=True)
    negative_example: Mapped[str] = mapped_column(Text, nullable=True)
    display_order: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    atomic_rule: Mapped["AtomicRule"] = relationship(back_populates="levels")


class RuleTemplateLink(Base):
    """AtomicRule 与模板条目之间经审核的来源映射。"""

    __tablename__ = "rule_template_links"
    __table_args__ = (
        CheckConstraint(
            "relationship_type IN ('support', 'constraint', 'format_baseline', 'exception')",
            name="ck_rule_template_links_relationship_type",
        ),
        CheckConstraint(
            "match_method IN ('exact', 'heuristic', 'llm_suggestion', 'manual')",
            name="ck_rule_template_links_match_method",
        ),
        CheckConstraint(
            "review_status IN ('pending', 'confirmed', 'rejected')",
            name="ck_rule_template_links_review_status",
        ),
        CheckConstraint(
            "match_confidence IS NULL OR "
            "(match_confidence >= 0 AND match_confidence <= 1)",
            name="ck_rule_template_links_match_confidence",
        ),
        UniqueConstraint(
            "rule_id",
            "template_item_id",
            name="uq_rule_template_links_rule_item",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("atomic_rules.id"),
        nullable=False,
        index=True,
    )
    template_item_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("template_items.id"),
        nullable=False,
        index=True,
    )
    relationship_type: Mapped[str] = mapped_column(String(50), nullable=False)
    match_method: Mapped[str] = mapped_column(String(50), nullable=False)
    match_confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=True)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    review_status: Mapped[str] = mapped_column(String(50), nullable=False)
    reviewed_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    rule: Mapped["AtomicRule"] = relationship(back_populates="template_links")
    template_item: Mapped["TemplateItem"] = relationship(back_populates="rule_links")


def _session_entity(session, model, entity_id):
    """优先从同一事务的 pending 对象解析，再退回 identity map/数据库。"""
    if entity_id is None:
        return None
    for candidate in session.new:
        if isinstance(candidate, model) and candidate.id == entity_id:
            return candidate
    return session.get(model, entity_id)


def _related_or_get(session, instance, relationship_name, model, foreign_key):
    related = instance.__dict__.get(relationship_name)
    if related is not None:
        relationship_history = sa_inspect(instance).attrs[relationship_name].history
        if related.id == foreign_key or related in relationship_history.added:
            return related
    return _session_entity(session, model, foreign_key)


def _rule_version(session, rule):
    return _related_or_get(
        session,
        rule,
        "rubric_version",
        RubricVersion,
        rule.rubric_version_id,
    )


def _source_rule_artifact(session, source_rule):
    return _related_or_get(
        session,
        source_rule,
        "artifact",
        SourceArtifact,
        source_rule.source_artifact_id,
    )


def _template_item_artifact(session, template_item):
    return _related_or_get(
        session,
        template_item,
        "artifact",
        SourceArtifact,
        template_item.source_artifact_id,
    )


def _validate_rule_criterion_lineage(session, rule, version=None, criterion=None):
    version = version or _rule_version(session, rule)
    criterion = criterion or _related_or_get(
        session,
        rule,
        "criterion",
        RubricCriterion,
        rule.criterion_id,
    )
    if version is not None and criterion is not None and version.rubric_id != criterion.rubric_id:
        raise ValueError("AtomicRule 的 criterion 必须属于 RubricVersion 对应的 Rubric")


def _validate_source_rule_lineage(session, rule, source_rule, version=None, artifact=None):
    version = version or _rule_version(session, rule)
    artifact = artifact or _source_rule_artifact(session, source_rule)
    if version is not None and artifact is not None and artifact.compilation_id != version.compilation_id:
        raise ValueError("AtomicRule 不能关联其它 compilation 的 SourceRule")


def _validate_template_link_lineage(session, link, rule=None, version=None, artifact=None):
    rule = rule or _related_or_get(
        session,
        link,
        "rule",
        AtomicRule,
        link.rule_id,
    )
    template_item = _related_or_get(
        session,
        link,
        "template_item",
        TemplateItem,
        link.template_item_id,
    )
    if rule is None or template_item is None:
        return
    version = version or _rule_version(session, rule)
    artifact = artifact or _template_item_artifact(session, template_item)
    if version is not None and artifact is not None and version.compilation_id != artifact.compilation_id:
        raise ValueError("RuleTemplateLink 两端必须来自同一个 compilation")


@event.listens_for(Session, "before_flush")
def _validate_atomic_rule_lineage(session, _flush_context, _instances):
    """防止规则、criterion 和来源在不同 Rubric/编译版本间被静默串接。"""
    candidates = tuple(session.new) + tuple(session.dirty)
    seen = set()
    for candidate in candidates:
        object_key = id(candidate)
        if object_key in seen or candidate in session.deleted:
            continue
        seen.add(object_key)

        if isinstance(candidate, RubricCriterion):
            state = sa_inspect(candidate)
            rubric_changed = (
                candidate in session.new
                or state.attrs.rubric_id.history.has_changes()
                or state.attrs.rubric.history.has_changes()
            )
            if rubric_changed:
                for rule in candidate.atomic_rules:
                    if rule not in session.deleted:
                        _validate_rule_criterion_lineage(
                            session,
                            rule,
                            criterion=candidate,
                        )

        elif isinstance(candidate, RubricVersion):
            state = sa_inspect(candidate)
            version_lineage_changed = (
                candidate in session.new
                or state.attrs.rubric_id.history.has_changes()
                or state.attrs.compilation_id.history.has_changes()
            )
            if version_lineage_changed:
                for rule in candidate.atomic_rules:
                    if rule in session.deleted:
                        continue
                    _validate_rule_criterion_lineage(
                        session,
                        rule,
                        version=candidate,
                    )
                    for source_rule in rule.source_rules:
                        _validate_source_rule_lineage(
                            session,
                            rule,
                            source_rule,
                            version=candidate,
                        )
                    for link in rule.template_links:
                        if link not in session.deleted:
                            _validate_template_link_lineage(
                                session,
                                link,
                                rule=rule,
                                version=candidate,
                            )

        elif isinstance(candidate, SourceArtifact):
            state = sa_inspect(candidate)
            compilation_changed = (
                candidate in session.new
                or state.attrs.compilation_id.history.has_changes()
                or state.attrs.compilation.history.has_changes()
            )
            if compilation_changed:
                for source_rule in candidate.source_rules:
                    if source_rule in session.deleted:
                        continue
                    for rule in source_rule.atomic_rules:
                        if rule not in session.deleted:
                            _validate_source_rule_lineage(
                                session,
                                rule,
                                source_rule,
                                artifact=candidate,
                            )
                for template_item in candidate.template_items:
                    if template_item in session.deleted:
                        continue
                    for link in template_item.rule_links:
                        if link not in session.deleted:
                            _validate_template_link_lineage(
                                session,
                                link,
                                artifact=candidate,
                            )

        elif isinstance(candidate, AtomicRule):
            state = sa_inspect(candidate)
            version = _rule_version(session, candidate)
            _validate_rule_criterion_lineage(session, candidate, version=version)

            if version is not None:
                version_changed = (
                    candidate in session.new
                    or state.attrs.rubric_version_id.history.has_changes()
                    or state.attrs.rubric_version.history.has_changes()
                )
                source_rules = (
                    candidate.source_rules
                    if version_changed
                    else state.attrs.source_rules.history.added
                )
                for source_rule in source_rules:
                    _validate_source_rule_lineage(session, candidate, source_rule, version)
                if version_changed:
                    for link in candidate.template_links:
                        if link not in session.deleted:
                            _validate_template_link_lineage(
                                session,
                                link,
                                rule=candidate,
                                version=version,
                            )

        elif isinstance(candidate, SourceRule):
            state = sa_inspect(candidate)
            artifact_changed = (
                candidate in session.new
                or state.attrs.source_artifact_id.history.has_changes()
                or state.attrs.artifact.history.has_changes()
            )
            if artifact_changed:
                for rule in candidate.atomic_rules:
                    if rule not in session.deleted:
                        _validate_source_rule_lineage(session, rule, candidate)

        elif isinstance(candidate, TemplateItem):
            state = sa_inspect(candidate)
            artifact_changed = (
                candidate in session.new
                or state.attrs.source_artifact_id.history.has_changes()
                or state.attrs.artifact.history.has_changes()
            )
            if artifact_changed:
                for link in candidate.rule_links:
                    if link not in session.deleted:
                        _validate_template_link_lineage(session, link)

        elif isinstance(candidate, RuleTemplateLink):
            _validate_template_link_lineage(session, candidate)


P103_LIFECYCLE_OPERATION_KEY = "p103_lifecycle_operation"
_P103_TRANSITIONS = {
    "submit_for_review": ("draft", "review"),
    "return_to_draft": ("review", "draft"),
    "publish": ("review", "published"),
}
_P103_STATUSES = frozenset({"draft", "review", "published"})
_P103_CONTENT_TYPES = (
    Rubric,
    RubricCriterion,
    RubricCompilation,
    SourceArtifact,
    SourceRule,
    RubricVersion,
    TemplateItem,
    AtomicRule,
    RuleLevel,
    RuleTemplateLink,
)
_P103_CONTENT_RELATIONSHIPS = {
    Rubric: frozenset({"criteria"}),
    RubricCriterion: frozenset({"rubric", "atomic_rules"}),
    RubricCompilation: frozenset({"rubric", "artifacts"}),
    SourceArtifact: frozenset({"compilation", "source_rules", "template_items"}),
    SourceRule: frozenset({"artifact", "atomic_rules"}),
    RubricVersion: frozenset({"atomic_rules"}),
    TemplateItem: frozenset({"artifact", "rule_links"}),
    AtomicRule: frozenset(
        {"rubric_version", "criterion", "source_rules", "levels", "template_links"}
    ),
    RuleLevel: frozenset({"atomic_rule"}),
    RuleTemplateLink: frozenset({"rule", "template_item"}),
}
_P103_OPERATION_COLUMNS = {
    "submit_for_review": {Rubric: frozenset({"status"})},
    "return_to_draft": {Rubric: frozenset({"status"})},
    "publish": {
        Rubric: frozenset({"status", "published_at", "published_by"}),
        RubricCompilation: frozenset(
            {"reviewed_by", "reviewed_at", "published_at", "final_version_hash"}
        ),
        RubricVersion: frozenset({"version_hash"}),
    },
}


def _p103_changed_columns(instance):
    state = sa_inspect(instance)
    return {
        attribute.key
        for attribute in state.mapper.column_attrs
        if state.attrs[attribute.key].history.has_changes()
    }


def _p103_changed_content_relationships(instance):
    state = sa_inspect(instance)
    protected = _P103_CONTENT_RELATIONSHIPS.get(type(instance), ())
    return {
        relationship_name
        for relationship_name in protected
        if state.attrs[relationship_name].history.has_changes()
    }


def _p103_has_content_change(session, instance):
    if instance in session.new or instance in session.deleted:
        return True
    return bool(
        _p103_changed_columns(instance)
        or _p103_changed_content_relationships(instance)
    )


def _p103_current_rubric_id(session, instance, seen=None):
    seen = seen or set()
    marker = id(instance)
    if marker in seen:
        return None
    seen.add(marker)

    if isinstance(instance, Rubric):
        return instance.id
    if isinstance(instance, RubricCriterion):
        parent = _related_or_get(
            session,
            instance,
            "rubric",
            Rubric,
            instance.rubric_id,
        )
    elif isinstance(instance, RubricCompilation):
        parent = _related_or_get(
            session,
            instance,
            "rubric",
            Rubric,
            instance.rubric_id,
        )
    elif isinstance(instance, RubricVersion):
        return instance.rubric_id
    elif isinstance(instance, SourceArtifact):
        parent = _related_or_get(
            session,
            instance,
            "compilation",
            RubricCompilation,
            instance.compilation_id,
        )
    elif isinstance(instance, (SourceRule, TemplateItem)):
        parent = _related_or_get(
            session,
            instance,
            "artifact",
            SourceArtifact,
            instance.source_artifact_id,
        )
    elif isinstance(instance, AtomicRule):
        parent = _related_or_get(
            session,
            instance,
            "rubric_version",
            RubricVersion,
            instance.rubric_version_id,
        )
    elif isinstance(instance, RuleLevel):
        parent = _related_or_get(
            session,
            instance,
            "atomic_rule",
            AtomicRule,
            instance.atomic_rule_id,
        )
    elif isinstance(instance, RuleTemplateLink):
        parent = _related_or_get(
            session,
            instance,
            "rule",
            AtomicRule,
            instance.rule_id,
        )
    else:
        return None
    return _p103_current_rubric_id(session, parent, seen) if parent is not None else None


def _p103_database_rubric_id(session, instance):
    entity_id = getattr(instance, "id", None)
    if entity_id is None:
        return None

    rubrics = Rubric.__table__
    criteria = RubricCriterion.__table__
    compilations = RubricCompilation.__table__
    artifacts = SourceArtifact.__table__
    source_rules = SourceRule.__table__
    versions = RubricVersion.__table__
    templates = TemplateItem.__table__
    rules = AtomicRule.__table__
    levels = RuleLevel.__table__
    links = RuleTemplateLink.__table__

    if isinstance(instance, Rubric):
        statement = select(rubrics.c.id).where(rubrics.c.id == entity_id)
    elif isinstance(instance, RubricCriterion):
        statement = select(criteria.c.rubric_id).where(criteria.c.id == entity_id)
    elif isinstance(instance, RubricCompilation):
        statement = select(compilations.c.rubric_id).where(compilations.c.id == entity_id)
    elif isinstance(instance, SourceArtifact):
        statement = (
            select(compilations.c.rubric_id)
            .select_from(artifacts.join(compilations))
            .where(artifacts.c.id == entity_id)
        )
    elif isinstance(instance, SourceRule):
        statement = (
            select(compilations.c.rubric_id)
            .select_from(source_rules.join(artifacts).join(compilations))
            .where(source_rules.c.id == entity_id)
        )
    elif isinstance(instance, RubricVersion):
        statement = select(versions.c.rubric_id).where(versions.c.id == entity_id)
    elif isinstance(instance, TemplateItem):
        statement = (
            select(compilations.c.rubric_id)
            .select_from(templates.join(artifacts).join(compilations))
            .where(templates.c.id == entity_id)
        )
    elif isinstance(instance, AtomicRule):
        statement = (
            select(versions.c.rubric_id)
            .select_from(rules.join(versions))
            .where(rules.c.id == entity_id)
        )
    elif isinstance(instance, RuleLevel):
        statement = (
            select(versions.c.rubric_id)
            .select_from(levels.join(rules).join(versions))
            .where(levels.c.id == entity_id)
        )
    elif isinstance(instance, RuleTemplateLink):
        statement = (
            select(versions.c.rubric_id)
            .select_from(links.join(rules).join(versions))
            .where(links.c.id == entity_id)
        )
    else:
        return None
    return session.connection().execute(statement).scalar_one_or_none()


def _p103_rubric_ids(session, instance):
    return {
        rubric_id
        for rubric_id in (
            _p103_current_rubric_id(session, instance),
            _p103_database_rubric_id(session, instance),
        )
        if rubric_id is not None
    }


def _p103_has_provenance(session, rubric_id, rubric=None):
    for compilation in session.new:
        if not isinstance(compilation, RubricCompilation) or compilation in session.deleted:
            continue
        related_rubric = compilation.__dict__.get("rubric")
        if (rubric is not None and related_rubric is rubric) or (
            rubric_id is not None and _p103_current_rubric_id(session, compilation) == rubric_id
        ):
            return True
    if rubric_id is None:
        return False
    statement = select(RubricCompilation.id).where(
        RubricCompilation.rubric_id == rubric_id
    ).limit(1)
    return session.connection().execute(statement).scalar_one_or_none() is not None


def _p103_database_rubric_status(session, rubric_id):
    if rubric_id is None:
        return None
    statement = select(Rubric.status).where(Rubric.id == rubric_id)
    return session.connection().execute(statement).scalar_one_or_none()


def _p103_validate_operation_change(session, instance, operation):
    if instance in session.new or instance in session.deleted:
        raise ValueError("评分标准状态转换不能夹带版本图结构变更")
    changed_relationships = _p103_changed_content_relationships(instance)
    changed_columns = _p103_changed_columns(instance)
    allowed_columns = _P103_OPERATION_COLUMNS[operation].get(type(instance))
    if changed_relationships or allowed_columns is None or not changed_columns <= allowed_columns:
        raise ValueError("评分标准状态转换不能夹带版本内容修改")


def _p103_validate_publish_signoff(session, rubric, candidates):
    signed_compilations = []
    signoff_fields = _P103_OPERATION_COLUMNS["publish"][RubricCompilation]
    for candidate in candidates:
        if not isinstance(candidate, RubricCompilation):
            continue
        if rubric.id not in _p103_rubric_ids(session, candidate):
            continue
        if _p103_changed_columns(candidate) & signoff_fields:
            signed_compilations.append(candidate)
    if len(signed_compilations) != 1:
        raise ValueError("发布必须且只能原子签核一次 compilation")

    compilation = signed_compilations[0]
    published_at = rubric.published_at
    if (
        compilation.status != "validated"
        or compilation.reviewed_by is None
        or compilation.reviewed_at is None
        or compilation.published_at is None
        or not compilation.final_version_hash
        or published_at is None
        or compilation.reviewed_at != published_at
        or compilation.published_at != published_at
        or compilation.reviewed_at < compilation.created_at
        or _session_entity(session, User, compilation.reviewed_by) is None
    ):
        raise ValueError("发布签核字段不完整或不一致")

    version_ids = session.connection().execute(
        select(RubricVersion.id).where(
            RubricVersion.compilation_id == compilation.id
        )
    ).scalars().all()
    versions = [
        _session_entity(session, RubricVersion, version_id)
        for version_id in version_ids
    ]
    versions.extend(
        version
        for version in session.new
        if isinstance(version, RubricVersion)
        and version.compilation_id == compilation.id
        and version not in versions
    )
    if len(versions) != 1:
        raise ValueError("发布的 compilation 必须对应且只对应一个版本")
    version = versions[0]
    if (
        version is None
        or version.rubric_id != rubric.id
        or version.version_hash != compilation.final_version_hash
    ):
        raise ValueError("发布版本的来源或内容哈希不一致")


def _p103_is_lower_hex_digest(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _p103_validate_atomic_published_import(session, rubric, candidates):
    """校验一次 flush 中导入的、已经签核且被批次锁定的冻结版本图。

    这是创建期的窄例外，不使用可遗留在 ``session.info`` 的 bypass 标志：
    rubric、compilation、version 和 pinned batch 必须同时是 pending INSERT，
    且完整身份互相匹配。对象一旦持久化，后续任何内容或状态修改仍走原有
    published immutable guard。
    """

    if rubric not in session.new or rubric.status != "published":
        raise ValueError("带 provenance 的新评分标准必须从 draft 开始")
    if (
        rubric.published_at is None
        or not rubric.created_by
        or _session_entity(session, User, rubric.created_by) is None
    ):
        raise ValueError("原子导入的已发布评分标准缺少创建者或发布时间")

    compilations = [
        candidate
        for candidate in candidates
        if isinstance(candidate, RubricCompilation)
        and candidate in session.new
        and candidate not in session.deleted
        and _p103_current_rubric_id(session, candidate) == rubric.id
    ]
    if len(compilations) != 1:
        raise ValueError("原子导入必须且只能包含一个新 compilation")
    compilation = compilations[0]
    if (
        compilation.rubric_id != rubric.id
        or compilation.status != "validated"
        or not compilation.created_by
        or _session_entity(session, User, compilation.created_by) is None
        or not compilation.reviewed_by
        or _session_entity(session, User, compilation.reviewed_by) is None
        or compilation.reviewed_at is None
        or compilation.published_at is None
        or compilation.reviewed_at != rubric.published_at
        or compilation.published_at != rubric.published_at
        or not _p103_is_lower_hex_digest(compilation.final_version_hash)
    ):
        raise ValueError("原子导入的 compilation 签核字段不完整或不一致")

    versions = [
        candidate
        for candidate in candidates
        if isinstance(candidate, RubricVersion)
        and candidate in session.new
        and candidate not in session.deleted
        and candidate.compilation_id == compilation.id
    ]
    if len(versions) != 1:
        raise ValueError("原子导入的 compilation 必须对应且只对应一个新版本")
    version = versions[0]
    profile = version.business_profile_key
    scheme = version.hash_scheme
    if (
        version.rubric_id != rubric.id
        or version.compilation_id != compilation.id
        or version.version_hash != compilation.final_version_hash
        or not isinstance(version.workflow_profile, str)
        or not version.workflow_profile.strip()
        or not isinstance(profile, str)
        or not profile.strip()
        or scheme not in {"rubric-content-v1", "rubric-content-v2"}
        or (scheme == "rubric-content-v1" and profile != "thesis")
        or not version.created_by
        or _session_entity(session, User, version.created_by) is None
    ):
        raise ValueError("原子导入的版本来源、profile 或内容哈希不一致")

    pinned_batches = [
        candidate
        for candidate in candidates
        if isinstance(candidate, GradingBatch)
        and candidate in session.new
        and candidate not in session.deleted
        and (
            candidate.rubric_id == rubric.id
            or candidate.rubric_version_id == version.id
        )
    ]
    if not pinned_batches or any(
        batch.rubric_id != rubric.id
        or batch.rubric_version_id != version.id
        for batch in pinned_batches
    ):
        raise ValueError("原子导入必须包含与 rubric/version 完整匹配的 pinned batch")

    graph_candidates = [
        candidate
        for candidate in candidates
        if isinstance(candidate, _P103_CONTENT_TYPES)
        and rubric.id in _p103_rubric_ids(session, candidate)
    ]
    if any(
        candidate not in session.new or candidate in session.deleted
        for candidate in graph_candidates
    ):
        raise ValueError("原子导入不能混入已持久化或待删除的版本图对象")


@event.listens_for(Session, "before_flush")
def _protect_published_rubric_graph(session, _flush_context, _instances):
    """P1-03：严格保护带 provenance 的评分标准；legacy 评分标准保持兼容。"""
    candidates = tuple(dict.fromkeys((*session.new, *session.dirty, *session.deleted)))
    operation = session.info.get(P103_LIFECYCLE_OPERATION_KEY)
    transitions = {}

    for candidate in candidates:
        if not isinstance(candidate, Rubric):
            continue
        if not _p103_has_provenance(session, candidate.id, candidate):
            continue
        if candidate.status not in _P103_STATUSES:
            raise ValueError("评分标准状态必须是 draft、review 或 published")
        if candidate in session.new:
            if candidate.status == "published":
                _p103_validate_atomic_published_import(
                    session,
                    candidate,
                    candidates,
                )
            elif candidate.status != "draft":
                raise ValueError("带 provenance 的新评分标准必须从 draft 开始")
            continue
        previous_status = _p103_database_rubric_status(session, candidate.id)
        if previous_status == candidate.status:
            continue
        expected_transition = _P103_TRANSITIONS.get(operation)
        actual_transition = (previous_status, candidate.status)
        if actual_transition != expected_transition:
            raise ValueError(
                f"非法评分标准状态转换：{previous_status} -> {candidate.status}"
            )
        transitions[candidate.id] = candidate

    for candidate in candidates:
        if not isinstance(candidate, _P103_CONTENT_TYPES):
            continue
        if not _p103_has_content_change(session, candidate):
            continue
        rubric_ids = _p103_rubric_ids(session, candidate)

        transitioning_ids = rubric_ids.intersection(transitions)
        if transitioning_ids:
            _p103_validate_operation_change(session, candidate, operation)

        for rubric_id in rubric_ids:
            if not _p103_has_provenance(session, rubric_id):
                continue
            if _p103_database_rubric_status(session, rubric_id) == "published":
                raise ValueError("已发布评分标准不可修改；请先深克隆为新草稿")

    if operation == "publish":
        for rubric in transitions.values():
            _p103_validate_publish_signoff(session, rubric, candidates)


class EvaluationBatch(Base):
    """Profile-neutral batch pinned to one immutable published rubric."""

    __tablename__ = "evaluation_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rubric_version_id", "rubric_id"],
            ["rubric_versions.id", "rubric_versions.rubric_id"],
            name="fk_evaluation_batches_rubric_version_rubric",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'archived')",
            name="ck_evaluation_batches_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id"),
        nullable=True,
        index=True,
        deferred=True,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rubric_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("rubrics.id"), nullable=False
    )
    rubric_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    ai_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ai_connections.id"), nullable=True, deferred=True
    )
    ai_connection_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True, deferred=True)
    ai_connection_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, deferred=True
    )
    business_profile_key: Mapped[str] = mapped_column(String(100), nullable=False)
    business_profile_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="draft"
    )
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    rubric: Mapped["Rubric"] = relationship()
    rubric_version: Mapped["RubricVersion"] = relationship(
        primaryjoin=foreign(rubric_version_id) == RubricVersion.id,
        foreign_keys=[rubric_version_id],
    )
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="evaluation_batch",
        passive_deletes=True,
    )


class Submission(Base):
    """Generic source artifact owned by an EvaluationBatch."""

    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(
            _lower_hex_digest_check("source_artifact_hash"),
            name="ck_submissions_source_artifact_hash",
        ),
        CheckConstraint(
            "byte_length >= 0",
            name="ck_submissions_nonnegative_byte_length",
        ),
        CheckConstraint(
            "status IN ('uploaded', 'parsing', 'parsed', 'failed', "
            "'scored', 'pending_review', 'reviewed')",
            name="ck_submissions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id"),
        nullable=True,
        index=True,
        deferred=True,
    )
    evaluation_batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("evaluation_batches.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    source_artifact_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_artifact_ref: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(200), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    submission_metadata: Mapped[dict] = mapped_column(
        "metadata",
        MutableDict.as_mutable(JSON),
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'"),
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="uploaded"
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    evaluation_batch: Mapped["EvaluationBatch"] = relationship(
        back_populates="submissions"
    )
    document_snapshots: Mapped[list["DocumentSnapshot"]] = relationship(
        back_populates="submission",
        passive_deletes=True,
    )
    scoring_runs: Mapped[list["ScoringRun"]] = relationship(
        back_populates="submission",
        foreign_keys="ScoringRun.submission_id",
    )


class DocumentSnapshot(Base):
    """Immutable, content-addressed generic document projection."""

    __tablename__ = "document_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "id",
            "submission_id",
            name="uq_document_snapshots_id_submission",
        ),
        UniqueConstraint(
            "submission_id",
            "snapshot_hash",
            name="uq_document_snapshots_submission_hash",
        ),
        CheckConstraint(
            _lower_hex_digest_check("content_hash"),
            name="ck_document_snapshots_content_hash",
        ),
        CheckConstraint(
            _lower_hex_digest_check("snapshot_hash"),
            name="ck_document_snapshots_snapshot_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    submission_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(100), nullable=False)
    business_profile_key: Mapped[str] = mapped_column(String(100), nullable=False)
    business_profile_version: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    parser_version: Mapped[str] = mapped_column(String(100), nullable=False)
    normalizer_version: Mapped[str] = mapped_column(String(100), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_ref: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_payload: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )

    submission: Mapped["Submission"] = relationship(
        back_populates="document_snapshots",
        foreign_keys=[submission_id],
    )
    scoring_runs: Mapped[list["ScoringRun"]] = relationship(
        back_populates="document_snapshot",
        foreign_keys="ScoringRun.document_snapshot_id",
        overlaps="scoring_runs,submission",
    )


@event.listens_for(Session, "before_flush")
def _protect_document_snapshot_immutability(session, _flush_context, _instances):
    for candidate in tuple(session.dirty) + tuple(session.deleted):
        if (
            isinstance(candidate, DocumentSnapshot)
            and sa_inspect(candidate).persistent
        ):
            raise ValueError("DocumentSnapshot is immutable after insertion")


class GradingBatch(Base):
    __tablename__ = "grading_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rubric_version_id", "rubric_id"],
            ["rubric_versions.id", "rubric_versions.rubric_id"],
            name="fk_grading_batches_rubric_version_rubric",
            ondelete="RESTRICT",
        ),
        # 业务阶段枚举。约束只是最后一道数据校验：合法转移由
        # services/batches/state.py 统一裁决，客户端不能直接写阶段。
        CheckConstraint(
            "status IN ('draft', 'parsing', 'scoring', 'scored', "
            "'scored_with_errors', 'reviewed', 'archived')",
            name="ck_grading_batches_status",
        ),
        CheckConstraint(
            "state_version >= 1", name="ck_grading_batches_state_version_positive"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=True)  # P4.3 预留（单租户暂不强隔离）
    organization_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=True, index=True, deferred=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    major: Mapped[str] = mapped_column(String(100), nullable=True)
    academic_year: Mapped[str] = mapped_column(String(20), nullable=True)
    paper_type: Mapped[str] = mapped_column(String(50), nullable=True)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    rubric_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    ai_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ai_connections.id"), nullable=True, deferred=True
    )
    ai_connection_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True, deferred=True)
    ai_connection_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True, deferred=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="draft")
    # 乐观并发：每次经 state.apply_event 的阶段变更 +1。客户端带上它就能
    # 让过期请求冲突失败，而不是静默覆盖别人的结果。
    state_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    rubric: Mapped["Rubric"] = relationship()
    # 同理，仅用 version id 建立 flush 依赖；rubric 一致性由复合 FK 保证。
    rubric_version: Mapped["RubricVersion | None"] = relationship(
        primaryjoin=foreign(rubric_version_id) == RubricVersion.id,
        foreign_keys=[rubric_version_id],
    )
    papers: Mapped[list["Paper"]] = relationship(back_populates="batch", cascade="all, delete-orphan")
    scoring_jobs: Mapped[list["BatchScoringJob"]] = relationship(
        back_populates="batch",
        cascade="all, delete-orphan",
        order_by="BatchScoringJob.generation",
    )


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=True)  # P4.3 预留（单租户暂不强隔离）
    organization_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("organizations.id"), nullable=True, index=True)
    batch_id: Mapped[str] = mapped_column(String(36), ForeignKey("grading_batches.id"), nullable=False)
    student_id: Mapped[str] = mapped_column(String(100), nullable=True)
    student_name: Mapped[str] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=True)
    department: Mapped[str] = mapped_column(String(100), nullable=True)
    major: Mapped[str] = mapped_column(String(100), nullable=True)
    advisor: Mapped[str] = mapped_column(String(100), nullable=True)
    file_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    parsed_text_path: Mapped[str] = mapped_column(Text, nullable=True)
    parse_quality: Mapped[float] = mapped_column(Numeric(5, 3), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="uploaded")
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    batch: Mapped["GradingBatch"] = relationship(back_populates="papers")
    chunks: Mapped[list["PaperChunk"]] = relationship(back_populates="paper", cascade="all, delete-orphan")
    scoring_runs: Mapped[list["ScoringRun"]] = relationship(back_populates="paper", cascade="all, delete-orphan")


class PaperChunk(Base):
    __tablename__ = "paper_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(String(36), ForeignKey("papers.id"), nullable=False)
    section_title: Mapped[str] = mapped_column(Text, nullable=True)
    page_start: Mapped[int] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int] = mapped_column(Integer, nullable=True)
    paragraph_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    paper: Mapped["Paper"] = relationship(back_populates="chunks")


class BatchScoringJob(Base):
    """Durable coordinator state for a recoverable batch-scoring execution."""

    __tablename__ = "batch_scoring_jobs"
    __table_args__ = (
        UniqueConstraint(
            "grading_batch_id",
            "generation",
            name="uq_batch_scoring_jobs_batch_generation",
        ),
        CheckConstraint(
            "generation > 0",
            name="ck_batch_scoring_jobs_positive_generation",
        ),
        CheckConstraint(
            "max_workers >= 1 AND max_workers <= 16",
            name="ck_batch_scoring_jobs_worker_bound",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'cancel_requested', 'completed', "
            "'completed_with_errors', 'canceled', 'failed')",
            name="ck_batch_scoring_jobs_status",
        ),
        CheckConstraint(
            "total_items >= 0 AND pending_count >= 0 AND running_count >= 0 "
            "AND succeeded_count >= 0 AND skipped_count >= 0 "
            "AND failed_count >= 0 AND canceled_count >= 0",
            name="ck_batch_scoring_jobs_nonnegative_counts",
        ),
        CheckConstraint(
            _lower_hex_digest_check("observation_policy_hash"),
            name="ck_batch_scoring_jobs_policy_hash",
        ),
        Index(
            "ix_batch_scoring_jobs_one_active_per_batch",
            "grading_batch_id",
            unique=True,
            sqlite_where=sql_text(
                "status IN ('queued', 'running', 'cancel_requested')"
            ),
            postgresql_where=sql_text(
                "status IN ('queued', 'running', 'cancel_requested')"
            ),
        ),
        Index(
            "ix_batch_scoring_jobs_batch_created",
            "grading_batch_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    grading_batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("grading_batches.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    rescore: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_workers: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="queued")
    total_items: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    running_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    succeeded_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    canceled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    observation_policy: Mapped[dict] = mapped_column(JSON, nullable=False)
    observation_policy_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metrics_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    runner_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    batch: Mapped["GradingBatch"] = relationship(back_populates="scoring_jobs")
    items: Mapped[list["BatchScoringItem"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="(BatchScoringItem.created_at, BatchScoringItem.id)",
    )


class BatchScoringItem(Base):
    """One durable, retryable paper checkpoint within a batch-scoring job."""

    __tablename__ = "batch_scoring_items"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "paper_id", name="uq_batch_scoring_items_job_paper"
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'skipped', "
            "'failed', 'canceled')",
            name="ck_batch_scoring_items_status",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_batch_scoring_items_nonnegative_attempts",
        ),
        Index("ix_batch_scoring_items_job_status", "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("batch_scoring_jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    paper_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("papers.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scoring_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    baseline_scoring_run_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("scoring_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    telemetry: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    attempt_history: Mapped[list] = mapped_column(
        MutableList.as_mutable(JSON), nullable=False, default=list
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    job: Mapped["BatchScoringJob"] = relationship(back_populates="items")
    paper: Mapped["Paper"] = relationship()
    scoring_run: Mapped["ScoringRun | None"] = relationship(
        foreign_keys=[scoring_run_id]
    )
    baseline_scoring_run: Mapped["ScoringRun | None"] = relationship(
        foreign_keys=[baseline_scoring_run_id]
    )


class ScoringRun(Base):
    __tablename__ = "scoring_runs"
    __table_args__ = (
        CheckConstraint(
            "(paper_id IS NOT NULL AND submission_id IS NULL) OR "
            "(paper_id IS NULL AND submission_id IS NOT NULL)",
            name="ck_scoring_runs_exactly_one_target",
        ),
        CheckConstraint(
            "(submission_id IS NULL AND document_snapshot_id IS NULL) OR "
            "(submission_id IS NOT NULL AND document_snapshot_id IS NOT NULL)",
            name="ck_scoring_runs_submission_snapshot_target",
        ),
        CheckConstraint(
            "(%s AND policy_hash IS NULL AND policy_schema_version IS NULL) "
            "OR (%s AND policy_hash IS NOT NULL "
            "AND policy_schema_version IS NOT NULL AND length(policy_schema_version) > 0 "
            "AND %s)"
            % (
                _json_null_check("policy_snapshot"),
                _json_present_check("policy_snapshot"),
                _lower_hex_digest_check("policy_hash"),
            ),
            name="ck_scoring_runs_policy_identity",
        ),
        CheckConstraint(
            "(rubric_source_kind IS NULL "
            "AND rubric_snapshot_hash IS NULL "
            "AND rubric_version_id IS NULL "
            "AND rubric_version_hash IS NULL "
            "AND rubric_hash_scheme IS NULL "
            "AND business_profile_key IS NULL "
            "AND business_profile_version IS NULL "
            "AND prompt_version IS NULL "
            "AND runtime_identity IS NULL "
            "AND workflow_profile IS NULL "
            "AND execution_plan_snapshot IS NULL "
            "AND execution_plan_hash IS NULL "
            "AND plan_schema_version IS NULL "
            "AND checker_manifest IS NULL "
            "AND source_artifact_hash IS NULL "
            "AND normalized_content_hash IS NULL "
            "AND document_snapshot_ref IS NULL "
            "AND document_snapshot_hash IS NULL "
            "AND document_schema_version IS NULL "
            "AND engine_version IS NULL "
            "AND rescore_generation IS NULL "
            "AND idempotency_key IS NULL) "
            "OR (rubric_source_kind IS NOT NULL "
            "AND rubric_source_kind IN ('published_version', 'legacy_unversioned') "
            "AND rubric_snapshot_hash IS NOT NULL AND %s "
            "AND business_profile_key IS NOT NULL AND length(business_profile_key) > 0 "
            "AND ((business_profile_version IS NULL AND prompt_version IS NULL "
            "AND runtime_identity IS NULL) OR (business_profile_version IS NOT NULL "
            "AND length(business_profile_version) > 0 AND prompt_version IS NOT NULL "
            "AND length(prompt_version) > 0 AND runtime_identity IS NOT NULL)) "
            "AND workflow_profile IS NOT NULL AND length(workflow_profile) > 0 "
            "AND execution_plan_snapshot IS NOT NULL "
            "AND execution_plan_hash IS NOT NULL AND %s "
            "AND plan_schema_version IS NOT NULL AND length(plan_schema_version) > 0 "
            "AND checker_manifest IS NOT NULL "
            "AND source_artifact_hash IS NOT NULL AND %s "
            "AND normalized_content_hash IS NOT NULL AND %s "
            "AND document_snapshot_ref IS NOT NULL AND length(document_snapshot_ref) > 0 "
            "AND document_snapshot_hash IS NOT NULL AND %s "
            "AND document_schema_version IS NOT NULL AND length(document_schema_version) > 0 "
            "AND engine_version IS NOT NULL AND length(engine_version) > 0 "
            "AND rescore_generation IS NOT NULL AND rescore_generation >= 0 "
            "AND idempotency_key IS NOT NULL AND %s "
            "AND policy_snapshot IS NOT NULL "
            "AND policy_hash IS NOT NULL AND %s "
            "AND policy_schema_version IS NOT NULL AND length(policy_schema_version) > 0 "
            "AND ((rubric_source_kind = 'legacy_unversioned' "
            "AND rubric_version_id IS NULL AND rubric_version_hash IS NULL "
            "AND rubric_hash_scheme IS NULL) "
            "OR (rubric_source_kind = 'published_version' "
            "AND rubric_version_id IS NOT NULL "
            "AND rubric_version_hash IS NOT NULL AND %s "
            "AND rubric_hash_scheme IS NOT NULL AND length(rubric_hash_scheme) > 0)))"
            % tuple(
                _lower_hex_digest_check(name)
                for name in (
                    "rubric_snapshot_hash",
                    "execution_plan_hash",
                    "source_artifact_hash",
                    "normalized_content_hash",
                    "document_snapshot_hash",
                    "idempotency_key",
                    "policy_hash",
                    "rubric_version_hash",
                )
            ),
            name="ck_scoring_runs_core_replay_identity",
        ),
        ForeignKeyConstraint(
            ["document_snapshot_id", "submission_id"],
            ["document_snapshots.id", "document_snapshots.submission_id"],
            name="fk_scoring_runs_document_snapshot_submission",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            [
                "rubric_version_id",
                "rubric_id",
                "rubric_version_hash",
                "rubric_hash_scheme",
            ],
            [
                "rubric_versions.id",
                "rubric_versions.rubric_id",
                "rubric_versions.version_hash",
                "rubric_versions.hash_scheme",
            ],
            name="fk_scoring_runs_rubric_version_identity",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("idempotency_key", name="uq_scoring_runs_idempotency_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_id: Mapped[str] = mapped_column(String(36), nullable=True)  # P4.3 预留（单租户暂不强隔离）
    # Kept deferred while the additive tenant migration is rolled out so the
    # ORM can still read historical schemas during migration verification.
    organization_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("organizations.id"),
        nullable=True,
        index=True,
        deferred=True,
    )
    paper_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("papers.id"), nullable=True
    )
    submission_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("submissions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    document_snapshot_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True
    )
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    model_provider: Mapped[str] = mapped_column(String(100), nullable=False, default="mock")
    model_name: Mapped[str] = mapped_column(String(100), nullable=False, default="mock-criterion-scorer")
    model_version: Mapped[str] = mapped_column(String(100), nullable=True, default="v1")
    ai_connection_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ai_connections.id"), nullable=True, deferred=True
    )
    ai_connection_key_version: Mapped[int | None] = mapped_column(Integer, nullable=True, deferred=True)
    ai_connection_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, deferred=True
    )
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="created")
    ai_total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    final_total_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    grade: Mapped[str] = mapped_column(String(50), nullable=True)
    need_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
    # 篇章一致性发现（设计§8）：确定性（图表/引文）+ 语义（研究问题↔结论）合并，进报告、供人工复核。
    coherence_findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 格式问题清单（设计§9）：被评论文有效格式 vs 模板 FormatSpec 的比对发现。
    format_findings: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    policy_snapshot: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_schema_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rubric_source_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rubric_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rubric_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    rubric_version_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    rubric_hash_scheme: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_profile_key: Mapped[str | None] = mapped_column(String(100), nullable=True)
    business_profile_version: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    prompt_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    runtime_identity: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    workflow_profile: Mapped[str | None] = mapped_column(String(100), nullable=True)
    execution_plan_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    execution_plan_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_schema_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    checker_manifest: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    source_artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalized_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_snapshot_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    document_schema_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    rescore_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    paper: Mapped["Paper | None"] = relationship(back_populates="scoring_runs")
    submission: Mapped["Submission | None"] = relationship(
        back_populates="scoring_runs",
        foreign_keys=[submission_id],
    )
    document_snapshot: Mapped["DocumentSnapshot | None"] = relationship(
        back_populates="scoring_runs",
        foreign_keys=[document_snapshot_id, submission_id],
        overlaps="scoring_runs,submission",
    )
    rubric: Mapped["Rubric"] = relationship()
    items: Mapped[list["ScoreItem"]] = relationship(
        back_populates="scoring_run",
        cascade="all, delete-orphan",
        order_by="ScoreItem.created_at",
    )
    rule_tasks: Mapped[list["RuleScoringTask"]] = relationship(
        back_populates="scoring_run",
        cascade="all, delete-orphan",
        order_by="(RuleScoringTask.created_at, RuleScoringTask.id)",
    )
    manual_review_tasks: Mapped[list["ManualReviewTask"]] = relationship(
        back_populates="scoring_run",
        cascade="all, delete-orphan",
        order_by="(ManualReviewTask.created_at, ManualReviewTask.id)",
    )


class ScoreItem(Base):
    __tablename__ = "score_items"
    __table_args__ = (
        CheckConstraint(
            "(%s AND aggregation_schema_version IS NULL "
            "AND auto_score_status IS NULL AND ai_score IS NOT NULL) "
            "OR (%s AND aggregation_schema_version IS NOT NULL "
            "AND length(aggregation_schema_version) > 0 "
            "AND auto_score_status IS NOT NULL "
            "AND auto_score_status IN ('calculated', 'invalid', 'blocked') "
            "AND ((auto_score_status = 'calculated' AND ai_score IS NOT NULL) "
            "OR (auto_score_status IN ('invalid', 'blocked') AND ai_score IS NULL)))"
            % (_json_null_check("aggregation"), _json_present_check("aggregation")),
            name="ck_score_items_aggregation_state",
        ),
        CheckConstraint(
            "(rule_results IS NULL AND rule_results_schema_version IS NULL) "
            "OR (rule_results IS NOT NULL AND rule_results_schema_version IS NOT NULL "
            "AND length(rule_results_schema_version) > 0)",
            name="ck_score_items_rule_results_identity",
        ),
        CheckConstraint(
            "review_revision >= 1", name="ck_score_items_review_revision_positive"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    criterion_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubric_criteria.id"), nullable=False)
    max_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    ai_score: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    final_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    evidence_sufficient: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    deductions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 结构化扣分（设计哲学第3条/N6）：每项带 points/reason/rule_ref/evidence_*；deductions 为其展示投影。
    deduction_items: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # 分档制选档（带证据）与 hybrid 子检查结果（设计§2/§6.3），llm_direct/deductive 时为空。
    band_selection: Mapped[dict] = mapped_column(JSON, nullable=True)
    sub_results: Mapped[list] = mapped_column(JSON, nullable=True)
    suggestion: Mapped[str] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Numeric(5, 3), nullable=True)
    need_manual_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 「为什么这条要我看」。结构化在先，review_reason 只是派生展示文本。
    # 历史行保持 NULL——不为补一个展示字段而重评权威结果。
    review_reasons: Mapped[list | None] = mapped_column(JSON, nullable=True)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 复核写入的乐观并发游标：两个复核者不得静默覆盖彼此。
    review_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    raw_model_output: Mapped[dict] = mapped_column(JSON, nullable=True)
    aggregation: Mapped[dict | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    aggregation_schema_version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    auto_score_status: Mapped[str | None] = mapped_column(String(50), nullable=True)
    rule_results: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    rule_results_schema_version: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)

    scoring_run: Mapped["ScoringRun"] = relationship(back_populates="items")
    criterion: Mapped["RubricCriterion"] = relationship(back_populates="score_items")

    @property
    def criterion_name(self):
        return self.criterion.name if self.criterion else None

    @property
    def criterion_code(self):
        return self.criterion.code if self.criterion else None


class ReviewLog(Base):
    __tablename__ = "review_logs"
    __table_args__ = (
        CheckConstraint(
            "(policy_hash IS NULL AND resolution_type IS NULL) "
            "OR (policy_hash IS NOT NULL AND %s "
            "AND resolution_type IS NOT NULL "
            "AND resolution_type IN ('ordinary_override', 'resolve_validation', "
            "'resolve_block', 'rescore'))" % _lower_hex_digest_check("policy_hash"),
            name="ck_review_logs_policy_resolution",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    score_item_id: Mapped[str] = mapped_column(String(36), ForeignKey("score_items.id"), nullable=True)
    reviewer_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id"), nullable=False)
    before_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    after_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolution_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ReviewCommandReceipt(Base):
    """批量复核命令的幂等回执（前端 v2 计划 §5-B）。

    同一个幂等键重放时返回原结果而不是再写一遍；同键不同载荷是冲突，不是
    覆盖——后者会让「重试」悄悄变成「执行了另一件事」。

    回执与 ReviewLog 同事务提交：否则崩溃可能留下「已写日志但无回执」，
    重试就会重复写入。
    """

    __tablename__ = "review_command_receipts"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "actor_id",
            "command",
            "idempotency_key",
            name="uq_review_command_receipts_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    command: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    #: 载荷指纹。同键不同指纹即冲突。
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )


class RuleScoringTask(Base):
    """Durable audit/checkpoint for one AtomicRule execution."""

    __tablename__ = "rule_scoring_tasks"
    __table_args__ = (
        UniqueConstraint(
            "scoring_run_id",
            "rule_code",
            name="uq_rule_scoring_tasks_run_rule",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'retry_wait', "
            "'failed_exhausted', 'skipped', 'review_required', 'canceled', "
            "'deferred_provider')",
            name="ck_rule_scoring_tasks_status",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1",
            name="ck_rule_scoring_tasks_attempts",
        ),
        Index("ix_rule_scoring_tasks_run_status", "scoring_run_id", "status"),
        Index("ix_rule_scoring_tasks_org_status", "organization_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    scoring_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scoring_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    score_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("score_items.id", ondelete="SET NULL"), nullable=True
    )
    criterion_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(200), nullable=False)
    judge_type: Mapped[str] = mapped_column(String(50), nullable=False)
    dependency_rule_codes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="pending")
    blocking_final_total: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    provider_error: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    result_snapshot: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    scoring_run: Mapped["ScoringRun"] = relationship(back_populates="rule_tasks")
    score_item: Mapped["ScoreItem | None"] = relationship()
    manual_review_task: Mapped["ManualReviewTask | None"] = relationship(
        back_populates="rule_scoring_task", uselist=False
    )


class ManualReviewTask(Base):
    """Tenant-scoped, claimable workflow for a blocking automatic result."""

    __tablename__ = "manual_review_tasks"
    __table_args__ = (
        UniqueConstraint(
            "rule_scoring_task_id",
            name="uq_manual_review_tasks_rule_task",
        ),
        CheckConstraint(
            "status IN ('open', 'claimed', 'resolved', 'canceled', 'superseded')",
            name="ck_manual_review_tasks_status",
        ),
        CheckConstraint("version >= 1", name="ck_manual_review_tasks_version"),
        CheckConstraint(
            "priority >= 0 AND priority <= 100",
            name="ck_manual_review_tasks_priority",
        ),
        Index("ix_manual_review_tasks_org_status", "organization_id", "status"),
        Index("ix_manual_review_tasks_run_status", "scoring_run_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("organizations.id"), nullable=False
    )
    scoring_run_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("scoring_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    rule_scoring_task_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("rule_scoring_tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    score_item_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("score_items.id", ondelete="SET NULL"), nullable=True
    )
    criterion_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_code: Mapped[str | None] = mapped_column(String(200), nullable=True)
    trigger_code: Mapped[str] = mapped_column(String(100), nullable=False)
    trigger_message: Mapped[str] = mapped_column(Text, nullable=False)
    blocking_final_total: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default="open")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    assigned_reviewer_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolution_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_evidence: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow, onupdate=utcnow
    )

    scoring_run: Mapped["ScoringRun"] = relationship(
        back_populates="manual_review_tasks"
    )
    rule_scoring_task: Mapped["RuleScoringTask | None"] = relationship(
        back_populates="manual_review_task"
    )
    score_item: Mapped["ScoreItem | None"] = relationship()
    assigned_reviewer: Mapped["User | None"] = relationship()


class CalibrationAnchor(Base):
    """L2 跨文档校准锚点（设计§7）：脱敏范文 + 已知分数 + 理由，按 rubric/评分项归档。
    评分时作 few-shot 校准模型宽严；**用脱敏范文，不用真实学生论文互锚**（隐私/公平，§15.3）。"""

    __tablename__ = "calibration_anchors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    rubric_id: Mapped[str] = mapped_column(String(36), ForeignKey("rubrics.id"), nullable=False)
    criterion_code: Mapped[str] = mapped_column(String(50), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    max_score: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    label: Mapped[str] = mapped_column(String(50), nullable=True)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), nullable=False, default="范文")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)


class ReleaseGateProfile(Base):
    """用户建立的不可变发布门禁关系快照，不保存论文或教师明细。"""

    __tablename__ = "release_gate_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["rubric_version_id", "rubric_id"],
            ["rubric_versions.id", "rubric_versions.rubric_id"],
            name="fk_release_gate_profiles_rubric_version",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "gate_key",
            "name",
            name="uq_release_gate_profiles_gate_name",
        ),
        UniqueConstraint("profile_hash", name="uq_release_gate_profiles_hash"),
        CheckConstraint(
            "gate_key IN ('GATE-01', 'GATE-02', 'GATE-03')",
            name="ck_release_gate_profiles_gate_key",
        ),
        CheckConstraint(
            "status IN ('active', 'retired')",
            name="ck_release_gate_profiles_status",
        ),
        CheckConstraint(
            _lower_hex_digest_check("profile_hash"),
            name="ck_release_gate_profiles_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    gate_key: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    rubric_id: Mapped[str] = mapped_column(String(36), nullable=False)
    rubric_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    dataset_identity: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    model_identity: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    anchors_identity: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    acceptance_thresholds: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    regression_tolerances: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active"
    )
    created_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )

    rubric_version: Mapped["RubricVersion"] = relationship(
        primaryjoin=foreign(rubric_version_id) == RubricVersion.id,
        foreign_keys=[rubric_version_id],
    )
    runs: Mapped[list["ReleaseGateRun"]] = relationship(
        back_populates="profile",
        order_by="ReleaseGateRun.created_at",
        passive_deletes=True,
    )


class ReleaseGateRun(Base):
    """与 profile 绑定的仓库安全 candidate/final 门禁记录。"""

    __tablename__ = "release_gate_runs"
    __table_args__ = (
        UniqueConstraint(
            "id", "candidate_sha256", name="uq_release_gate_runs_id_candidate"
        ),
        UniqueConstraint(
            "evaluation_id", name="uq_release_gate_runs_evaluation_id"
        ),
        UniqueConstraint(
            "candidate_sha256", name="uq_release_gate_runs_candidate"
        ),
        CheckConstraint(
            _lower_hex_digest_check("candidate_sha256"),
            name="ck_release_gate_runs_candidate_hash",
        ),
        CheckConstraint(
            "final_record_sha256 IS NULL OR "
            + _lower_hex_digest_check("final_record_sha256"),
            name="ck_release_gate_runs_final_hash",
        ),
        CheckConstraint(
            "status IN ('candidate_awaiting_approval', 'passed', "
            "'failed_thresholds', 'ineligible')",
            name="ck_release_gate_runs_status",
        ),
        CheckConstraint(
            "(status = 'candidate_awaiting_approval' AND final_record IS NULL "
            "AND final_record_sha256 IS NULL AND finalized_at IS NULL) OR "
            "(status <> 'candidate_awaiting_approval' AND final_record IS NOT NULL "
            "AND final_record_sha256 IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_release_gate_runs_final_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("release_gate_profiles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    evaluation_id: Mapped[str] = mapped_column(String(200), nullable=False)
    candidate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_record: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="candidate_awaiting_approval"
    )
    final_record: Mapped[dict | None] = mapped_column(
        MutableDict.as_mutable(JSON(none_as_null=True)), nullable=True
    )
    final_record_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    profile: Mapped["ReleaseGateProfile"] = relationship(back_populates="runs")
    approval: Mapped["ReleaseGateApproval | None"] = relationship(
        back_populates="run",
        passive_deletes=True,
        uselist=False,
    )


class ReleaseGateApproval(Base):
    """由生产用户作出的、绑定精确 candidate hash 的批准记录。"""

    __tablename__ = "release_gate_approvals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "candidate_sha256"],
            ["release_gate_runs.id", "release_gate_runs.candidate_sha256"],
            name="fk_release_gate_approvals_exact_candidate",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("run_id", name="uq_release_gate_approvals_run"),
        CheckConstraint(
            _lower_hex_digest_check("candidate_sha256"),
            name="ck_release_gate_approvals_candidate_hash",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    candidate_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    privacy_review: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    accepted_baseline: Mapped[dict] = mapped_column(
        MutableDict.as_mutable(JSON), nullable=False
    )
    approved_by: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    approved_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utcnow
    )

    run: Mapped["ReleaseGateRun"] = relationship(back_populates="approval")


class SpreadsheetWriteLog(Base):
    __tablename__ = "spreadsheet_write_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scoring_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("scoring_runs.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utcnow)
