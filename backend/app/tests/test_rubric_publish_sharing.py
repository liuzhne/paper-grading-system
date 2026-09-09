"""发布时一次选定分享范围（D-029）。

导入默认「仅自己可见」，而可见范围此前**完全不可改**：`PATCH /rubrics` 在非草稿
状态返回 409，且 `RubricUpdate` 里根本没有 `visibility` 字段。所以这是新能力。

范围与编译产物在同一次 `publish` 里原子生效，发布后一起冻结——与「已发布模板请先
复制为新版本后再编辑」保持同一条语义，不引入第二种可变性。

**这里放宽了一条现有规则**：组织可见此前需 `org_admin`，改为创建者本人即可。否则
「默认仅自己可见」会让每份标准都要走一次审批才能给同事用。跨组织的「所有人」维持
`platform_admin`。
"""

from backend.app.db import models
from backend.app.tests.test_ops_permissions import _login_org_admin
from backend.app.tests.test_ops_permissions import _login_platform_admin


def _publishable(client):
    """走完整生命周期，建一份**可发布**的草稿，返回 (rubric_id, compilation_id)。

    `draft -> recompile -> 逐条规则审核 -> submit-review -> publish` 是既有流程，
    不能因为要测分享范围就绕过它：没有已确认规则的标准提交审核会被阻断项挡下，
    那是设计如此。
    """
    criterion = {
        "code": "T01",
        "name": "需求分析",
        "max_score": 20,
        "description": "需求分析应完整、明确并可验证。",
        "evidence_hints": ["需求分析"],
        "deduction_rules": [],
        "scoring_mode": "deductive",
    }
    created = client.post(
        "/api/rubrics",
        json={
            "name": "分享范围测试",
            "version": "v1",
            "total_score": 20,
            "criteria": [criterion],
        },
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]

    before = client.get("/api/rubrics/%s/execution-draft" % rubric_id).json()
    recovered = client.post(
        "/api/rubrics/%s/recompile" % rubric_id,
        json={
            "supersedes_compilation_id": before["active_compilation"]["id"],
            "version": "v2",
            "reason": "确认扣分规则",
            "criteria": [
                {**criterion, "deduction_rules": ["核心需求缺失，扣 6 分"]}
            ],
        },
    )
    assert recovered.status_code == 200, recovered.text

    active = client.get("/api/rubrics/%s/execution-draft" % rubric_id).json()[
        "active_compilation"
    ]
    for rule in active["rules"]:
        code = rule["rule_code"]
        assert client.post(
            "/api/rubrics/%s/rules/%s/submit-review" % (rubric_id, code),
            json={"reason": "提交"},
        ).status_code == 200
        assert client.post(
            "/api/rubrics/%s/rules/%s/approve" % (rubric_id, code),
            json={"reason": "确认"},
        ).status_code == 200

    submitted = client.post("/api/rubrics/%s/submit-review" % rubric_id)
    assert submitted.status_code == 200, submitted.text
    return rubric_id, active["id"]


def _visibility(client, rubric_id):
    with client.session_factory() as session:
        return session.get(models.Rubric, rubric_id).visibility


def test_publishing_without_a_scope_keeps_the_current_one(client):
    """不传范围就沿用导入时的——扩大范围必须是显式动作。"""
    rubric_id, compilation_id = _publishable(client)

    response = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id},
    )

    assert response.status_code == 200, response.text
    assert _visibility(client, rubric_id) == "private"


def test_the_creator_can_share_to_their_organization_at_publish(client):
    """D-029 放宽的那条：创建者本人即可分享到本组织，不必找 org_admin。"""
    rubric_id, compilation_id = _publishable(client)

    response = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id, "visibility": "organization"},
    )

    assert response.status_code == 200, response.text
    assert _visibility(client, rubric_id) == "organization"


def test_scope_and_compilation_take_effect_together(client):
    """一次请求两件事：发布哪一份、分享给谁。"""
    rubric_id, compilation_id = _publishable(client)

    client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id, "visibility": "organization"},
    )

    with client.session_factory() as session:
        rubric = session.get(models.Rubric, rubric_id)
        assert rubric.status == "published"
        assert rubric.visibility == "organization"


def test_an_unknown_scope_is_rejected(client):
    rubric_id, compilation_id = _publishable(client)

    response = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id, "visibility": "everyone"},
    )

    assert response.status_code == 422


def test_cross_organization_sharing_still_needs_a_platform_admin(client, monkeypatch):
    """「所有人」跨组织生效，影响面与本组织不同，维持 `platform_admin`。"""
    _login_org_admin(client, monkeypatch, "share-org-admin")
    rubric_id, compilation_id = _publishable(client)

    response = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id, "visibility": "system"},
    )

    assert response.status_code == 403


def test_a_platform_admin_can_share_to_everyone(client, monkeypatch):
    _login_platform_admin(client, monkeypatch)
    rubric_id, compilation_id = _publishable(client)

    response = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": compilation_id, "visibility": "system"},
    )

    assert response.status_code == 200, response.text
    assert _visibility(client, rubric_id) == "system"
