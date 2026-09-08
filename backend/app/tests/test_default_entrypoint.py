"""默认入口与旧壳下线（用户决定，2026-09-08）。

旧 SPA 直接下线，根路径直接给新工作台。此前的 `WORKBENCH_DEFAULT_ENTRY` 开关与
常驻 `/legacy/` 回退窗口一并取消。

下线旧壳最容易被忽略的后果是**已经发出去的链接**：邀请注册与密码重置的邮件里带
的是 `/register?token=...` 和 `/reset-password?token=...`，它们此前由旧壳承接。
工作台不接这两条路由就下线旧页，等于把所有在途邀请作废，而收件人只会看到 404。
"""


def test_root_serves_the_workbench(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "/workbench/assets/" in response.text


def test_legacy_shell_is_gone(client):
    """旧壳不再提供页面。留着它等于留下两套并存的界面。"""
    response = client.get("/legacy/")

    assert response.status_code == 404


def test_sent_invitation_links_still_resolve(client):
    """邀请链接指向 /register，必须仍然返回工作台的 HTML 外壳。"""
    response = client.get("/register?token=whatever")

    assert response.status_code == 200
    assert "/workbench/assets/" in response.text


def test_sent_reset_links_still_resolve(client):
    response = client.get("/reset-password?token=whatever")

    assert response.status_code == 200
    assert "/workbench/assets/" in response.text


def test_login_serves_the_workbench(client):
    response = client.get("/login")

    assert response.status_code == 200
    assert "/workbench/assets/" in response.text


def test_workbench_paths_still_work(client):
    assert client.get("/workbench").status_code == 200
    assert client.get("/workbench/review").status_code == 200


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
