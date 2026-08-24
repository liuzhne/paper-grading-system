from sqlalchemy import select

from backend.app.core.config import settings
from backend.app.db import models


def _login_public_user(client, monkeypatch, username):
    monkeypatch.setattr(settings, "AUTH_ENABLED", True)
    monkeypatch.setattr(settings, "AUTH_PASSWORD", "bootstrap-admin-password")
    monkeypatch.setattr(settings, "AUTH_SECRET", "d" * 48)
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


def test_batches_are_filtered_by_current_organization_and_cross_org_ids_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="owner batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.commit()
        batch_id = batch.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "other")
    assert client.get("/api/batches").json() == []
    assert client.get("/api/batches/%s/summary" % batch_id).status_code == 404


def test_papers_are_filtered_by_current_organization_and_cross_org_ids_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "paper-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="paper tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="paper owner batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.flush()
        paper = models.Paper(batch_id=batch.id, owner_id=owner_id, organization_id=owner_org_id, file_name="private.docx", file_path="private.docx")
        session.add(paper)
        session.commit()
        paper_id = paper.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "paper-other")
    assert client.get("/api/papers").json() == []
    assert client.get("/api/papers/%s" % paper_id).status_code == 404
    assert client.get("/api/papers/%s/parsed" % paper_id).status_code == 404


def test_cross_organization_scoring_run_exports_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "run-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="run tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="run owner batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.flush()
        paper = models.Paper(batch_id=batch.id, owner_id=owner_id, organization_id=owner_org_id, file_name="private.docx", file_path="private.docx")
        session.add(paper)
        session.flush()
        run = models.ScoringRun(paper_id=paper.id, rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(run)
        session.commit()
        run_id = run.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "run-other")
    assert client.get("/api/scoring-runs/%s/export.json" % run_id).status_code == 404
    assert client.get("/api/scoring-runs/%s/report" % run_id).status_code == 404


def test_cross_organization_batch_operations_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "batch-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="batch tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="private batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.commit()
        batch_id = batch.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "batch-other")
    for suffix in ("", "/score", "/start", "/ranking", "/drift", "/review-sample", "/drift-monitor"):
        method = client.post if suffix in {"/score", "/start"} else client.get
        assert method("/api/batches/%s%s" % (batch_id, suffix)).status_code == 404
    assert client.patch("/api/batches/%s" % batch_id, json={"name": "stolen"}).status_code == 404


def test_cross_organization_legacy_scoring_operations_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "score-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="score tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="score owner batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.flush()
        paper = models.Paper(batch_id=batch.id, owner_id=owner_id, organization_id=owner_org_id, file_name="private.docx", file_path="private.docx")
        session.add(paper)
        session.flush()
        run = models.ScoringRun(paper_id=paper.id, rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(run)
        session.commit()
        paper_id, batch_id, run_id = paper.id, batch.id, run.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "score-other")
    assert client.get("/api/scoring-runs").json() == []
    assert client.get("/api/scoring-runs", params={"paper_id": paper_id}).json() == []
    assert client.get("/api/scoring-runs", params={"batch_id": batch_id}).json() == []
    assert client.post("/api/papers/%s/score" % paper_id).status_code == 404
    assert client.get("/api/scoring-runs/%s" % run_id).status_code == 404
    assert client.post("/api/scoring-runs/%s/retry" % run_id).status_code == 404
    assert client.get("/api/scoring-runs/%s/items" % run_id).status_code == 404
    assert client.patch("/api/score-items/not-an-item", json={"final_score": 0, "reason": "steal"}).status_code == 404
    assert client.post("/api/scoring-runs/%s/review" % run_id, json={"reason": "steal"}).status_code == 404
    assert client.get("/api/scoring-runs/%s/review-logs" % run_id).status_code == 404


def test_cross_organization_batch_scoring_jobs_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "job-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="job tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="job owner batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.flush()
        job = models.BatchScoringJob(
            grading_batch_id=batch.id,
            generation=1,
            observation_policy={},
            observation_policy_hash="0" * 64,
        )
        session.add(job)
        session.commit()
        batch_id, job_id = batch.id, job.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "job-other")
    assert client.post(
        "/api/batches/%s/score-jobs" % batch_id,
        json={"observation_policy": {}},
    ).status_code == 404
    assert client.get("/api/batches/%s/score-jobs/latest" % batch_id).status_code == 404
    assert client.get("/api/batch-scoring-jobs/%s" % job_id).status_code == 404
    assert client.post("/api/batch-scoring-jobs/%s/cancel" % job_id).status_code == 404
    assert client.post("/api/batch-scoring-jobs/%s/retry" % job_id).status_code == 404
    assert client.post("/api/batch-scoring-jobs/%s/run" % job_id).status_code == 404


