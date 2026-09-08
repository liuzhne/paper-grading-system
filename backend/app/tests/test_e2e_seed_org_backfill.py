"""验收种子的组织归属回填不能靠手工清单（前端 v2 计划 §12.1）。

鉴权模式下组织过滤真的生效，种子行必须归属到默认组织，否则登录进来看到的是
一个空系统——那会被误读成「数据没种上」。

原实现写死了 4 个模型，是按「今天有哪些查询按组织过滤」选的。带
`organization_id` 的模型有 17 个；哪天有人给 `RubricVersion` 或 `ExportEvent`
加上过滤，这份清单不会自己更新，验收会静默地变成空数据——**而空数据往往还是
「通过」**，因为断言写的是「看不到别的组织的东西」。

改成从映射注册表推导，清单就不会烂。
"""

from e2e_server.seed import organization_scoped_models


def test_every_nullable_org_column_is_covered():
    names = {model.__name__ for model in organization_scoped_models()}

    # 抽查几个原先漏掉的。
    assert {"RubricVersion", "ExportEvent"} <= names
    # 原清单里的仍在。
    assert {"Rubric", "GradingBatch", "Paper", "ScoringRun"} <= names


def test_not_null_columns_are_excluded():
    """NOT NULL 的列在建行时就必须给值，回填轮不到它们。

    把它们收进来只会掩盖「建行时忘了给组织」这个更该暴露的问题。
    """
    names = {model.__name__ for model in organization_scoped_models()}

    assert "ManualReviewTask" not in names
    assert "OrganizationMember" not in names


def test_the_list_is_derived_not_handwritten():
    import inspect

    from e2e_server import seed

    source = inspect.getsource(seed.organization_scoped_models)
    # 手抄一份模型名，正是这条测试要防的。
    assert "registry" in source or "mappers" in source
