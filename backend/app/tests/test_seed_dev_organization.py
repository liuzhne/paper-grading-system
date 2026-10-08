"""开发种子只用默认组织里的、或开发用户自己的旧种子，不碰别人的同名数据。"""

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db.models import GradingBatch
from backend.app.db.models import Organization
from backend.app.db.models import Rubric
from backend.app.db.models import User
from backend.app.scripts.seed_dev import seed


RUBRIC_NAME, RUBRIC_VERSION = "本科毕业论文通用评分标准", "v1.0"
BATCH_NAME = "2026 届论文评分开发批次"


def _default_org(db):
    return db.scalar(select(Organization).where(Organization.name == settings.DEFAULT_ORGANIZATION_NAME))


def _seeded(db):
    org = _default_org(db)
    rubric = db.scalar(select(Rubric).where(Rubric.name == RUBRIC_NAME, Rubric.organization_id == org.id))
    batch = db.scalar(select(GradingBatch).where(GradingBatch.name == BATCH_NAME, GradingBatch.organization_id == org.id))
    return org, rubric, batch


def _foreign(db, *, organization_id, created_by):
    rubric = Rubric(name=RUBRIC_NAME, version=RUBRIC_VERSION, organization_id=organization_id, created_by=created_by)
    db.add(rubric)
    db.flush()
    batch = GradingBatch(name=BATCH_NAME, rubric_id=rubric.id, organization_id=organization_id, created_by=created_by)
    db.add(batch)
    db.commit()
    return rubric.id, batch.id


def test_fresh_seed_lands_in_the_default_organization_and_is_idempotent(client):
    with client.session_factory() as db:
        seed(db)
        seed(db)
        org, rubric, batch = _seeded(db)
        assert rubric is not None and batch is not None
        assert batch.rubric_id == rubric.id
        assert db.query(GradingBatch).filter(GradingBatch.name == BATCH_NAME).count() == 1


def test_same_named_records_of_another_organization_are_never_reused(client):
    with client.session_factory() as db:
        other = Organization(name="另一个学院")
        db.add(other)
        db.flush()
        rubric_id, batch_id = _foreign(db, organization_id=other.id, created_by=settings.DEFAULT_DEV_USER_ID)

        seed(db)

        _, rubric, batch = _seeded(db)
        assert rubric.id != rubric_id and batch.id != batch_id
        # 默认组织的批次只能挂自己组织的标准。
        assert batch.rubric_id == rubric.id
        assert db.get(GradingBatch, batch_id).organization_id == other.id
        assert db.get(Rubric, rubric_id).organization_id == other.id


def test_dev_users_own_legacy_seed_is_claimed_instead_of_duplicated(client):
    with client.session_factory() as db:
        seed(db)  # 先建出开发用户
        org, rubric, batch = _seeded(db)
        # 退回多租户之前的样子：种子没有组织归属。
        rubric.organization_id = None
        batch.organization_id = None
        db.commit()

        seed(db)

        _, claimed_rubric, claimed_batch = _seeded(db)
        assert (claimed_rubric.id, claimed_batch.id) == (rubric.id, batch.id)
        assert db.query(GradingBatch).filter(GradingBatch.name == BATCH_NAME).count() == 1


def test_another_users_unowned_records_are_left_alone(client):
    with client.session_factory() as db:
        stranger = User(id="stranger-user", username="stranger", display_name="别人", role="teacher")
        db.add(stranger)
        db.flush()
        rubric_id, batch_id = _foreign(db, organization_id=None, created_by=stranger.id)

        seed(db)

        assert db.get(Rubric, rubric_id).organization_id is None
        assert db.get(GradingBatch, batch_id).organization_id is None
        _, rubric, batch = _seeded(db)
        assert rubric.id != rubric_id and batch.id != batch_id
