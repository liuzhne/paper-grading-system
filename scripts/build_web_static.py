"""Materialize the static Web entrypoint for Vercel's edge CDN.

The browser app defaults to ``/api`` when the dynamic local-development
configuration is absent, so this production copy must not contain secrets or
environment-specific settings.

Entrypoints assembled here (frontend v2 plan §8.2/§8.3):

* the product site: the landing page at ``/`` and its sub page at ``/about/``
  (user decision, 2026-10-17; the legacy SPA that used to live at ``/`` was
  retired on 2026-09-08);
* the v2 workbench at ``/workbench/``, plus ``/login`` ``/register``
  ``/reset-password`` for links already sent by email.

All of them come out of one Vite build and share ``/workbench/assets/*``.

The workbench is opt-in via ``--with-workbench`` until the Node build step is
wired into CI and the Vercel build command. Enabling it requires an installed
``frontend/workbench/node_modules``; a missing toolchain is a hard failure
rather than a silent skip, so a release can never ship a stale workbench.
"""

import argparse
from hashlib import sha256
import json
from pathlib import Path
import re
from shutil import copytree, rmtree
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ROOT / "frontend" / "workbench"
OUTPUT = ROOT / "public"
AUTH_PAGES = ("login", "register", "reset-password")
#: 官网入口页：产物里的落点 → Vite 构建目录里的来源（相对 dist/ 与 public/workbench/）。
SITE_PAGES = {
    "index.html": "site/index.html",
    "about/index.html": "site/about/index.html",
}
ASSET_REFERENCE = re.compile(
    r'(?P<prefix>["\'])/assets/(?P<name>app\.js|styles\.css)(?:\?[^"\']*)?(?P=prefix)'
)


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

    # 官网两张入口页（宣传页与子页）由同一次构建产出。两张页都要本地宿主注入
    # 配置：宣传页查会话要用 API_PREFIX，申请表单要看 siteLeads 开关。
    for page in SITE_PAGES.values():
        html = dist / page
        if not html.is_file():
            raise SystemExit("workbench build produced no dist/%s" % page)
        if CLIENT_CONFIG_MARKER not in html.read_text(encoding="utf-8"):
            raise SystemExit(
                "dist/%s lost the PGS_CLIENT_CONFIG marker; local hosts could not "
                "inject API_PREFIX or the lead-form switch." % page
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


#: 宣传页的动效档位。`reduced`：固定为少动效版（不论访客的系统设置）；不指定即正常
#: 动效，访客自己开了「减少动态效果」时仍会照顾。
LANDING_MOTIONS = ("reduced",)
CLIENT_CONFIG_MARKER = "<!-- PGS_CLIENT_CONFIG -->"


def pin_landing_motion(html: str, motion: str) -> str:
    """把宣传页的动效档位写进入口页的全局配置。

    只给不经过 Python 的静态宿主用（Vercel 部署任务，用户决定 2026-10-17）：本地
    FastAPI 与 Lightsail 在请求时替换同一个标记注入 `API_PREFIX`，标记被这里占掉后
    就注入不了——所以这一步只跑在部署任务的工作区里，不进仓库的 `public/`。
    """

    if motion not in LANDING_MOTIONS:
        raise SystemExit("未知的宣传页动效档位：%s" % motion)
    if CLIENT_CONFIG_MARKER not in html:
        raise SystemExit(
            "宣传页入口里没有 %s 标记：已经写过动效档位，或产物不是本脚本组装的。"
            % CLIENT_CONFIG_MARKER
        )
    config = json.dumps({"landingMotion": motion}, ensure_ascii=False)
    return html.replace(
        CLIENT_CONFIG_MARKER,
        "<script>window.__PGS_CONFIG__ = %s;</script>" % config,
        1,
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
    parser.add_argument(
        "--landing-motion",
        choices=LANDING_MOTIONS,
        help=(
            "pin the landing page's motion level in the assembled entry page. "
            "Only for static hosts that never run FastAPI (the Vercel deploy "
            "job); never commit the result."
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
    OUTPUT.mkdir(parents=True, exist_ok=True)
    if preserved is not None:
        preserved.rename(workbench_output)

    if args.with_workbench:
        _build_workbench()

    # 入口页：`/` 是宣传页、`/about/` 是官网子页（用户决定，2026-10-17），三条
    # 公开路由仍是工作台。
    #
    # 这一步不能只改 FastAPI 的路由：Vercel 直接静态托管 `public/`，`/` 命中的
    # 是这里写出的 `index.html`，根本不经过 Python。后端改了而产物没改，本地与
    # Docker 都对，**唯独生产还是旧页面**。
    #
    # `/register` `/reset-password` 同样要写：邮件里已经发出去的链接指向它们，
    # 收件人不会重新拿到新链接。
    workbench_index = workbench_output / "index.html"
    if not workbench_index.is_file():
        raise SystemExit(
            "public/workbench/index.html 缺失，无法组装入口页。\n"
            "带 --with-workbench 重新构建，或确认仓库里已提交工作台产物。"
        )
    index = workbench_index.read_text(encoding="utf-8")
    for page in AUTH_PAGES:
        page_dir = OUTPUT / page
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "index.html").write_text(index, encoding="utf-8")

    # 官网页的来源留在 public/workbench/site/ 里：普通构建只保留工作台产物，
    # 要能从那里重新写出入口页，而不是要求每次都带 --with-workbench。
    if args.landing_motion:
        landing = workbench_output / SITE_PAGES["index.html"]
        if landing.is_file():
            landing.write_text(
                pin_landing_motion(landing.read_text(encoding="utf-8"), args.landing_motion),
                encoding="utf-8",
            )

    for target, source in SITE_PAGES.items():
        source_path = workbench_output / source
        if not source_path.is_file():
            raise SystemExit(
                "public/workbench/%s 缺失，无法组装官网入口页。\n"
                "带 --with-workbench 重新构建。" % source
            )
        target_path = OUTPUT / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")


if __name__ == "__main__":
    main()
