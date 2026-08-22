"""ADR-0003 §2: operator-owned ClankRegistry seed for Inbox bridging.

The registry validates every primary_clank_id written to the Agent Inbox.
Motherclank never invents identities: the onboarded ids are exactly the four
Phase 2C adapters plus 'fleet-wide' (registry-exempt). Extending the onboarded
set is an ADR-0002 onboarding decision, not a bridge concern.
"""
from __future__ import annotations

from clank_runtime.registry.core import ClankRegistration, ClankRegistry

ONBOARDED = ("watch-clank", "smartphone-clank", "korean-tech-wire", "feature-phone-clank")


def operator_registry() -> ClankRegistry:
    reg = ClankRegistry()
    for clank_id in ONBOARDED:
        reg.register(ClankRegistration(clank_id=clank_id, display_name=clank_id))
    return reg
