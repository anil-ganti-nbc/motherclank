"""F2 — registry-driven adapter onboarding: zero Motherclank core edits.

Proves that onboarding a new observer Clank requires only a registry file,
never a change to motherclank source (Canonical Standard v0.1 §37 / Law 18).
"""
from __future__ import annotations

import json
import sys
import types

import pytest

from motherclank import adapters


class _StubAdapter:
    registered = {}

    def __init__(self, db_path):
        self.db_path = db_path
        _StubAdapter.registered[str(db_path)] = True


def _install_stub_module(monkeypatch, module_name, class_name):
    module = types.ModuleType(module_name)
    setattr(module, class_name, type(class_name, (_StubAdapter,), {}))
    monkeypatch.setitem(sys.modules, module_name, module)
    return module_name, class_name


def test_builtin_registry_covers_the_four_validated_clanks():
    registry = adapters.load_registry(None)
    assert set(registry) >= {
        "watch-clank", "smartphone-clank", "korean-tech-wire", "feature-phone-clank"}


def test_registry_override_adds_a_clank_without_core_edits(tmp_path, monkeypatch):
    module_name, class_name = _install_stub_module(
        monkeypatch, "clank_fleet.adapters.oem_radar", "OemRadarAdapter")

    registry_file = tmp_path / "adapter-registry.json"
    registry_file.write_text(json.dumps({
        "extend_builtin": True,
        "oem-radar": {
            "module": module_name,
            "class": class_name,
            "db": "oem_radar.db",
            "qc": False,
        },
    }), encoding="utf-8")

    real_state = tmp_path / "real-state"
    real_state.mkdir()

    built = adapters.build_adapters(real_state, registry_path=registry_file)
    assert "oem-radar" in built["adapters"]
    assert isinstance(built["adapters"]["oem-radar"], _StubAdapter)
    assert built["qc_adapters"]  # builtin QC members preserved by extend


def test_registry_replace_semantics(tmp_path):
    registry_file = tmp_path / "adapter-registry.json"
    registry_file.write_text(json.dumps({
        "extend_builtin": False,
        "solo-clank": {"module": "m", "class": "C", "db": "solo.db"},
    }), encoding="utf-8")
    registry = adapters.load_registry(registry_file)
    assert set(registry) == {"solo-clank"}


def test_malformed_registry_rows_fail_loudly(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"x-clank": {"module": "m"}}), encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable):
        adapters.load_registry(bad)


def test_env_variable_selects_registry(tmp_path, monkeypatch):
    env_file = tmp_path / "env-registry.json"
    env_file.write_text(json.dumps({"extend_builtin": False}), encoding="utf-8")
    monkeypatch.setenv("MOTHERCLANK_ADAPTER_REGISTRY", str(env_file))
    assert adapters.load_registry(None) == {}
