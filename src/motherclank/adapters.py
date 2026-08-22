"""Adapter loading: reuse Diagnostic Clank's read-only adapters UNCHANGED.

The adapters live in the diagnostic-clank checkout (clank-fleet + clank-runtime
packages; not on PyPI). We locate them as workspace siblings and import from
there — no vendoring, no copying, so adapter evolution stays single-sourced.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REQUIRED = [
    ("clank_fleet.adapters.watch_clank", "WatchClankAdapter"),
    ("clank_fleet.adapters.smartphone_clank", "SmartphoneClankAdapter"),
    ("clank_fleet.adapters.korean_tech_wire", "KoreanTechWireAdapter"),
    ("clank_fleet.adapters.feature_phone", "FeaturePhoneAdapter"),
]


class AdapterPlaneUnavailable(RuntimeError):
    pass


def _candidate_roots(explicit: Path | None) -> list[Path]:
    roots: list[Path] = []
    if explicit:
        roots.append(explicit)
        if (explicit / "diagnostic-clank").exists():
            roots.append(explicit / "diagnostic-clank")
    here = Path(__file__).resolve()
    for base in (*here.parents, *here.parents[0].parents):
        cand = base / "diagnostic-clank"
        if cand.exists():
            roots.append(cand)
    return roots


def ensure_adapter_plane(diagnostic_clank_path: Path | None = None) -> None:
    """Put clank-fleet/clank-runtime sources on sys.path, idempotently."""
    for root in _candidate_roots(diagnostic_clank_path):
        fleet = root / "clank-fleet" / "src"
        runtime = root / "clank-runtime" / "src"
        if (fleet / "clank_fleet" / "adapters").exists() and (runtime / "clank_runtime").exists():
            for pth in (str(fleet), str(runtime)):
                if pth not in sys.path:
                    sys.path.insert(0, pth)
            return
    raise AdapterPlaneUnavailable(
        "diagnostic-clank checkout with clank-fleet/clank-runtime sources not found; "
        "pass --adapters-src pointing at it"
    )


def build_adapters(real_state_dir: Path) -> dict[str, object]:
    """Instantiate the four onboarded adapters against read-only DB copies."""
    ensure_adapter_plane()
    from clank_fleet.adapters.feature_phone import FeaturePhoneAdapter  # noqa: PLC0415
    from clank_fleet.adapters.korean_tech_wire import KoreanTechWireAdapter  # noqa: PLC0415
    from clank_fleet.adapters.smartphone_clank import SmartphoneClankAdapter  # noqa: PLC0415
    from clank_fleet.adapters.watch_clank import WatchClankAdapter  # noqa: PLC0415
    from clank_runtime.version import ADAPTER_CONTRACT_VERSION  # noqa: PLC0415

    d = real_state_dir
    adapters = {
        "watch-clank": WatchClankAdapter(db_path=d / "watch_clank.db"),
        "smartphone-clank": SmartphoneClankAdapter(db_path=d / "smartphone_clank.db"),
        "korean-tech-wire": KoreanTechWireAdapter(db_path=d / "korean_tech_wire.db"),
        "feature-phone-clank": FeaturePhoneAdapter(db_path=d / "feature_phone_clank.db"),
    }
    versions = {"adapter_contract_version": ADAPTER_CONTRACT_VERSION}
    return {"adapters": adapters, "versions": versions}
