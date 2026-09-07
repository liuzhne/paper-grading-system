"""运维与门禁端点的角色与组织范围收紧（前端 v2 计划 §2.1）。

这些用例必须在**启用鉴权**下运行：仓库默认的 `AUTH_ENABLED=False`
是显式开发模式，守卫会放行，覆盖不到真实权限判定。
"""

from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


def _enable_auth(monkeypatch):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "d" * 48)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite_only")
    monkeypatch.setattr(settings, "AUTH_COOKIE_SECURE", False)


def _login_platform_admin(client, monkeypatch):
    _enable_auth(monkeypatch)
    assert (
        client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "bootstrap-admin-password"},
        ).status_code
        == 204
    )


def _login_org_admin(client, monkeypatch, username):
    """建一个只在自己组织里是 org_admin、平台角色非管理员的用户。"""
    _login_platform_admin(client, monkeypatch)
    email = "%s@example.test" % username
    password = "%s correct password" % username
    default_org_id = client.get("/api/organizations").json()[0]["id"]
    invitation = client.post(
        f"/api/organizations/{default_org_id}/members",
        json={"email": email, "role": "member"},
    )
    assert invitation.status_code == 201, invitation.text
    assert (
        client.post(
            "/api/auth/register",
            json={
                "username": username,
                "email": email,
                "display_name": username,
                "password": password,
                "invitation_token": invitation.json()["invitation_token"],
            },
        ).status_code
        == 201
    )
    with client.session_factory() as session:
        user = session.scalar(
            select(models.User).where(models.User.username == username)
        )
        organization = models.Organization(
            name="%s workspace" % username, created_by=user.id
        )
        session.add(organization)
        session.flush()
        session.add(
            models.OrganizationMember(
                organization_id=organization.id, user_id=user.id, role="org_admin"
            )
        )
        user_id = user.id
        session.commit()
        organization_id = organization.id
    client.post("/api/auth/logout")
    assert (
        client.post(
            "/api/auth/login", json={"username": username, "password": password}
        ).status_code
        == 204
    )
    assert (
        client.post(
            "/api/auth/organization-context",
            json={"organization_id": organization_id},
        ).status_code
        == 200
    )
    return user_id, organization_id


def _seed_batch_job(session, *, organization_id, owner_id, name):
    rubric = models.Rubric(
        name="%s rubric" % name,
        version="v1",
        total_score=100,
        owner_id=owner_id,
        organization_id=organization_id,
    )
    session.add(rubric)
    session.flush()
    batch = models.GradingBatch(
        name=name,
        rubric_id=rubric.id,
        owner_id=owner_id,
        organization_id=organization_id,
    )
    session.add(batch)
    session.flush()
    session.add(
        models.BatchScoringJob(
            grading_batch_id=batch.id,
            status="running",
            generation=1,
            observation_policy={},
            observation_policy_hash="0" * 64,
        )
    )
    return batch.id


def test_org_admin_cannot_read_platform_ops_readiness(client, monkeypatch):
    _login_org_admin(client, monkeypatch, "ops-org-admin")

    assert client.get("/api/system/ops-readiness").status_code == 403


def test_platform_admin_reads_ops_readiness(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)

    response = client.get("/api/system/ops-readiness")

    assert response.status_code == 200
    assert response.json()["schema_version"] == "ops-readiness@1"


def test_org_admin_cannot_run_platform_llm_check(client, monkeypatch):
    _login_org_admin(client, monkeypatch, "llm-org-admin")

    assert client.get("/api/system/llm-check").status_code == 403


def test_org_admin_cannot_reach_release_gates(client, monkeypatch):
    _login_org_admin(client, monkeypatch, "gate-org-admin")

    assert client.get("/api/release-gates/profiles").status_code == 403


def test_organization_readiness_counts_only_the_current_organization(
    client, monkeypatch
):
    """先过滤再计数：别的组织的批任务不得进入本组织的指标。"""
    owner_id, owner_org_id = _login_org_admin(client, monkeypatch, "readiness-owner")
    with client.session_factory() as session:
        _seed_batch_job(
            session,
            organization_id=owner_org_id,
            owner_id=owner_id,
            name="owner batch",
        )
        session.commit()

    mine = client.get("/api/system/organization-readiness")
    assert mine.status_code == 200
    payload = mine.json()
    assert payload["schema_version"] == "organization-readiness@1"
    assert payload["organization_id"] == owner_org_id
    assert payload["signals"]["batch_jobs"]["active_count"] == 1

    client.post("/api/auth/logout")
    other_id, other_org_id = _login_org_admin(client, monkeypatch, "readiness-other")
    assert other_org_id != owner_org_id

    theirs = client.get("/api/system/organization-readiness").json()
    assert theirs["organization_id"] == other_org_id
    assert theirs["signals"]["batch_jobs"]["active_count"] == 0


def test_organization_readiness_excludes_platform_host_facts(client, monkeypatch):
    """磁盘 / 数据库 / 部署安全属于宿主事实，不下发给组织管理员。"""
    _login_org_admin(client, monkeypatch, "readiness-scope")

    signals = client.get("/api/system/organization-readiness").json()["signals"]

    assert set(signals) == {"batch_jobs"}


def test_capabilities_matches_the_guards_for_an_org_admin(client, monkeypatch):
    """能力表与守卫必须同判据，否则导航与可访问性错位。"""
    _login_org_admin(client, monkeypatch, "capability-org-admin")

    abilities = client.get("/api/system/capabilities").json()["abilities"]

    assert abilities["view_platform_ops"] is False
    assert abilities["view_organization_ops"] is True


def test_legacy_spa_gates_the_platform_llm_check_entry():
    """旧 SPA 在并存期必须服从新的服务端守卫（前端 v2 计划 §8.3）。

    `/system/llm-check` 已收紧为平台管理员专用。旧页面那个「测试模型连接」
    按钮对所有登录用户可见，若不同步隐藏并处理 403，非平台管理员点击后只会
    拿到一个无从下手的错误。
    """
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[3]
        / "frontend"
        / "web"
        / "assets"
        / "app.js"
    ).read_text(encoding="utf-8")

    assert 'document.querySelector("#llm-check-btn")' in source
    assert 'llmCheckButton.classList.toggle("hidden", !isPlatformAdmin)' in source
    assert "error.status === 403" in source
    # BYOK 自测走另一条所有者校验路径，提示必须把用户指过去。
    assert "我的 AI 连接" in source
