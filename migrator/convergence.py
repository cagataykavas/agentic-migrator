from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from .models import TestResult


class ConvergenceInputError(ValueError):
    """Raised when repair-loop evidence is incomplete or out of order."""


@dataclass(frozen=True)
class ConvergencePolicy:
    max_state_occurrences: int = 3
    max_unique_states: int = 8
    detect_two_cycle: bool = True

    def validate(self) -> None:
        if self.max_state_occurrences < 2:
            raise ConvergenceInputError("max_state_occurrences must be at least 2")
        if self.max_unique_states < 1:
            raise ConvergenceInputError("max_unique_states must be positive")
        if not isinstance(self.detect_two_cycle, bool):
            raise ConvergenceInputError("detect_two_cycle must be boolean")


@dataclass(frozen=True)
class ConvergenceObservation:
    attempt: int
    candidate_sha256: str
    test_source_sha256: str
    failure_sha256: str
    state_sha256: str
    active_rule_ids: tuple[str, ...]
    state_occurrences: int
    unique_state_count: int
    stalled: bool
    reason: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "candidate_sha256": self.candidate_sha256,
            "test_source_sha256": self.test_source_sha256,
            "failure_sha256": self.failure_sha256,
            "state_sha256": self.state_sha256,
            "active_rule_ids": list(self.active_rule_ids),
            "state_occurrences": self.state_occurrences,
            "unique_state_count": self.unique_state_count,
            "stalled": self.stalled,
            "reason": self.reason,
        }


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class RepairConvergenceGuard:
    """Detect repeated or oscillating repair states using content fingerprints."""

    def __init__(self, policy: ConvergencePolicy | None = None) -> None:
        self.policy = policy or ConvergencePolicy()
        self.policy.validate()
        self._state_history: list[str] = []
        self._state_counts: dict[str, int] = {}

    @staticmethod
    def _failure_fingerprint(result: TestResult) -> str:
        payload = {
            "kind": result.kind.value,
            "name": result.name.strip(),
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
        return _digest(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def observe(
        self,
        *,
        attempt: int,
        candidate: str,
        test_source: str,
        test_result: TestResult,
        active_rule_ids: tuple[str, ...] = (),
    ) -> ConvergenceObservation:
        expected_attempt = len(self._state_history) + 1
        if attempt != expected_attempt:
            raise ConvergenceInputError(
                f"attempt must be consecutive; expected {expected_attempt}, got {attempt}"
            )
        if not isinstance(candidate, str) or not isinstance(test_source, str):
            raise ConvergenceInputError("candidate and test_source must be strings")
        if test_result.passed:
            raise ConvergenceInputError("only failed test results can be observed")
        if any(not isinstance(rule_id, str) or not rule_id for rule_id in active_rule_ids):
            raise ConvergenceInputError("active_rule_ids must contain non-empty strings")

        candidate_sha = _digest(candidate)
        test_source_sha = _digest(test_source)
        failure_sha = self._failure_fingerprint(test_result)
        state_sha = _digest(f"{candidate_sha}:{test_source_sha}:{failure_sha}")
        self._state_history.append(state_sha)
        occurrences = self._state_counts.get(state_sha, 0) + 1
        self._state_counts[state_sha] = occurrences

        reason: str | None = None
        if occurrences >= self.policy.max_state_occurrences:
            reason = "repeated_repair_state"
        elif (
            self.policy.detect_two_cycle
            and len(self._state_history) >= 4
            and self._state_history[-4] == self._state_history[-2]
            and self._state_history[-3] == self._state_history[-1]
            and self._state_history[-2] != self._state_history[-1]
        ):
            reason = "two_state_oscillation"
        elif len(self._state_counts) > self.policy.max_unique_states:
            reason = "unique_state_budget_exceeded"

        return ConvergenceObservation(
            attempt=attempt,
            candidate_sha256=candidate_sha,
            test_source_sha256=test_source_sha,
            failure_sha256=failure_sha,
            state_sha256=state_sha,
            active_rule_ids=tuple(sorted(active_rule_ids)),
            state_occurrences=occurrences,
            unique_state_count=len(self._state_counts),
            stalled=reason is not None,
            reason=reason,
        )
