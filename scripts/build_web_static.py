"""Materialize the static Web entrypoint for Vercel's edge CDN.

The browser app defaults to ``/api`` when the dynamic local-development
configuration is absent, so this production copy must not contain secrets or
environment-specific settings.
"""

from hashlib import sha256
from pathlib import Path
import re
from shutil import rmtree


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "web"
OUTPUT = ROOT / "public"
AUTH_PAGES = ("login", "register", "reset-password")
ASSET_REFERENCE = re.compile(
    r'(?P<prefix>["\'])/assets/(?P<name>app\.js|styles\.css)(?:\?[^"\']*)?(?P=prefix)'
)


def _fingerprinted_assets(index: str) -> tuple[str, dict[str, bytes]]:
    """Return HTML and immutable assets whose URLs change with their content."""

    assets: dict[str, bytes] = {}
    names: dict[str, str] = {}
    for source_name in ("app.js", "styles.css"):
        payload = (SOURCE / "assets" / source_name).read_bytes()
        stem, suffix = source_name.rsplit(".", 1)
        output_name = f"{stem}.{sha256(payload).hexdigest()[:12]}.{suffix}"
        assets[output_name] = payload
        names[source_name] = output_name

    def replace_reference(match: re.Match[str]) -> str:
        quote = match.group("prefix")
        return f"{quote}/assets/{names[match.group('name')]}{quote}"

    rewritten, count = ASSET_REFERENCE.subn(replace_reference, index)
    if count != len(names):
        raise RuntimeError(
            f"expected {len(names)} static asset references, replaced {count}"
        )
    return rewritten, assets


def main() -> None:
    if OUTPUT.exists():
        rmtree(OUTPUT)
    (OUTPUT / "assets").mkdir(parents=True)

    index = (SOURCE / "index.html").read_text(encoding="utf-8")
    # Local FastAPI injects this marker for non-default API prefixes. In the
    # production CDN artifact the frontend safely falls back to same-origin
    # /api, which avoids rendering the page through the Python function.
    index = index.replace("    <!-- PGS_CLIENT_CONFIG -->\n", "", 1)
    index, assets = _fingerprinted_assets(index)
    (OUTPUT / "index.html").write_text(index, encoding="utf-8")
    for page in AUTH_PAGES:
        page_dir = OUTPUT / page
        page_dir.mkdir()
        (page_dir / "index.html").write_text(index, encoding="utf-8")
    for name, payload in assets.items():
        (OUTPUT / "assets" / name).write_bytes(payload)


if __name__ == "__main__":
    main()
