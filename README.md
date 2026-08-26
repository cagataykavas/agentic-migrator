# Agentic Migrator

> **A test-driven, self-improving code-migration system that turns successful LLM repairs into reusable deterministic rules.**

Agentic Migrator is a public reference architecture for modernizing code without handing an entire repository to an LLM and hoping for the best. It treats the LLM as a **bounded exception handler and rule synthesizer**, while deterministic transformations, isolated validation, rollback boundaries and governance remain explicit software components.

All examples are synthetic. No proprietary source code, migration rules or internal test suites are included.

## Architecture

```mermaid
flowchart TD
    REPO[Repository] --> SCAN[Dependency + risk scan]
    SCAN --> WT[Detached Git worktree]
    WT --> RULES[Deterministic rule engine]
    MEMORY[(Governed rule memory)] --> RULES
    RULES --> EDIT[Source-preserving structural edits]
    EDIT --> DIFF[ChangeSet + semantic API diff]
    DIFF --> TEST[Bounded sandbox validation]
    TEST --> PASS{Tests pass?}

    PASS -- yes --> ACCEPT[Approved migration artifact]
    ACCEPT --> MANIFEST[SHA-256 manifest + patch + metrics]

    PASS -- no --> CLASSIFY[Failure classification]
    CLASSIFY --> LLM[Structured LLM repair proposal]
    BUDGET[Call / token / cost budget] --> LLM
    LLM --> REGRESS[Candidate + regression validation]
    REGRESS --> GOOD{Improves outcome?}
    GOOD -- yes --> GOV[Quarantine / canary / promotion]
    GOV --> MEMORY
    GOOD -- no --> ROLLBACK[Rollback / retry budget]

    CLASSIFY --> HARNESS{Repeated harness failure?}
    HARNESS -- yes --> TESTFIX[Guarded test-repair proposal]
    TESTFIX --> TEST
```

The core principle is simple:

```text
legacy repository
  → inspect dependency/risk structure
  → isolate work
  → deterministic migration first
  → validate
  → ask the LLM only for unresolved failures
  → promote reusable repairs only after evidence
```

## What is implemented

### 1. Dependency-aware repository planning

`migrator/project_scan.py` parses Python files, builds local dependency relationships, identifies dependency cycles and produces an ordered migration plan with explicit risk reasons. The CLI exposes it directly:

```bash
agentic-migrator scan . --format markdown --output artifacts/migration-plan.md
```

This makes repository-scale migration an ordering/problem-decomposition task rather than a directory-wide rewrite.

### 2. Source-preserving structural migration

`migrator/ast_rules.py` uses the Python AST to identify semantic nodes, but deliberately **does not serialize the whole program with `ast.unparse()`**.

Instead it applies byte-range edits back onto the original UTF-8 source. That means deterministic migrations can change imports, bound module names, keyword arguments and attribute chains while preserving unrelated:

- comments;
- string literals;
- whitespace and formatting;
- source text outside the targeted syntax node.

For example, an API migration can change:

```python
# legacy_client is intentionally mentioned in this comment
import legacy_client
result = legacy_client.request(timeout_seconds=5)
```

into code using `modern_client.request(timeout=5)` without rewriting the comment merely because it contains the old identifier.

The structural transformer is covered by regression tests that explicitly require comments and strings to survive.

### 3. Deterministic rules + governed learned rules

Known transformations are cheap deterministic rules. When those rules do not resolve a failure, the LLM interface receives a compact failure packet and proposes a **structured reusable rule**, not an unrestricted whole-program rewrite.

Successful proposals do not immediately become globally trusted. `migrator/governance.py` models a rule lifecycle:

```text
quarantined → canary → promoted
               ↓
            disabled
```

Promotion uses validation success, cross-migration reuse, success rate and regression evidence. A previously promoted rule can be quarantined again if regression evidence appears.

### 4. Bounded sandbox validation

`migrator/sandbox.py` provides a bounded local subprocess boundary for migration validation:

- no shell invocation;
- executable allowlist;
- stripped environment with explicit allowlisted overrides;
- absolute/parent-path rejection for materialized files;
- temporary working directory;
- execution timeout;
- captured-output byte limit.

