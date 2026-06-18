"""FastAPI 鉴权依赖：access 门禁 + 当前用户。

- enforce_auth：opt-in 门禁——开启鉴权时要求有效 token，否则放行（默认 off，故现有用例/CLI 不受影响）。
- current_user_id：单租户下恒返回 DEFAULT_DEV_USER_ID（created_by/owner_id 用），FK 安全；
  将来多用户时改为映射登录身份即可（owner_id 列已预留）。
"""

from fastapi import Header
from fastapi import HTTPException

from backend.app.core.config import settings
from backend.app.services.auth import auth_active
from backend.app.services.auth import verify_token


def _user_from_header(authorization: str):
    token = authorization[7:] if (authorization or "").lower().startswith("bearer ") else authorization
    return verify_token(token) if token else None


def enforce_auth(authorization: str = Header(default="")):
    if not auth_active():
        return
    if not _user_from_header(authorization):
        raise HTTPException(status_code=401, detail="未登录或会话失效，请先 POST /api/auth/login")


def current_user_id(authorization: str = Header(default="")) -> str:
    # 单租户：始终归属到同一用户行（DEFAULT_DEV_USER_ID），不论是否登录。
    return settings.DEFAULT_DEV_USER_ID
