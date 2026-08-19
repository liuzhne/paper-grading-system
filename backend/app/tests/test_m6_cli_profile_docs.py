from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_web_cli_parity_matrix_documents_v1_and_v2_profile_surfaces():
    text = (ROOT / "docs" / "web-cli-功能对等.md").read_text(encoding="utf-8")

    assert "--profile" in text
    assert "/api/v2/submissions" in text
    assert "run-export@2" in text
    assert "GradeScale" in text
    assert "默认 thesis" in text
