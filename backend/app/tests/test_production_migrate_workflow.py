"""生产迁移工作流的安全约束（2026-09-08 生产 503 后建立）。

生产运行角色 `pgs_app` 没有 DDL，迁移必须用 owner 连接串。把它放进 GitHub
Secrets 由工作流使用，好过在某台笔记本上手工执行：凭据不进任何人的命令行与
shell 历史，每次执行有审批与日志。

但一个"能改生产库"的按钮本身就是风险。这里钉住几条不该被随手改掉的约束。
"""

import pathlib

import yaml


WORKFLOW = (
    pathlib.Path(__file__).resolve().parents[3]
    / ".github"
    / "workflows"
    / "production-migrate.yml"
)


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _job():
    return _workflow()["jobs"]["migrate"]


def _triggers(document):
    # YAML 把裸 `on:` 解析成布尔 True。
    return document.get("on", document.get(True))


def test_never_runs_automatically():
    """只能手动触发。跟着 push 跑意味着每次合并都动生产库。"""
    triggers = _triggers(_workflow())

    assert set(triggers) == {"workflow_dispatch"}


def test_requires_the_production_environment_approval():
    assert _job()["environment"] == "production"


def test_defaults_to_dry_run():
    """默认不写入。默认值是误点时真正生效的那个值。"""
    inputs = _triggers(_workflow())["workflow_dispatch"]["inputs"]

    assert inputs["dry_run"]["default"] is True
    assert inputs["expected_head"]["required"] is True


def test_the_write_step_is_gated_on_dry_run_being_off():
    steps = {step["name"]: step for step in _job()["steps"] if "name" in step}

    assert "false" in steps["Upgrade to head"]["if"]


def test_the_gate_accepts_both_the_boolean_and_the_string_form():
    """UI 触发给的是真布尔，`gh workflow run -f` 走 API 时可能是字符串。

    只写 `== false` 的话，命令行触发会让写入步骤被**静默跳过**，而整个 run 显示
    成功——「什么都没做但看起来做了」比失败更难发现。
    """
    steps = {step["name"]: step for step in _job()["steps"] if "name" in step}

    for name in ("Upgrade to head", "Verify with the runtime role"):
        condition = steps[name]["if"]
        assert "inputs.dry_run == false" in condition, name
        assert "inputs.dry_run == 'false'" in condition, name


def test_head_is_checked_against_the_commit():
    """输入的 head 要与本 commit 一致，误点不该把库带到没人打算去的版本。"""
    steps = [step.get("name", "") for step in _job()["steps"]]

    assert "Refuse to run unless the requested head matches this commit" in steps


def test_verification_uses_the_runtime_role_not_the_owner():
    """owner 什么都读得到，证明不了应用能不能工作。

    0026/0027 的新表缺 `pgs_app` 授权，就是只有用运行角色验证才暴露得出来。
    """
    steps = {step["name"]: step for step in _job()["steps"] if "name" in step}
    verify = steps["Verify with the runtime role"]

    assert "RUNTIME_DATABASE_URL" in verify["env"]["DATABASE_URL"]
    assert "MIGRATION_DATABASE_URL" not in verify["env"]["DATABASE_URL"]


def test_no_database_dump_is_uploaded_as_an_artifact():
    """生产库含学生 PII，不得进入 CI artifact（仓库既有约定）。"""
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "upload-artifact" not in text
    assert "pg_dump" not in text


def test_concurrency_prevents_two_migrations_at_once():
    assert _workflow()["concurrency"]["group"] == "production-database"
    assert _workflow()["concurrency"]["cancel-in-progress"] is False


def test_an_ipv6_only_host_is_refused_before_touching_the_database():
    """GitHub runner 没有 IPv6 出口。

    Supabase 的 Direct connection 只解析出 IPv6，在 runner 上必然
    「Network is unreachable」——那段 traceback 看起来像数据库挂了，实际只是
    选错了连接串。这条预检要排在任何数据库操作之前，并说清该换成 Session pooler。
    """
    steps = [step.get("name", "") for step in _job()["steps"]]
    text = WORKFLOW.read_text(encoding="utf-8")

    assert steps.index("Refuse an IPv6-only host early") < steps.index(
        "Survey the database before migrating"
    )
    assert "Session pooler" in text
    # Transaction pooler（6543）不支持迁移需要的会话级特性，要明确排除。
    assert "6543" in text


def test_the_runtime_check_covers_every_table_that_needed_a_grant():
    """运行角色验证要覆盖所有新建表，否则这道防线会随时间失效。

    每次新建表都是一次可能漏授权的机会（0026/0027 就漏过，由 0029 补）。验证清单
    停在旧表上，等于只证明了「以前那两张还能读」。
    """
    import re

    text = WORKFLOW.read_text(encoding="utf-8")
    versions = (
        pathlib.Path(__file__).resolve().parents[3] / "alembic" / "versions"
    )

    granted = set()
    for path in versions.glob("0*.py"):
        source = path.read_text(encoding="utf-8")
        if "TO pgs_app" not in source:
            continue
        granted |= set(re.findall(r"ON TABLE (\w+) TO pgs_app", source))
        for match in re.findall(r'^TABLE\s*=\s*"([^"]+)"', source, re.M):
            granted.add(match)
        for match in re.findall(r'^TABLES\s*=\s*\(([^)]*)\)', source, re.M):
            granted |= set(re.findall(r'"([^"]+)"', match))
        for match in re.findall(r'for table in \(([^)]*)\)', source):
            granted |= set(re.findall(r'"([^"]+)"', match))

    missing = sorted(table for table in granted if table not in text)
    assert not missing, "迁移给这些表授了权，但工作流没验证运行角色能读：%s" % (
        ", ".join(missing)
    )
