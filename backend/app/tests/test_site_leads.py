"""官网试用与部署评估申请（docs/官网申请落库改造方案.md）。

公开提交在开发模式与鉴权模式下行为一致（不需要登录）；管理接口的权限判定只有
在启用鉴权时才测得出来，那部分复用运维权限用例的登录助手。
"""

from datetime import timedelta
import logging

from sqlalchemy import func
from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models
from backend.app.db.models import utcnow
from backend.app.services import site_leads
from backend.app.tests.test_ops_permissions import _login_org_admin
from backend.app.tests.test_ops_permissions import _login_platform_admin


EMAIL = "Teacher@Example.EDU.cn"


def _enable(monkeypatch, **limits):
    monkeypatch.setattr(settings, "SITE_LEADS_ENABLED", True)
    for name, value in limits.items():
        monkeypatch.setattr(settings, name, value)


def _trial(client, email=EMAIL, **extra):
    return client.post("/api/site/leads", json={"kind": "trial", "email": email, **extra})


def _deploy(client, email="dean@example.edu.cn", organization="某大学 计算机学院", note="", **extra):
    return client.post(
        "/api/site/leads",
        json={
            "kind": "deploy_assessment",
            "email": email,
            "organization_name": organization,
            "note": note,
            **extra,
        },
    )


def _rows(client):
    with client.session_factory() as session:
        return session.scalars(select(models.SiteLead).order_by(models.SiteLead.created_at)).all()


# ---------- 开关 ----------


def test_disabled_by_default_the_endpoint_does_not_exist(client):
    response = _trial(client)

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "site_leads_disabled"
    assert "siteLeads" not in client.get("/").text
    assert _rows(client) == []


def test_enabled_site_pages_carry_the_switch_and_retention(client, monkeypatch):
    _enable(monkeypatch, SITE_LEADS_RETENTION_DAYS=90)

    for path in ("/", "/about"):
        html = client.get(path).text
        assert '"siteLeads": true' in html, path
        assert '"siteLeadsRetentionDays": 90' in html, path
    # 工作台入口不需要这个开关。
    assert "siteLeads" not in client.get("/login").text
    assert "siteLeads" not in client.get("/workbench/").text


# ---------- 提交 ----------


def test_trial_stores_only_the_normalized_email(client, monkeypatch):
    _enable(monkeypatch)

    response = _trial(client, email="  " + EMAIL + "  ", organization_name="不该存", note="不该存")

    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    (lead,) = _rows(client)
    assert lead.kind == "trial"
    assert lead.email == "teacher@example.edu.cn"
    assert lead.organization_name is None and lead.note is None
    assert lead.source_path == "/"
    assert lead.status == "new" and lead.submit_count == 1


def test_deploy_assessment_stores_school_and_note(client, monkeypatch):
    _enable(monkeypatch)

    response = _deploy(client, note="三门课，约 200 份\n下学期开始")

    assert response.status_code == 202
    (lead,) = _rows(client)
    assert lead.kind == "deploy_assessment"
    assert lead.organization_name == "某大学 计算机学院"
    assert lead.note == "三门课，约 200 份\n下学期开始"
    assert lead.source_path == "/about"


def test_invalid_fields_get_chinese_messages_per_field(client, monkeypatch):
    _enable(monkeypatch)

    response = _deploy(client, email="not-an-email", organization="  ", note="长" * 501)

    assert response.status_code == 422
    fields = response.json()["detail"]["fields"]
    assert fields == {
        "email": "请填写有效的邮箱",
        "organization_name": "请填写学校与院系",
        "note": "备注最多 500 个字",
    }
    assert _rows(client) == []


def test_control_characters_are_stripped(client, monkeypatch):
    _enable(monkeypatch)

    assert _deploy(client, organization="某大学\x00\x07 学院").status_code == 202

    assert _rows(client)[0].organization_name == "某大学 学院"


