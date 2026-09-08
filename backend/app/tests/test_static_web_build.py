from pathlib import Path
import re
from shutil import rmtree
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[3]


def test_vercel_static_web_build_is_self_contained(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_web_static.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    # Vercel discovers public files before the Python build hook runs, so these
    # generated files are intentionally retained as deployable repository assets.
    public = ROOT / "public"
    for path in (
        "index.html",
        "login/index.html",
        "register/index.html",
        "reset-password/index.html",
    ):
        assert (public / path).is_file()
    index = (public / "index.html").read_text(encoding="utf-8")
    # CDN 产物不带注入的全局配置：前端回落同源 /api，不必经 Python 渲染页面。
    assert "__PGS_CONFIG__" not in index

    # 入口引用的是工作台的指纹资源，且这些文件真的在产物里——引用存在而文件
    # 缺失，浏览器拿到的是 404 被当成 JS 执行的语法错误。
    referenced = set(re.findall(r"/workbench/assets/[A-Za-z0-9_.-]+", index))
    assert referenced
    for href in referenced:
        assert (public / href.lstrip("/")).is_file(), href

    # 四条公开路由必须是同一份外壳，否则邀请链接打开的会是另一个版本。
    for page in ("login", "register", "reset-password"):
        assert (public / page / "index.html").read_text(encoding="utf-8") == index


def test_plain_build_preserves_assembled_workbench(tmp_path):
    """普通构建只重建旧入口，不得删掉已组装的 /workbench 产物。

    否则任何一次不带 --with-workbench 的构建（包括本文件上一个用例）都会
    产出一个 /workbench 全部 404 的部署产物。
    """
    workbench = ROOT / "public" / "workbench"
    created = not workbench.exists()
    if created:
        (workbench / "assets").mkdir(parents=True)
        (workbench / "index.html").write_text("<!-- sentinel -->", encoding="utf-8")
    sentinel = workbench / "assets" / "sentinel.txt"
    sentinel.write_text("keep-me", encoding="utf-8")

    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_web_static.py")],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert (workbench / "index.html").is_file()
        assert sentinel.read_text(encoding="utf-8") == "keep-me"
    finally:
        if created:
            rmtree(workbench, ignore_errors=True)
        else:
            sentinel.unlink(missing_ok=True)


def test_assembled_index_is_the_workbench_not_the_legacy_shell():
    """静态产物的入口必须是工作台（用户决定，2026-09-08）。

    Vercel 直接静态托管 `public/`，`/` 命中的是 `public/index.html`——它不经过
    FastAPI 的路由。后端把根路径改成工作台、而这份文件还是旧壳，本地与 Docker
    都对，**唯独生产还是旧页面**。
    """
    index = (ROOT / "public" / "index.html").read_text(encoding="utf-8")

    assert "/workbench/assets/" in index
    assert "衡鉴" not in index


def test_sent_link_paths_are_assembled_as_the_workbench_too():
    """`/register` `/reset-password` 的静态页同样要是工作台。

    邮件里已经发出去的链接指向它们；产物里留着旧壳，收件人打开的就还是旧页面。
    """
    for page in ("login", "register", "reset-password"):
        path = ROOT / "public" / page / "index.html"

        assert path.is_file(), page
        assert "/workbench/assets/" in path.read_text(encoding="utf-8"), page
