"""三种部署都要能拿到同一份静态产物（前端 v2 计划 §8.2）。

Docker 那一行是这么写的：「容器启动与页面/**字体**/动态模块可用，离线运行不访问
CDN」。而镜像此前根本没有 COPY `public/`——工作台在容器里全是 404，legacy 入口
因为走 `frontend/web` 反而正常，所以故障看起来像「新页面没部署上」而不是
「镜像少了一层」。compose 冒烟只 curl 了 `/api/system/integrations`，看不见这件事。

字体许可证同理：SIL OFL 1.1 要求许可证随字体分发。字体文件进了产物、许可证没进，
是一个安静的合规缺口——没有任何东西会报错。
"""

import pathlib
import re


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_dockerfile_ships_the_assembled_static_artifact():
    """FastAPI 从 `public/workbench` 托管新页；不 COPY 它，容器里就只有 404。"""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    copies = re.findall(r"^COPY\s+(\S+)", dockerfile, re.MULTILINE)

    assert "public" in copies, copies


def test_dockerfile_does_not_install_node():
    """§8.2：运行镜像不安装 Node/npm。产物在镜像外组装好再复制进来。"""
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "npm install" not in dockerfile
    assert "apt-get install" not in dockerfile or "nodejs" not in dockerfile


def test_the_font_licence_ships_with_the_font():
    """SIL OFL 1.1 要求许可证随字体分发。"""
    workbench = REPO_ROOT / "public" / "workbench"
    fonts = list((workbench / "assets").glob("*ibm-plex-mono*.woff2"))

    assert fonts, "自托管字体未进产物"
    licences = list(workbench.glob("*LICENSE*")) + list(workbench.glob("*OFL*"))
    assert licences, "字体进了产物但许可证没有"


def test_the_shipped_licence_is_the_real_one():
    """占位文件同样是合规缺口，只是更难发现。"""
    workbench = REPO_ROOT / "public" / "workbench"
    licences = list(workbench.glob("*LICENSE*")) + list(workbench.glob("*OFL*"))

    text = licences[0].read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE" in text.upper()
    assert len(text) > 1000


def test_the_page_points_at_the_licence():
    """许可证放进产物但没人找得到，等于没放。"""
    index = (REPO_ROOT / "public" / "workbench" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "LICENSE" in index or "OFL" in index


def test_vercel_config_has_no_route_for_the_retired_legacy_assets():
    """旧 SPA 下线后 `public/assets` 不再产出（用户决定，2026-09-08）。

    留着指向它的规则不会报错，只会让下一个读配置的人以为那条路径还在用——
    然后照着它去排查一个根本不存在的目录。
    """
    import json

    config = json.loads((REPO_ROOT / "vercel.json").read_text(encoding="utf-8"))
    sources = [entry["source"] for entry in config.get("headers", [])]

    assert "/assets/(.*)" not in sources
    # 工作台自己的指纹资源仍要有长缓存。
    assert "/workbench/assets/(.*)" in sources


def test_vercel_serves_the_workbench_at_the_public_entry_paths():
    """`/` 与三条鉴权路由都是静态文件，不依赖 rewrite。

    产物里必须真有这些文件，否则 Vercel 会 404——而这条路径不经过 FastAPI，
    后端的路由改动救不了它。
    """
    for path in ("index.html", "login/index.html", "register/index.html",
                 "reset-password/index.html"):
        assert (REPO_ROOT / "public" / path).is_file(), path
