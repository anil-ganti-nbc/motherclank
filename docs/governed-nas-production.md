# Governed NAS production orchestration

This source change does not install a package or admit Board. COPS-000074
requires exact-SHA CI, NAS qualification and a separately hash-reviewed root
installation transaction before selecting this implementation.

## Authority

A root-owned release directory contains six explicitly hashed files:
`nas_production_runner.py`, `nas_production_library.py`,
`observer_topology.py`, `observer-inventory.json`, `adapter-registry.json`,
and `snapshot-spec.json`. The separate `production-manifest.json` records
Mother source SHA, Diagnostic SHA, image ID, observer 0.2, snapshot 1.0 and
the production-specific artifact scope. The operator-reviewed manifest SHA
is mandatory in every runner invocation, and is embedded in the scheduled
launcher. No cyclic source-commit/image-digest pin is embedded in Git source.

The inventory is JSON (valid YAML). Each explicit deployment includes an
`observer` binding: transport, exact registry mapping, exact SQLite snapshot
specification (null for Feature Phone), and expected schema. `observer_policy`
pins Diagnostic, image, contract versions and KTW-only QC. Membership is the
deployment set, never the observed manifest or discovered files. Duplicate
identities, instances, canonical stores and snapshot filenames fail closed.
Changing inventory requires a new reviewed release manifest, even if the
source revision does not change. A supported five-child configuration remains
possible; six is not a code constant.

## Execution

`--dry-check`, `--qualify`, `--operator-run`, and `--run` each require
`--package-sha256 HASH`. All require host root. Qualification is permitted
only below the fixed COPS-000074 build staging root and writes a fresh detached
copy of Mother derived state. Operator and scheduled runs require the fixed
production package path plus the installed admission marker and rollback hash.

All modes share the actual production consumer, proof gate, inventory parser,
snapshot specification, registry, mount guards and ten pipeline phases. The
library retains create/inspect-before-start, non-root/no-network/kernel-RO,
process exit agreement, orphan-risk preservation, append-only continuity,
KTW-only QC, and non-current-evidence=>UNKNOWN rules from the prior deployment.
Historical standalone proof/export-request entry points were not copied into
the new production library. The original COPS-000081 deployment is preserved.

Qualification/operator runs use the release's exact `qualification_publication`
request ID and receipt/context hashes. They validate the original request,
hash-check the original accepted publication and preserve its clocks. They do
not request an export or claim the old request happened now. The consumer
computes current freshness normally. Scheduled runs retain the child-owned
export-request path. Scheduled origin is never established merely by selecting
`--run`: independent DSM evidence remains mandatory.

## Required release evidence (not supplied by a source commit alone)

- Exact new Mother SHA and reviewed Diagnostic0770 build/import provenance.
- All repository and NAS suites, with exits and honest skipped counts.
- Five isolated scenarios and sibling/non-mutation proof.
- Actual host runner `--qualify` with disposable six-child configuration;
  malformed/missing/extra/duplicate/QC/pin-negative cases fail closed.
- Fresh rollback archive, safe install and one OPERATOR_TRIGGERED live run.
- Independent later Task14 natural execution evidence; unchanged daily cadence.

Board collector, promotion, delivery and canonical-state mutation remain outside
this observer package's authority.

## Accepted publication versus current read-only proof

Feature Phone's historical export `SUCCESS` is not a current freshness claim.
The host corroborator binds M0 provenance to the hash-pinned manifest and checks
the copy clock at M0 intake. A validated copy at or below the existing 36000-second
limit is `VALID_AND_FRESH` and requires its zero-change DB proof. Beyond that
limit it is `VALID_BUT_STALE`: `SNAPSHOT_STALE` and effective `STALE` are required,
and a current DB-proof entry must be absent because the reader skipped the copy.
Native-child staleness alone does not imply the copy was skipped. M1 remains
`UNKNOWN` for non-current evidence. Sealed publication/hash/lineage validation
still runs before the pipeline; this distinction never refreshes evidence or
turns malformed publication data into an accepted stale result.
