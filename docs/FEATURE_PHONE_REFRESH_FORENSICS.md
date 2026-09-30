# Feature Phone refresh diagnostics — COPS-000087, linked to COPS-000081

This is development-only work. The restored four-child task-14 launcher,
its 1 October schedule, child writers, live Diagnostic and Board are unchanged.
Do not install the development gate or producer into task 14 from this document.

## Original failure boundary

The `86bbc6e` producer's outer `except sqlite3.Error` collapses these **15
distinct SQLite call/property boundaries** into `SQLITE_REFRESH_FAILED`, in
execution order:

1. Source `sqlite3.connect(mode=ro)`.
2. Source `execute(PRAGMA query_only=ON)` (connection-local read guard).
3. Destination `sqlite3.connect(.partial)`.
4. Source `backup(destination)`.
5. Destination `close()` at the first `closing` context exit.
6. Source `total_changes` property read.
7. Source `close()` in the try body.
8. Copy `sqlite3.connect(.partial)`.
9. Copy `execute(PRAGMA journal_mode=DELETE)` (destination only).
10. Copy `execute(PRAGMA query_only=ON)` (destination only).
11. Copy `execute(PRAGMA integrity_check)`.
12. Integrity cursor `fetchone()`.
13. Copy `execute(PRAGMA foreign_key_check)`.
14. Iteration of the FK cursor.
15. Copy `close()` at the second `closing` context exit.

The two metadata reads each comprise execute and two fetches, but their SQLite
exceptions are converted to `AS_OF_QUERY_FAILED` / `SCHEMA_QUERY_FAILED`, not
the generic code. Integrity/FK verdict failures are `SnapshotError`, not that
generic code. Filesystem stat, hash, rename, partial cleanup, and manifest
fsync/publication are filesystem operations, not SQLite calls. There is no
chmod/chown in the producer. The old finally-block source close is outside
the catch and could escape it; the diagnostic patch records cleanup errors.
The number above counts exposed call sites, **not** SQLite's unbounded internal
fault mechanisms or every possible SQLite error code.

## Diagnostic artifact, not a new snapshot contract

`refresh-diagnostics.json` is a separate, versioned diagnostic artifact in the
new isolated attempt directory. It is not a field extension to governed
snapshot manifest v1.0. Required fields, states, and fail-closed rules in the
manifest are unchanged. The record contains a fixed stage/operation name,
exception class, SQLite numeric code/name where the interpreter supplies them,
elapsed time, completion flags and cleanup exception classes. It never stores
exception messages, traceback, SQL, DB rows, payloads or credentials. Missing
SQLite attributes on older Python are null, not fabricated.

The producer still creates a failure-evidence manifest and exits zero when
that succeeds. Zero means evidence was recorded, not that every refresh
succeeded. The admission gate rejects FAILED/UNAVAILABLE first with a nonzero
exit. SUCCESS still requires exact instance/lane/source/copy paths, hash,
size, integrity, schema, runtime identity and freshness. No automatic retry
or last-good promotion was added.

## Historical-reference semantics

`last_good_snapshot_ref` is populated only by an explicitly configured,
policy-authorized reference. An absent reference has reason
`NO_CONFIGURED_LAST_GOOD_REFERENCE`, not a claim that history does not exist.

The optional spec input `retained_prior_snapshot_refs` carries independently
verified historical hashes into the **diagnostic artifact only**. A nonempty
list is labeled `RETAINED_PRIOR_SNAPSHOT_NOT_ADMITTED_AS_FALLBACK`. An explicit
empty list means the caller inspected the scoped retained evidence and found
none (`NO_RETAINED_PRIOR_SNAPSHOT`); an omitted list means history was not
inspected. This is never a directory sweep or automatic fallback selection.
Even a configured reference stays historical on failed refresh; current copy
paths remain null, freshness REFRESH_FAILED/UNAVAILABLE and child freshness
UNKNOWN. A successful copy with no native clock also remains UNKNOWN.

## Tests

The new hermetic tests exercise source-open, backup and integrity-query errors,
failed integrity verdicts, null-path FAILED/UNAVAILABLE structural validation,
real process rejection exits, successful exact path/hash binding, retained
unadmitted proof, configured historical references, and UNKNOWN child clocks.
An isolated live-source test is separate evidence and must not retroactively
establish the discarded original exception if the failure is not reproduced.
