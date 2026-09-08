"""冻结正文投影与证据定位（前端 v2 计划 §5-A）。

评分工作区中间栏的数据源。两条来源的身份强度**不同**，不能混为一谈：

- 正式 Core：`run.document_snapshot` 是不可变快照，`evidence_unit_id` 是硬锚点。
- legacy compatibility：只有当前 `PaperChunk`，是**可变的解析结果**，可能已与
  当初评分时看到的文本不一致。界面必须标明这一点，不能伪称冻结快照。

查不到冻结快照时**不静默退回最新解析文本**——那会让用户以为自己在核对当初
的证据，实际看的是重新解析后的另一份文本。
"""

from datetime import datetime

from backend.app.db import models
from backend.app.services.scoring import document_view


def _legacy_run(session, *, chunks=2):
    rubric = models.Rubric(name="dv rubric", version="v1", total_score=100)
    session.add(rubric)
    session.flush()
    batch = models.GradingBatch(name="dv batch", rubric_id=rubric.id, status="scored")
    session.add(batch)
    session.flush()
    paper = models.Paper(
        batch_id=batch.id, file_name="p.pdf", file_path="p.pdf", status="parsed"
    )
    session.add(paper)
    session.flush()
    for index in range(chunks):
        session.add(
            models.PaperChunk(
                paper_id=paper.id,
                section_title="第 %d 章" % (index + 1),
                page_start=index + 1,
                page_end=index + 1,
                paragraph_ids=[],
                text="第 %d 段正文内容。" % (index + 1),
            )
        )
    run = models.ScoringRun(
        paper_id=paper.id,
        rubric_id=rubric.id,
        status="scored",
        created_at=datetime(2026, 9, 1, 12, 0, 0),
    )
    session.add(run)
    session.flush()
    return run, paper


# ---------------------------------------------------------------- legacy


def test_legacy_run_projects_chunks_with_pages(client):
    with client.session_factory() as session:
        run, _ = _legacy_run(session, chunks=2)

        view = document_view.build_document_view(session, run)

        assert view["source_kind"] == "legacy_chunks"
        assert len(view["blocks"]) == 2
        first = view["blocks"][0]
        assert first["section_title"] == "第 1 章"
        assert first["page_start"] == 1
        assert first["chunk_id"]
        assert first["evidence_unit_id"] is None


def test_legacy_view_declares_that_the_text_is_a_current_reparse(client):
    """legacy 不是冻结快照，必须显式标注，否则用户会误以为在核对原始证据。"""
    with client.session_factory() as session:
        run, _ = _legacy_run(session)

        view = document_view.build_document_view(session, run)

        assert view["frozen"] is False
        assert view["document_snapshot_hash"] is None
        assert view["text_provenance"] == "current_parse"


def test_blocks_have_stable_ids_across_calls(client):
    with client.session_factory() as session:
        run, _ = _legacy_run(session, chunks=3)

        first = document_view.build_document_view(session, run)
        second = document_view.build_document_view(session, run)

        assert [b["block_id"] for b in first["blocks"]] == [
            b["block_id"] for b in second["blocks"]
        ]


# ---------------------------------------------------------------- Core


class _StubSnapshot:
    def __init__(self, payload, snapshot_hash="b" * 64):
        self.snapshot_payload = payload
        self.snapshot_hash = snapshot_hash


class _StubRun:
    """Core run 的最小投影输入。

    完整的 EvaluationBatch → RubricVersion → Submission → DocumentSnapshot 链路
    在别处已有集成覆盖；这里测的是纯投影函数，用桩对象能把断言聚焦在投影规则上。
    """

    def __init__(self, snapshot, *, run_id="run-core"):
        self.id = run_id
        self.submission_id = "sub-1"
        self.paper_id = None
        self.document_snapshot = snapshot


_CORE_PAYLOAD = {
    "sections": [
        {
            "section_path": ["1"],
            "section_ordinal": 0,
            "heading": "研究方法",
            "normalized_text": "方法段落全文。",
            "evidence_unit_ids": ["u1"],
        }
    ],
    "evidence_units": [
        {
            "evidence_unit_id": "u1",
            "normalized_text": "方法段落全文。",
            "section_path": ["1"],
            "section_ordinal": 0,
            "unit_ordinal": 0,
        }
    ],
}


def test_core_run_projects_the_frozen_snapshot(client):
    run = _StubRun(_StubSnapshot(_CORE_PAYLOAD))

    with client.session_factory() as session:
        view = document_view.build_document_view(session, run)

    assert view["source_kind"] == "core_snapshot"
    assert view["frozen"] is True
    assert view["document_snapshot_hash"] == "b" * 64
    assert view["text_provenance"] == "frozen_snapshot"


def test_core_blocks_carry_evidence_unit_ids_and_no_fabricated_pages(client):
    """Core 快照没有页码来源，必须留空而不是编一个。"""
    run = _StubRun(_StubSnapshot(_CORE_PAYLOAD))

    with client.session_factory() as session:
        blocks = document_view.build_document_view(session, run)["blocks"]

    assert blocks[0]["evidence_unit_id"] == "u1"
    assert blocks[0]["chunk_id"] is None
    assert blocks[0]["page_start"] is None
    assert blocks[0]["section_title"] == "研究方法"


def test_core_run_without_snapshot_does_not_fall_back_to_reparsed_text(client):
    """防御分支：查不到冻结快照时给出明确原因，不静默退回最新解析文本。"""
    run = _StubRun(None)

    with client.session_factory() as session:
        view = document_view.build_document_view(session, run)

    assert view["blocks"] == []
    assert view["source_kind"] == "unavailable"
    assert view["unavailable_reason"]
    assert view["text_provenance"] != "current_parse"


