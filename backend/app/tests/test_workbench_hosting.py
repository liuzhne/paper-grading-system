"""本地 FastAPI 托管 v2 工作台的路由合同（前端 v2 计划 §8.2、§8.3）。

关键约束：
- `/workbench/*` 深链接刷新必须回 HTML，否则新页刷新即 404；
- 缺失的资源必须 404，**不能回落成 SPA 的 HTML**（否则浏览器会把
  HTML 当 JS 执行，故障表现为难以定位的语法错误）；
- `/api/*` 不能被 SPA 回退吞掉。
"""

from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
WORKBENCH_INDEX = ROOT / "public" / "workbench" / "index.html"

pytestmark = pytest.mark.skipif(
    not WORKBENCH_INDEX.is_file(),
    reason="workbench 产物未组装（需 scripts/build_web_static.py --with-workbench）",
)


def test_workbench_root_serves_html(client):
    response = client.get("/workbench")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_workbench_deep_link_refresh_serves_html(client):
    response = client.get("/workbench/batches/abc/grade")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_workbench_injects_api_prefix(client):
    body = client.get("/workbench").text

    assert "__PGS_CONFIG__" in body
    assert "<!-- PGS_CLIENT_CONFIG -->" not in body


def test_missing_workbench_asset_is_404_not_html(client):
    response = client.get("/workbench/assets/does-not-exist.js")

    assert response.status_code == 404
    assert not response.headers.get("content-type", "").startswith("text/html")


def test_api_is_not_swallowed_by_spa_fallback(client):
    response = client.get("/api/system/capabilities")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
