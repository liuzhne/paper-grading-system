"""评分项的证据展示投影（前端 v2 计划 §5-A）。

原始 `ScoreItem.evidence` 保留审计语义不动，展示投影是**另一个字段**，
通过 `include_view=true` 显式索取，避免破坏旧客户端与 golden。

最容易做错的一条：**不能从 payload_hash 反推模型原句。**
Core 只留哈希时，界面能显示的是「对应证据单元的上下文」，不是当初的引文。
把上下文冒充成引文，会让复核者以为自己在核对模型真正引用的那句话。
"""

from datetime import datetime

from backend.app.db import models
from backend.app.services.scoring import evidence_view


def _run_with_item(session, *, evidence, chunks=2):
    rubric = models.Rubric(name="ev rubric", version="v1", total_score=100)
    session.add(rubric)
    session.flush()
    criterion = models.RubricCriterion(
        rubric_id=rubric.id, code="C01", name="研究方法", max_score=25, weight=1
    )
    session.add(criterion)
    batch = models.GradingBatch(name="ev batch", rubric_id=rubric.id, status="scored")
    session.add(batch)
    session.flush()
    paper = models.Paper(
        batch_id=batch.id, file_name="p.pdf", file_path="p.pdf", status="parsed"
    )
    session.add(paper)
    session.flush()
    made_chunks = []
    for index in range(chunks):
        chunk = models.PaperChunk(
            paper_id=paper.id,
            section_title="3.%d 研究方法" % (index + 1),
            page_start=12 + index,
            page_end=12 + index,
            paragraph_ids=[],
            text="本文采用分层抽样方法，样本量为 240。",
        )
        session.add(chunk)
        made_chunks.append(chunk)
    session.flush()
    run = models.ScoringRun(
        paper_id=paper.id,
        rubric_id=rubric.id,
        status="scored",
        created_at=datetime(2026, 9, 1, 12, 0, 0),
    )
    session.add(run)
    session.flush()
    item = models.ScoreItem(
        scoring_run_id=run.id,
        criterion_id=criterion.id,
        max_score=25,
        ai_score=21,
        final_score=21,
        evidence_sufficient=True,
        reason="方法描述完整",
        deductions=[],
        deduction_items=[],
        evidence=evidence(made_chunks),
        confidence=0.61,
    )
    session.add(item)
    session.flush()
    return run, item, made_chunks


# ---------------------------------------------------------------- legacy 引文


def test_verified_quote_is_shown_as_a_quote_with_its_location(client):
    with client.session_factory() as session:
        run, item, chunks = _run_with_item(
            session,
            evidence=lambda cs: [
                {
                    "quote": "本文采用分层抽样方法",
                    "location": "3.1",
                    "chunk_id": cs[0].id,
                }
            ],
        )

        views = evidence_view.build_evidence_view(session, run, item)

        assert len(views) == 1
        view = views[0]
        assert view["location_status"] == "verified"
        assert view["quote"] == "本文采用分层抽样方法"
        assert view["anchor_id"] == chunks[0].id
        assert view["section_title"] == "3.1 研究方法"
        assert view["page_start"] == 12
        assert view["source_kind"] == "legacy_chunks"


def test_quote_that_no_longer_matches_the_text_is_not_shown_as_verified(client):
    """解析结果可能已变化。引文对不上时必须说明，不能照旧高亮。"""
    with client.session_factory() as session:
        run, item, chunks = _run_with_item(
            session,
            evidence=lambda cs: [
                {
                    "quote": "这句话在当前解析文本里已经不存在了",
                    "location": "3.1",
                    "chunk_id": cs[0].id,
                }
            ],
        )

        view = evidence_view.build_evidence_view(session, run, item)[0]

        assert view["location_status"] == "quote_not_found"
        assert view["anchor_id"] == chunks[0].id


def test_evidence_pointing_at_a_missing_chunk_reports_why(client):
    with client.session_factory() as session:
        run, item, _ = _run_with_item(
            session,
            evidence=lambda cs: [
                {"quote": "任意", "location": "?", "chunk_id": "chunk-gone"}
            ],
        )

        view = evidence_view.build_evidence_view(session, run, item)[0]

        assert view["location_status"] == "anchor_missing"
        assert view["page_start"] is None
        assert view["unlocatable_reason"]