def test_honeypot_is_accepted_but_dropped(client, monkeypatch, caplog):
    _enable(monkeypatch)

    with caplog.at_level(logging.INFO):
        response = _trial(client, website="https://spam.example")

    assert response.status_code == 202
    assert response.json() == {"status": "received"}
    assert _rows(client) == []
    assert "site_lead_honeypot" in caplog.text


def test_raw_ip_is_never_stored(client, monkeypatch):
    _enable(monkeypatch)

    _trial(client)

    (lead,) = _rows(client)
    # TestClient 的客户端地址是 "testclient"。
    assert lead.ip_hash == site_leads.hash_ip("testclient")
    assert lead.ip_hash != "testclient" and len(lead.ip_hash) == 64


def test_logs_never_contain_the_email(client, monkeypatch, caplog):
    _enable(monkeypatch)

    with caplog.at_level(logging.INFO):
        _trial(client)
        _deploy(client)

    assert "site_lead_received" in caplog.text
    assert "teacher@example.edu.cn" not in caplog.text.lower()
    assert "dean@example.edu.cn" not in caplog.text


# ---------- 去重 ----------


def test_resubmitting_within_30_days_merges_into_one_row(client, monkeypatch):
    _enable(monkeypatch)

    _deploy(client, organization="旧院系")
    second = _deploy(client, organization="新院系", note="补充说明")

    assert second.status_code == 202
    (lead,) = _rows(client)
    assert lead.submit_count == 2
    assert lead.organization_name == "新院系"
    assert lead.note == "补充说明"


def test_closed_or_old_applications_start_a_new_row(client, monkeypatch):
    _enable(monkeypatch)
    _trial(client)
    with client.session_factory() as session:
        lead = session.scalar(select(models.SiteLead))
        lead.status = "closed"
        session.commit()

    _trial(client)

    assert len(_rows(client)) == 2

    with client.session_factory() as session:
        for lead in session.scalars(select(models.SiteLead)):
            lead.status = "new"
            lead.last_submitted_at = utcnow() - timedelta(days=31)
            lead.ip_hash = None
        session.commit()

    _trial(client)

    assert len(_rows(client)) == 3


# ---------- 限流 ----------


def test_per_ip_hourly_limit_returns_429_with_retry_after(client, monkeypatch):
    _enable(monkeypatch, SITE_LEADS_PER_IP_HOURLY=2)

    assert _trial(client, email="a1@example.cn").status_code == 202
    assert _trial(client, email="a2@example.cn").status_code == 202
    response = _trial(client, email="a3@example.cn")

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "per_ip_hourly"
    assert 1 <= int(response.headers["Retry-After"]) <= 3600
    assert len(_rows(client)) == 2


def test_per_ip_daily_limit(client, monkeypatch):
    _enable(monkeypatch, SITE_LEADS_PER_IP_DAILY=2)
    _trial(client, email="b1@example.cn")
    _trial(client, email="b2@example.cn")
    with client.session_factory() as session:
        for lead in session.scalars(select(models.SiteLead)):
            lead.last_submitted_at = utcnow() - timedelta(hours=2)
        session.commit()

    response = _trial(client, email="b3@example.cn")

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "per_ip_daily"


def test_global_daily_limit_caps_new_rows(client, monkeypatch, caplog):
    _enable(monkeypatch, SITE_LEADS_DAILY_LIMIT=1)
    _trial(client, email="c1@example.cn")
    # 换一个来源，排除按 IP 限流的干扰。
    with client.session_factory() as session:
        session.scalar(select(models.SiteLead)).ip_hash = "other"
        session.commit()

    with caplog.at_level(logging.WARNING):
        response = _trial(client, email="c2@example.cn")

    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "daily_limit"
    assert "site_lead_daily_limit_reached" in caplog.text
    # 已有申请再提交只是合并，不受日上限影响。
    with client.session_factory() as session:
        session.scalar(select(models.SiteLead)).ip_hash = None
        session.commit()
    assert _trial(client, email="c1@example.cn").status_code == 202


# ---------- 管理接口 ----------


