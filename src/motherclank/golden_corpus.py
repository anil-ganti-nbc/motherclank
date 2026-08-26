"""Golden Incident Corpus - the fleet's expensive lessons as data.

Each entry is a stable ID binding: title, affected plane, evidence shape,
required derivation, forbidden derivations, provenance requirements, and
the executable fixtures (or registered-pending evidence) that lock it in.

Status values:
    executable                 - fixture(s) exist and run in CI
    registered_pending_fixture - registered with real origin evidence;
                                 fixture requires host-side evidence that
                                 does not exist yet (never faked)
"""

from __future__ import annotations

CORPUS_SPEC_VERSION = "1"

ENTRIES: tuple[dict, ...] = (
    {"id": "GIC-01", "title": "first_seen != new reference",
     "plane": "novelty",
     "evidence_shape": "source-dated age exceeds policy window on first "
                       "local sighting",
     "expected": ["event class UNCONFIRMED/BASELINE_CATCHUP, never plain NEW"],
     "forbidden": ["notification flood on initialization"],
     "provenance": "publication time + baseline epoch",
     "status": "executable",
     "covered_by": ["conformance/test_fleet_laws.py",
                    "tests/test_golden_incidents_g1_g8.py::"
                    "test_db_loss_new_epoch_baseline_never_reads_as_novelty"],
     "origin": "watch Timex cluster f9a401a / L-WATCH-002"},
    {"id": "GIC-02", "title": "legitimate legitimate-zero source",
     "plane": "collection health",
     "evidence_shape": "fetch ok, count 0, attempt fresh",
     "expected": ["healthy/ok preserved"],
     "forbidden": ["zero treated as failure"],
     "provenance": "source_health row / crawler_runs stats",
     "status": "executable",
     "covered_by": ["tests/test_fgt_onboarding.py::"
                    "test_fgt_g1_successful_attempt_zero_findings_is_ok",
                    "tests/test_observer_expansion.py::"
                    "test_golden_a_natural_zero_crawl_is_healthy_not_failure"],
     "origin": "FGT quiet-week semantics; OEM due-gating"},
    {"id": "GIC-03", "title": "ZERO vs STAGNANT",
     "plane": "recency/freshness",
     "evidence_shape": "ok-with-count-0 fresh  vs  ok-but-attempt-stale",
     "expected": ["fresh zero stays healthy; stale attempt downgrades to "
                  "UNKNOWN via recency rule"],
     "forbidden": ["stale attempt upgraded to healthy because status=ok"],
     "provenance": "last_attempt_at vs observation time",
     "status": "executable",
     "covered_by": ["tests/test_golden_corpus.py::"
                    "test_gic03_zero_vs_stagnant"],
     "origin": "Law 3 silent-failure family"},
    {"id": "GIC-04", "title": "scheduler fired, process never started",
     "plane": "execution liveness",
     "evidence_shape": "positive fire trace, process_started=false",
     "expected": ["MATERIALIZATION_GAP, pre-exec class, cause unattributed"],
     "forbidden": ["collector-regression diagnosis", "fabricated NO stages "
                   "without positive contrary evidence"],
     "provenance": "scheduler trace (journal/wrapper)",
     "status": "executable",
     "covered_by": ["tests/test_p41_no_work.py::"
                    "test_g3_preexec_failure_raises_materialization_gap_"
                    "not_collector_fault",
                    "tests/test_p42_attestation.py::"
                    "test_p42_g4_preexec_gap_unchanged"],
     "origin": "INC-20260822 root stash/logs ownership outage"},
    {"id": "GIC-05", "title": "started but participant record absent under "
             "mandatory materialization",
     "plane": "execution liveness",
     "evidence_shape": "fired+started trace, policy ALWAYS, bound exceeded,"
                       " no newer run row",
     "expected": ["MATERIALIZATION_GAP persistence-failure class"],
     "forbidden": ["gap inference for WHEN_WORK_ATTEMPTED/OPTIONAL lanes"],
     "provenance": "trace + materialization_policy",
     "status": "executable",
     "covered_by": ["tests/test_p41_no_work.py::"
                    "test_g3_preexec_failure_raises_materialization_gap_"
                    "not_collector_fault",
                    "tests/test_p41_no_work.py::"
                    "test_oem_real_shape_detect_integration_positive_control"],
     "origin": "P-4.1 positive control"},
    {"id": "GIC-06", "title": "legitimate NO_WORK_DUE",
     "plane": "execution liveness",
     "evidence_shape": "attested execution_result=no_work_due",
     "expected": ["NO_WORK_DUE; RUN_MATERIALIZED intentionally NO"],
     "forbidden": ["MATERIALIZATION_GAP", "remediation recommendation"],
     "provenance": "extractor id/version + bounded output excerpt",
     "status": "executable",
     "covered_by": ["tests/test_p42_attestation.py::"
                    "test_oem_real_shape_synthesis_never_fabricates_gap"],
     "origin": "OEM Radar live specimen 2026-08-24T18:20Z"},
    {"id": "GIC-07", "title": "observer blindness",
     "plane": "observer",
     "evidence_shape": "adapter failure / unreadable store",
     "expected": ["UNKNOWN across planes; harvest completes"],
     "forbidden": ["missing execution inferred", "fleet abort"],
     "provenance": "FAILED_ADAPTER error verbatim",
     "status": "executable",
     "covered_by": ["tests/test_m0.py::"
                    "test_corrupt_adapter_does_not_abort_fleet",
                    "tests/test_p4_golden.py::"
                    "test_p4_g2_observer_blind_stays_unknown"],
     "origin": "Smartwatch missing-volume window"},
    {"id": "GIC-08", "title": "restored DB keeps lineage",
     "plane": "continuity",
     "evidence_shape": "RESTORE_FROM_BACKUP event, prior epoch retained",
     "expected": ["RESTORED_HISTORY/GAP_KNOWN; NOT a new epoch; HEALTHY "
                  "possible independently"],
     "forbidden": ["NEW_EPOCH claim", "history silently merged"],
     "provenance": "backup manifest + continuity registry",
     "status": "executable",
     "covered_by": ["tests/test_golden_incidents_g1_g8.py::"
                    "test_g1_smartwatch_restore_keeps_lineage_and_reports_gap",
                    "tests/test_p42_attestation.py::"
                    "test_smartwatch_continuity_gap_guard_unchanged"],
     "origin": "smartwatch restore 2026-08-23"},
    {"id": "GIC-09", "title": "total loss creates explicit NEW_EPOCH",
     "plane": "continuity/novelty",
     "evidence_shape": "no backup; fresh DB; baseline quiet",
     "expected": ["NEW_EPOCH; baseline suppression; absence never zero"],
     "forbidden": ["novelty flood", "pre/post histories merged"],
     "provenance": "continuity registry event",
     "status": "executable",
     "covered_by": ["tests/test_golden_db_loss.py::"
                    "test_db_loss_new_epoch_baseline_never_reads_as_novelty_"
                    "or_recovery_story",
                    "tests/test_golden_incidents_g1_g8.py::"
                    "test_g2_feature_phone_new_epoch_never_reads_as_organic_"
                    "disappearance"],
     "origin": "feature-phone total loss 2026-08-23"},
    {"id": "GIC-10", "title": "intentionally dormant lane",
     "plane": "execution liveness",
     "evidence_shape": "policy RETIRED/MANUAL; stale artifacts prove nothing",
     "expected": ["INTENTIONALLY_DORMANT; no missing-run anomaly"],
     "forbidden": ["STALE_RUN", "MATERIALIZATION_GAP"],
     "provenance": "expectations registry",
     "status": "executable",
     "covered_by": ["tests/test_p4_golden.py::"
                    "test_p4_g4_intentional_dormancy_emits_no_missing_run_"
                    "anomaly"],
     "origin": "tablet-clank-soak.service retirement"},
    {"id": "GIC-11", "title": "multi-cadence scheduler evidence",
     "plane": "execution liveness",
     "evidence_shape": "positive traces, cadence_seconds null",
     "expected": ["stage evidence retained; state UNKNOWN unless positive "
                  "pre-exec/no-work proof applies"],
     "forbidden": ["invented persistence-delay threshold"],
     "provenance": "expectations registry multi_cadence flag",
     "status": "executable",
     "covered_by": ["tests/test_p4_golden.py::"
                    "test_p4_g7a_multi_cadence_lane_trace_proves_gap",
                    "tests/test_p4_golden.py::"
                    "test_p4_g7b_multi_cadence_fired_and_started_never_"
                    "fabricates_a_gap",
                    "tests/test_p42_attestation.py::"
                    "test_p42_g7_multi_cadence_attestation_retained"],
     "origin": "feature-phone/watch convergence-pass finding"},
    {"id": "GIC-12", "title": "application failure after successful start",
     "plane": "execution liveness / health",
     "evidence_shape": "failed participant run materialized",
     "expected": ["application failure via health/run semantics"],
     "forbidden": ["pre-exec MATERIALIZATION_GAP"],
     "provenance": "run row status/outcome",
     "status": "executable",
     "covered_by": ["tests/test_p42_attestation.py::"
                    "test_p42_g3_attested_failed_is_never_no_work",
                    "tests/test_golden_incidents_g1_g8.py::"
                    "test_p4_g3_application_failure_is_not_a_materialization_"
                    "gap"],
     "origin": "P-4.1 negative controls"},
    {"id": "GIC-13", "title": "delivery independent of generation",
     "plane": "delivery",
     "evidence_shape": "generation substrate + separate delivery states",
     "expected": ["generated/pending/sent/failed/suppressed distinct"],
     "forbidden": ["generation implies delivered"],
     "provenance": "notifications/webhook_deliveries rows or log-only marker",
     "status": "executable",
     "covered_by": ["tests/test_observer_expansion.py::"
                    "test_golden_d_generation_and_delivery_separate",
                    "tests/test_fgt_onboarding.py::"
                    "test_fgt_g4_delivery_claims_never_borrow_from_generation"],
     "origin": "subscription-notification incident; OEM outbox split"},
    {"id": "GIC-14", "title": "schema drift / unsupported participant schema",
     "plane": "persistence",
     "evidence_shape": "expected table absent or query incompatible",
     "expected": ["WARNING + degraded-to-WARNING/UNKNOWN overall; never a "
                  "crash, never fabricated zeros"],
     "forbidden": ["silent empty results presented as healthy"],
     "provenance": "sqlite error/table inventory",
     "status": "executable",
     "covered_by": ["tests/test_golden_corpus.py::test_gic14_schema_drift"],
     "origin": "smartwatch pre-mapping stage discipline"},
    {"id": "GIC-15", "title": "duplicate/replayed evidence",
     "plane": "observer ingestion",
     "evidence_shape": "two observations sharing one invocation_key",
     "expected": ["one logical fire; richest evidence wins; superseded ids "
                  "reported; disk append-only"],
     "forbidden": ["double-counted fires", "silent history rewrite"],
     "provenance": "invocation_key + superseded warnings",
     "status": "executable",
     "covered_by": ["tests/test_p42_attestation.py::"
                    "test_p42_g8_duplicate_invocation_dedup_append_only"],
     "origin": "probe rerun enrichment pattern"},
    {"id": "GIC-16", "title": "backup exists, integrity unverified",
     "plane": "survivability",
     "evidence_shape": "BACKUP_CREATED without hash/integrity records",
     "expected": ["UNVERIFIED; RECOVERY_POINT_WITHOUT_ARTIFACT_HASH warning"],
     "forbidden": ["VERIFIED claim from existence"],
     "provenance": "survivability events",
     "status": "executable",
     "covered_by": ["tests/test_p4_golden.py::"
                    "test_p4_g5_backup_without_artifact_hash_warns_and_flags"],
     "origin": "ACT-011 field discipline"},
    {"id": "GIC-17", "title": "integrity verified, restore never tested",
     "plane": "survivability",
     "evidence_shape": "integrity record present, drill absent",
     "expected": ["INTEGRITY_VERIFIED, never RESTORE_VERIFIED"],
     "forbidden": ["restore claims without drills"],
     "provenance": "drill records relationship",
     "status": "executable",
     "covered_by": ["tests/test_p4_golden.py::"
                    "test_g6_backup_existence_is_unverified"],
     "origin": "ACT-011 method note"},
    {"id": "GIC-18", "title": "off-host copy in temporary scratch",
     "plane": "survivability",
     "evidence_shape": "TRANSFERRED_OFFHOST destination_class="
                       "temporary_scratch",
     "expected": ["durable gate stays OPEN"],
     "forbidden": ["scratch counted as redundancy"],
     "provenance": "destination_class on transfer record",
     "status": "executable",
     "covered_by": ["tests/test_p4_golden.py::"
                    "test_g8_temporary_scratch_offhost_does_not_close_"
                    "durability_gate"],
     "origin": "post-ACT-011 posture"},
    {"id": "GIC-19", "title": "qualification without rewriting history",
     "plane": "epistemology",
     "evidence_shape": "later continuity knowledge applied at derive time",
     "expected": ["original artifact bytes unchanged; qualification additive"],
     "forbidden": ["editing append-only lines to clean the timeline"],
     "provenance": "registry hash on derived payloads",
     "status": "executable",
     "covered_by": ["tests/test_continuity.py::"
                    "test_snapshot_annotation_does_not_mutate_the_original_"
                    "artifact",
                    "tests/test_reconcile_history.py::"
                    "test_reconcile_is_read_only"],
     "origin": "ADR-0006 append-only principle"},
    {"id": "GIC-20", "title": "capability absent vs unsupported vs unknown",
     "plane": "capability contract",
     "evidence_shape": "tri-state statements with evidence refs",
     "expected": ["canonical CapabilityState only; violations surfaced"],
     "forbidden": ["bare booleans", "UNKNOWN collapsed into either flavor "
                   "of unsupported"],
     "provenance": "evidence ref per statement",
     "status": "executable",
     "covered_by": ["tests/test_p41_no_work.py::"
                    "test_capability_states_canonical_vocabulary_enforced",
                    "tests/test_fgt_onboarding.py::"
                    "test_fgt_g10_only_canonical_capability_states",
                    "tests/test_adapter_contract_v02.py::"
                    "test_every_registered_adapter_emits_canonical_capability_"
                    "states"],
     "origin": "KTW/CTW/FPC delivery distinctions"},
    # ---- mined additions (real fleet incidents beyond the brief) -------
    {"id": "GIC-21", "title": "directory sweep mistaken for fleet inventory",
     "plane": "membership",
     "evidence_shape": "local checkout listing used as membership",
     "expected": ["membership only via registry/inventory"],
     "forbidden": ["filesystem-derived members"],
     "provenance": "fleet.yaml classification",
     "status": "executable",
     "covered_by": ["conformance/test_fleet_laws.py"],
     "origin": "L-FLEET-001 tablet omission"},
    {"id": "GIC-22", "title": "resource naming mistaken for identity",
     "plane": "destructive safety",
     "evidence_shape": "volume name containing 'staging' destroyed real "
                       "production state",
     "expected": ["identity resolved from metadata/evidence, never names"],
     "forbidden": ["pattern-derived destructive targets"],
     "provenance": "ADR-0009 contract",
     "status": "registered_pending_fixture",
     "covered_by": ["continuity/seeds/"
                    "INC-20260822-23-fleet-outage-and-volume-loss.jsonl"],
     "origin": "INC-20260823 family B"},
    {"id": "GIC-23", "title": "runtime state consumed by tree-wide git ops",
     "plane": "deployment safety",
     "evidence_shape": "untracked logs/ inside checkouts; stash -u recreates "
                       "ownership",
     "expected": ["runtime/source separation law; scoped chown remediation"],
     "forbidden": ["stash/clean/reset against live checkouts"],
     "provenance": ".gitignore commits + ownership audit",
     "status": "registered_pending_fixture",
     "covered_by": ["ADR-0009"],
     "origin": "INC-20260822 family A"},
    {"id": "GIC-24", "title": "dual scheduler authority per lane",
     "plane": "scheduling",
     "evidence_shape": "two enabled mechanisms, one failing silently",
     "expected": ["exactly one enabled authority; disabled provably inert"],
     "forbidden": ["aggregated health across authorities"],
     "provenance": "inventory + conformance law5 tests",
     "status": "executable",
     "covered_by": ["conformance/../motherclank/tests/test_p4_golden.py::"
                    "test_retired_lane_trace_does_not_create_anomalies",
                    "conformance/test_fleet_laws.py"],
     "origin": "smartwatch systemd timer; SemInt cron bypass"},
    {"id": "GIC-25", "title": "capability collapsed into false boolean",
     "plane": "capability contract",
     "evidence_shape": "delivery supported-by-code but unconfigured/absent "
                       "on host",
     "expected": ["tri-state with evidence refs distinguishes all causes"],
     "forbidden": ["boolean capability fields"],
     "provenance": "capability statement evidence refs",
     "status": "executable",
     "covered_by": ["tests/test_fgt_onboarding.py::"
                    "test_fgt_g10_only_canonical_capability_states"],
     "origin": "KTW/CTW/FPC delivery mismatch"},
)


