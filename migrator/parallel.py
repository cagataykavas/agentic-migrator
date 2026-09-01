from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class MigrationTask(Generic[T]):
    task_id: str
    payload: T
    risk_score: float = 0.0


@dataclass(frozen=True)
class MigrationTaskResult(Generic[R]):
    task_id: str
    success: bool
    duration_ms: float
    value: R | None = None
    error_type: str | None = None
    error_message: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class ParallelMigrationExecutor(Generic[T, R]):
    """Execute independent migration units concurrently with deterministic reporting.

    The executor deliberately separates execution order from result order. Tasks may finish
    in any order, but callers receive results sorted by task id so benchmark output and CI
    artifacts remain reproducible. A risk ceiling can force high-risk units to remain out
    of automatic parallel execution and be handled by a review lane instead.
    """

    def __init__(
        self,
        worker: Callable[[T], R],
        *,
        max_workers: int = 4,
        automatic_risk_ceiling: float = 0.80,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if not 0.0 <= automatic_risk_ceiling <= 1.0:
            raise ValueError("automatic_risk_ceiling must be in [0, 1]")
        self.worker = worker
        self.max_workers = max_workers
        self.automatic_risk_ceiling = automatic_risk_ceiling

    def partition(
        self, tasks: Iterable[MigrationTask[T]]
    ) -> tuple[list[MigrationTask[T]], list[MigrationTask[T]]]:
        automatic: list[MigrationTask[T]] = []
        review: list[MigrationTask[T]] = []
        for task in tasks:
            (automatic if task.risk_score <= self.automatic_risk_ceiling else review).append(task)
        return automatic, review

    def run(
        self, tasks: Iterable[MigrationTask[T]]
    ) -> tuple[list[MigrationTaskResult[R]], list[MigrationTask[T]]]:
        automatic, review = self.partition(tasks)
        if not automatic:
            return [], sorted(review, key=lambda task: task.task_id)

        futures: dict[Future[R], tuple[MigrationTask[T], int]] = {}
        results: list[MigrationTaskResult[R]] = []

        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="migration") as pool:
            for task in automatic:
                started = time.perf_counter_ns()
                futures[pool.submit(self.worker, task.payload)] = (task, started)

            for future in as_completed(futures):
                task, started = futures[future]
                duration_ms = (time.perf_counter_ns() - started) / 1_000_000
                try:
                    value = future.result()
                except Exception as exc:  # noqa: BLE001 - worker failures are data, not fatal here.
                    results.append(
                        MigrationTaskResult(
                            task_id=task.task_id,
                            success=False,
                            duration_ms=duration_ms,
                            error_type=type(exc).__name__,
                            error_message=str(exc),
                        )
                    )
                else:
                    results.append(
                        MigrationTaskResult(
                            task_id=task.task_id,
                            success=True,
                            duration_ms=duration_ms,
                            value=value,
                        )
                    )

        return sorted(results, key=lambda result: result.task_id), sorted(
            review, key=lambda task: task.task_id
        )

    @staticmethod
    def summary(
        results: Iterable[MigrationTaskResult[R]], review: Iterable[MigrationTask[T]]
    ) -> dict[str, float | int]:
        materialized = list(results)
        review_items = list(review)
        completed = len(materialized)
        successes = sum(result.success for result in materialized)
        return {
            "completed": completed,
            "succeeded": successes,
            "failed": completed - successes,
            "review_required": len(review_items),
            "success_rate": successes / completed if completed else 0.0,
            "wall_work_ms": round(sum(result.duration_ms for result in materialized), 3),
        }
