from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SourceFile:
    path: str
    lines: int
    imports: tuple[str, ...]
    local_dependencies: tuple[str, ...]
    syntax_valid: bool
    parse_error: str | None = None


@dataclass(frozen=True)
class MigrationUnit:
    path: str
    order: int
    risk_score: int
    reasons: tuple[str, ...]
    depends_on: tuple[str, ...]


@dataclass
class ProjectInventory:
    root: Path
    files: dict[str, SourceFile] = field(default_factory=dict)

    @property
    def total_lines(self) -> int:
        return sum(item.lines for item in self.files.values())

    @property
    def invalid_files(self) -> list[SourceFile]:
        return [item for item in self.files.values() if not item.syntax_valid]

    def dependency_edges(self) -> list[tuple[str, str]]:
        edges: list[tuple[str, str]] = []
        for source in self.files.values():
            for dependency in source.local_dependencies:
                edges.append((source.path, dependency))
        return edges


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "build",
    "dist",
}


def module_name_from_path(path: Path) -> str:
    without_suffix = path.with_suffix("")
    parts = list(without_suffix.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def parse_imports(source: str) -> tuple[str, ...]:
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                module = "." * node.level + module
            imports.append(module)
    return tuple(dict.fromkeys(imports))


def discover_python_files(root: Path) -> list[Path]:
    result: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        result.append(path)
    return sorted(result)


def build_inventory(root: str | Path) -> ProjectInventory:
    root_path = Path(root).resolve()
    discovered = discover_python_files(root_path)

    module_to_path: dict[str, str] = {}
    for absolute in discovered:
        relative = absolute.relative_to(root_path)
        module_to_path[module_name_from_path(relative)] = relative.as_posix()

    inventory = ProjectInventory(root=root_path)
    for absolute in discovered:
        relative = absolute.relative_to(root_path)
        relative_name = relative.as_posix()
        text = absolute.read_text(encoding="utf-8")
        line_count = len(text.splitlines())

        try:
            imports = parse_imports(text)
            syntax_valid = True
            parse_error = None
        except SyntaxError as exc:
            imports = ()
            syntax_valid = False
            parse_error = f"{exc.msg} at line {exc.lineno}"

        local_dependencies: list[str] = []
        current_package = list(relative.with_suffix("").parts[:-1])
        for imported in imports:
            if imported.startswith("."):
                level = len(imported) - len(imported.lstrip("."))
                suffix = imported[level:]
                package = current_package[: max(0, len(current_package) - level + 1)]
                candidate = ".".join(package + ([suffix] if suffix else []))
            else:
                candidate = imported

            # Match the most specific local module prefix.
            matches = [
                (module_name, path)
                for module_name, path in module_to_path.items()
                if candidate == module_name or candidate.startswith(module_name + ".")
            ]
            if matches:
                _, dependency_path = max(matches, key=lambda item: len(item[0]))
                if dependency_path != relative_name:
                    local_dependencies.append(dependency_path)

        inventory.files[relative_name] = SourceFile(
            path=relative_name,
            lines=line_count,
            imports=imports,
            local_dependencies=tuple(dict.fromkeys(local_dependencies)),
            syntax_valid=syntax_valid,
            parse_error=parse_error,
        )

    return inventory


def _strongly_connected_components(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan SCC implementation used to identify dependency cycles."""
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for neighbor in graph.get(node, set()):
            if neighbor not in indices:
                visit(neighbor)
                lowlinks[node] = min(lowlinks[node], lowlinks[neighbor])
            elif neighbor in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[neighbor])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(component)

    for node in graph:
        if node not in indices:
            visit(node)
    return components


def migration_plan(
    inventory: ProjectInventory,
    *,
    high_risk_imports: Iterable[str] = (),
) -> list[MigrationUnit]:
    high_risk = tuple(high_risk_imports)
    graph = {
        path: set(item.local_dependencies)
        for path, item in inventory.files.items()
    }

    components = _strongly_connected_components(graph)
    component_id: dict[str, int] = {}
    for idx, component in enumerate(components):
        for path in component:
            component_id[path] = idx

    component_dependencies: dict[int, set[int]] = {
        idx: set() for idx in range(len(components))
    }
    for path, dependencies in graph.items():
        src_component = component_id[path]
        for dependency in dependencies:
            dst_component = component_id[dependency]
            if src_component != dst_component:
                component_dependencies[src_component].add(dst_component)

    ordered_components: list[int] = []
    visited: set[int] = set()

    def visit_component(component: int) -> None:
        if component in visited:
            return
        visited.add(component)
        for dependency in sorted(component_dependencies[component]):
            visit_component(dependency)
        ordered_components.append(component)

    for component in range(len(components)):
        visit_component(component)

    units: list[MigrationUnit] = []
    order = 1
    for component in ordered_components:
        members = sorted(components[component])
        cyclic = len(members) > 1
        for path in members:
            source = inventory.files[path]
            risk = 10
            reasons: list[str] = []

            if not source.syntax_valid:
                risk += 45
                reasons.append("source_does_not_parse")
            if source.lines > 500:
                risk += 18
                reasons.append("large_file")
            elif source.lines > 250:
                risk += 10
                reasons.append("medium_large_file")

            dependency_count = len(source.local_dependencies)
            if dependency_count >= 6:
                risk += 15
                reasons.append("high_local_coupling")
            elif dependency_count >= 3:
                risk += 7
                reasons.append("moderate_local_coupling")

            if cyclic:
                risk += 18
                reasons.append("dependency_cycle")

            if high_risk and any(
                imported.startswith(prefix)
                for imported in source.imports
                for prefix in high_risk
            ):
                risk += 20
                reasons.append("high_risk_target_api")

            if not reasons:
                reasons.append("low_structural_risk")

            units.append(
                MigrationUnit(
                    path=path,
                    order=order,
                    risk_score=min(risk, 100),
                    reasons=tuple(reasons),
                    depends_on=source.local_dependencies,
                )
            )
            order += 1

    return units


def plan_as_markdown(plan: Iterable[MigrationUnit]) -> str:
    rows = list(plan)
    body = "\n".join(
        f"| {unit.order} | `{unit.path}` | {unit.risk_score} | "
        f"{', '.join(unit.reasons)} | {', '.join(unit.depends_on) or '—'} |"
        for unit in rows
    )
    return (
        "# Repository migration plan\n\n"
        "| Order | File | Risk | Reasons | Depends on |\n"
        "|---:|---|---:|---|---|\n"
        f"{body}\n"
    )
