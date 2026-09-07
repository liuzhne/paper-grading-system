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
    assert "__PGS_CONFIG__" not in index
    asset_names = sorted(path.name for path in (public / "assets").iterdir())
    assert len(asset_names) == 2
    assert any(re.fullmatch(r"app\.[0-9a-f]{12}\.js", name) for name in asset_names)
    assert any(re.fullmatch(r"styles\.[0-9a-f]{12}\.css", name) for name in asset_names)
    for name in asset_names:
        assert f"/assets/{name}" in index
    assert "/assets/app.js?" not in index
    assert "/assets/styles.css?" not in index
    assert (public / "login" / "index.html").read_text(encoding="utf-8") == index


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
