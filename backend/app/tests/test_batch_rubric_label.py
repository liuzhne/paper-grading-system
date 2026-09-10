"""批次列表要能显示绑定的评分标准（设计稿有这一列）。

`BatchRead` 只给了 `rubric_id`，前端拿不到可显示的内容——列表里只能是一串 UUID，
或者干脆不显示。**批次绑定哪个标准版本决定了它怎么判分**，这是评分任务页里仅次于
批次名的信息。

不让前端按 id 逐个去查 `/rubrics/{id}`：一页 20 个批次就是 20 次请求，而这份数据
本来就在同一个查询里。
"""

from backend.app.db import models


def _rubric(client, name="本科毕业论文评分标准", version="v3.1"):
    """走完整生命周期建一份**已发布**标准——建批次要求它已发布。"""
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
        json={"name": name, "version": version, "total_score": 20, "criteria": [criterion]},
    )
    assert created.status_code == 200, created.text
    rubric_id = created.json()["id"]

    before = client.get("/api/rubrics/%s/execution-draft" % rubric_id).json()
    assert client.post(
        "/api/rubrics/%s/recompile" % rubric_id,
        json={
            "supersedes_compilation_id": before["active_compilation"]["id"],
            "version": "%s-c" % version,
            "reason": "确认扣分规则",
            "criteria": [{**criterion, "deduction_rules": ["核心需求缺失，扣 6 分"]}],
        },
    ).status_code == 200

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

    assert client.post("/api/rubrics/%s/submit-review" % rubric_id).status_code == 200
    published = client.post(
        "/api/rubrics/%s/publish" % rubric_id,
        json={"compilation_id": active["id"], "reason": "发布"},
    )
    assert published.status_code == 200, published.text
    return rubric_id


def _batch(client, rubric_id, name="2026 届毕业论文评分"):
    created = client.post(
        "/api/batches", json={"name": name, "rubric_id": rubric_id}
    )
    assert created.status_code == 200, created.text
    return created.json()["id"]


def test_batch_list_carries_the_rubric_label(client):
    rubric_id = _rubric(client)
    _batch(client, rubric_id)

    rows = client.get("/api/batches").json()

    row = next(r for r in rows if r["rubric_id"] == rubric_id)
    assert row["rubric_name"] == "本科毕业论文评分标准"
    assert row["rubric_version_label"] == "v3.1-c"


def test_a_single_batch_carries_it_too(client):
    rubric_id = _rubric(client, name="课程报告评分标准", version="v1.2")
    batch_id = _batch(client, rubric_id)

    row = client.get("/api/batches/%s" % batch_id).json()

    assert row["rubric_name"] == "课程报告评分标准"
    assert row["rubric_version_label"] == "v1.2-c"


def test_a_dangling_rubric_reference_does_not_break_the_list(client):
    """标准找不到时列表仍要能出来。

    宁可这一列空着，也不能让整个评分任务页 500——用户会以为系统坏了。

    已发布标准有守卫拦着删不掉（这是对的），所以直接把批次的引用指向一个不存在
    的 id 来构造这个状态：历史数据迁移、跨组织不可见都会产生它。
    """
    rubric_id = _rubric(client)
    batch_id = _batch(client, rubric_id)
    with client.session_factory() as session:
        batch = session.get(models.GradingBatch, batch_id)
        batch.rubric_id = "00000000-0000-0000-0000-00000000dead"
        session.commit()

    response = client.get("/api/batches")

    assert response.status_code == 200
    row = next(r for r in response.json() if r["id"] == batch_id)
    assert row["rubric_name"] is None
    assert row["rubric_version_label"] is None


def test_the_label_does_not_cost_one_query_per_batch(client):
    """一页批次只查一次标准表。

    按 id 逐个去查，一页 20 个批次就是 20 次往返；而这份数据本来就在同一个查询
    里拿得到。
    """
    from sqlalchemy import event

    rubric_id = _rubric(client)
    for index in range(5):
        _batch(client, rubric_id, name="批次 %d" % index)

    statements = []
    with client.session_factory() as session:
        bind = session.get_bind()

    def _record(conn, cursor, statement, parameters, context, executemany):
        if "rubrics" in statement.lower():
            statements.append(statement)

    event.listen(bind, "before_cursor_execute", _record)
    try:
        client.get("/api/batches")
    finally:
        event.remove(bind, "before_cursor_execute", _record)

    assert len(statements) <= 2, "查询了 %d 次 rubrics" % len(statements)
