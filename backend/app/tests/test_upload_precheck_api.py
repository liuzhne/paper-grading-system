"""`POST /api/batches/{id}/upload-precheck`（前端 v2 计划 §5-E、§6）。

预检**只接收已归档的 paper ID 并汇总既有解析诊断**：不重新传文件，也不额外
跑一次全文解析。否则一次"预检"会把整批材料重解析一遍，既慢又可能与后续评分
用到的解析结果不一致。

扫描件按决策 1 明确拒绝：系统不支持 OCR，提示提供文字版而不是承诺「将走
OCR 流程」。
"""

from backend.app.db import models


def _seed(client, papers):
    """papers: [(status, parse_quality, error_message)]"""
    with client.session_factory() as session:
        rubric = models.Rubric(name="pc rubric", version="v1", total_score=100)
        session.add(rubric)
        session.flush()
        batch = models.GradingBatch(name="pc batch", rubric_id=rubric.id, status="draft")
        session.add(batch)
        session.flush()
        ids = []
        for index, (status, quality, error) in enumerate(papers):
            paper = models.Paper(
                batch_id=batch.id,
                file_name="p%d.pdf" % index,
                file_path="p%d.pdf" % index,
                status=status,
                parse_quality=quality,
                error_message=error,
                student_id="SE-%03d" % index,
            )
            session.add(paper)
            session.flush()
            ids.append(paper.id)
        session.commit()
        return batch.id, ids


def _precheck(client, batch_id, ids):
    return client.post(
        f"/api/batches/{batch_id}/upload-precheck", json={"paper_ids": ids}
    )


def test_all_parsed_materials_are_ready(client):
    batch_id, ids = _seed(client, [("parsed", 0.9, None), ("parsed", 0.85, None)])

    body = _precheck(client, batch_id, ids).json()

    assert body["ready_count"] == 2
    assert body["blocking_count"] == 0
    assert body["findings"] == []


def test_a_scanned_pdf_is_rejected_with_actionable_text(client):
    """决策 1：不支持 OCR。文案要给出下一步，而不是承诺一个不存在的流程。"""
    batch_id, ids = _seed(
        client,
        [
            (
                "failed",
                None,
                "no readable text found; scanned PDFs require OCR and are not supported in MVP",
            )
        ],
    )

    body = _precheck(client, batch_id, ids).json()

    finding = body["findings"][0]
    assert finding["code"] == "scanned_document"
    assert finding["severity"] == "blocking"
    assert "OCR" in finding["message"]
    assert "文字版" in finding["message"]
    assert "将走 OCR" not in finding["message"]


def test_other_parse_failures_are_reported_as_blocking(client):
    batch_id, ids = _seed(client, [("failed", None, "unsupported file type")])

    body = _precheck(client, batch_id, ids).json()

    assert body["blocking_count"] == 1
    assert body["findings"][0]["code"] == "parse_failed"


def test_unparsed_material_blocks_scoring(client):
    """还没解析完就开评分会把「未解析」误当成「解析出来是空的」。"""
    batch_id, ids = _seed(client, [("uploaded", None, None)])

    body = _precheck(client, batch_id, ids).json()

    assert body["ready_count"] == 0
    assert body["findings"][0]["code"] == "not_parsed"
    assert body["findings"][0]["severity"] == "blocking"


def test_low_parse_quality_is_a_warning_not_a_blocker(client):
    """质量偏低仍可评分，只是提醒复核时多留意。"""
    batch_id, ids = _seed(client, [("parsed", 0.35, None)])

    body = _precheck(client, batch_id, ids).json()

    assert body["blocking_count"] == 0
    finding = body["findings"][0]
    assert finding["code"] == "low_parse_quality"
    assert finding["severity"] == "warning"


def test_findings_identify_the_material(client):
    batch_id, ids = _seed(client, [("failed", None, "unsupported file type")])

    finding = _precheck(client, batch_id, ids).json()["findings"][0]

    assert finding["paper_id"] == ids[0]
    assert finding["student_id"] == "SE-000"


def test_precheck_rejects_papers_outside_the_batch(client):
    batch_id, ids = _seed(client, [("parsed", 0.9, None)])
    other_batch, other_ids = _seed(client, [("parsed", 0.9, None)])

    response = _precheck(client, batch_id, other_ids)

    assert response.status_code == 404


def test_precheck_requires_at_least_one_paper(client):
    batch_id, _ = _seed(client, [("parsed", 0.9, None)])

    assert _precheck(client, batch_id, []).status_code == 422


def test_precheck_is_bounded(client):
    batch_id, ids = _seed(client, [("parsed", 0.9, None)])

    assert _precheck(client, batch_id, ids * 600).status_code == 422


def test_precheck_does_not_reparse(client, monkeypatch):
    """预检只读既有诊断。若它触发解析，一次预检会把整批重解析一遍。"""
    from backend.app.services.papers import ingestion

    called = []
    monkeypatch.setattr(
        ingestion,
        "parse_and_persist",
        lambda *a, **k: called.append(1),
        raising=False,
    )
    batch_id, ids = _seed(client, [("parsed", 0.9, None)])

    _precheck(client, batch_id, ids)

    assert called == []


def test_can_start_reports_whether_scoring_may_begin(client):
    ready_batch, ready_ids = _seed(client, [("parsed", 0.9, None)])
    blocked_batch, blocked_ids = _seed(client, [("failed", None, "boom")])

    assert _precheck(client, ready_batch, ready_ids).json()["can_start"] is True
    assert _precheck(client, blocked_batch, blocked_ids).json()["can_start"] is False
