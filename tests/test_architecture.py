import ast
import hashlib
from collections.abc import Iterable
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "src" / "dwp_agent"
APPLIED_MIGRATION_CHECKSUMS = {
    "V1__create_agent_runtime_control_plane.sql":
        "6bd37dc39e2b79a8047e9bddf5e48e262750503d8853f15d487b1b5c06543000",
    "V2__create_dwaion_conversations_and_feedback.sql":
        "0d12df2089765a3e406bf3188998758d3e337f75c0b1c9b59c50e8f80ca28aa6",
    "V3__extend_grounding_sources_for_approval_expert.sql":
        "e1c95e04ca773c48517eeeb8fab3c78e81bb7f3aaa31cfa5086074d2a8d1db17",
    "V4__govern_retention_and_encryption_key_versions.sql":
        "62d42c7c81345b02f6a6a30783022b17746598c03ef70e1d63e23cc84b5c14dd",
    "V5__audit_retention_policy_changes.sql":
        "4319840a4dcec6260da711506e6d633ca5978f4a92663e10636df5fb74f6a490",
    "V6__govern_sources_actions_safety_evaluation_and_audit.sql":
        "ceae76cf2c3a36cf9563caaf22ab48c9ea37259321c2cc2ae468f3271ab97758",
    "V7__expand_default_safety_source_scopes.sql":
        "5e21c2bcecd0ff4e937b769f62dde5cc0ee737282251b496642fa709ed921ee4",
    "V8__govern_evaluation_run_leases.sql":
        "4460e14e5f79fb2d9465298e62f442bb9612d8b8c2929fe2a87fd60ee4420b8e",
}


def test_runtime_uses_installable_src_package() -> None:
    assert (PACKAGE_ROOT / "__init__.py").is_file()
    assert not list(ROOT.glob("*.py"))
    assert (PACKAGE_ROOT / "migrations" / "V1__create_agent_runtime_control_plane.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V2__create_dwaion_conversations_and_feedback.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V4__govern_retention_and_encryption_key_versions.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V5__audit_retention_policy_changes.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V6__govern_sources_actions_safety_evaluation_and_audit.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V7__expand_default_safety_source_scopes.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V8__govern_evaluation_run_leases.sql").is_file()
    assert (
        PACKAGE_ROOT / "migrations" / "V12__disable_implicit_governance_defaults.sql"
    ).is_file()
    assert (PACKAGE_ROOT / "migrations" / "V13__adopt_envelope_encryption_v2.sql").is_file()
    assert (PACKAGE_ROOT / "migrations" / "V14__govern_agent_run_leases.sql").is_file()
    assert (
        PACKAGE_ROOT / "migrations" / "V15__fence_agent_run_lease_owners.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT / "migrations" / "V16__create_one_time_question_launches.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT
        / "migrations"
        / "V17__bind_conversation_messages_to_run_leases.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT
        / "migrations"
        / "V18__enforce_completed_conversation_message_leases.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT / "migrations" / "V19__create_governed_agent_proposals.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT
        / "migrations"
        / "V20__harden_agent_proposal_idempotency.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT
        / "migrations"
        / "V21__protect_meeting_workload_assertions_from_replay.sql"
    ).is_file()
    assert (
        PACKAGE_ROOT
        / "migrations"
        / "V22__govern_proactive_proposal_analysis.sql"
    ).is_file()


def test_migration_versions_are_unique() -> None:
    migrations = (PACKAGE_ROOT / "migrations").glob("V*__*.sql")
    versions = [migration.stem.split("__", 1)[0] for migration in migrations]

    assert len(versions) == len(set(versions))


def test_applied_migrations_are_immutable() -> None:
    migration_root = PACKAGE_ROOT / "migrations"
    actual = {
        name: hashlib.sha256((migration_root / name).read_bytes()).hexdigest()
        for name in APPLIED_MIGRATION_CHECKSUMS
    }
    assert actual == APPLIED_MIGRATION_CHECKSUMS


def test_runtime_modules_stay_within_reviewable_size() -> None:
    oversized = {
        path.relative_to(ROOT): len(path.read_text(encoding="utf-8").splitlines())
        for path in PACKAGE_ROOT.rglob("*.py")
        if len(path.read_text(encoding="utf-8").splitlines()) > 500
    }
    assert oversized == {}


def test_runtime_modules_have_no_import_time_cycles() -> None:
    modules = {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    graph = {
        module: _top_level_dependencies(path, modules)
        for module, path in modules.items()
    }
    cycles = _dependency_cycles(graph)

    assert cycles == []


def test_runtime_modules_are_reachable_from_application_entrypoint() -> None:
    modules = {_module_name(path): path for path in PACKAGE_ROOT.rglob("*.py")}
    graph = {
        module: _all_dependencies(path, modules)
        for module, path in modules.items()
    }
    entrypoints = {"dwp_agent", "dwp_agent.main"}

    assert entrypoints <= modules.keys()
    assert set(modules) - _reachable_modules(graph, entrypoints) == set()


def _module_name(path: Path) -> str:
    relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(("dwp_agent", *parts))


def _top_level_dependencies(
    path: Path, modules: dict[str, Path]
) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _dependencies(path, modules, tree.body)


def _all_dependencies(path: Path, modules: dict[str, Path]) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return _dependencies(path, modules, ast.walk(tree))


def _dependencies(
    path: Path,
    modules: dict[str, Path],
    nodes: Iterable[ast.AST],
) -> set[str]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    dependencies: set[str] = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            dependencies.update(
                alias.name for alias in node.names if alias.name in modules
            )
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        target = node.module or ""
        if node.level:
            package_parts = package.split(".")
            target_parts = package_parts[: len(package_parts) - node.level + 1]
            if target:
                target_parts.extend(target.split("."))
            target = ".".join(target_parts)
        if target in modules:
            dependencies.add(target)
        dependencies.update(
            child
            for alias in node.names
            if (child := f"{target}.{alias.name}") in modules
        )
    dependencies.discard(module)
    return dependencies


def _reachable_modules(
    graph: dict[str, set[str]], entrypoints: set[str]
) -> set[str]:
    reachable: set[str] = set()
    pending = list(entrypoints)
    while pending:
        module = pending.pop()
        if module in reachable:
            continue
        reachable.add(module)
        pending.extend(graph[module] - reachable)
    return reachable


def _dependency_cycles(graph: dict[str, set[str]]) -> list[tuple[str, ...]]:
    visited: set[str] = set()
    active: list[str] = []
    active_set: set[str] = set()
    cycles: set[tuple[str, ...]] = set()

    def visit(module: str) -> None:
        if module in visited:
            return
        if module in active_set:
            start = active.index(module)
            cycle = active[start:]
            rotations = [
                tuple(cycle[index:] + cycle[:index])
                for index in range(len(cycle))
            ]
            cycles.add(min(rotations))
            return
        active.append(module)
        active_set.add(module)
        for dependency in sorted(graph[module]):
            visit(dependency)
        active.pop()
        active_set.remove(module)
        visited.add(module)

    for module in sorted(graph):
        visit(module)
    return sorted(cycles)
