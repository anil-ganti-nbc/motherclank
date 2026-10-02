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


def _row(**overrides):
    return {"module": "fixture.adapter", "class": "Adapter", "db": "copy.db",
            "instance_id": "nas-shadow", "lane_id": "experimental", **overrides}


@pytest.mark.parametrize("field", ["instance_id", "lane_id"])
@pytest.mark.parametrize("value", ["", " ", "../state", "two words", "UPPER", 7])
def test_invalid_snapshot_identity_rejected_before_adapter_import(tmp_path, field, value):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"extend_builtin": False,
                                   "test-clank": _row(**{field: value})}), encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match=field):
        adapters.load_registry(registry)


@pytest.mark.parametrize("cid", ["", "../child", "test clank", "UPPER"])
def test_invalid_child_identity_rejected(tmp_path, cid):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"extend_builtin": False, cid: _row()}), encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="invalid registry row"):
        adapters.load_registry(registry)


@pytest.mark.parametrize("text", [
    '{"extend_builtin":false,"test-clank":{},"test-clank":{}}',
    '{"extend_builtin":false,"test-clank":{"db":"first.db","db":"second.db"}}',
    "extend_builtin: false\ntest-clank: {}\ntest-clank: {}\n",
    "extend_builtin: false\ntest-clank:\n  db: first.db\n  db: second.db\n",
])
def test_duplicate_json_or_yaml_keys_never_silently_replace_identity(tmp_path, text):
    registry = tmp_path / "registry.txt"
    registry.write_text(text, encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="duplicate adapter registry key"):
        adapters.load_registry(registry)


def test_relative_store_alias_is_duplicate(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"extend_builtin": False,
                                   "one": _row(), "two": _row(db="./copy.db")}), encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="duplicate store identity"):
        adapters.load_registry(registry)


def test_absolute_relative_store_collision_rejected_before_instantiation(tmp_path, monkeypatch):
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"extend_builtin": False,
                                   "one": _row(),
                                   "two": _row(db=str(tmp_path / "copy.db"))}), encoding="utf-8")
    monkeypatch.setattr(adapters, "ensure_adapter_plane", lambda *args: None)
    # The fixture module does not exist: collision must be caught before any
    # imports or constructors, not after one adapter already read its input.
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="duplicate store identity"):
        adapters.build_adapters(tmp_path, registry_path=registry)


def test_absolute_feature_phone_accepted_path_still_supported(tmp_path):
    accepted = "/app/fp-accepted/export-20261002T061502Z-716e3870f39d42ed/snapshot.db"
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({"extend_builtin": False,
                                   "feature-phone-clank": _row(db=accepted)}), encoding="utf-8")
    assert adapters.load_registry(registry)["feature-phone-clank"]["db"] == accepted


def test_false_string_cannot_silently_enable_builtin_registry(tmp_path):
    registry = tmp_path / "registry.json"
    registry.write_text('{"extend_builtin":"false"}', encoding="utf-8")
    with pytest.raises(adapters.AdapterPlaneUnavailable, match="must be boolean"):
        adapters.load_registry(registry)