`migrator/runners.py` adapts this boundary to the migration engine through `PytestSandboxRunner`. Candidate code and its active test harness are materialized into a temporary workspace and evaluated with pytest without mutating the developer checkout.

A standalone CLI path is also available:

```bash
agentic-migrator sandbox-test candidate.py test_candidate.py --timeout 20
```

> `LocalSandbox` is a process-isolation reference boundary, **not a hardened hostile-code sandbox**. Untrusted code should additionally run inside a locked-down container, VM, seccomp profile or remote worker pool.

### 5. Git isolation and patch boundaries

`migrator/repository.py` provides a thin Git boundary with:

- clean-checkout enforcement;
- HEAD/branch checkpoints;
- detached temporary worktrees;
- binary diff / patch export;
- patch validation/application;
- destructive reset only through an explicit opt-in flag.

A failed migration can therefore die inside a disposable worktree rather than leaving a developer checkout half-mutated.

```bash
agentic-migrator git-checkpoint . --require-clean
agentic-migrator git-patch . --output artifacts/migration.patch
```

### 6. Auditable change plans and rollback

The repository intentionally separates two responsibilities:

- `migrator.gitops.ChangeSet` — an **in-memory preflight plan** for create/update/delete operations, unified diffs, SHA-256 before/after hashes and per-file rollback;
- `migrator.workspace.MigrationWorkspace` — a **filesystem transaction layer** that snapshots files, applies an approved text plan, verifies post-change hashes and restores the original snapshot.

Both boundaries reject attempts to escape the migration root. This keeps “what the agent proposes” separate from “what is actually written to disk.”

### 7. Semantic API diff

Passing tests is necessary but not always sufficient for a library migration. `migrator/semantic_diff.py` compares public function/class signatures before and after a transformation and reports:

- added public symbols;
- removed public symbols;
- changed signatures;
- whether the API delta is potentially breaking.

```bash
agentic-migrator semantic-diff before.py after.py --fail-on-breaking
```

This gives CI a lightweight compatibility gate in addition to behavior tests.

### 8. LLM usage and enforceable budgets

`migrator/cost.py` keeps an append-only JSONL usage ledger for optional LLM-assisted repair calls. A `BudgetPolicy` can reject a projected call **before it is recorded/executed** when it would exceed configured limits for:

- number of calls;
- input tokens;
- output tokens;
- estimated USD cost.

This complements the engine's `max_attempts`: autonomy is bounded both by convergence attempts and by explicit LLM resource budgets.

```bash
agentic-migrator cost-summary --ledger artifacts/llm_usage.jsonl
```

### 9. Guarded test repair

Test repair is treated as a separate authority domain from source repair. The guard layer rejects obvious success-redefinition patterns such as:

- reducing the assertion count;
- introducing unconditional `assert True`;
- broad exception swallowing;
- adding pytest skip directives.

Accepted test changes retain a unified diff and are recorded in the migration trace.

### 10. Migration observability

`migrator/metrics.py` projects execution traces into CI/portfolio metrics including:

| Metric | Why it matters |
|---|---|
| Pass rate | Overall migration convergence |
| First-pass success | Effectiveness of deterministic knowledge |
| LLM avoidance rate | How often repair calls are unnecessary |
| Learned-rule reuse | Whether repair knowledge becomes reusable |
| Average attempts | Convergence efficiency |
| Rule usage | Which migration knowledge provides value |
| Failure-kind distribution | Where migration effort is spent |
| Test-repair rate | How often validation infrastructure changes |
| Regression failures | Whether candidate rules damage known scenarios |

The synthetic benchmark writes machine-readable JSON and a Markdown summary:

```bash
python examples/benchmark_report.py
```

Benchmark records in this public repository demonstrate the metric pipeline; they are not claimed production results.

## Failure model

The engine distinguishes failure categories rather than turning every exception into the same prompt:

```mermaid
flowchart LR
    F[Failure] --> S[Syntax]
    F --> I[Import]
    F --> R[Runtime]
    F --> A[Assertion]
    F --> H[Test harness]
    F --> U[Unknown]

    S --> P[Repair strategy]
    I --> P
    R --> P
    A --> P
    H --> T[Separate test-repair policy]
    U --> B[Bounded fallback]
```

A later, more local failure can count as progress: moving from a syntax/import failure to a specific assertion failure means the migrated code has advanced far enough to execute target behavior.

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

