"""单租户简单登录：HMAC 签名会话 token（无外部依赖）。

opt-in：仅当 AUTH_ENABLED=True 且配了 AUTH_PASSWORD 才真正生效；否则本地/CLI 免登录。
单租户模型：一套共享凭据保护整个部署；登录只做"访问门禁"，不做多用户身份隔离
（owner_id 列已预留，为将来多用户铺路）。
"""

import hashlib
import hmac
import time

from backend.app.core.config import settings


def auth_active() -> bool:
    return bool(settings.AUTH_ENABLED and settings.AUTH_PASSWORD)


def credentials_ok(username: str, password: str) -> bool:
    if not settings.AUTH_PASSWORD:
        return False
    return hmac.compare_digest(username or "", settings.AUTH_USERNAME) and hmac.compare_digest(
        password or "", settings.AUTH_PASSWORD
    )


def _sign(body: str) -> str:
    return hmac.new(settings.AUTH_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(username: str) -> str:
    body = "%s|%d" % (username, int(time.time()) + settings.AUTH_TOKEN_TTL_SECONDS)
    return "%s|%s" % (body, _sign(body))


def verify_token(token: str):
    """返回用户名（有效）或 None（无效/过期/篡改）。"""
    try:
        username, exp, sig = (token or "").rsplit("|", 2)
    except ValueError:
        return None
    body = "%s|%s" % (username, exp)
    if not hmac.compare_digest(sig, _sign(body)):
        return None
    try:
        if int(exp) < int(time.time()):
            return None
    except ValueError:
        return None
    return username
