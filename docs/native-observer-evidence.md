# Optional native observer evidence (COPS-000074)

Observer surface `0.2` and shared adapter payload `0.1.0-v3` are unchanged.
An adapter may opt into native summary transport with a callable
`observer_evidence()` returning a mapping whose `payload_version` is `1.0`.
An absent, raising, or unsupported-version envelope cannot authorize native
summary probing. Existing adapters with a similarly named `source_summary()`
but no envelope retain their prior observation behavior.

The optional `source_summary()` returns a list of native mappings;
`diagnostic_summary()` returns a native mapping. Motherclank transports these
without child-specific SQL or reinterpretation. The qualified Board adapter
preserves the exact roster and grouped-condition shapes from Board `613c3a1`.
Its separate evidence envelope carries availability, run/receipt/condition/
sighting evidence and revision distinctions. An empty roster is not healthy
zero. Unknown evidence remains explicit; native uncertainty is not novelty.

Harvest isolates extension exceptions and malformed envelopes. Synthesis
retains the native evidence verbatim, including failure blocks, even when
snapshot or child-execution freshness makes the derived claim UNKNOWN.
Enclosing state and snapshot provenance still qualify the evidence's age;
retention does not upgrade an old run or a failed refresh into current health.

Snapshot contract `1.0` remains mandatory for NAS intake. Child instance/lane,
canonical path, native clock and source/deployed revision attestations belong
to the independently verified registry/manifest, not an event revision or an
adapter name. Registry duplicate keys, ambiguous stores and malformed lane
identities fail closed. Qualified absolute sealed-export paths remain valid.

This change grants no child write, collection, scheduler, promotion, condition
closure, notification or production-inventory authority. COPS-000074 tests
and candidate artifacts are isolated; production admission requires review.
