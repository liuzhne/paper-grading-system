from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


def _public_registration(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "a" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "public")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)


def _verification_token(client, username):
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == username))
        assert user is not None
        token = session.scalar(
            select(models.EmailVerificationToken.token).where(
                models.EmailVerificationToken.user_id == user.id
            )
        )
        assert token
        return token


def test_public_registration_requires_email_verification_before_cookie_session(client, monkeypatch):
    _public_registration(monkeypatch)

    registered = client.post(
        "/api/auth/register",
        json={
            "username": "teacher-a",
            "email": "teacher-a@example.test",
            "display_name": "Teacher A",
            "password": "correct horse battery staple",
        },
    )
    assert registered.status_code == 201, registered.text
    assert "token" not in registered.json()

    assert client.post(
        "/api/auth/login",
        json={"username": "teacher-a", "password": "correct horse battery staple"},
    ).status_code == 403

    verified = client.post(
        "/api/auth/verify-email",
        json={"token": _verification_token(client, "teacher-a")},
    )
    assert verified.status_code == 204

    logged_in = client.post(
        "/api/auth/login",
        json={"username": "teacher-a", "password": "correct horse battery staple"},
    )
    assert logged_in.status_code == 204
    assert "httponly" in logged_in.headers["set-cookie"].lower()
    assert "samesite=lax" in logged_in.headers["set-cookie"].lower()

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "teacher-a"
    assert me.json()["organization"]["role"] == "member"


def test_logout_revokes_server_session_and_current_principal_requires_membership(client, monkeypatch):
    _public_registration(monkeypatch)
    client.post(
        "/api/auth/register",
        json={
            "username": "teacher-b",
            "email": "teacher-b@example.test",
            "display_name": "Teacher B",
            "password": "another correct horse battery staple",
        },
    )
    client.post(
        "/api/auth/verify-email",
        json={"token": _verification_token(client, "teacher-b")},
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "teacher-b", "password": "another correct horse battery staple"},
    ).status_code == 204

    organizations = client.get("/api/organizations")
    assert organizations.status_code == 200
    assert len(organizations.json()) == 1

    logged_out = client.post("/api/auth/logout")
    assert logged_out.status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/organizations").status_code == 401


def test_bootstrap_admin_is_created_once_and_is_platform_admin(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_USERNAME", "bootstrap-admin")
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "b" * 48)
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)

    login = client.post(
        "/api/auth/login",
        json={"username": "bootstrap-admin", "password": "bootstrap-admin-password"},
    )
    assert login.status_code == 204
    assert client.get("/api/auth/me").json()["user"]["platform_role"] == "platform_admin"

    with client.session_factory() as session:
        assert session.scalar(select(models.User).where(models.User.username == "bootstrap-admin"))
        assert session.scalar(select(models.Organization))


def test_invite_only_registration_uses_a_one_time_organization_invitation(client, monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "c" * 48)
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bootstrap-admin-password"}).status_code == 204
    organization_id = client.get("/api/organizations").json()[0]["id"]
    invited = client.post(
        f"/api/organizations/{organization_id}/members",
        json={"email": "invited@example.test", "role": "teacher"},
    )
    assert invited.status_code == 201, invited.text
    with client.session_factory() as session:
        invitation = session.scalar(select(models.OrganizationInvitation))
        assert invitation is not None
        invite_token = invitation.token

    registered = client.post(
        "/api/auth/register",
        json={
            "username": "invited-teacher",
            "email": "invited@example.test",
            "display_name": "Invited Teacher",
            "password": "invited teacher password",
            "invitation_token": invite_token,
        },
    )
    assert registered.status_code == 201, registered.text
    assert client.post("/api/auth/verify-email", json={"token": _verification_token(client, "invited-teacher")}).status_code == 204
    assert client.post("/api/auth/login", json={"username": "invited-teacher", "password": "invited teacher password"}).status_code == 204
    assert client.get("/api/auth/me").json()["organization"] == {"id": organization_id, "role": "teacher"}
