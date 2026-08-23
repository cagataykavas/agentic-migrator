# Agentic Migrator — Visual Showcase

This page is a compact visual companion to the main README.

## 1. Repository-scale control plane

```mermaid
flowchart TB
    REPO[Repository] --> GIT[Clean Git checkpoint]
    GIT --> WT[Detached isolated worktree]
    WT --> SCAN[Project scanner]
    SCAN --> AST[AST parse + import inventory]
    AST --> GRAPH[Local dependency graph]
    GRAPH --> PLAN[Risk-aware migration plan]

    PLAN --> UNIT[Migration unit]
    UNIT --> STRUCT[Structural AST rules]
    UNIT --> TEXT[Text / config rules]
    STRUCT --> CAND[Candidate]
    TEXT --> CAND

    CAND --> TEST[Test scenarios]
    CAND --> SEM[Public API semantic diff]
    TEST --> OK{Pass?}
    SEM --> OK
    OK -- yes --> TRACE[Trace + metrics]
    OK -- no --> CLASS[Failure classifier]
    CLASS --> SYNTH[LLM rule synthesizer]
    SYNTH --> COST[Token / cost ledger]
    SYNTH --> QUAR[Quarantined learned rule]
    QUAR --> VALID[Validation + regressions]
    VALID --> GOV{Rule governor}
    GOV -- canary --> CANARY[Canary reuse]
    GOV -- promote --> MEMORY[(Promoted rule memory)]
    GOV -- disable --> OFF[Disabled]
    MEMORY --> STRUCT

    TRACE --> PATCH[Patch artifact]
    PATCH --> APPROVE{Human / CI approval}
    APPROVE -- reject --> DROP[Discard worktree]
    APPROVE -- accept --> APPLY[Explicit patch apply]
```

## 2. Learned-rule lifecycle

```mermaid
stateDiagram-v2
    [*] --> Quarantined: LLM proposes reusable rule
    Quarantined --> Canary: minimum validation evidence
    Canary --> Promoted: cross-migration reuse + high success rate
    Canary --> Disabled: failure rate too high
    Promoted --> Quarantined: regression detected
    Quarantined --> Disabled: repeated failures
    Promoted --> Disabled: policy decision
```

The important design choice is that **LLM output is not immediately trusted as global memory**. A successful repair can enter quarantine, accumulate evidence, move through canary reuse and only then become broadly reusable.

## 3. Migration observability

```mermaid
flowchart LR
    TRACE[Migration traces] --> AGG[Metrics aggregator]
    AGG --> PASS[Pass / first-pass rate]
    AGG --> LLM[LLM avoidance]
    AGG --> REUSE[Learned-rule reuse]
    AGG --> FAIL[Failure distribution]
    AGG --> REG[Regression failures]
    AGG --> TEST[Test-repair rate]
    COST[LLM usage ledger] --> TOK[Input/output tokens]
    COST --> USD[Estimated cost]
    PASS --> REPORT[CI / benchmark report]
    LLM --> REPORT
    REUSE --> REPORT
    FAIL --> REPORT
    REG --> REPORT
    TEST --> REPORT
    TOK --> REPORT
    USD --> REPORT
```

## 4. Why AST rules matter

A text rule can accidentally rewrite strings, comments or unrelated identifiers. `migrator/ast_rules.py` adds a structural layer for transformations such as:

- import-module migration;
- function keyword migration;
- qualified attribute migration.

This gives the engine two deterministic tools:

```text
cheap exact text rules
        +
syntax-aware AST transforms
        ↓
LLM exception handling only when necessary
```

## 5. Repository planning

`migrator/project_scan.py`:

1. discovers Python source files;
2. parses imports;
3. maps local dependencies;
4. detects dependency cycles using Tarjan strongly-connected components;
5. generates a dependency-aware migration order;
6. assigns risk based on parse failures, file size, coupling, cycles and configured high-risk APIs.

Run:

```bash
PYTHONPATH=. python examples/repository_migration_plan.py . \
  --high-risk-import legacy_sdk \
  --output artifacts/migration_plan.md
```

## 6. Git isolation and patch approval

`migrator/repository.py` now provides a real Git safety boundary:

- refuses to start an isolated migration from a dirty checkout;
- records the current commit/branch checkpoint;
- creates a **detached temporary worktree** for migration attempts;
- deletes the temporary worktree after the attempt;
- exports binary-safe patch artifacts;
- can validate/apply patches explicitly;
- keeps destructive hard reset behind an explicit `allow_destructive=True` flag.

The source checkout therefore does not need to be the experimental workspace.

Useful commands:

```bash
agentic-migrator git-checkpoint . --require-clean
agentic-migrator git-patch . --base HEAD --output artifacts/migration.patch
```

## 7. Semantic migration guard

Passing tests does not prove that a public API stayed compatible. `migrator/semantic_diff.py` adds an AST-based public API surface check for Python modules.

It detects:

- added public functions/classes;
- removed public functions/classes;
- changed function argument signatures;
- changed public method surfaces on classes.

```bash
agentic-migrator semantic-diff before.py after.py --fail-on-breaking
```

This is intentionally a **guardrail**, not a claim that static API comparison proves behavioral equivalence. It complements tests and scenario validation.

## 8. LLM cost / token accounting

`migrator/cost.py` provides a provider-agnostic append-only JSONL ledger for optional LLM repair calls. A pricing record defines input/output price per million tokens; every rule-synthesis or test-repair call can record:

- model;
- operation;
- input/output token counts;
- calculated USD cost;
- migration metadata;
- timestamp.

```bash
agentic-migrator cost-summary --ledger artifacts/llm_usage.jsonl
```

This makes **LLM avoidance** measurable not only as a percentage but eventually as latency/cost avoided by deterministic and learned-rule reuse.

## 9. Portfolio dashboard

A static UI prototype is included at [`migration_dashboard.html`](migration_dashboard.html). It visualizes the kinds of signals the control plane exposes: convergence, deterministic reuse, LLM avoidance, rule promotion and failing migration units.

## 10. What I would build next

Already implemented from the old roadmap: containerized bounded test runner, Git worktree isolation, semantic public-API diff and token/cost accounting.

Next high-value extensions:

- Java and TypeScript language adapters;
- OpenTelemetry traces across migration attempts;
- patch-level web approval workflow;
- rule-store persistence in PostgreSQL/object storage;
- distributed worker queue for repository-scale batches;
- canary migration campaigns and automatic rollback;
- dependency/version compatibility resolver;
- semantic equivalence probes using generated differential tests.
