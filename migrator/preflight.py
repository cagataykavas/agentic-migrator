from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .adapters import AdapterRegistry
from .parallel import MigrationTask, ParallelMigrationExecutor
from .tracing import TraceRecorder

_IGNORED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


@dataclass(frozen=True)
class PreflightSource:
    path: str
    language: str
    lines: int
    bytes: int
    risk_score: float
    high_risk_match: str | None = None


@dataclass(frozen=True)
class PreflightValidation:
    path: str
    language: str
    valid: bool
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class PreflightReport:
    root: str
    discovered: tuple[PreflightSource, ...]
    validated: tuple[PreflightValidation, ...]
    review_lane: tuple[PreflightSource, ...]
    failed_workers: tuple[dict[str, Any], ...]
    execution: dict[str, float | int]
    trace: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_ignored(path: Path) -> bool:
    return any(part in _IGNORED_PARTS for part in path.parts)


def _matched_pattern(relative_path: str, patterns: tuple[str, ...]) -> str | None:
    return next((pattern for pattern in patterns if fnmatch.fnmatch(relative_path, pattern)), None)


def estimate_preflight_risk(
    *,
    relative_path: str,
    line_count: int,
    high_risk_patterns: tuple[str, ...] = (),
) -> tuple[float, str | None]:
    """Return a transparent operational heuristic, not a semantic safety probability.

    Public-demo risk is intentionally simple and explainable: larger files receive a
    gradually higher score and an explicit user-supplied glob can add a high-risk bump.
    The score only controls which preflight execution lane a file enters.
    """
    size_component = min(max(line_count, 0) / 2_000.0, 0.65)
    matched = _matched_pattern(relative_path, high_risk_patterns)
    explicit_component = 0.30 if matched is not None else 0.0
    return min(1.0, round(0.05 + size_component + explicit_component, 4)), matched


def discover_sources(
    root: str | Path,
    *,
    registry: AdapterRegistry | None = None,
    high_risk_patterns: tuple[str, ...] = (),
) -> tuple[PreflightSource, ...]:
    root_path = Path(root).resolve()
    if not root_path.exists() or not root_path.is_dir():
        raise ValueError(f"preflight root must be an existing directory: {root_path}")

    registry = registry or AdapterRegistry()
    suffixes = set(registry.supported_suffixes())
    discovered: list[PreflightSource] = []

    for path in sorted(root_path.rglob("*")):
        if not path.is_file() or _is_ignored(path.relative_to(root_path)):
            continue
        if path.suffix.lower() not in suffixes:
            continue

        relative = path.relative_to(root_path).as_posix()
        content = path.read_text(encoding="utf-8")
        unit = registry.source_unit(relative, content)
        line_count = len(unit.content.splitlines())
        risk_score, matched = estimate_preflight_risk(
            relative_path=relative,
            line_count=line_count,
            high_risk_patterns=high_risk_patterns,
        )
        discovered.append(
            PreflightSource(
                path=relative,
                language=unit.language,
                lines=line_count,
                bytes=len(unit.content.encode("utf-8")),
                risk_score=risk_score,
                high_risk_match=matched,
            )
        )

    return tuple(discovered)


def run_repository_preflight(
    root: str | Path,
    *,
    max_workers: int = 4,
    automatic_risk_ceiling: float = 0.80,
    high_risk_patterns: tuple[str, ...] = (),
    trace_output: str | Path | None = None,
) -> PreflightReport:
    root_path = Path(root).resolve()
    registry = AdapterRegistry()
    recorder = TraceRecorder(output=trace_output)

    with recorder.span("preflight.repository", root=str(root_path)):
        with recorder.span("preflight.discover"):
            discovered = discover_sources(
                root_path,
                registry=registry,
                high_risk_patterns=high_risk_patterns,
            )
        recorder.event(
            "preflight.discovered",
            files=len(discovered),
            languages=sorted({item.language for item in discovered}),
        )

        by_path = {item.path: item for item in discovered}

        def validate(relative_path: str) -> PreflightValidation:
            content = (root_path / relative_path).read_text(encoding="utf-8")
            result = registry.validate(relative_path, content)
            return PreflightValidation(
                path=relative_path,
                language=by_path[relative_path].language,
                valid=result.valid,
                diagnostics=result.diagnostics,
            )

        tasks = [
            MigrationTask(item.path, item.path, risk_score=item.risk_score)
            for item in discovered
        ]
        executor: ParallelMigrationExecutor[str, PreflightValidation] = ParallelMigrationExecutor(
            validate,
            max_workers=max_workers,
            automatic_risk_ceiling=automatic_risk_ceiling,
        )

        with recorder.span(
            "preflight.parallel_validate",
            max_workers=max_workers,
            automatic_risk_ceiling=automatic_risk_ceiling,
        ):
            results, review_tasks = executor.run(tasks)

        validations = tuple(
            result.value
            for result in results
            if result.success and result.value is not None
        )
        failed_workers = tuple(
            {
                "path": result.task_id,
                "error_type": result.error_type,
                "error_message": result.error_message,
            }
            for result in results
            if not result.success
        )
        review_lane = tuple(by_path[task.task_id] for task in review_tasks)
        execution = executor.summary(results, review_tasks)

        recorder.event(
            "preflight.completed",
            validated=len(validations),
            invalid=sum(not item.valid for item in validations),
            review_required=len(review_lane),
            worker_failures=len(failed_workers),
        )

    return PreflightReport(
        root=str(root_path),
        discovered=discovered,
        validated=validations,
        review_lane=review_lane,
        failed_workers=failed_workers,
        execution=execution,
        trace=recorder.summary(),
    )
