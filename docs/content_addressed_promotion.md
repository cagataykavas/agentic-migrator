# Content-addressed migration promotion

Passing tests is not enough if the candidate changes after the tests run, the patch is replaced, or the target branch advances before application. `migrator.promotion` adds an explicit promotion boundary for that time-of-check/time-of-use window.

## What the receipt binds

`issue_promotion_receipt` accepts a clean base repository, an isolated candidate worktree, the exported patch and a server-owned `PromotionPolicy`. It then:

1. confirms that the candidate and base have the same Git commit;
2. snapshots all tracked and non-ignored candidate files;
3. runs the exact required verification commands through the existing shell-free bounded runner;
4. snapshots the candidate again and rejects tracked or non-ignored mutations during verification;
5. applies the patch to a fresh detached worktree from the clean base;
6. requires the projected tree to exactly match the verified candidate;
7. issues a canonical SHA-256 receipt binding the base commit, patch, complete candidate tree, verification evidence and promotion policy.

The tree digest covers each UTF-8 relative path, regular-file mode, byte size and content digest. Symlinks and special files fail closed. File, per-file byte, total-tree byte, patch byte, verification-output and timeout budgets are explicit policy values.

```python
from migrator.promotion import (
    PromotionPolicy,
    issue_promotion_receipt,
    verify_promotion_receipt,
)
from migrator.repository import GitRepository

policy = PromotionPolicy(
    required_commands=("ruff check .", "pytest -q"),
    timeout_seconds=120,
)

receipt = issue_promotion_receipt(
    base_repository=GitRepository("./service"),
    candidate_repository=GitRepository("./candidate-worktree"),
    patch_path="artifacts/migration.patch",
    policy=policy,
)

decision = verify_promotion_receipt(
    receipt=receipt,
    base_repository=GitRepository("./service"),
    patch_path="artifacts/migration.patch",
    target_base_sha=GitRepository("./service").head_sha(),
    policy=policy,
)
assert decision.accepted
```

At the final promotion boundary, `verify_promotion_receipt` rechecks the receipt digest, active policy, target base commit, patch bytes and patch-projected candidate tree. A base-branch advance, modified patch, changed policy, dirty base or non-reproducible candidate produces stable reason codes instead of silently widening authority.

## Trust boundary

The receipt proves content consistency inside this process boundary; it is not a signature or remote attestation. An attacker with permission to rewrite both the receipt and the trusted policy can manufacture a new internally consistent bundle. Production systems should sign the receipt in a separate identity domain, store it in append-only evidence storage and bind its digest to the pull request or deployment transaction.

The filesystem snapshot intentionally excludes Git-ignored artifacts such as caches and build output. A verification command must not depend on an ignored artifact for the correctness of promoted source. Submodules, symlinks, non-UTF-8 paths and special files are rejected rather than ambiguously serialized.

The next increment is to call this boundary directly from `migrate-repo`, persist the receipt beside the patch and require a signed receipt digest in the PR-creation adapter.