def test_admin_endpoints_require_a_platform_admin(client, monkeypatch):
    _enable(monkeypatch)
    _trial(client)
    lead_id = _rows(client)[0].id

    _login_org_admin(client, monkeypatch, "orgadmin")
    assert client.get("/api/site/leads").status_code == 403
    assert client.patch(f"/api/site/leads/{lead_id}", json={"status": "spam"}).status_code == 403
    assert client.delete(f"/api/site/leads/{lead_id}").status_code == 403

    client.post("/api/auth/logout")
    assert client.get("/api/site/leads").status_code == 401
    # 公开提交在启用鉴权后仍然不需要登录。
    assert _trial(client, email="anon@example.cn").status_code == 202


def test_platform_admin_lists_updates_and_deletes(client, monkeypatch):
    _enable(monkeypatch)
    _trial(client)
    _deploy(client)
    _login_platform_admin(client, monkeypatch)

    page = client.get("/api/site/leads?status=new").json()
    assert page["total"] == 2 and page["new_count"] == 2
    deploy = next(item for item in page["items"] if item["kind"] == "deploy_assessment")
    assert deploy["organization_name"] == "某大学 计算机学院"
    assert "ip_hash" not in deploy

    updated = client.patch(f"/api/site/leads/{deploy['id']}", json={"status": "contacted"})
    assert updated.status_code == 200
    assert updated.json()["status"] == "contacted" and updated.json()["handled_at"]
    assert client.get("/api/site/leads").json()["new_count"] == 1
    assert client.get("/api/site/leads?status=contacted").json()["total"] == 1
    assert client.patch(f"/api/site/leads/{deploy['id']}", json={"status": "weird"}).status_code == 422

    assert client.delete(f"/api/site/leads/{deploy['id']}").status_code == 204
    assert client.delete(f"/api/site/leads/{deploy['id']}").status_code == 404
    assert client.get("/api/site/leads").json()["total"] == 1

    with client.session_factory() as session:
        handled = session.scalars(
            select(models.AuditLog).where(models.AuditLog.event_type.like("site_lead.%"))
        ).all()
    assert [event.event_type for event in handled] == ["site_lead.status_changed", "site_lead.deleted"]
    for event in handled:
        assert event.actor_id is not None
        # 审计里不记邮箱。
        assert "dean@example.edu.cn" not in str(event.event_metadata)


# ---------- 到期清理 ----------


def test_expired_applications_are_purged_once_a_day(client, monkeypatch):
    _enable(monkeypatch, SITE_LEADS_RETENTION_DAYS=180)
    _trial(client, email="old@example.cn")
    _trial(client, email="fresh@example.cn")
    with client.session_factory() as session:
        old = session.scalar(select(models.SiteLead).where(models.SiteLead.email == "old@example.cn"))
        old.last_submitted_at = utcnow() - timedelta(days=181)
        session.commit()

    now = utcnow()
    assert site_leads.purge_expired_if_due(client.session_factory, now) == 1
    assert [lead.email for lead in _rows(client)] == ["fresh@example.cn"]
    # 同一天再跑不动手。
    assert site_leads.purge_expired_if_due(client.session_factory, now + timedelta(hours=3)) is None
    assert site_leads.purge_expired_if_due(client.session_factory, now + timedelta(days=1, minutes=1)) == 0


def test_global_sweep_runs_the_purge(client, monkeypatch):
    from backend.app.services.work_queue import sweep

    _enable(monkeypatch)
    _trial(client, email="stale@example.cn")
    with client.session_factory() as session:
        session.scalar(select(models.SiteLead)).last_submitted_at = utcnow() - timedelta(days=400)
        session.commit()

    sweep.run_global_sweep(client.session_factory)

    with client.session_factory() as session:
        assert session.scalar(select(func.count(models.SiteLead.id))) == 0


def test_a_failing_purge_does_not_break_the_sweep(client, monkeypatch, caplog):
    from backend.app.services.work_queue import sweep

    def boom(*_args, **_kwargs):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(site_leads, "purge_expired_if_due", boom)

    with caplog.at_level(logging.ERROR):
        sweep.run_global_sweep(client.session_factory)

    assert "site_leads_purge_failed" in caplog.text
