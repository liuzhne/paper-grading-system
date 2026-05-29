from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.config import settings
from backend.app.db.models import User


def ensure_dev_user(db: Session):
    user = db.scalar(select(User).where(User.id == settings.DEFAULT_DEV_USER_ID))
    if user is not None:
        return user
    user = User(
        id=settings.DEFAULT_DEV_USER_ID,
        username=settings.DEFAULT_DEV_USERNAME,
        display_name="本地开发用户",
        role="developer",
    )
    db.add(user)
    db.flush()
    return user

