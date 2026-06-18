"""单租户登录端点（opt-in）。AUTH_ENABLED=False 时登录恒不需要、且无保护意义。"""

from fastapi import APIRouter
from fastapi import Header
from fastapi import HTTPException
from pydantic import BaseModel

from backend.app.services.auth import auth_active
from backend.app.services.auth import credentials_ok
from backend.app.services.auth import issue_token
from backend.app.services.auth import verify_token

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.get("/status")
def auth_status():
    """前端据此决定是否显示登录页。"""
    return {"auth_required": auth_active()}


@router.post("/login")
def login(payload: LoginRequest):
    if not auth_active():
        # 未启用鉴权：返回一个无意义 token，前端无需登录。
        return {"token": "", "auth_required": False}
    if not credentials_ok(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"token": issue_token(payload.username), "auth_required": True}


@router.get("/me")
def me(authorization: str = Header(default="")):
    if not auth_active():
        return {"auth_required": False, "user": None}
    token = authorization[7:] if authorization.lower().startswith("bearer ") else authorization
    user = verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话失效")
    return {"auth_required": True, "user": user}