def ids() -> tuple[str, ...]:
    return tuple(e["id"] for e in ENTRIES)


def get(gic_id: str) -> dict | None:
    return next((e for e in ENTRIES if e["id"] == gic_id), None)

# ---- P-4.3 additions: evidence semantics / lane configuration -------------

ENTRIES += (
    {"id": "GIC-26", "title": "native run timestamp vs derived activity "
              "timestamp", "plane": "semantic clocks",
     "evidence_shape": "MAX(derived) vs native run row for the same lane",
     "expected": ["clock labels distinguish DERIVED_ACTIVITY_MAX from "
                  "native_run_row"],
     "forbidden": ["interchangeable comparison without annotation"],
     "provenance": "last_run.clock + derived_from",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic26_native_run_vs_derived_activity_are_labeled"],
     "origin": "v0.3 audit of last_run overload"},
    {"id": "GIC-27", "title": "scheduler time compared with participant time",
     "plane": "semantic clocks",
     "evidence_shape": "trace invoked_at vs participant run timestamp",
     "expected": ["ordering allowed with visible cross_clock_comparison "
                  "annotation"],
     "forbidden": ["silent treatment as one clock"],
     "provenance": "trace.clock + last_run.clock",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic27_cross_clock_comparison_is_annotated_not_"
                    "silent"],
     "origin": "P-4 persistence-gap derivation"},
    {"id": "GIC-28", "title": "unknown evidence type", "plane": "evidence",
     "evidence_shape": "envelope with unregistered evidence_type",
     "expected": ["visible in unknown_evidence; zero derived claims"],
     "forbidden": ["invented claims", "observation abort"],
     "provenance": "envelope content hash",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic28_unknown_evidence_type_is_visible_and_claim_"
                    "free"],
     "origin": "v0.3 forward-compatibility requirement"},
    {"id": "GIC-29", "title": "unsupported evidence major", "plane": "evidence",
     "evidence_shape": "registered type, newer major version",
     "expected": ["UNSUPPORTED_MAJOR listing supported majors; claim-free"],
     "forbidden": ["best-effort parsing of unknown shapes"],
     "provenance": "type registry majors",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic29_unsupported_major_is_visible_and_claim_free"],
     "origin": "v0.3 versioning requirement"},
    {"id": "GIC-30", "title": "malformed known-type payload", "plane": "evidence",
     "evidence_shape": "known type/major, payload violates schema",
     "expected": ["KNOWN_PAYLOAD_INVALID with violations"],
     "forbidden": ["partial claims from invalid payloads"],
     "provenance": "type validator output",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic30_malformed_known_payload_is_visible_and_claim_"
                    "free"],
     "origin": "capability-validation precedent"},
    {"id": "GIC-31", "title": "contradictory lane identity", "plane": "lane config",
     "evidence_shape": "one instance_id claimed by two clanks",
     "expected": ["conflict detected at configuration level"],
     "forbidden": ["cross-contaminated health/dedup"],
     "provenance": "lane-config identity sweep",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic31_contradictory_lane_identity_detected"],
     "origin": "registry duplicate-store lesson"},
    {"id": "GIC-32", "title": "declaration without observation",
     "plane": "config/observation separation",
     "evidence_shape": "PERIODIC expectation, empty harvest",
     "expected": ["no fired/executed/materialized claims anywhere"],
     "forbidden": ["configuration manufacturing observations"],
     "provenance": "expectations registry only",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic32_declaration_alone_never_manufactures_"
                    "observation"],
     "origin": "ADR-0008/0011 boundary"},
    {"id": "GIC-33", "title": "observation contradicting declaration",
     "plane": "config/observation separation",
     "evidence_shape": "no_work_due trace against ALWAYS policy lane",
     "expected": ["derivation may qualify; declaration object unchanged"],
     "forbidden": ["observations silently rewriting policy"],
     "provenance": "immutable expectation record",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic33_observation_never_rewrites_declaration"],
     "origin": "P-4.1 OEM false-gap root cause"},
    {"id": "GIC-34", "title": "multi-cadence lane stays multi-cadence",
     "plane": "lane config",
     "evidence_shape": "cadence null + multi_cadence true",
     "expected": ["config valid; no single cadence invented"],
     "forbidden": ["placeholder cadence values"],
     "provenance": "operator-verified seed migration",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic34_multi_cadence_config_stays_multi_cadence"],
     "origin": "smartphone/watch/feature-phone lanes"},
    {"id": "GIC-35", "title": "large collector expansion, zero core edits",
     "plane": "hot-swap boundary",
     "evidence_shape": "census envelope scaling 3 -> 18 collectors",
     "expected": ["identical observer handling at any scale"],
     "forbidden": ["brand/collector tokens in Motherclank core"],
     "provenance": "collector_census envelope",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic35_watch_collector_expansion_zero_core_edits"],
     "origin": "Watch expansion programme dogfood"},
    {"id": "GIC-36", "title": "new evidence primitive via extension path",
     "plane": "evidence extension",
     "evidence_shape": "runtime type+consumer registration, new payload shape",
     "expected": ["derived claim produced through public API only"],
     "forbidden": ["core edits for participant-specific semantics"],
     "provenance": "type registry + consumer output",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic36_new_evidence_primitive_without_participant_"
                    "core_edits"],
     "origin": "v0.3 extension-path proof"},
    {"id": "GIC-37", "title": "fresh observer time over stale occurrence",
     "plane": "semantic clocks",
     "evidence_shape": "observed_at now, occurred_at weeks old",
     "expected": ["both timestamps preserved verbatim; staleness not "
                  "laundered"],
     "forbidden": ["occurred_at overwritten by ingestion time"],
     "provenance": "envelope dual timestamps",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic37_fresh_observer_time_never_launders_stale_"
                    "occurrence"],
     "origin": "GIC-03 generalization"},
    {"id": "GIC-38", "title": "event time newer than ingestion (clock anomaly)",
     "plane": "semantic clocks",
     "evidence_shape": "occurred_at after observed_at",
     "expected": ["anomaly preserved verbatim for downstream consumers"],
     "forbidden": ["silent normalization of impossible clocks"],
     "provenance": "envelope dual timestamps",
     "status": "executable",
     "covered_by": ["tests/test_p43_evidence.py::"
                    "test_gic38_event_time_newer_than_ingestion_flagged_"
                    "verbatim"],
     "origin": "distributed-host reality"},
    {"id": "GIC-39", "title": "participant-native confidence mistaken "
              "for observer truth", "plane": "provenance/epistemology",
     "evidence_shape": "participant 0..1 confidence float on domain objects",
     "expected": ["value preserved verbatim as participant-native"],
     "forbidden": ["normalizing into observer truth judgments"],
     "provenance": "payload.native_confidence with explicit disclaimer",
     "status": "executable",
     "covered_by": ["tests/test_si_onboarding.py::"
                    "test_gic39_participant_confidence_is_not_observer_truth"],
     "origin": "semiconductor-intelligence claims.confidence"},
    {"id": "GIC-40", "title": "application execution present with empty "
              "provider-attempt substrate",
     "plane": "execution liveness / dual execution planes",
     "evidence_shape": "operational_job_runs rows exist; provider_runs "
                       "table absent or zero rows; sources configured as "
                       "manual-only (polling_enabled=0)",
     "expected": ["application liveness known/current from job plane",
                  "provider plane independently empty/unknown",
                  "no MATERIALIZATION_GAP from provider_runs=0 alone"],
     "forbidden": ["provider_runs=0 implying application failure",
                   "collapsing job and provider planes into one substrate",
                   "fabricating provider attempts from job evidence"],
     "provenance": "last_run.clock=native_operational_job_run + "
                   "substrate=operational_job_runs; "
                   "sources.polling_enabled evidence",
     "status": "executable",
     "covered_by": ["tests/test_p44_execution_planes.py::"
                    "test_gic40_app_execution_present_provider_empty_is_"
                    "honest",
                    "tests/test_p44_execution_planes.py::"
                    "test_gic40b_with_provider_runs_both_planes_present"],
     "origin": "SI live convergence: OperationalScheduler active but sole "
               "source is manual (polling_enabled=0); provider_runs=0 is "
               "legitimate"},
    {"id": "GIC-41", "title": "declared optional evidence extension "
              "unreachable from observer dispatch",
     "plane": "observer contract / hot-swap boundary",
     "evidence_shape": "adapter implements a valid typed-evidence producer "
                       "(e.g., evidence_envelopes) but observer dispatch "
                       "uses a hardcoded method list that omits it",
     "expected": ["generic registry-driven extension dispatch invokes all "
                  "declared extensions without snapshot.py edits per new "
                  "extension",
                  "conformance test catches unreachable declared extensions"],
     "forbidden": ["hardcoded extension invocation lists in core dispatch",
                   "adapters passing tests while their production evidence "
                   "producer is unreachable"],
     "provenance": "contract.register_optional_extension + "
                   "optional_extension_names()",
     "status": "executable",
     "covered_by": ["tests/test_p44_execution_planes.py::"
                    "test_generic_dispatch_invokes_newly_registered_extension",
                    "tests/test_p44_execution_planes.py::"
                    "test_gic41_evidence_envelopes_flow_through_full_harvest",
                    "tests/test_p44_execution_planes.py::"
                    "test_core_has_no_hardcoded_extension_list"],
     "origin": "P-4.4 live convergence: intelligence_assertion@1 existed "
               "at unit level but SI adapter never emitted envelopes "
               "through live harvest because snapshot.py's hardcoded "
               "list didn't include evidence_envelopes"},
)
