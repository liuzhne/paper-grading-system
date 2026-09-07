"""前端调用的端点必须真实存在（前端 v2 计划 §12.2 的 schema 差异检查）。

前端是纯 JS，调错路径不会有任何编译期报错——它在浏览器里表现为一个 404，
而 404 常被页面当成「暂无数据」渲染掉。后端改名或下线一个路由时，没有任何
东西会告诉写前端的人。

这条门禁从源码里把调用路径抠出来，逐条比对 FastAPI 真实的 OpenAPI。它不检查
请求体与响应形状（那由各自的接口测试负责），只回答一个问题：**这个地址还在吗**。

方法与路径分开断言：路径存在但方法不对（比如 POST 改成了 PATCH）同样是 404
级别的故障，且更难在浏览器里看出来。
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from backend.app.core.config import settings
from backend.app.main import app


FRONTEND_SRC = (
    pathlib.Path(__file__).resolve().parents[3] / "frontend" / "workbench" / "src"
)

#: `api.get("/x")` / `api.post(`/x/${id}`, body)` 两种写法都要抓到。
_CALL = re.compile(
    r"""api\.(get|post|patch|del)\(\s*(["'`])(.+?)\2""",
    re.DOTALL,
)

_METHODS = {"get": "get", "post": "post", "patch": "patch", "del": "delete"}


def _normalise(path):
    """把调用路径与 OpenAPI 路径归一到同一形状。

    前端写 `${batchId}`、OpenAPI 写 `{batch_id}`，两者指的是同一个位置；查询串
    不参与路由匹配，去掉。
    """
    path = path.split("?", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    path = re.sub(r"\{[^}]*\}", "{}", path)
    return path.rstrip("/") or "/"


def _frontend_calls():
    """扫 `.js` 与 `.vue` 两种。页面级调用都写在 SFC 的 `<script setup>` 里，
    只扫 `.js` 会漏掉一多半，而漏掉的部分恰好静默通过。
    """
    calls = set()
    sources = sorted(FRONTEND_SRC.rglob("*.js")) + sorted(FRONTEND_SRC.rglob("*.vue"))
    for source in sources:
        if source.name.endswith(".test.js"):
            continue
        for method, _quote, raw in _CALL.findall(source.read_text(encoding="utf-8")):
            # 拼接出来的路径（如 `${base}/x`）无法静态判定，跳过而不是猜。
            if raw.startswith("$") or not raw.startswith("/"):
                continue
            calls.add((_METHODS[method], _normalise(raw)))
    return calls


def _openapi_operations():
    prefix = settings.API_PREFIX.rstrip("/")
    operations = set()
    for path, methods in app.openapi()["paths"].items():
        if prefix and path.startswith(prefix):
            path = path[len(prefix) :]
        for method in methods:
            operations.add((method.lower(), _normalise(path)))
    return operations


def test_the_extractor_actually_finds_calls():
    """归一化写错时集合会变空，所有断言都会「通过」。先证明它非空。"""
    calls = _frontend_calls()

    assert len(calls) >= 20, calls
    assert ("get", "/batches/overview") in calls
    assert ("post", "/batches/{}/score") in calls


def test_every_frontend_call_hits_a_real_endpoint():
    missing = sorted(_frontend_calls() - _openapi_operations())

    assert not missing, "前端调用了不存在的端点：\n" + "\n".join(
        "  %s %s" % (method.upper(), path) for method, path in missing
    )


@pytest.mark.parametrize(
    "path",
    [
        "/system/capabilities",
        "/batches/overview",
        "/batches/{}/review-queue",
        "/batches/{}/export-precheck",
        "/rubrics/{}/rule-coverage",
    ],
)
def test_v2_endpoints_stay_published(path):
    """这几条是 v2 页面独有的入口，旧 SPA 不会替它们兜底。"""
    published = {p for _method, p in _openapi_operations()}

    assert path in published


def test_openapi_snapshot_is_deterministic(tmp_path):
    """快照要能用来做差异比对，两次导出必须逐字节一致。"""
    from backend.app.scripts.dump_openapi import dump

    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    dump(first)
    dump(second)

    assert first.read_bytes() == second.read_bytes()
    document = json.loads(first.read_text(encoding="utf-8"))
    assert document["openapi"].startswith("3.")
    assert document["paths"]