agentic-migrator --help
agentic-migrator scan . --format markdown
pytest -q
```

Useful CLI commands:

```text
scan             dependency-aware migration planning
transform        apply structural source-preserving migration rules
semantic-diff    compare public API signatures
sandbox-test     validate a candidate in the bounded temporary runner
git-checkpoint   inspect the source repository checkpoint
git-patch        export a reviewable migration patch
cost-summary     summarize LLM usage and estimated cost
```

## Repository layout

```text
agentic-migrator/
├── migrator/
│   ├── engine.py          # bounded migration / repair loop
│   ├── ast_rules.py       # AST-guided source-preserving transforms
│   ├── project_scan.py    # dependency + migration-risk planning
│   ├── rules.py           # deterministic and learned rule storage
│   ├── governance.py      # quarantine / canary / promotion policy
│   ├── sandbox.py         # bounded subprocess validation boundary
│   ├── runners.py         # sandbox → MigrationEngine adapters
│   ├── semantic_diff.py   # public API compatibility comparison
│   ├── gitops.py          # in-memory ChangeSet + diffs / hashes
│   ├── workspace.py       # filesystem snapshot / apply / verify / rollback
│   ├── repository.py      # Git checkpoint / worktree / patch boundary
│   ├── cost.py            # token/cost ledger + budget gates
│   ├── metrics.py         # migration observability
│   ├── test_guard.py      # guarded harness repair
│   ├── llm.py             # structured synthesizer interface
│   ├── models.py          # domain objects and failure types
│   └── cli.py             # command-line interface
├── rules/
│   └── builtin.json
├── examples/
│   ├── python_api_migration.py
│   └── benchmark_report.py
├── tests/                 # engine, governance, sandbox, Git and migration tests
├── .github/workflows/
│   └── ci.yml
├── Dockerfile
├── pyproject.toml
└── README.md
```

## Engineering decisions

**LLM calls are deliberately late.** Reusable deterministic knowledge is tried first, reducing cost, nondeterminism and prompt sensitivity.

**Source editing and source parsing are separate concerns.** The AST identifies what should change; byte-range edits preserve everything else.

**Agent memory is explicit.** Successful repairs become versionable rule artifacts with IDs and provenance rather than disappearing into chat history.

**Rule learning does not imply rule trust.** New rules accumulate evidence through quarantine/canary states before broad reuse.

**Changing implementation and changing validation are different authorities.** Test repair has separate guardrails and audit evidence.

**Repository mutation is transactional.** Proposed changes, filesystem application and Git integration are separate layers with hashes, manifests, worktrees and rollback paths.

**Autonomy has multiple budgets.** Attempts, allowed processes, runtime, output volume, LLM calls, tokens and estimated cost all have explicit boundaries.

## Interview walkthrough

A concise explanation of the project:

> I built a deterministic-first code-migration system. It scans repository dependencies to plan migration order, applies AST-guided edits while preserving untouched source text, and validates candidates in a bounded temporary pytest runner. Known transformations stay deterministic. Unresolved failures can ask an LLM for a small reusable rule, but calls are budgeted and learned rules pass regression and governance gates before wider reuse. Repository changes are represented as hashed change sets, validated in isolated Git worktrees and can be rolled back from snapshots. I also compare public API signatures and collect migration/LLM-usage metrics, so the system is testable and auditable rather than a giant rewrite prompt.

## What this demonstrates

- agentic AI with bounded autonomy;
- deterministic-first code migration;
- AST-guided, source-preserving transformations;
- dependency-aware repository planning;
- sandboxed test execution boundaries;
- Git worktree isolation and patch workflows;
- semantic API compatibility checks;
- create/update/delete manifests and rollback;
- persistent rule memory and governance;
- LLM call/token/cost budgeting;
- guarded test repair;
- regression testing and CI/CD;
- observability and audit trails;
- human-reviewable automation.

## CI

GitHub Actions installs the package, runs Ruff and pytest, executes the synthetic migration benchmark, smoke-tests the CLI, builds the Docker image and checks the container entrypoint.

The project is an actively developed reference implementation. Current adapters focus on Python migrations; stronger hostile-code isolation and additional language-specific migration adapters belong behind the same existing control boundaries rather than inside the LLM prompt.
