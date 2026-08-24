from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


def _invite_only_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "a" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite_only")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)


def _login_admin(client, monkeypatch):
    _invite_only_auth(monkeypatch)
    assert client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "bootstrap-admin-password"},
    ).status_code == 204
    return client.get("/api/organizations").json()[0]["id"]


def _create_invitation(client, organization_id, email, role="member"):
    response = client.post(
        f"/api/organizations/{organization_id}/members",
        json={"email": email, "role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()["invitation_token"]


def test_invitation_bound_registration_allows_immediate_login_without_email_verification(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "teacher-a@example.test", "teacher")

    registered = client.post(
        "/api/auth/register",
        json={
            "username": "teacher-a",
            "email": "teacher-a@example.test",
            "display_name": "Teacher A",
            "password": "correct horse battery staple",
            "invitation_token": invitation_token,
        },
    )
    assert registered.status_code == 201, registered.text
    assert "token" not in registered.json()

    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == "teacher-a"))
        assert user is not None
        assert user.email_verified_at is None

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
    assert me.json()["organization"] == {"id": organization_id, "role": "teacher"}


def test_registration_rejects_non_invited_or_mismatched_email(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "invited@example.test")

    mismatched = client.post(
        "/api/auth/register",
        json={
            "username": "wrong-email",
            "email": "other@example.test",
            "display_name": "Wrong Email",
            "password": "correct horse battery staple",
            "invitation_token": invitation_token,
        },
    )
    assert mismatched.status_code == 400

    uninvited = client.post(
        "/api/auth/register",
        json={
            "username": "uninvited",
            "email": "uninvited@example.test",
            "display_name": "Uninvited",
            "password": "correct horse battery staple",
        },
    )
    assert uninvited.status_code == 403


def test_registration_page_can_resolve_only_an_active_invitation(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "new-teacher@example.test", "teacher")

    resolved = client.post("/api/auth/invitations/resolve", json={"token": invitation_token})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() == {
        "organization_id": organization_id,
        "organization_name": "Default Organization",
        "email": "new-teacher@example.test",
        "role": "teacher",
    }

    unknown = client.post("/api/auth/invitations/resolve", json={"token": "not-a-real-token"})
    assert unknown.status_code == 400

    assert client.post(
        "/api/auth/register",
        json={
            "username": "new-teacher",
            "email": "new-teacher@example.test",
            "display_name": "New Teacher",
            "password": "correct horse battery staple",
            "invitation_token": invitation_token,
        },
    ).status_code == 201
    consumed = client.post("/api/auth/invitations/resolve", json={"token": invitation_token})
    assert consumed.status_code == 400


def test_logout_revokes_server_session_and_current_principal_requires_membership(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "teacher-b@example.test")
    client.post(
        "/api/auth/register",
        json={
            "username": "teacher-b",
            "email": "teacher-b@example.test",
            "display_name": "Teacher B",
            "password": "another correct horse battery staple",
            "invitation_token": invitation_token,
        },
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
    assert invited.json()["invitation_token"]
    with client.session_factory() as session:
        invitation = session.scalar(select(models.OrganizationInvitation))
        assert invitation is not None
        invite_token = invitation.token
    assert invited.json()["invitation_token"] == invite_token

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
    assert client.post("/api/auth/login", json={"username": "invited-teacher", "password": "invited teacher password"}).status_code == 204
    assert client.get("/api/auth/me").json()["organization"] == {"id": organization_id, "role": "teacher"}


def test_email_verification_and_email_reset_request_endpoints_are_removed(client, monkeypatch):
    _invite_only_auth(monkeypatch)
    assert client.post("/api/auth/verify-email", json={"token": "unused"}).status_code == 404
    assert client.post("/api/auth/password-reset/request", json={"email": "user@example.test"}).status_code == 404


def test_org_admin_issues_single_active_reset_token_and_user_confirms_password_twice(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "reset-user@example.test")
    registered = client.post(
        "/api/auth/register",
        json={
            "username": "reset-user",
            "email": "reset-user@example.test",
            "display_name": "Reset User",
            "password": "initial secure password",
            "invitation_token": invitation_token,
        },
    )
    assert registered.status_code == 201, registered.text
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == "reset-user"))
        assert user is not None
        user_id = user.id

    first = client.post(f"/api/organizations/{organization_id}/members/{user_id}/password-reset-token")
    assert first.status_code == 201, first.text
    second = client.post(f"/api/organizations/{organization_id}/members/{user_id}/password-reset-token")
    assert second.status_code == 201, second.text
    assert first.json()["reset_token"] != second.json()["reset_token"]

    stale = client.post(
        "/api/auth/password-reset/confirm",
        json={
            "token": first.json()["reset_token"],
            "password": "new secure password",
            "password_confirmation": "new secure password",
        },
    )
    assert stale.status_code == 400
    mismatched_passwords = client.post(
        "/api/auth/password-reset/confirm",
        json={
            "token": second.json()["reset_token"],
            "password": "new secure password",
            "password_confirmation": "different secure password",
        },
    )
    assert mismatched_passwords.status_code == 422
    reset = client.post(
        "/api/auth/password-reset/confirm",
        json={
            "token": second.json()["reset_token"],
            "password": "new secure password",
            "password_confirmation": "new secure password",
        },
    )
    assert reset.status_code == 204, reset.text
    assert client.post(
        "/api/auth/login",
        json={"username": "reset-user", "password": "new secure password"},
    ).status_code == 204


def test_non_admin_cannot_issue_password_reset_token(client, monkeypatch):
    organization_id = _login_admin(client, monkeypatch)
    invitation_token = _create_invitation(client, organization_id, "member@example.test")
    assert client.post(
        "/api/auth/register",
        json={
            "username": "member",
            "email": "member@example.test",
            "display_name": "Member",
            "password": "member secure password",
            "invitation_token": invitation_token,
        },
    ).status_code == 201
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == "member"))
        assert user is not None
        user_id = user.id
    assert client.post("/api/auth/login", json={"username": "member", "password": "member secure password"}).status_code == 204
    denied = client.post(f"/api/organizations/{organization_id}/members/{user_id}/password-reset-token")
    assert denied.status_code == 403
