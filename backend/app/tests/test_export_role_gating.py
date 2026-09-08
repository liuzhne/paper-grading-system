"""导出出口一律受角色限制（用户决定，2026-09-08）。

导出是成绩数据离开系统的地方：批次 xlsx 带全体学生的学号、姓名与各项得分，
HTML 报告与 JSON 带完整评语和证据引文。组织归属只回答「是不是本组织的数据」，
不回答「这个人该不该把它导出去」。

`write-sheet` 早有门控，四个 GET 出口没有——而带走数据的恰恰是 GET。
"""

import inspect

import pytest

from backend.app.api.routes import exports as routes


#: 会让数据离开系统的端点。新增出口必须进这个清单。
DATA_EXITS = (
    "list_export_logs",
    "export_batch",
    "report",
    "export_run_json",
    "write_sheet",
)


@pytest.mark.parametrize("name", DATA_EXITS)
def test_every_export_exit_is_role_gated(name):
    source = inspect.getsource(getattr(routes, name))

    assert "require_organization_role" in source, name


def test_no_route_in_the_module_escapes_the_list():
    """新增导出端点时不能漏进清单。

    清单是人工维护的，但「有没有漏」可以自动查：模块里每个路由函数要么在清单
    里，要么自己带门控。
    """
    import re

    source = inspect.getsource(routes)
    blocks = re.split(r"\n(?=@router\.)", source)
    missing = []
    for block in blocks:
        if not block.startswith("@router."):
            continue
        name = re.search(r"\ndef (\w+)\(", block)
        if not name:
            continue
        if name.group(1) in DATA_EXITS:
            continue
        if "require_organization_role" not in block:
            missing.append(name.group(1))

    assert not missing, "导出端点未列入清单且无门控：%s" % ", ".join(missing)
