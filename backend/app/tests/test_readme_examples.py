"""README 快速开始的命令必须在仓库自带的 examples/ 上跑通。

README 承诺“复制就能跑”：不建库、不要 API Key。示例文件或命令一旦失效，
第一个打开仓库的人就会卡在第一步，所以用测试守住。
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backend.app.cli.main import app
from backend.app.core.config import settings
from backend.app.services.rubric_import.template import EXAMPLE_ROWS


ROOT = Path(__file__).resolve().parents[3]
QUICKSTART = "uv run pgs score examples/thesis.docx --no-db --rubric-file examples/rubric.xlsx --mock"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _mock_llm_only():
    saved = (settings.DATABASE_URL, settings.STORAGE_ROOT, settings.LLM_PROVIDER)
    settings.LLM_PROVIDER = "mock"
    yield
    settings.DATABASE_URL, settings.STORAGE_ROOT, settings.LLM_PROVIDER = saved


def test_readme_quickstart_names_the_bundled_examples():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert QUICKSTART in readme
    assert (ROOT / "examples/thesis.docx").is_file()
    assert (ROOT / "examples/rubric.xlsx").is_file()


def test_readme_quickstart_scores_examples_without_database(tmp_path):
    ghost_db = tmp_path / "should_not_exist.db"
    result = runner.invoke(
        app,
        [
            "score",
            str(ROOT / "examples/thesis.docx"),
            "--no-db",
            "--rubric-file",
            str(ROOT / "examples/rubric.xlsx"),
            "--mock",
            "--json",
            "--db",
            str(ghost_db),
        ],
    )

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["stateless"] is True
    [only] = data["results"]
    assert only["status"] == "ok"
    # 示例评分标准就是导入模板的示例行；模板改了而示例没重新生成，这里会失败。
    assert len(only["items"]) == len(EXAMPLE_ROWS)
    assert not ghost_db.exists()
