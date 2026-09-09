"""评分标准的写端点一律受角色门控（v3 计划 §4.2）。

`_visible_rubric` 只查组织归属，不查角色。**发布一个评分标准决定了全组织的论文
怎么被打分**；编辑草稿、批准 AI 起草的规则同理。这些端点此前难以触及只是因为旧
SPA 下线了 UI，而 v3 恰恰要把它们放回界面上——先补门控再暴露。

用源码契约而不是逐端点造数据：这类缺陷是「少写了一行守卫」，逐个搭 fixture 成本
高，且新增端点时极易漏掉。运行时判定另有 `test_ops_permissions.py` 覆盖同一套
`require_organization_role`。
"""

import inspect
import re

import pytest

from backend.app.api.routes import rubrics


def _write_routes():
    source = inspect.getsource(rubrics)
    blocks = re.split(r"\n(?=@router\.)", source)
    found = {}
    for block in blocks:
        header = block.split("\n", 1)[0]
        if not re.match(r"@router\.(post|patch|put|delete)\(", header):
            continue
        name = re.search(r"\ndef (\w+)\(", block)
        if name:
            found[name.group(1)] = block
    return found


def test_the_module_still_has_write_routes():
    """先证明这条门禁在看真东西——正则失配会让它变成一条永远通过的空测试。"""
    assert len(_write_routes()) >= 10


@pytest.mark.parametrize("name", sorted(_write_routes()))
def test_every_rubric_write_route_is_role_gated(name):
    block = _write_routes()[name]

    assert "require_organization_role" in block, (
        "%s 是写端点但没有角色门控；`_visible_rubric` 只查组织归属，不查角色。" % name
    )
