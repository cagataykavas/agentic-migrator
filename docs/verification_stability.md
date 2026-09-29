# Repeated verification stability gate

A single green test run is weak promotion evidence for an agent-generated patch. A flaky test,
order dependency, leaked process state or verification-time source mutation can let unchanged
candidate bytes pass once and fail later. `migrator.verification_stability` replays the same patch
from the same base commit in independent detached Git worktrees before promotion.

Each trial:

1. starts from the exact clean base commit;
2. applies the content-addressed patch with Git's three-way apply boundary;
3. snapshots every tracked and non-ignored regular file;
4. runs the ordered, shell-free verification suite with executable, timeout and output budgets;
5. snapshots the candidate again and rejects verification-time source mutation; and
6. records outcome and output digests without putting raw tool output in the audit report.

The default policy requires three passes and one outcome variant. Exact stdout/stderr stability is
measured but is not a default release condition because healthy tools often print durations or
temporary paths. Set `--max-output-variants 1` when a verification command is expected to produce
byte-stable output.

```bash
python -m migrator.verification_stability \
  --repository ./legacy-service \
  --patch artifacts/migration.patch \
  --command "ruff check ." \
  --command "pytest -q" \
  --trials 3 \
  --output artifacts/verification-stability.json
```

Exit code `0` means the policy accepted the evidence, `2` means a well-formed audit failed the
release policy, and `3` means the request or operational evidence was malformed. Reports contain
the base, patch and policy identities; per-trial pass state; candidate before/after identities;
outcome/output identities; quorum totals; stable reason codes; and one canonical evidence digest.

## Policy choices

- `trials` is bounded from 2 to 20 so the gate cannot create an unbounded validation loop.
- `min_passes` defaults to all trials. A lower quorum is explicit policy, not a hidden retry.
- `max_outcome_variants` defaults to one. A pass/fail or step-sequence change is therefore a
  release failure even if a relaxed quorum is met.
- output truncation fails closed by default because a successful exit with incomplete diagnostics
  is weak evidence.
- candidate file count, tree bytes, patch bytes, command timeout and captured output are bounded.

## Trust boundary and limitations

Repeated success reduces the chance of admitting an intermittent failure; it does not prove the
absence of flakiness. All trials run on one host and inherit the same kernel, clock, dependency
cache and external services. The local subprocess runner is not a hardened hostile-code sandbox.
Production use should distribute trials across isolated workers, pin dependency and container
digests, disable network access unless explicitly required, and bind this report to the signed
promotion receipt.

Output digests can still support dictionary attacks against predictable output, so evidence access
and retention remain security controls. A future increment should schedule trials across distinct
worker identities and compare environment attestations together with the patch and base digest.