def test_evidence_without_any_anchor_is_reported_not_silently_dropped(client):
    with client.session_factory() as session:
        run, item, _ = _run_with_item(
            session,
            evidence=lambda cs: [{"quote": "无锚点引文", "location": "", "chunk_id": None}],
        )

        view = evidence_view.build_evidence_view(session, run, item)[0]

        assert view["location_status"] == "no_anchor"
        assert view["unlocatable_reason"]


# ---------------------------------------------------------------- Core 哈希


class _StubSnapshot:
    snapshot_hash = "b" * 64
    snapshot_payload = {
        "sections": [],
        "evidence_units": [
            {
                "evidence_unit_id": "u1",
                "normalized_text": "本文采用分层抽样方法，样本量为 240。",
                "section_path": ["3", "1"],
                "section_ordinal": 0,
                "unit_ordinal": 0,
            }
        ],
    }


class _StubCoreRun:
    id = "run-core"
    submission_id = "sub-1"
    paper_id = None
    document_snapshot = _StubSnapshot()


class _StubItem:
    def __init__(self, evidence):
        self.evidence = evidence


def test_hash_only_core_evidence_shows_unit_context_not_a_fabricated_quote(client):
    """只有 payload_hash 时不得反推原句——上下文必须被标成上下文。"""
    item = _StubItem(
        [
            {
                "evidence_unit_id": "u1",
                "evidence_type": "source_quote",
                "locator": {"kind": "text_span", "evidence_unit_id": "u1"},
                "payload_hash": "d" * 64,
            }
        ]
    )

    with client.session_factory() as session:
        view = evidence_view.build_evidence_view(session, _StubCoreRun(), item)[0]

    assert view["source_kind"] == "core_snapshot"
    assert view["snapshot_hash"] == "b" * 64
    assert view["anchor_id"] == "u1"
    assert view["location_status"] == "unit_context"
    # 上下文是证据单元全文，不是模型当初引用的那一句。
    assert view["quote"] is None
    assert view["context_text"] == "本文采用分层抽样方法，样本量为 240。"


def test_core_evidence_for_an_unknown_unit_reports_why(client):
    item = _StubItem(
        [
            {
                "evidence_unit_id": "does-not-exist",
                "evidence_type": "source_quote",
                "locator": {"kind": "text_span", "evidence_unit_id": "does-not-exist"},
                "payload_hash": "d" * 64,
            }
        ]
    )

    with client.session_factory() as session:
        view = evidence_view.build_evidence_view(session, _StubCoreRun(), item)[0]

    assert view["location_status"] == "anchor_missing"
    assert view["quote"] is None
    assert view["unlocatable_reason"]


def test_non_quote_evidence_type_is_labelled_as_a_check_fact(client):
    """结构/格式类发现不是引文，不能伪装成原文摘录。"""
    item = _StubItem(
        [
            {
                "evidence_unit_id": None,
                "evidence_type": "format_finding",
                "locator": {"kind": "section", "section_ordinal": 0},
                "payload_hash": "d" * 64,
            }
        ]
    )

    with client.session_factory() as session:
        view = evidence_view.build_evidence_view(session, _StubCoreRun(), item)[0]

    assert view["evidence_type"] == "format_finding"
    assert view["location_status"] == "check_fact"
    assert view["quote"] is None


# ---------------------------------------------------------------- 端点


def test_items_endpoint_keeps_the_legacy_shape_by_default(client):
    with client.session_factory() as session:
        run, _, _ = _run_with_item(
            session,
            evidence=lambda cs: [
                {"quote": "本文采用分层抽样方法", "location": "3.1", "chunk_id": cs[0].id}
            ],
        )
        session.commit()
        run_id = run.id

    body = client.get(f"/api/scoring-runs/{run_id}/items").json()

    assert "evidence" in body[0]
    assert "evidence_view" not in body[0], "旧默认响应不得被就地改写"


def test_items_endpoint_adds_the_projection_on_request(client):
    with client.session_factory() as session:
        run, _, _ = _run_with_item(
            session,
            evidence=lambda cs: [
                {"quote": "本文采用分层抽样方法", "location": "3.1", "chunk_id": cs[0].id}
            ],
        )
        session.commit()
        run_id = run.id

    body = client.get(f"/api/scoring-runs/{run_id}/items?include_view=true").json()

    assert body[0]["evidence"], "原始 evidence 仍需保留审计语义"
    assert body[0]["evidence_view"][0]["location_status"] == "verified"
    assert body[0]["evidence_view"][0]["page_start"] == 12
