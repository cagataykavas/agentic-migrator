# Agent trace evidence audit

Agentic Migrator already records dependency-free JSONL spans for CI and offline
review. `trace-audit` turns those records into an enforceable evidence boundary:
it rejects artifacts whose lineage or timing no longer supports the claimed run.

```bash
agentic-migrator trace-audit artifacts/preflight-trace.jsonl \
  --require-span preflight.repository \
  --require-span preflight.parallel_validate \
  --output artifacts/trace-audit.json
```

The command exits `0` only when all checks pass. Invalid input and policy
failures exit `2` and still produce a machine-readable report.

## Enforced invariants

- one trace ID and exactly one root span;
- unique span IDs, existing parents, and acyclic parent lineage;
- child timestamps contained within their parent;
- event timestamps contained within their owning span;
- completed spans with consistent nanosecond timestamps and `duration_ms`;
- valid `ok` or `error` status plus object-shaped attributes;
- optional required control-plane span names;
- an 8 MB / 10,000-span ingestion bound, with duplicate JSON keys,
  non-finite numbers, blank lines, and symlink inputs rejected.

Checks operate on the whole file, so the recorder's child-before-parent JSONL
completion order is valid. Reports are deterministic and include stable issue
codes suitable for release gates. A structurally valid span with `status=error`
is counted in `error_spans` but does not by itself fail this structural audit;
release policy can decide whether failed agent work is admissible.

## Scope

This is a structural evidence audit, not proof of execution or tamper
resistance. A syntactically valid trace can still be fabricated, and the audit
does not infer semantic correctness from span names. Production pipelines
should retain the trace and audit report in an immutable artifact store and bind
their digests to the reviewed migration manifest. A natural next increment is a
content-addressed envelope that signs the trace, policy, and audit result as one
release decision.
