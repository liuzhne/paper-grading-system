"""流水线分层（RUNBOOK 3.0，2026-11-07）：develop 只跑代码测试，main 才有发布门禁与部署。

这里钉住几条一改就会悄悄失效的约束：提交阶段不碰部署密钥；发布门禁复用同一份测试定义；
部署只在 push main 时运行；分片不漏文件、不重复；生产手动工作流只认 main。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(document: dict) -> dict:
    # YAML 把裸 `on:` 解析成布尔 True。
    return document.get("on", document.get(True))


def _shard_module():
    spec = importlib.util.spec_from_file_location("ci_test_shard", ROOT / "scripts" / "ci_test_shard.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_commit_stage_runs_only_for_develop_and_never_deploys():
    workflow = _load("tests.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch", "workflow_call"}
    assert triggers["pull_request"]["branches"] == ["develop"]
    assert triggers["push"]["branches"] == ["develop"]

    text = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")
    for marker in ("environment:", "secrets.", "docker compose", "deploy", "ssh ", "postgres:16"):
        assert marker not in text, marker
    assert workflow["permissions"] == {"contents": "read"}


def test_the_commit_stage_has_one_aggregate_check_for_branch_protection():
    jobs = _load("tests.yml")["jobs"]
    gate = jobs["tests-passed"]
    assert set(gate["needs"]) == set(jobs) - {"tests-passed"}
    assert gate["if"] == "${{ !cancelled() }}"  # 前面失败或被跳过时照样运行并报失败


def test_the_backend_matrix_and_the_shard_count_agree():
    job = _load("tests.yml")["jobs"]["unit-and-sqlite-migrations"]
    shards = job["strategy"]["matrix"]["shard"]
    assert job["strategy"]["fail-fast"] is False  # 一片失败不取消其他片，一次看到全部失败
    run = next(step["run"] for step in job["steps"] if step.get("name") == "Run this shard of the self-contained suite")
    assert f"--total {len(shards)}" in run
    assert 'if [[ ${#files[@]} -eq 0 ]]' in run  # 空列表等于不带参数，会把整套再跑一遍
    assert "python -m pytest -q" in run and "uv sync --locked" in str(job["steps"])


def test_every_test_file_lands_in_exactly_one_shard():
    shard = _shard_module()
    files = shard.test_files()
    assert "backend/app/tests/test_ci_pipeline.py" in files
    for total in (1, 2, 4, 7):
        parts = shard.partition(files, shard.load_weights(), total)
        flat = [name for part in parts for name in part]
        assert sorted(flat) == files and len(flat) == len(set(flat)), total
        assert all(part == sorted(part) for part in parts)  # 片内保持整套运行时的顺序
    assert shard.partition(files, shard.load_weights(), 4) == shard.partition(files, shard.load_weights(), 4)


def test_shards_use_both_default_pytest_file_patterns(tmp_path):
    """本地 pytest 会收集 ``*_test.py``；分片漏了它，CI 就悄悄少跑这些文件。"""
    shard = _shard_module()
    tests = tmp_path / "backend" / "app" / "tests"
    (tests / "sub").mkdir(parents=True)
    (tests / "__pycache__").mkdir()
    for name in ("test_a.py", "b_test.py", "test_c_test.py", "helper.py", "conftest.py", "sub/test_d.py", "__pycache__/test_e.py"):
        (tests / name).write_text("", encoding="utf-8")
    prefix = "backend/app/tests/"
    assert shard.test_files(tmp_path) == [
        prefix + "b_test.py",
        prefix + "sub/test_d.py",
        prefix + "test_a.py",
        prefix + "test_c_test.py",  # 两种规则都匹配，只出现一次
    ]


def test_pytest_keeps_the_default_file_patterns_the_shards_assume():
    """改了 python_files 或 testpaths，scripts/ci_test_shard.py 要一起改。"""
    config = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "python_files" not in config
    assert 'testpaths = ["backend/app/tests"]' in config
    assert not (ROOT / "pytest.ini").exists() and not (ROOT / "setup.cfg").exists()


def test_unknown_files_weigh_the_median_and_the_split_is_balanced():
    shard = _shard_module()
    files = ["a.py", "b.py", "c.py", "d.py", "new.py"]
    parts = shard.partition(files, {"a.py": 10, "b.py": 1, "c.py": 1, "d.py": 1}, 2)
    assert parts == [["a.py"], ["b.py", "c.py", "d.py", "new.py"]]


def test_the_shard_script_refuses_more_shards_than_files(monkeypatch, capsys):
    shard = _shard_module()
    monkeypatch.setattr(shard, "test_files", lambda root=None: ["backend/app/tests/test_only.py"])
    assert shard.main(["--shard", "1", "--total", "2"]) == 2
    assert "分不出 2 片" in capsys.readouterr().err


def test_the_release_workflow_runs_only_for_main_and_reuses_the_tests():
    workflow = _load("ci.yml")
    triggers = _triggers(workflow)
    assert triggers["push"]["branches"] == ["main"]
    assert triggers["pull_request"]["branches"] == ["main"]

    jobs = workflow["jobs"]
    assert jobs["tests"]["uses"] == "./.github/workflows/tests.yml"
    assert "unit-and-sqlite-migrations" not in jobs and "frontend-workbench" not in jobs  # 测试只定义在一处
    assert "tests" in jobs["docker-compose-smoke"]["needs"]


def test_only_develop_and_hotfix_branches_may_open_a_release_pr():
    job = _load("ci.yml")["jobs"]["release-source"]
    assert job["if"] == "github.event_name == 'pull_request'"
    run = job["steps"][0]["run"]
    assert "develop|hotfix/*)" in run
    assert '"$HEAD_REPO" = "$GITHUB_REPOSITORY"' in run  # fork 来的 develop 不算


def test_the_release_gate_covers_every_gate_and_tolerates_only_a_skipped_source_check():
    jobs = _load("ci.yml")["jobs"]
    gate = jobs["release-gates-passed"]
    assert set(gate["needs"]) == {
        "release-source",
        "tests",
        "postgres-migrations-constraints-and-restore",
        "docker-compose-smoke",
        "production-config",
    }
    assert gate["if"] == "${{ !cancelled() }}"
    run = gate["steps"][0]["run"]
    assert '[ "$EVENT" = "pull_request" ] && [ "$SOURCE" != "success" ]' in run


def test_deployments_happen_only_on_a_push_to_main_after_the_gates():
    jobs = _load("ci.yml")["jobs"]
    for name in ("deploy-lightsail-production", "deploy-vercel-production"):
        condition = jobs[name]["if"]
        assert "github.event_name == 'push'" in condition and "github.ref == 'refs/heads/main'" in condition
        assert {"docker-compose-smoke", "tests"} <= set(jobs[name]["needs"])
        # release-source 在 push 时被跳过；放进 needs 会让部署跟着被跳过。
        assert "release-source" not in jobs[name]["needs"]
    assert "production-config" in jobs["deploy-lightsail-production"]["needs"]


def test_the_cutover_refuses_anything_but_main():
    jobs = _load("production-cutover.yml")["jobs"]
    assert jobs["cutover"]["needs"] == "require-main"
    guard = jobs["require-main"]
    assert "environment" not in guard  # 守卫本身拿不到生产密钥
    assert '"$REF" != "refs/heads/main"' in guard["steps"][0]["run"]


def test_production_migrations_run_only_from_a_release_ref():
    """先迁移、后合并（RUNBOOK 11.12）：从 develop / hotfix/* 触发，功能分支不行。"""
    jobs = _load("production-migrate.yml")["jobs"]
    assert jobs["migrate"]["needs"] == "require-release-ref"
    guard = jobs["require-release-ref"]
    assert "environment" not in guard
    run = guard["steps"][0]["run"]
    assert "refs/heads/main|refs/heads/develop|refs/heads/hotfix/*)" in run
    assert run.rstrip().endswith("exit 1")  # 其余一律拒绝