def test_cross_organization_v2_resources_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "v2-owner")
    digest = "0" * 64
    with client.session_factory() as session:
        batch = models.EvaluationBatch(
            owner_id=owner_id,
            organization_id=owner_org_id,
            name="private v2 batch",
            rubric_id="private-rubric",
            rubric_version_id="private-version",
            business_profile_key="technical_proposal",
            business_profile_version="technical-proposal-profile@1",
        )
        session.add(batch)
        session.flush()
        submission = models.Submission(
            organization_id=owner_org_id,
            evaluation_batch_id=batch.id,
            source_artifact_hash=digest,
            source_artifact_ref="blob:sha256:" + digest,
            file_name="private.docx",
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            byte_length=1,
            submission_metadata={},
        )
        session.add(submission)
        session.flush()
        snapshot = models.DocumentSnapshot(
            submission_id=submission.id,
            schema_version="document-snapshot@1",
            business_profile_key="technical_proposal",
            business_profile_version="technical-proposal-profile@1",
            parser_version="test",
            normalizer_version="test",
            content_hash=digest,
            snapshot_hash=digest,
            snapshot_ref="snapshot:test",
            snapshot_payload={},
        )
        session.add(snapshot)
        session.flush()
        run = models.ScoringRun(
            submission_id=submission.id,
            document_snapshot_id=snapshot.id,
            rubric_id="private-rubric",
            owner_id=owner_id,
            organization_id=owner_org_id,
        )
        session.add(run)
        session.commit()
        batch_id, submission_id, run_id = batch.id, submission.id, run.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "v2-other")
    assert client.post(
        "/api/v2/submissions",
        data={"evaluation_batch_id": batch_id, "metadata_json": "{}"},
        files={"file": ("private.docx", b"x", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")},
    ).status_code == 404
    assert client.get("/api/v2/submissions/%s" % submission_id).status_code == 404
    assert client.get("/api/v2/submissions/%s/document-snapshot" % submission_id).status_code == 404
    assert client.post("/api/v2/submissions/%s/score" % submission_id, json={}).status_code == 404
    assert client.get("/api/v2/scoring-runs/%s" % run_id).status_code == 404
    assert client.get("/api/v2/scoring-runs/%s/review-logs" % run_id).status_code == 404
    assert client.get("/api/v2/scoring-runs/%s/export.json" % run_id).status_code == 404
    assert client.get("/api/v2/scoring-runs/%s/report" % run_id).status_code == 404
    assert client.get("/api/v2/scoring-runs/%s/export.xlsx" % run_id).status_code == 404


def test_cross_organization_calibration_anchors_are_hidden(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "anchor-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="anchor tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.commit()
        rubric_id = rubric.id

    client.post("/api/auth/logout")
    _login_public_user(client, monkeypatch, "anchor-other")
    assert client.get("/api/calibration/anchors", params={"rubric_id": rubric_id}).status_code == 404
    assert client.post(
        "/api/calibration/anchors",
        json={
            "rubric_id": rubric_id,
            "criterion_code": "private",
            "score": 1,
            "max_score": 1,
            "excerpt": "private",
        },
    ).status_code == 404


def test_organization_member_cannot_create_a_scoring_batch(client, monkeypatch):
    owner_id, owner_org_id = _login_public_user(client, monkeypatch, "role-owner")
    with client.session_factory() as session:
        rubric = models.Rubric(name="role tenant rubric", version="v1", total_score=100, owner_id=owner_id, organization_id=owner_org_id)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="admin-visible batch", rubric_id=rubric.id, owner_id=owner_id, organization_id=owner_org_id)
        session.add(batch)
        session.commit()
        rubric_id, batch_id = rubric.id, batch.id

    client.post("/api/auth/logout")
    member_id, _member_org_id = _login_public_user(client, monkeypatch, "ordinary-member")
    with client.session_factory() as session:
        session.add(models.OrganizationMember(organization_id=owner_org_id, user_id=member_id, role="member"))
        session.commit()

    response = client.post(
        "/api/batches",
        headers={"X-Organization-ID": owner_org_id},
        json={"name": "forbidden batch", "rubric_id": rubric_id},
    )
    assert response.status_code == 403

    with client.session_factory() as session:
        session.get(models.User, member_id).platform_role = "platform_admin"
        membership = session.scalar(
            select(models.OrganizationMember).where(
                models.OrganizationMember.organization_id == owner_org_id,
                models.OrganizationMember.user_id == member_id,
            )
        )
        session.delete(membership)
        session.commit()
    assert client.get(
        "/api/batches/%s" % batch_id,
        headers={"X-Organization-ID": owner_org_id},
    ).status_code == 200
