# Repair-loop convergence guard

An attempt limit bounds cost, but it does not explain why an agent failed to converge.
A repair loop can repeatedly generate the same candidate and failure, alternate between
two states, or keep producing unique states until the budget is exhausted. Treating all
three as the same generic failure makes debugging and policy decisions harder.

`migrator.convergence.RepairConvergenceGuard` fingerprints each failed repair state from:

- the complete candidate source;
- the active test source; and
- the normalized test name, failure kind, stdout and stderr.

Only SHA-256 digests enter the convergence evidence. Full source and test output remain
in their existing controlled artifacts. Active rule IDs are sorted and recorded for
diagnosis but deliberately excluded from the state identity: adding a rule that produces
the exact same candidate and failure is not progress.

## Stop reasons

| Reason | Meaning |
| --- | --- |
| `repeated_repair_state` | The same candidate, test harness and failure reached the configured occurrence limit. |
| `two_state_oscillation` | The last four failures form an A→B→A→B cycle. |
| `unique_state_budget_exceeded` | The loop produced more distinct failed states than policy permits. |
| `attempt_budget_exhausted` | The engine used every attempt without another convergence reason. |

The migration engine records every observation in `MigrationTrace.convergence_states`.
When it stops, `MigrationConvergenceError` carries the complete trace and a stable
`termination_reason` for CI, metrics or human review.

The default repeated-state limit follows the existing repeated-test-failure threshold.
This ordering is intentional: on the threshold attempt, the engine still lets an
authorized test repair or improving rule proceed before it terminates a stalled loop.

## Trust boundary and limitations

State equality is exact, not semantic. Harmless whitespace or nondeterministic failure
text creates a different fingerprint; callers should stabilize tool output where
possible. Conversely, equal fingerprints prove repeated inputs and outputs, not why a
repair failed. The guard does not evaluate proposal safety, correctness or authorization;
those remain separate admission, sandbox and verification boundaries.

Fingerprints are evidence identifiers, not confidentiality controls. A production trace
store still needs access control, retention policy and integrity protection.
