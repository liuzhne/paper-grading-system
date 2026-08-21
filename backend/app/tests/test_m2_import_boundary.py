"""M2 scoring Core 的静态依赖边界合同。

这组测试只解析源码 AST，不导入生产模块，因此即使 Core 的某个适配器依赖
尚未安装，也不会在收集阶段触发数据库、文件系统或网络副作用。M2 已关闭
全部已知边界债务；任何反向依赖、语法错误或新增副作用均是普通测试失败。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CORE_ROOT = PROJECT_ROOT / "backend/app/services/scoring/core"
CORE_PACKAGE = "backend.app.services.scoring.core"
FORBIDDEN_FRAMEWORK_ROOTS = {"fastapi", "sqlalchemy"}
# M2 closes the former evidence -> outer validator inversion.  Keep no
# grandfathered imports after the milestone: reintroducing that dependency is
# an ordinary boundary failure, not an expected xfail.
KNOWN_M2_IMPORT_DEBT: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class ImportViolation:
    path: Path
    lineno: int
    imported: str

    def render(self) -> str:
        return f"{self.path.relative_to(PROJECT_ROOT)}:{self.lineno}: {self.imported}"


def _source_files() -> tuple[Path, ...]:
    files = tuple(sorted(CORE_ROOT.rglob("*.py")))
    assert files, f"M2 Core source directory is missing or empty: {CORE_ROOT}"
    return files


def _parse(path: Path) -> ast.Module:
    # SyntaxError is intentionally exposed as a collection-time contract bug.
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_name(path: Path) -> str:
    relative = path.relative_to(PROJECT_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _resolve_from_import(path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""

    module_parts = _module_name(path).split(".")
    if path.name != "__init__.py":
        module_parts.pop()
    ascend = node.level - 1
    if ascend >= len(module_parts):
        return "<invalid-relative-import>"
    base = module_parts[: len(module_parts) - ascend]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def _is_forbidden_domain_import(imported: str) -> bool:
    """Core may depend on itself, but never on outer application layers."""

    if imported in {"<dynamic-import>", "<invalid-relative-import>"}:
        return True
    if imported.startswith("<dynamic-") or imported == "<invalid-relative-import>":
        return True
    if imported.split(".", 1)[0] in FORBIDDEN_FRAMEWORK_ROOTS | {
        "importlib",
        "pkgutil",
        "pydoc",
    }:
        return True
    if imported == CORE_PACKAGE or imported.startswith(f"{CORE_PACKAGE}."):
        return False
    return imported == "backend.app" or imported.startswith("backend.app.")


def _tree_domain_import_violations(
    path: Path,
    tree: ast.Module,
) -> tuple[ImportViolation, ...]:
    violations: list[ImportViolation] = []
    for node in ast.walk(tree):
        imported_names: list[str] = []
        if isinstance(node, ast.Import):
            imported_names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported_names.append(_resolve_from_import(path, node))
            if any(alias.name == "__import__" for alias in node.names):
                imported_names.append("<dynamic-import>")
        elif isinstance(node, ast.Name) and node.id == "__import__":
            # Any reference can be assigned and called later; Core uses
            # explicit registries and therefore has no valid dynamic-import
            # path at all.
            imported_names.append("<dynamic-import>")
        elif isinstance(node, ast.Attribute) and node.attr == "import_module":
            imported_names.append("<dynamic-import>")
        elif isinstance(node, ast.Attribute) and node.attr == "__import__":
            imported_names.append("<dynamic-import>")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "import_module"
        ):
            imported_names.append("<dynamic-import>")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec"}
        ):
            imported_names.append("<dynamic-execution>")

        for imported in imported_names:
            if _is_forbidden_domain_import(imported):
                violations.append(
                    ImportViolation(path=path, lineno=node.lineno, imported=imported)
                )
    return tuple(violations)


def _domain_import_violations() -> tuple[ImportViolation, ...]:
    violations: list[ImportViolation] = []
    for path in _source_files():
        violations.extend(_tree_domain_import_violations(path, _parse(path)))
    return tuple(violations)


DOMAIN_IMPORT_VIOLATIONS = _domain_import_violations()


def _violation_key(violation: ImportViolation) -> tuple[str, str]:
    return (
        violation.path.relative_to(PROJECT_ROOT).as_posix(),
        violation.imported,
    )


KNOWN_DEBT_VIOLATIONS = tuple(
    violation
    for violation in DOMAIN_IMPORT_VIOLATIONS
    if _violation_key(violation) in KNOWN_M2_IMPORT_DEBT
)
UNEXPECTED_IMPORT_VIOLATIONS = tuple(
    violation
    for violation in DOMAIN_IMPORT_VIOLATIONS
    if _violation_key(violation) not in KNOWN_M2_IMPORT_DEBT
)


@pytest.mark.parametrize(
    "imported",
    (
        "fastapi",
        "sqlalchemy.orm",
        "backend.app.db.models",
        "backend.app.services.scoring.validator",
        "<dynamic-import>",
        "<dynamic-execution>",
        "<invalid-relative-import>",
    ),
)
def test_boundary_classifier_rejects_every_frozen_forbidden_import_class(imported):
    assert _is_forbidden_domain_import(imported)


@pytest.mark.parametrize(
    "imported",
    ("decimal", "typing", CORE_PACKAGE, f"{CORE_PACKAGE}.canonical"),
)
def test_boundary_classifier_allows_stdlib_and_internal_core_imports(imported):
    assert not _is_forbidden_domain_import(imported)


@pytest.mark.parametrize(
    "source",
    (
        "from builtins import __import__ as load\nload('backend.app.db.models')",
        "import builtins\nbuiltins.__import__('backend.app.db.models')",
        "import importlib\nimportlib.import_module('backend.app.db.models')",
        "import pkgutil\npkgutil.resolve_name('backend.app.db.models:Paper')",
        "import pydoc\npydoc.locate('backend.app.db.models.Paper')",
        "import sqlalchemy.orm",
        "from fastapi import Depends",
    ),
)
def test_ast_gate_rejects_dynamic_framework_and_outer_layer_bypass_forms(source):
    path = CORE_ROOT / "synthetic_boundary_probe.py"
    tree = ast.parse(source, filename=str(path))

    assert _tree_domain_import_violations(path, tree)


def test_core_adds_no_new_forbidden_domain_framework_or_dynamic_imports():
    details = "\n".join(item.render() for item in UNEXPECTED_IMPORT_VIOLATIONS)
    assert UNEXPECTED_IMPORT_VIOLATIONS == (), (
        "new scoring Core boundary violations are never covered by the known-debt xfail:\n"
        + details
    )
    assert len(KNOWN_DEBT_VIOLATIONS) <= len(KNOWN_M2_IMPORT_DEBT), (
        "a known forbidden import was duplicated instead of removed"
    )


def test_core_removes_the_exact_known_outer_validator_dependency():
    assert KNOWN_DEBT_VIOLATIONS == ()


@pytest.mark.parametrize(
    "forbidden_name",
    ("Paper", "ParsedPaper", "ScoringRun", "ScoreItem"),
)
def test_core_does_not_name_legacy_orm_entities(forbidden_name: str):
    occurrences: list[str] = []
    for path in _source_files():
        tree = _parse(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == forbidden_name:
                occurrences.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                )
            elif isinstance(node, ast.Attribute) and node.attr == forbidden_name:
                occurrences.append(
                    f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}"
                )

    assert occurrences == [], (
        f"legacy entity {forbidden_name!r} leaked into scoring Core: "
        + ", ".join(occurrences)
    )


def _dotted_name(node: ast.AST) -> str | None:
    parts: list[str] = []
    cursor = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if not isinstance(cursor, ast.Name):
        return None
    parts.append(cursor.id)
    return ".".join(reversed(parts))


def _import_aliases(tree: ast.Module, path: Path) -> dict[str, str]:
    aliases: dict[str, str] = {"open": "builtins.open"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".", 1)[0]] = item.name
        elif isinstance(node, ast.ImportFrom):
            module = _resolve_from_import(path, node)
            for item in node.names:
                if item.name != "*":
                    aliases[item.asname or item.name] = f"{module}.{item.name}"

    # Resolve simple aliases such as ``wall_now = datetime.datetime.now``.
    for _ in range(3):
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            dotted = _dotted_name(value)
            if dotted is None:
                continue
            first, *rest = dotted.split(".")
            resolved = ".".join((aliases.get(first, first), *rest))
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and aliases.get(target.id) != resolved:
                    aliases[target.id] = resolved
                    changed = True
        if not changed:
            break
    return aliases


def test_core_has_no_direct_filesystem_network_or_wall_clock_access():
    # Conservative by design: Core receives all of these effects through
    # ports. Pure path manipulation also belongs in an adapter because M2 Core
    # contracts explicitly exclude local paths.
    forbidden_import_roots = {
        "aiohttp",
        "http",
        "httpx",
        "os",
        "pathlib",
        "requests",
        "shutil",
        "socket",
        "sqlite3",
        "subprocess",
        "tempfile",
        "time",
        "urllib",
    }
    forbidden_effect_calls = {
        "builtins.open",
        "asyncio.open_connection",
        "asyncio.start_server",
        "datetime.date.today",
        "datetime.datetime.now",
        "datetime.datetime.today",
        "datetime.datetime.utcnow",
        "io.open",
        "io.FileIO",
        "os.environ.get",
        "os.getenv",
        "os.open",
        "os.read",
        "os.write",
        "sqlite3.connect",
        "time.monotonic",
        "time.perf_counter",
        "time.time",
        "time.time_ns",
    }
    forbidden_uses: list[str] = []

    for path in _source_files():
        tree = _parse(path)
        aliases = _import_aliases(tree, path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [item.name for item in node.names]
                for name in imported:
                    if name.split(".", 1)[0] in forbidden_import_roots:
                        forbidden_uses.append(
                            f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: import {name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = _resolve_from_import(path, node)
                if module.split(".", 1)[0] in forbidden_import_roots:
                    forbidden_uses.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: from {module}"
                    )
            elif isinstance(node, ast.Call):
                dotted = _dotted_name(node.func)
                if dotted is None:
                    continue
                first, *rest = dotted.split(".")
                resolved = ".".join((aliases.get(first, first), *rest))
                if resolved in forbidden_effect_calls:
                    forbidden_uses.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: {resolved}(...)"
                    )
            elif isinstance(node, ast.Subscript):
                dotted = _dotted_name(node.value)
                if dotted is None:
                    continue
                first, *rest = dotted.split(".")
                resolved = ".".join((aliases.get(first, first), *rest))
                if resolved == "os.environ":
                    forbidden_uses.append(
                        f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}: os.environ[...]"
                    )

    assert forbidden_uses == [], (
        "M2 Core must receive I/O and time through ports, not access them directly:\n"
        + "\n".join(forbidden_uses)
    )
