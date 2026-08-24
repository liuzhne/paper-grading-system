from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


def _login(client, monkeypatch, username):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "r" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite_only")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)
    email = "%s@example.test" % username
    password = "%s correct password" % username
    assert client.post("/api/auth/login", json={"username": "admin", "password": "bootstrap-admin-password"}).status_code == 204
    default_org_id = client.get("/api/organizations").json()[0]["id"]
    invitation = client.post(f"/api/organizations/{default_org_id}/members", json={"email": email, "role": "member"})
    assert invitation.status_code == 201, invitation.text
    assert client.post("/api/auth/register", json={"username": username, "email": email, "display_name": username, "password": password, "invitation_token": invitation.json()["invitation_token"]}).status_code == 201
    with client.session_factory() as session:
        user = session.scalar(select(models.User).where(models.User.username == username))
        organization = models.Organization(name="%s workspace" % username, created_by=user.id)
        session.add(organization)
        session.flush()
        session.add(models.OrganizationMember(organization_id=organization.id, user_id=user.id, role="org_admin"))
        user_id = user.id
        session.commit()
        organization_id = organization.id
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 204
    assert client.post("/api/auth/organization-context", json={"organization_id": organization_id}).status_code == 200
    return user_id, organization_id


def test_rubric_list_enforces_system_organization_and_private_visibility(client, monkeypatch):
    owner_id, owner_org_id = _login(client, monkeypatch, "rubric-owner")
    with client.session_factory() as session:
        system = models.Rubric(name="system rubric", version="v1", total_score=100, visibility="system")
        shared = models.Rubric(name="shared rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id, visibility="organization")
        private = models.Rubric(name="private rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id, visibility="private")
        session.add_all([system, shared, private])
        session.commit()
        private_id = private.id

    response = client.get("/api/rubrics")
    assert response.status_code == 200, response.text
    assert {item["name"] for item in response.json()} == {"system rubric", "shared rubric", "private rubric"}

    client.post("/api/auth/logout")
    other_id, _other_org_id = _login(client, monkeypatch, "rubric-other")
    with client.session_factory() as session:
        session.add(models.OrganizationMember(organization_id=owner_org_id, user_id=other_id, role="teacher"))
        session.commit()
    response = client.get("/api/rubrics", headers={"X-Organization-ID": owner_org_id})
    assert {item["name"] for item in response.json()} == {"system rubric", "shared rubric"}
    assert client.get("/api/rubrics/%s" % private_id, headers={"X-Organization-ID": owner_org_id}).status_code == 404
    assert client.patch(
        "/api/rubrics/%s" % private_id,
        headers={"X-Organization-ID": owner_org_id},
        json={"description": "stolen"},
    ).status_code == 404


def test_create_private_rubric_inherits_current_organization(client, monkeypatch):
    owner_id, owner_org_id = _login(client, monkeypatch, "create-rubric-owner")
    response = client.post(
        "/api/rubrics",
        json={
            "name": "my private rubric",
            "version": "v1",
            "visibility": "private",
            "total_score": 100,
            "criteria": [{"code": "C1", "name": "criterion", "max_score": 100}],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["visibility"] == "private"
    with client.session_factory() as session:
        rubric = session.get(models.Rubric, response.json()["id"])
        assert (rubric.owner_id, rubric.organization_id, rubric.visibility) == (
            owner_id,
            owner_org_id,
            "private",
        )
        version = session.scalar(
            select(models.RubricVersion).where(models.RubricVersion.rubric_id == rubric.id)
        )
        assert version.organization_id == owner_org_id


def test_same_name_and_version_are_allowed_in_separate_organization_scopes(client):
    with client.session_factory() as session:
        first_org = models.Organization(name="first org")
        second_org = models.Organization(name="second org")
        session.add_all([first_org, second_org])
        session.flush()
        session.add_all(
            [
                models.Rubric(name="shared name", version="v1", total_score=100, organization_id=first_org.id, visibility="organization"),
                models.Rubric(name="shared name", version="v1", total_score=100, organization_id=second_org.id, visibility="organization"),
            ]
        )
        session.commit()
