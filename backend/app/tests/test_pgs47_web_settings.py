"""PGS-47 的 Cookie 会话、组织设置和 BYOK 前端契约。"""

from pathlib import Path

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


ROOT = Path(__file__).resolve().parents[3]


def _enable_public_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "w" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "public")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)


def _register_and_login(client, monkeypatch, username: str):
    _enable_public_auth(monkeypatch)
    password = f"{username} secure password"
    assert client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.test",
            "display_name": username,
            "password": password,
        },
    ).status_code == 201
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == username))
        token = session.scalar(
            select(models.EmailVerificationToken.token).where(
                models.EmailVerificationToken.user_id == user.id
            )
        )
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 204
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 204
    return user


def test_cookie_session_can_switch_only_to_a_membership_and_list_current_org_members(client, monkeypatch):
    user = _register_and_login(client, monkeypatch, "pgs47-owner")
    with client.session_factory() as session:
        first_org = session.scalar(
            select(models.OrganizationMember.organization_id).where(
                models.OrganizationMember.user_id == user.id
            )
        )
        second_org = models.Organization(name="PGS47 second organization", created_by=user.id)
        session.add(second_org)
        session.flush()
        session.add(models.OrganizationMember(organization_id=second_org.id, user_id=user.id, role="org_admin"))
        session.commit()
        second_org_id = second_org.id

    switched = client.post("/api/auth/organization-context", json={"organization_id": second_org_id})
    assert switched.status_code == 200, switched.text
    assert switched.json()["organization"] == {"id": second_org_id, "role": "org_admin"}
    assert client.get("/api/auth/me").json()["organization"]["id"] == second_org_id

    members = client.get(f"/api/organizations/{second_org_id}/members")
    assert members.status_code == 200, members.text
    assert members.json() == [
        {
            "user_id": user.id,
            "username": "pgs47-owner",
            "display_name": "pgs47-owner",
            "email": "pgs47-owner@example.test",
            "role": "org_admin",
        }
    ]

    role_updated = client.patch(
        f"/api/organizations/{second_org_id}/members/{user.id}",
        json={"role": "teacher"},
    )
    assert role_updated.status_code == 200, role_updated.text
    assert role_updated.json()["role"] == "teacher"

    forbidden = client.post("/api/auth/organization-context", json={"organization_id": first_org + "-other"})
    assert forbidden.status_code == 404


def test_static_web_uses_cookie_session_and_exposes_pgs47_configuration_flow():
    html = (ROOT / "frontend/web/index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend/web/assets/app.js").read_text(encoding="utf-8")

    for element_id in (
        "page-settings",
        "organization-switcher",
        "organization-members",
        "organization-invite-form",
        "ai-connection-form",
        "ai-connection-list",
        "batch-ai-connection",
        "rubric-visibility-filter",
        "login-register-details",
        "email-verification-form",
        "password-reset-details",
        "password-reset-confirm-form",
    ):
        assert f'id="{element_id}"' in html

    assert "localStorage" not in script
    assert 'credentials: "same-origin"' in script
    for marker in (
        "/auth/register",
        "/auth/verify-email",
        "/auth/logout",
        "/auth/password-reset/request",
        "/auth/password-reset/confirm",
        "/auth/organization-context",
        "/organizations/${state.currentOrganizationId}/members",
        "/members/${target.dataset.memberUpdate}",
        "/ai-connections/test-draft",
        "/rotate-key",
        "/disable",
        "ai_connection_id",
        "visibility",
        "论文内容将发送至所选厂商",
    ):
        assert marker in script or marker in html
