import pytest

from migrator.gitops import ProposedChange
from migrator.proposal_policy import ProposalPolicy, evaluate_proposal


def _change(path: str, diff: str, operation: str = "update") -> ProposedChange:
    return ProposedChange(path, operation, "before", "after", diff)


def test_bounded_source_proposal_is_accepted_with_evidence():
    decision = evaluate_proposal(
        [_change("migrator/engine.py", "--- a\n+++ b\n-old\n+new\n")],
        policy=ProposalPolicy(allowed_roots=("migrator",)),
    )

    assert decision.accepted
    assert decision.changed_lines == 2
    assert decision.to_dict()["evidence"][0]["added_lines"] == 1


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        (".github/workflows/ci.yml", "protected_path:.github/workflows/ci.yml"),
        ("tests/test_engine.py", "test_change_not_allowed:tests/test_engine.py"),
        ("docs/design.md", "outside_allowed_roots:docs/design.md"),
    ],
)
def test_server_owned_scope_rejects_sensitive_or_out_of_scope_paths(path, reason):
    decision = evaluate_proposal(
        [_change(path, "+line\n")],
        policy=ProposalPolicy(allowed_roots=("migrator",)),
    )
    assert not decision.accepted
    assert reason in decision.reasons


def test_delete_requires_explicit_authority():
    decision = evaluate_proposal([_change("migrator/old.py", "-old\n", "delete")])
    assert decision.reasons == ("delete_not_allowed:migrator/old.py",)


def test_file_and_line_budgets_are_enforced_together():
    changes = [_change(f"migrator/{index}.py", "+one\n+two\n") for index in range(3)]
    decision = evaluate_proposal(
        changes,
        policy=ProposalPolicy(max_changed_files=2, max_changed_lines=4),
    )
    assert decision.reasons == (
        "changed_file_budget_exceeded",
        "changed_line_budget_exceeded",
    )


def test_empty_and_duplicate_or_escaping_paths_fail_closed():
    assert evaluate_proposal([]).reasons == ("empty_proposal",)
    duplicate = [_change("migrator/a.py", "+a\n"), _change("migrator/a.py", "+b\n")]
    with pytest.raises(ValueError, match="unique"):
        evaluate_proposal(duplicate)
    with pytest.raises(ValueError, match="unsafe"):
        evaluate_proposal([_change("../escape.py", "+bad\n")])


def test_test_or_delete_authority_can_be_granted_explicitly():
    decision = evaluate_proposal(
        [_change("tests/new_test.py", "+test\n", "create")],
        policy=ProposalPolicy(allow_test_changes=True, allowed_roots=("tests",)),
    )
    assert decision.accepted
