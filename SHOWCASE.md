# Agentic Migrator — Visual Showcase

This page is a compact visual companion to the main README.

## 1. Repository-scale control plane

```mermaid
flowchart TB
    REPO[Repository] --> SCAN[Project scanner]
    SCAN --> AST[AST parse + import inventory]
    AST --> GRAPH[Local dependency graph]
    GRAPH --> PLAN[Risk-aware migration plan]

    PLAN --> UNIT[Migration unit]
    UNIT --> STRUCT[Structural AST rules]
    UNIT --> TEXT[Text / config rules]
    STRUCT --> CAND[Candidate]
    TEXT --> CAND

    CAND --> TEST[Test scenarios]
    TEST --> OK{Pass?}
    OK -- yes --> TRACE[Trace + metrics]
    OK -- no --> CLASS[Failure classifier]
    CLASS --> SYNTH[LLM rule synthesizer]
    SYNTH --> QUAR[Quarantined learned rule]
    QUAR --> VALID[Validation + regressions]
    VALID --> GOV{Rule governor}
    GOV -- canary --> CANARY[Canary reuse]
    GOV -- promote --> MEMORY[(Promoted rule memory)]
    GOV -- disable --> OFF[Disabled]
    MEMORY --> STRUCT
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
    PASS --> REPORT[CI / benchmark report]
    LLM --> REPORT
    REUSE --> REPORT
    FAIL --> REPORT
    REG --> REPORT
    TEST --> REPORT
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

## 6. Portfolio dashboard

A static UI prototype is included at [`migration_dashboard.html`](migration_dashboard.html). It visualizes the kinds of signals the control plane would expose: convergence, deterministic reuse, LLM avoidance, rule promotion and failing migration units.

## 7. What I would build next

- containerized test runners with CPU/memory/time budgets;
- git worktree isolation per migration attempt;
- language adapters beyond Python;
- semantic-diff scoring;
- patch-level human approval;
- OpenTelemetry traces;
- rule-store persistence in PostgreSQL/object storage;
- distributed worker queue for repository-scale batches;
- canary migration campaigns and automatic rollback;
- cost/token accounting per migration family.
