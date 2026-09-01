from __future__ import annotations

import json
import tempfile
from pathlib import Path

from migrator.adapters import AdapterRegistry
from migrator.parallel import MigrationTask, ParallelMigrationExecutor
from migrator.tracing import TraceRecorder


def validate_source(item: tuple[str, str]) -> dict[str, object]:
    path, content = item
    registry = AdapterRegistry()
    result = registry.validate(path, content)
    return {
        "path": path,
        "language": registry.source_unit(path, content).language,
        "valid": result.valid,
        "diagnostics": list(result.diagnostics),
    }


def main() -> int:
    tasks = [
        MigrationTask("01-python", ("service.py", "def score(x):\n    return x + 1\n"), 0.15),
        MigrationTask("02-java", ("RiskService.java", "class RiskService { }\n"), 0.35),
        MigrationTask("03-js", ("client.js", "export const score = (x) => x + 1;\n"), 0.25),
        MigrationTask("99-review", ("LegacyCore.java", "class LegacyCore { }\n"), 0.92),
    ]

    with tempfile.TemporaryDirectory(prefix="agentic-migrator-demo-") as directory:
        trace_path = Path(directory) / "migration-trace.jsonl"
        recorder = TraceRecorder(trace_id="portfolio-demo", output=trace_path)
        executor = ParallelMigrationExecutor(validate_source, max_workers=3)

        with recorder.span("repository.migrate", candidate_units=len(tasks)):
            recorder.event("planning.complete", automatic_risk_ceiling=0.80)
            with recorder.span("repository.parallel_validation"):
                results, review = executor.run(tasks)
            recorder.event(
                "parallel.complete",
                completed=len(results),
                review_required=len(review),
            )

        report = {
            "execution": executor.summary(results, review),
            "results": [result.as_dict() for result in results],
            "review_lane": [task.task_id for task in review],
            "trace": recorder.summary(),
            "trace_jsonl": trace_path.read_text(encoding="utf-8").splitlines(),
        }
        print(json.dumps(report, indent=2, default=str))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
