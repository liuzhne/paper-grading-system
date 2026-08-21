from backend.app.services.document_parser.parser import parse_document
from backend.app.tests.conftest import make_dalian_neusoft_cover_docx
from backend.app.tests.conftest import publish_rubric_via_api


def test_dalian_neusoft_cover_metadata_parser(tmp_path):
    path = tmp_path / "cover.docx"
    path.write_bytes(make_dalian_neusoft_cover_docx().getvalue())

    parsed = parse_document(path)

    assert parsed.institution == "大连东软信息学院"
    assert parsed.title == "基于 Spring Boot 的网上图书商城管理系统的设计与实现"
    assert parsed.department == "软件学院"
    assert parsed.major == "软件工程（专升本）"
    assert parsed.student_name == "王子铭"
    assert parsed.student_id == "24201023601"
    assert parsed.advisor == "刘真 周绍斌"


def test_upload_applies_dalian_neusoft_cover_metadata(client):
    rubric_response = client.post(
        "/api/rubrics",
        json={
            "name": "封面解析评分标准",
            "version": "v1.0",
            "total_score": 100,
            "criteria": [
                {
                    "code": "C01",
                    "name": "系统设计",
                    "max_score": 100,
                    "evidence_hints": ["系统设计", "系统实现"],
                    "deduction_rules": ["设计说明不足扣分"],
                    "scoring_mode": "deductive",
                    "deduction_rules_structured": [
                        {
                            "match": "设计说明不足",
                            "points": 100,
                            "reason": "设计说明不足",
                        }
                    ],
                    "display_order": 1,
                }
            ],
        },
    )
    assert rubric_response.status_code == 200, rubric_response.text
    publish_rubric_via_api(client, rubric_response.json()["id"])

    batch_response = client.post(
        "/api/batches",
        json={
            "name": "封面解析批次",
            "rubric_id": rubric_response.json()["id"],
            "department": "默认学院",
            "major": "默认专业",
        },
    )
    assert batch_response.status_code == 200, batch_response.text

    docx = make_dalian_neusoft_cover_docx()
    upload_response = client.post(
        "/api/papers/upload",
        data={"batch_id": batch_response.json()["id"]},
        files={
            "file": (
                "dalian-neusoft-cover.docx",
                docx.getvalue(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert upload_response.status_code == 200, upload_response.text

    paper = upload_response.json()
    assert paper["status"] == "parsed"
    assert paper["title"] == "基于 Spring Boot 的网上图书商城管理系统的设计与实现"
    assert paper["department"] == "软件学院"
    assert paper["major"] == "软件工程（专升本）"
    assert paper["student_name"] == "王子铭"
    assert paper["student_id"] == "24201023601"
    assert paper["advisor"] == "刘真 周绍斌"

    parsed_response = client.get("/api/papers/%s/parsed" % paper["id"])
    assert parsed_response.status_code == 200
    assert parsed_response.json()["parsed"]["institution"] == "大连东软信息学院"
