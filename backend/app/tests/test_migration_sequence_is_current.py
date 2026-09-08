"""部署校验里的迁移清单必须跟得上真实迁移链（2026-09-08 CI 暴露）。

`postgres_verifier.MIGRATION_SEQUENCE` 是人工维护的，末项当作预期 head。加了
0024–0028 却没更新它，PostgreSQL 门禁里 `verify_postgres` 直接抛
「unexpected alembic head」——**而它抛在建 fixture 之前**，于是后面那条
「有数据时拒绝 lossy downgrade」拿到的是一个空库，降级一路走通，报成
「lossy downgrade unexpectedly succeeded」。

真正的缺陷离报错点隔了两步：清单过期 → fixture 没建 → 守卫没数据可拒。
这条测试把它钉在源头。
"""

from alembic.config import Config
from alembic.script import ScriptDirectory

from backend.app.services.deployment.postgres_verifier import (
    EXPECTED_HEAD,
    MIGRATION_SEQUENCE,
)


def _script_directory():
    config = Config()
    config.set_main_option("script_location", "alembic")
    return ScriptDirectory.from_config(config)


def test_expected_head_matches_the_real_alembic_head():
    script = _script_directory()

    assert EXPECTED_HEAD == script.get_current_head()


def test_sequence_is_a_contiguous_tail_of_the_real_chain():
    """清单是从 0011 起的尾段（更早的版本不在生产边界内），但必须**连续**且
    以真实 head 结尾。

    逐版本升级用的就是这个清单；中间漏掉任何一版，那一版在 PostgreSQL 上从未
    被单独执行过——而这正是这条门禁存在的理由。
    """
    script = _script_directory()
    actual = [rev.revision for rev in script.walk_revisions()][::-1]
    start = actual.index(MIGRATION_SEQUENCE[0])

    assert list(MIGRATION_SEQUENCE) == actual[start:]
