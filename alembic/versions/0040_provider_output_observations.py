"""Record what the model actually did: reasoning tokens and truncated answers.

Revision ID: 0040_provider_output_observations
Revises: 0039_site_leads
Create Date: 2026-10-19

The run identity says whether thinking is disabled, but nothing recorded
whether the model actually thought.  On 2026-10-16 a Vertex Gemini run spent
about 37k hidden reasoning tokens against an identity that said "thinking
off"; it was only visible by subtracting prompt and completion tokens from the
total by hand (docs/评分输出预算与截断恢复改造方案.md, P1).

Two counters per scoring run and per usage-ledger row:

- ``reasoning_tokens``: reported by the provider, or inferred as
  total - prompt - completion when the provider does not report them;
- ``truncated_count``: answers that ended because the output limit was hit.

Platform-model usage stays on ``scoring_runs`` (``ai_usage_ledger`` only
projects BYOK connections), so both tables get the columns.

Only columns are added: the table-level ``pgs_app`` grants and RLS policies of
0023/0029 already cover them.  The downgrade refuses while any row carries a
non-zero value, because the observation cannot be rebuilt afterwards.
"""

from alembic import op
import sqlalchemy as sa


revision = "0040_provider_output_observations"
down_revision = "0039_site_leads"
branch_labels = None
depends_on = None


TABLES = ("scoring_runs", "ai_usage_ledger")
COLUMNS = ("reasoning_tokens", "truncated_count")


def upgrade():
    for table in TABLES:
        for column in COLUMNS:
            op.add_column(
                table,
                sa.Column(column, sa.Integer(), nullable=False, server_default="0"),
            )


def downgrade():
    connection = op.get_bind()
    for table in TABLES:
        observed = connection.execute(
            sa.text(
                "SELECT count(*) FROM %s WHERE reasoning_tokens <> 0 OR truncated_count <> 0"
                % table
            )
        ).scalar_one()
        if observed:
            raise RuntimeError(
                "0040 downgrade would erase reasoning/truncation observations of %d %s row(s); "
                "archive them before downgrading" % (observed, table)
            )
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            for column in reversed(COLUMNS):
                batch.drop_column(column)
