from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from motherclank import adapters
from motherclank import snapshot as snap
from motherclank import synthesis as syn
from motherclank.report import render_report


def _cvc_fixture(tmp_path: Path, *, integrity: str = "PASS") -> Path:
    root = tmp_path / "cvc"
    package = root / "src" / "cvc"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    payload = {
        "schema_version": "cvc-observer.v0.1",
        "identity": {
            "clank_id": "cvc-clank", "display_name": "CVC Clank",
            "repo": "https://github.com/anil-ganti-nbc/cvc-clank",
            "lifecycle": "OPERATIONAL", "execution_model": "OPERATOR_TRIGGERED",
        },
        "health": {
            "corpus_integrity": integrity, "frozen_artifact_count": 42,
            "hash_mismatch_count": 1 if integrity == "FAIL" else 0,
            "manifest_consistent": integrity == "PASS",
            "runtime_state_readable": True, "state_read_errors": [],
            "failure_count": 1 if integrity == "FAIL" else 0,
        },
        "board": {
            "total_rules": 38,
            "support_distribution": {"E0": 0, "E1": 1, "E2": 9, "E3": 26, "E4": 2},
            "ratified_e4_count": 2, "ratified_e4_ids": ["STD-EPI-001", "STD-STD-002"],
            "matrix_version": "FLEET_SUPPORT_MATRIX_V0.2", "board_status": "FINAL_BOARD_SYNTHESIS",
        },
        "activity": {"latest_ingestion": None, "latest_review": None, "pending_reviews": 0, "unresolved_ingestion_count": 0},
        "triggers": {"open_count": 12, "open_trigger_ids": [f"CVC-FET-{i:03d}" for i in range(1, 13)], "items": []},
        "summary": {"status": "OPERATIONAL" if integrity == "PASS" else "DEGRADED", "integrity": integrity, "rules": 38, "ratified_e4": 2, "open_triggers": 12, "pending_reviews": 0},
    }
    (package / "observer.py").write_text(
        "import json\n"
        f"PAYLOAD = {json.dumps(payload)!r}\n"
        "def observer_snapshot(root):\n"
        "    return json.loads(PAYLOAD)\n",
        encoding="utf-8",
    )
    return root


def _inventory(tmp_path: Path) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(
        "repositories:\n"
        "  - name: cvc-clank\n"
        "    classification: CLANK\n"
        "    deployment_state: NOT_APPLICABLE\n",
        encoding="utf-8",
    )
    return path


def _diagnostic_root() -> Path:
    configured = os.environ.get("MOTHERCLANK_ADAPTER_ROOT")
    root = (Path(configured) if configured else
            Path(__file__).resolve().parents[1] / "diagnostic-clank")
    adapter_path = root / "clank-fleet" / "src" / "clank_fleet" / "adapters" / "cvc_clank.py"
    if not adapter_path.exists():
        pytest.skip("Diagnostic checkout with the CVC adapter is not available")
    return root


def test_manifest_resolves_cvc_once_and_registry_instantiates_it(tmp_path: Path) -> None:
    adapters.ensure_adapter_plane(_diagnostic_root())
    built = adapters.build_adapters(
        tmp_path / "real-state",
        diagnostic_clank_path=_diagnostic_root(),
        inventory_path=_inventory(tmp_path),
        cvc_root=_cvc_fixture(tmp_path),
    )

    assert list(built["adapters"]) == ["cvc-clank"]
    assert built["adapters"]["cvc-clank"].identity().clank_id == "cvc-clank"
    assert built["adapters"]["cvc-clank"].last_run()["supported"] is False


def test_cvc_integrity_health_surfaces_in_snapshot_and_synthesis(tmp_path: Path) -> None:
    adapters.ensure_adapter_plane(_diagnostic_root())
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    built = adapters.build_adapters(
        real_state, diagnostic_clank_path=_diagnostic_root(),
        inventory_path=_inventory(tmp_path), cvc_root=_cvc_fixture(tmp_path),
    )
    payload, warnings = snap.build_snapshot(
        inventory_path=_inventory(tmp_path), adapters_result=built,
        real_state_dir=real_state, out_dir=tmp_path,
    )
    block = payload["clanks"]["cvc-clank"]
    assert not warnings
    assert block["observer_snapshot"]["board"]["total_rules"] == 38
    assert block["observer_snapshot"]["triggers"]["open_count"] == 12
    assert block["status"]["extensions"]["recency_policy"] == "NONE"
    assert syn.synthesize_fleet(payload)["clanks"]["cvc-clank"]["state"] == "HEALTHY"
    report = render_report(payload)
    assert "| cvc-clank | HEALTHY | integrity | - | - |" in report
    assert "ratified_e4=2" in report


def test_failed_cvc_integrity_is_visible_and_does_not_mutate_sibling_state(tmp_path: Path) -> None:
    adapters.ensure_adapter_plane(_diagnostic_root())
    real_state = tmp_path / "real-state"
    real_state.mkdir()
    sentinel = real_state / "other-clank.db"
    sentinel.write_bytes(b"authoritative sibling state")
    before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
    built = adapters.build_adapters(
        real_state, diagnostic_clank_path=_diagnostic_root(),
        inventory_path=_inventory(tmp_path),
        cvc_root=_cvc_fixture(tmp_path, integrity="FAIL"),
    )
    block = snap.observe_clank(built["adapters"]["cvc-clank"])

    assert block["status"]["operational_state"] == "failed"
    assert block["health"]["overall_status"] == "failed"
    assert block["observer_snapshot"]["health"]["hash_mismatch_count"] == 1
    assert hashlib.sha256(sentinel.read_bytes()).hexdigest() == before
