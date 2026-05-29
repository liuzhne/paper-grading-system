"""Initial core schema.

Revision ID: 0001_initial_core
Revises:
Create Date: 2026-05-18
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=100), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("department", sa.String(length=100), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "rubrics",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("version", sa.String(length=50), nullable=False),
        sa.Column("total_score", sa.Numeric(6, 2), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("name", "version", name="uq_rubrics_name_version"),
    )
    op.create_table(
        "rubric_criteria",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("rubric_id", sa.String(length=36), sa.ForeignKey("rubrics.id"), nullable=False),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("max_score", sa.Numeric(6, 2), nullable=False),
        sa.Column("weight", sa.Numeric(6, 2), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("evidence_hints", sa.JSON(), nullable=False),
        sa.Column("deduction_rules", sa.JSON(), nullable=False),
        sa.Column("display_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("rubric_id", "code", name="uq_rubric_criteria_rubric_code"),
    )
    op.create_table(
        "grading_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("department", sa.String(length=100), nullable=True),
        sa.Column("major", sa.String(length=100), nullable=True),
        sa.Column("academic_year", sa.String(length=20), nullable=True),
        sa.Column("paper_type", sa.String(length=50), nullable=True),
        sa.Column("rubric_id", sa.String(length=36), sa.ForeignKey("rubrics.id"), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("created_by", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "papers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), sa.ForeignKey("grading_batches.id"), nullable=False),
        sa.Column("student_id", sa.String(length=100), nullable=True),
        sa.Column("student_name", sa.String(length=100), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("department", sa.String(length=100), nullable=True),
        sa.Column("major", sa.String(length=100), nullable=True),
        sa.Column("advisor", sa.String(length=100), nullable=True),
        sa.Column("file_name", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("parsed_text_path", sa.Text(), nullable=True),
        sa.Column("parse_quality", sa.Numeric(5, 3), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "paper_chunks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("paper_id", sa.String(length=36), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("section_title", sa.Text(), nullable=True),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("paragraph_ids", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "scoring_runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("paper_id", sa.String(length=36), sa.ForeignKey("papers.id"), nullable=False),
        sa.Column("rubric_id", sa.String(length=36), sa.ForeignKey("rubrics.id"), nullable=False),
        sa.Column("model_provider", sa.String(length=100), nullable=False),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("model_version", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("ai_total_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("final_total_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("grade", sa.String(length=50), nullable=True),
        sa.Column("need_manual_review", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "score_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scoring_run_id", sa.String(length=36), sa.ForeignKey("scoring_runs.id"), nullable=False),
        sa.Column("criterion_id", sa.String(length=36), sa.ForeignKey("rubric_criteria.id"), nullable=False),
        sa.Column("max_score", sa.Numeric(6, 2), nullable=False),
        sa.Column("ai_score", sa.Numeric(6, 2), nullable=False),
        sa.Column("final_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("evidence_sufficient", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("deductions", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("suggestion", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 3), nullable=True),
        sa.Column("need_manual_review", sa.Boolean(), nullable=False),
        sa.Column("raw_model_output", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "review_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scoring_run_id", sa.String(length=36), sa.ForeignKey("scoring_runs.id"), nullable=False),
        sa.Column("score_item_id", sa.String(length=36), sa.ForeignKey("score_items.id"), nullable=True),
        sa.Column("reviewer_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("before_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("after_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "spreadsheet_write_logs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("scoring_run_id", sa.String(length=36), sa.ForeignKey("scoring_runs.id"), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("spreadsheet_write_logs")
    op.drop_table("review_logs")
    op.drop_table("score_items")
    op.drop_table("scoring_runs")
    op.drop_table("paper_chunks")
    op.drop_table("papers")
    op.drop_table("grading_batches")
    op.drop_table("rubric_criteria")
    op.drop_table("rubrics")
    op.drop_table("users")

