"""默认入口与旧壳下线（用户决定，2026-09-08）。

旧 SPA 直接下线，此前的 `WORKBENCH_DEFAULT_ENTRY` 开关与常驻 `/legacy/` 回退窗口
一并取消。根路径起初直接给工作台；2026-10-17 起根路径是宣传页、`/about` 是官网
子页，工作台的规范地址是 `/workbench/`（用户决定）。

下线旧壳最容易被忽略的后果是**已经发出去的链接**：邀请注册与密码重置的邮件里带
的是 `/register?token=...` 和 `/reset-password?token=...`，它们此前由旧壳承接。
工作台不接这两条路由就下线旧页，等于把所有在途邀请作废，而收件人只会看到 404。
"""

from pathlib import Path
import re

from fastapi.testclient import TestClient


def _entry_script(html: str) -> str:
    """入口页的模块脚本名（site-*.js / about-*.js / workbench-*.js）。"""
    match = re.search(r'<script type="module"[^>]*src="/workbench/assets/([a-z]+)-', html)
    assert match, html[:300]
    return match.group(1)


def test_root_serves_the_landing_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert _entry_script(response.text) == "site"
    # 宣传页要查会话决定是否跳工作台：非默认 API_PREFIX 靠这里注入。
    assert "window.__PGS_CONFIG__" in response.text


def test_about_page_is_served_with_and_without_trailing_slash(client):
    for path in ("/about", "/about/"):
        response = client.get(path, follow_redirects=False)

        assert response.status_code == 200, path
        assert _entry_script(response.text) == "about", path


def test_root_falls_back_to_the_workbench_without_site_pages(monkeypatch, tmp_path):
    """产物里没有宣传页（只组装过旧版本）时，根路径回落工作台而不是 404。"""
    from backend.app import main

    public = Path(main.__file__).resolve().parents[2] / "public" / "workbench"
    workbench = tmp_path / "public" / "workbench"
    (workbench / "assets").mkdir(parents=True)
    (workbench / "index.html").write_text(
        (public / "index.html").read_text(encoding="utf-8"), encoding="utf-8"
    )
    fake_main = tmp_path / "backend" / "app" / "main.py"
    fake_main.parent.mkdir(parents=True)
    fake_main.write_text("", encoding="utf-8")
    monkeypatch.setattr(main, "__file__", str(fake_main))

    response = TestClient(main.create_app()).get("/")

    assert response.status_code == 200
    assert _entry_script(response.text) == "workbench"


def test_legacy_shell_is_gone(client):
    """旧壳不再提供页面。留着它等于留下两套并存的界面。"""
    response = client.get("/legacy/")

    assert response.status_code == 404


def test_sent_invitation_links_still_resolve(client):
    """邀请链接指向 /register，必须仍然返回工作台的 HTML 外壳。"""
    response = client.get("/register?token=whatever")

    assert response.status_code == 200
    assert _entry_script(response.text) == "workbench"


def test_sent_reset_links_still_resolve(client):
    response = client.get("/reset-password?token=whatever")

    assert response.status_code == 200
    assert _entry_script(response.text) == "workbench"


def test_login_serves_the_workbench(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert _entry_script(response.text) == "workbench"


def test_workbench_paths_still_work(client):
    for path in ("/workbench", "/workbench/", "/workbench/review"):
        response = client.get(path)

        assert response.status_code == 200, path
        assert _entry_script(response.text) == "workbench", path


def test_api_is_never_swallowed(client):
    response = client.get("/api/system/integrations")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")


def test_font_licence_is_served_as_a_file_not_the_spa_shell(client):
    """SIL OFL 1.1 要求许可证随字体分发（计划 §8）。"""
    response = client.get("/workbench/LICENSE-IBM-Plex-Mono.txt")

    assert response.status_code == 200
    assert "SIL OPEN FONT LICENSE" in response.text.upper()
    assert not response.text.lstrip().startswith("<!doctype")


def test_the_file_shortcut_cannot_escape_the_artifact(client):
    """只认一层文件名：带分隔符的路径一律走回退，拼不出 ../ 读到产物之外。"""
    for path in ("../../pyproject.toml", "assets/../../../etc/hosts"):
        response = client.get("/workbench/%s" % path)

        assert response.status_code in (200, 404)
        if response.status_code == 200:
            assert "[project]" not in response.text
