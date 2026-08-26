from __future__ import annotations

import difflib

FORBIDDEN_PATTERNS = (
    "assert True",
    "except Exception: pass",
    "except Exception:\n    pass",
    "pytest.skip(",
    "@pytest.mark.skip",
)


def validate_test_repair(original: str, proposed: str) -> tuple[bool, str]:
    if proposed == original:
        return False, "proposal made no change"
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in proposed and pattern not in original:
            return False, f"forbidden weakening pattern introduced: {pattern}"
    if original.count("assert ") > proposed.count("assert "):
        return False, "assertion count decreased"
    return True, "ok"


def unified_diff(
    original: str,
    proposed: str,
    fromfile: str = "tests/original.py",
    tofile: str = "tests/proposed.py",
) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(True),
            proposed.splitlines(True),
            fromfile=fromfile,
            tofile=tofile,
        )
    )
