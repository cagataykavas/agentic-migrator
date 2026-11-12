# Concurrent rule memory

`RuleStore` is the persistence boundary for reusable rules accepted by migration runs. A plain
read-modify-write sequence is unsafe once repository units run in parallel: two workers can read
the same snapshot, accept different rules and let the last writer silently discard the other
worker's evidence.

The store therefore provides three guarantees:

- every save is written to a temporary file in the store directory, flushed, and committed with
  `os.replace`, so readers observe either the previous complete snapshot or the next one;
- `upsert` holds one lock across the complete read-modify-write transaction;
- access is serialized between threads on every platform and between cooperating processes on
  POSIX systems with an advisory `flock` sidecar (`<store>.lock`).

The lock is advisory: every writer must use `RuleStore`. Direct edits, network filesystems that do
not honor `flock`, and a hostile process are outside this boundary. Production deployments with
multiple hosts should use a transactional shared database or a service that provides compare-and-
swap semantics; the local JSON store is intended for a single machine or worker volume.
