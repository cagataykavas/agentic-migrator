from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ast_rules import AttributeRewrite, ImportRewrite, KeywordRewrite, transform_source
from .gitops import ChangeSet
from .project_scan import ProjectInventory, build_inventory, migration_plan
from .semantic_diff import compare_public_api


@dataclass(frozen=True)
class FileTransformResult:
    path: str
    order: int
    risk_score: int
    reasons: tuple[str, ...]
    status: str
    applied_rules: tuple[str, ...] = ()
    api_breaking: bool = False
    api_diff: dict[str, object] | None = None


@dataclass
class RepositoryTransformPlan:
    root: str
    inventory: ProjectInventory = field(repr=False)
    changeset: ChangeSet = field(repr=False)
    files: list[FileTransformResult] = field(default_factory=list)

    @property
    def changed_files(self) -> int:
        return sum(item.status == "changed" for item in self.files)

    @property
    def skipped_files(self) -> int:
        return sum(item.status.startswith("skipped_") for item in self.files)

    @property
    def breaking_files(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.files if item.api_breaking)

    def as_dict(self) -> dict[str, object]:
        change_manifest = self.changeset.manifest()
        return {
            "root": self.root,
            "summary": {
                "python_files": len(self.inventory.files),
                "total_lines": self.inventory.total_lines,
                "changed_files": self.changed_files,
                "skipped_files": self.skipped_files,
                "breaking_files": list(self.breaking_files),
            },
            "files": [asdict(item) for item in self.files],
            "changes": change_manifest["changes"],
        }


def _source_map(root: Path, inventory: ProjectInventory) -> dict[str, str]:
    return {
        relative: (root / relative).read_text(encoding="utf-8")
        for relative in inventory.files
    }


def build_repository_transform_plan(
    root: str | Path,
    *,
    import_rewrites: tuple[ImportRewrite, ...] = (),
    keyword_rewrites: tuple[KeywordRewrite, ...] = (),
    attribute_rewrites: tuple[AttributeRewrite, ...] = (),
    high_risk_imports: tuple[str, ...] = (),
    max_risk: int = 100,
) -> RepositoryTransformPlan:
    """Build a repository-wide structural transform without touching the filesystem.

    Files are considered in the dependency-aware order produced by ``migration_plan``.
    Invalid Python files and files above ``max_risk`` are recorded as skipped instead
    of aborting the entire migration. Changed files retain a source diff in ``ChangeSet``
    and a public-API semantic diff for review.
    """
    if not 0 <= max_risk <= 100:
        raise ValueError("max_risk must be between 0 and 100")

    root_path = Path(root).resolve()
    inventory = build_inventory(root_path)
    changeset = ChangeSet(_source_map(root_path, inventory))
    units = migration_plan(inventory, high_risk_imports=high_risk_imports)
    results: list[FileTransformResult] = []

    for unit in units:
        source_info = inventory.files[unit.path]
        if not source_info.syntax_valid:
            results.append(
                FileTransformResult(
                    path=unit.path,
                    order=unit.order,
                    risk_score=unit.risk_score,
                    reasons=unit.reasons,
                    status="skipped_invalid_syntax",
                )
            )
            continue

        if unit.risk_score > max_risk:
            results.append(
                FileTransformResult(
                    path=unit.path,
                    order=unit.order,
                    risk_score=unit.risk_score,
                    reasons=unit.reasons,
                    status="skipped_risk_limit",
                )
            )
            continue

        before = changeset.read(unit.path)
        after, applied = transform_source(
            before,
            import_rewrites=import_rewrites,
            keyword_rewrites=keyword_rewrites,
            attribute_rewrites=attribute_rewrites,
        )
        if after == before:
            results.append(
                FileTransformResult(
                    path=unit.path,
                    order=unit.order,
                    risk_score=unit.risk_score,
                    reasons=unit.reasons,
                    status="unchanged",
                    applied_rules=applied,
                )
            )
            continue

        api_diff = compare_public_api(before, after)
        changeset.write(unit.path, after)
        results.append(
            FileTransformResult(
                path=unit.path,
                order=unit.order,
                risk_score=unit.risk_score,
                reasons=unit.reasons,
                status="changed",
                applied_rules=applied,
                api_breaking=api_diff.breaking,
                api_diff=api_diff.as_dict(),
            )
        )

    return RepositoryTransformPlan(
        root=str(root_path),
        inventory=inventory,
        changeset=changeset,
        files=results,
    )
