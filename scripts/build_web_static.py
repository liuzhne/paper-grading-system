"""Materialize the static Web entrypoint for Vercel's edge CDN.

The browser app defaults to ``/api`` when the dynamic local-development
configuration is absent, so this production copy must not contain secrets or
environment-specific settings.

Two entrypoints are assembled here (frontend v2 plan §8.2/§8.3):

* the legacy SPA at ``/`` with its immutable assets under ``/assets/*``;
* the v2 workbench at ``/workbench/`` with its own assets under
  ``/workbench/assets/*``.

The workbench is opt-in via ``--with-workbench`` until the Node build step is
wired into CI and the Vercel build command. Enabling it requires an installed
``frontend/workbench/node_modules``; a missing toolchain is a hard failure
rather than a silent skip, so a release can never ship a stale workbench.
"""

import argparse
from hashlib import sha256
from pathlib import Path
import re
from shutil import copytree, rmtree
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "frontend" / "web"
WORKBENCH = ROOT / "frontend" / "workbench"
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


def _build_workbench() -> None:
    """Run the workbench production build and copy it under ``public/workbench``.

    Vite already emits content-hashed asset names, so no extra fingerprinting is
    applied here.
    """

    if not (WORKBENCH / "node_modules").is_dir():
        raise SystemExit(
            "frontend/workbench/node_modules is missing.\n"
            "Run `npm ci` in frontend/workbench before building with "
            "--with-workbench, or omit the flag to assemble the legacy entry "
            "only. Never publish a release with a stale workbench bundle."
        )

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    result = subprocess.run(
        [npm, "run", "build"],
        cwd=WORKBENCH,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            "workbench build failed:\n" + (result.stderr or result.stdout)
        )

    dist = WORKBENCH / "dist"
    index = dist / "index.html"
    if not index.is_file():
        raise SystemExit("workbench build produced no dist/index.html")

    # Unlike the legacy entry, this single artifact is served by *both* hosts:
    # the CDN (where the marker stays an inert comment and the app falls back to
    # same-origin /api) and local FastAPI/Docker, which replaces the marker to
    # inject a non-default API_PREFIX. Stripping it here would break the latter.
    if "<!-- PGS_CLIENT_CONFIG -->" not in index.read_text(encoding="utf-8"):
        raise SystemExit(
            "workbench dist/index.html lost the PGS_CLIENT_CONFIG marker; "
            "local hosts could not inject a non-default API_PREFIX."
        )

    copytree(dist, OUTPUT / "workbench")
    _copy_font_licence(OUTPUT / "workbench")


#: 自托管字体的许可证来源。SIL OFL 1.1 要求许可证随字体分发——字体文件进了产物、
#: 许可证没进，是一个安静的合规缺口：没有任何东西会报错。
FONT_LICENCE = (
    WORKBENCH / "node_modules" / "@fontsource" / "ibm-plex-mono" / "LICENSE"
)


def _copy_font_licence(target: Path) -> None:
    if not FONT_LICENCE.is_file():
        raise SystemExit(
            "IBM Plex Mono 的许可证文件缺失（%s）。\n"
            "字体是自托管的，许可证必须随产物一起发布；先在 "
            "frontend/workbench 跑 `npm ci`。" % FONT_LICENCE
        )
    (target / "LICENSE-IBM-Plex-Mono.txt").write_text(
        FONT_LICENCE.read_text(encoding="utf-8"), encoding="utf-8"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--with-workbench",
        action="store_true",
        help=(
            "also build and assemble the v2 workbench at /workbench/. "
            "Requires frontend/workbench/node_modules (npm ci)."
        ),
    )
    args = parser.parse_args(argv)

    # A plain run regenerates only the legacy entry. Any already-assembled
    # workbench artifact is preserved across the rebuild: wiping it here would
    # silently ship a deployment whose /workbench routes 404. Only
    # --with-workbench replaces it.
    preserved: Path | None = None
    workbench_output = OUTPUT / "workbench"
    if OUTPUT.exists():
        if workbench_output.is_dir() and not args.with_workbench:
            preserved = ROOT / ".build-workbench-carry"
            if preserved.exists():
                rmtree(preserved)
            workbench_output.rename(preserved)
        rmtree(OUTPUT)
    (OUTPUT / "assets").mkdir(parents=True)
    if preserved is not None:
        preserved.rename(workbench_output)

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

    if args.with_workbench:
        _build_workbench()


if __name__ == "__main__":
    main()
