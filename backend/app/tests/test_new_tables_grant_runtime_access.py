"""新建表必须为生产最小权限角色配好访问（2026-09-08 生产 503 时发现）。

生产运行角色是 `pgs_app`，按设计**没有 DDL**，也不会自动获得新表的 DML 权限。
0023 把授权写进了迁移本身（「new tables must not depend on an out-of-band
grant/policy step」）；0026 与 0027 新建了两张表却没有照做。

后果不是迁移失败，而是**迁移成功之后应用照样报错**：schema 到了 head，运行角色
一碰 `review_command_receipts` / `export_events` 就 permission denied——把一个
故障换成另一个故障，且第二个更难认，因为版本号看起来是对的。

本地与 CI 不建 `pgs_app`，所以这条缺口在两边都测不出来。只能用源码契约兜住。

断言的是「链条里有某个迁移为它配好」，不是「建表那个迁移里」：0026/0027 已经在
本地与 CI 执行过，改动它们不会重跑，补授权只能靠新迁移。
"""

import pathlib
import re


VERSIONS = pathlib.Path(__file__).resolve().parents[3] / "alembic" / "versions"

#: 0023 之前的表在生产建库时已授权，不在本门禁范围内。
FIRST_GUARDED_REVISION = "0023"


def _migrations():
    for path in sorted(VERSIONS.glob("0*.py")):
        yield path.stem, path.read_text(encoding="utf-8")


def _tables_created():
    created = {}
    for name, source in _migrations():
        if name < FIRST_GUARDED_REVISION:
            continue
        for table in re.findall(r'op\.create_table\(\s*"([^"]+)"', source):
            created[table] = name
    return created


def _tables_granted():
    granted = set()
    for name, source in _migrations():
        if "pg_roles WHERE rolname = 'pgs_app'" not in source:
            continue
        # 表名可能直接写死，也可能来自模块级元组。
        literals = set(re.findall(r"ON TABLE (\w+) TO pgs_app", source))
        for match in re.findall(r'^(?:TABLES|_TABLES)\s*=\s*\(([^)]*)\)', source, re.M):
            literals |= set(re.findall(r'"([^"]+)"', match))
        if "% table" in source:
            for match in re.findall(r'for table in \(([^)]*)\)', source):
                literals |= set(re.findall(r'"([^"]+)"', match))
        granted |= literals
    return granted


def test_every_new_table_gets_runtime_access_somewhere_in_the_chain():
    created = _tables_created()
    granted = _tables_granted()

    missing = sorted(table for table in created if table not in granted)
    assert not missing, (
        "这些表没有任何迁移为 pgs_app 配好访问：%s（建表迁移：%s）"
        % (", ".join(missing), ", ".join(created[t] for t in missing))
    )


def test_the_grant_is_conditional_on_the_role_existing():
    """本地与 CI 没有 `pgs_app`，无条件执行会让所有非生产环境的迁移直接失败。"""
    for name, source in _migrations():
        if "TO pgs_app" not in source:
            continue
        assert "pg_roles WHERE rolname = 'pgs_app'" in source, name


def test_backfilled_grants_are_re_entrant():
    """补授权的迁移要能在已建表的环境上重跑。

    0026/0027 建的表在本地与 CI 已经存在，直接 `CREATE POLICY` 第二次就会失败。
    """
    source = (VERSIONS / "0029_runtime_access_for_v2_tables.py").read_text(
        encoding="utf-8"
    )

    assert "DROP POLICY IF EXISTS pgs_app_dml" in source