def test_schema_makes_a_core_run_without_a_snapshot_unreachable(client):
    """上面的防御分支只应出现在数据损坏时：约束禁止这种行状态入库。"""
    import sqlalchemy as sa

    with client.session_factory() as session:
        rubric = models.Rubric(name="ck rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        session.add(
            models.ScoringRun(
                submission_id="sub-x",
                document_snapshot_id=None,
                rubric_id=rubric.id,
                status="scored",
            )
        )
        try:
            session.flush()
        except sa.exc.IntegrityError:
            return
        raise AssertionError(
            "ck_scoring_runs_submission_snapshot_target 应拒绝无快照的 Core run"
        )


# ---------------------------------------------------------------- 分页与锚点


def test_paging_is_bounded_and_returns_a_cursor(client):
    with client.session_factory() as session:
        run, _ = _legacy_run(session, chunks=5)

        first = document_view.build_document_view(session, run, limit=2)

        assert len(first["blocks"]) == 2
        assert first["next_cursor"] is not None

        second = document_view.build_document_view(
            session, run, limit=2, cursor=first["next_cursor"]
        )
        assert [b["block_id"] for b in second["blocks"]] != [
            b["block_id"] for b in first["blocks"]
        ]


def test_last_page_has_no_cursor(client):
    with client.session_factory() as session:
        run, _ = _legacy_run(session, chunks=2)

        view = document_view.build_document_view(session, run, limit=10)

        assert view["next_cursor"] is None


def test_anchor_returns_the_page_containing_it_not_the_first_page(client):
    """点证据 chip 要能直接跳到它所在的那一页，而不是从头翻。"""
    with client.session_factory() as session:
        run, paper = _legacy_run(session, chunks=6)
        chunk_ids = [
            chunk.id
            for chunk in sorted(paper.chunks, key=lambda c: (c.page_start or 0, c.id))
        ]
        target = chunk_ids[4]

        view = document_view.build_document_view(
            session, run, limit=2, anchor_id=target
        )

        assert target in [b["chunk_id"] for b in view["blocks"]]


def test_unknown_anchor_falls_back_to_the_first_page_without_error(client):
    with client.session_factory() as session:
        run, _ = _legacy_run(session, chunks=3)

        view = document_view.build_document_view(
            session, run, limit=2, anchor_id="not-a-real-anchor"
        )

        assert len(view["blocks"]) == 2
        assert view["anchor_found"] is False


# ---------------------------------------------------------------- 端点


def _legacy_run_via_client(client):
    with client.session_factory() as session:
        run, paper = _legacy_run(session, chunks=3)
        session.commit()
        return run.id, paper.id


def test_endpoint_returns_the_projection(client):
    run_id, _ = _legacy_run_via_client(client)

    response = client.get(f"/api/scoring-runs/{run_id}/document-view")

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["source_kind"] == "legacy_chunks"
    assert len(body["blocks"]) == 3


def test_endpoint_forbids_shared_caching_of_paper_text(client):
    """正文含学生论文原文，绝不能进共享缓存。"""
    run_id, _ = _legacy_run_via_client(client)

    response = client.get(f"/api/scoring-runs/{run_id}/document-view")

    assert response.headers["cache-control"] == "private, no-store"


def test_endpoint_honours_limit_and_cursor(client):
    run_id, _ = _legacy_run_via_client(client)

    first = client.get(f"/api/scoring-runs/{run_id}/document-view?limit=2").json()

    assert len(first["blocks"]) == 2
    assert first["next_cursor"] is not None

    second = client.get(
        f"/api/scoring-runs/{run_id}/document-view?limit=2&cursor={first['next_cursor']}"
    ).json()
    assert len(second["blocks"]) == 1
    assert second["next_cursor"] is None


def test_endpoint_rejects_an_unbounded_limit(client):
    run_id, _ = _legacy_run_via_client(client)

    response = client.get(f"/api/scoring-runs/{run_id}/document-view?limit=100000")

    assert response.status_code == 422


def test_endpoint_is_scoped_to_visible_runs(client):
    assert (
        client.get("/api/scoring-runs/does-not-exist/document-view").status_code == 404
    )


def test_item_projection_forbids_shared_caching_too(client):
    """§5-A：document-view **与评分项展示投影**同样不得进共享缓存。

    `evidence_view` 里带的是学生原文的逐字引文，和正文投影同等敏感；只给
    document-view 设头，等于换一个端点就能把同样的内容缓存出去。
    """
    run_id, _ = _legacy_run_via_client(client)

    response = client.get(f"/api/scoring-runs/{run_id}/items?include_view=true")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"


def test_plain_item_response_is_not_cacheable_either(client):
    """默认响应里的 `evidence` 同样是原文引文，不因为形状旧就可以缓存。"""
    run_id, _ = _legacy_run_via_client(client)

    response = client.get(f"/api/scoring-runs/{run_id}/items")

    assert response.headers["cache-control"] == "private, no-store"


def test_review_queue_is_not_cacheable(client):
    """计划外加固：队列同样带学生姓名学号与分数。

    §5-A 只点名了正文投影与评分项投影，但复核队列是同一类数据、同一批页面在
    用。少一个头就等于换一个端点把同样的内容缓存出去。
    """
    from backend.app.db import models

    with client.session_factory() as session:
        rubric = models.Rubric(name="cache rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(
            name="cache batch", rubric_id=rubric.id, status="scored"
        )
        session.add(batch)
        session.commit()
        batch_id = batch.id

    response = client.get(f"/api/batches/{batch_id}/review-queue")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
