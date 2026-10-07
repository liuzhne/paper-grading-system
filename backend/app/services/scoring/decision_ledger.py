"""Database adapter for the Core ``DecisionLedger`` port.

Reuses a validated semantic rule decision when its full decision identity is
unchanged, so a retry only pays for the rules that failed.  Every read and
write runs in its own short transaction: a decision is committed as soon as the
executor accepts it, independent of whether the paper's run later persists.

The ledger fails soft.  A database error degrades to "no reuse" and is logged;
it never turns into a failed rule.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import timedelta
import logging

from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker

from backend.app.core.config import settings
from backend.app.db.models import RuleDecisionLedger
from backend.app.db.models import utcnow


logger = logging.getLogger(__name__)

_READS_BYPASSED = ContextVar("rule_decision_ledger_reads_bypassed", default=False)


@contextmanager
def bypass_ledger_reads():
    """Explicit rescoring: judge every rule again, but still record results."""

    token = _READS_BYPASSED.set(True)
    try:
        yield
    finally:
        _READS_BYPASSED.reset(token)


def ledger_reads_bypassed() -> bool:
    return _READS_BYPASSED.get()


def connection_scope(scorer) -> str:
    """Never replay one AI connection's results for another connection."""

    snapshot = getattr(scorer, "_ai_connection_snapshot", None) or {}
    connection_id = snapshot.get("ai_connection_id")
    if connection_id:
        return "connection:%s" % connection_id
    return "deployment:%s:%s" % (
        getattr(scorer, "provider", "unknown"),
        getattr(scorer, "model_name", "unknown"),
    )


def _token_count(usage, field) -> int:
    try:
        return max(0, int((usage or {}).get(field) or 0))
    except (TypeError, ValueError, AttributeError):
        return 0


class DatabaseDecisionLedger:
    def __init__(
        self,
        *,
        bind,
        organization_id,
        scope,
        ttl_days,
        read_enabled=True,
        now=utcnow,
    ):
        self._sessions = sessionmaker(
            bind=bind, autoflush=False, expire_on_commit=False
        )
        self.organization_id = organization_id
        self.scope_key = "%s|%s" % (organization_id or "no-organization", scope)
        self.ttl = timedelta(days=int(ttl_days))
        self.read_enabled = bool(read_enabled)
        self._now = now
        self.hits = 0
        self.misses = 0
        self.writes = 0
        # Rule codes whose persisted decision in this run is a replay.
        self.reused_rule_codes = set()

    def get(self, *, decision_identity):
        if not self.read_enabled:
            return None
        try:
            with self._sessions() as session:
                row = session.scalar(
                    select(RuleDecisionLedger).where(
                        RuleDecisionLedger.scope_key == self.scope_key,
                        RuleDecisionLedger.decision_identity_hash == decision_identity,
                        RuleDecisionLedger.expires_at > self._now(),
                    )
                )
                response = None if row is None else deepcopy(row.response)
                rule_code = None if row is None else row.rule_code
        except SQLAlchemyError:
            logger.warning("rule_decision_ledger_read_failed", exc_info=True)
            response = None
        if response is None:
            self.misses += 1
        else:
            self.hits += 1
            self.reused_rule_codes.add(rule_code)
        return response

    def known_identities(self, identities) -> set[str]:
        """Live identities among ``identities``; used for local estimates."""

        wanted = sorted(set(identities))
        if not self.read_enabled or not wanted:
            return set()
        try:
            with self._sessions() as session:
                rows = session.scalars(
                    select(RuleDecisionLedger.decision_identity_hash).where(
                        RuleDecisionLedger.scope_key == self.scope_key,
                        RuleDecisionLedger.decision_identity_hash.in_(wanted),
                        RuleDecisionLedger.expires_at > self._now(),
                    )
                ).all()
        except SQLAlchemyError:
            logger.warning("rule_decision_ledger_read_failed", exc_info=True)
            return set()
        return set(rows)

    def put(self, *, decision_identity, rule_code, response, usage=None):
        # A put means the provider judged this rule in this run (including a
        # stored entry that no longer validated), so it is not a replay.
        self.reused_rule_codes.discard(str(rule_code))
        now = self._now()
        try:
            with self._sessions() as session:
                # Bound growth without a scheduler (Vercel has none): expired
                # rows of this scope are removed whenever the scope writes.
                session.execute(
                    delete(RuleDecisionLedger).where(
                        RuleDecisionLedger.scope_key == self.scope_key,
                        RuleDecisionLedger.expires_at <= now,
                    )
                )
                row = session.scalar(
                    select(RuleDecisionLedger).where(
                        RuleDecisionLedger.scope_key == self.scope_key,
                        RuleDecisionLedger.decision_identity_hash == decision_identity,
                    )
                )
                if row is None:
                    row = RuleDecisionLedger(
                        organization_id=self.organization_id,
                        scope_key=self.scope_key,
                        decision_identity_hash=decision_identity,
                    )
                    session.add(row)
                row.rule_code = str(rule_code)
                row.response = deepcopy(dict(response))
                row.prompt_tokens = _token_count(usage, "prompt_tokens")
                row.completion_tokens = _token_count(usage, "completion_tokens")
                row.created_at = now
                row.expires_at = now + self.ttl
                session.commit()
            self.writes += 1
        except IntegrityError:
            # A concurrent worker recorded the same decision first.
            logger.info("rule_decision_ledger_write_raced rule_code=%s", rule_code)
        except SQLAlchemyError:
            logger.warning("rule_decision_ledger_write_failed", exc_info=True)


class RuleCallJournal:
    """In-memory ``ExecutionJournal`` for one paper run."""

    def __init__(self):
        self.reused_rule_codes = set()
        self.group_call_ids = {}

    def record_semantic_decision(self, *, rule_code, reused, group_call_id=None):
        if reused:
            self.reused_rule_codes.add(rule_code)
        else:
            self.reused_rule_codes.discard(rule_code)
        if group_call_id:
            self.group_call_ids[rule_code] = group_call_id


def build_decision_ledger(db, *, organization_id, scorer):
    """Ledger for one paper run, or ``None`` when reuse does not apply."""

    if not settings.SCORING_DECISION_LEDGER_ENABLED:
        return None
    # Mock judgments cost nothing and are not real decisions worth replaying.
    if str(getattr(scorer, "provider", "")) == "mock":
        return None
    return DatabaseDecisionLedger(
        bind=db.get_bind(),
        organization_id=organization_id,
        scope=connection_scope(scorer),
        ttl_days=settings.SCORING_DECISION_LEDGER_TTL_DAYS,
        read_enabled=not ledger_reads_bypassed(),
    )


__all__ = [
    "DatabaseDecisionLedger",
    "RuleCallJournal",
    "build_decision_ledger",
    "bypass_ledger_reads",
    "connection_scope",
    "ledger_reads_bypassed",
]
