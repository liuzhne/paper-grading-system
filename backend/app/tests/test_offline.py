"""离线就绪审计 + OFFLINE_MODE 网络守卫（CLI/Web 共用，自包含不联网）。"""

import pytest

from backend.app.core.config import settings
from backend.app.services.offline import network_touchpoints
from backend.app.services.offline import offline_ready
from backend.app.services.spreadsheet.writer import SpreadsheetWriteError
from backend.app.services.spreadsheet.writer import write_run_to_sheet


def test_offline_ready_when_local_and_mock_sheet(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "local")
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "mock")
    assert offline_ready() is True
    scopes = {p["component"]: p["scope"] for p in network_touchpoints()}
    assert scopes["LLM 评分"] == "local"  # 连本地端口
    assert scopes["表格导出"] == "offline"


def test_not_offline_ready_with_cloud_llm(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(settings, "OPENAI_COMPATIBLE_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "mock")
    assert offline_ready() is False


def test_offline_mode_blocks_sheet_export(monkeypatch):
    # 守卫在加载 run 之前就拦截，故 db=None 也能验证。
    monkeypatch.setattr(settings, "OFFLINE_MODE", True)
    monkeypatch.setattr(settings, "SHEET_WRITER_PROVIDER", "google_sheets")
    with pytest.raises(SpreadsheetWriteError):
        write_run_to_sheet(None, "any-run-id")
