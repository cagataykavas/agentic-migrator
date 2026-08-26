from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class FailureKind(str, Enum):
    SYNTAX = "syntax"
    IMPORT = "import"
    ASSERTION = "assertion"
    RUNTIME = "runtime"
    TEST_HARNESS = "test_harness"
    UNKNOWN = "unknown"


@dataclass
class Rule:
    id: str
    pattern: str
    replacement: str
    reason: str
    created_by: str = "human"
    success_count: int = 0
    failure_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TestResult:
    __test__ = False

    passed: bool
    name: str
    stdout: str = ""
    stderr: str = ""
    kind: FailureKind = FailureKind.UNKNOWN


@dataclass
class MigrationTrace:
    attempts: int = 0
    applied_rules: list[str] = field(default_factory=list)
    learned_rules: list[str] = field(default_factory=list)
    test_repairs: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
