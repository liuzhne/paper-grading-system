from pathlib import Path
import re
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
