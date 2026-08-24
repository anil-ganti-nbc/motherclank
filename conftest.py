"""Ensure tests exercise THIS workspace's sources, not whichever editable
install or sibling checkout last won site-packages (multiple operator/agent
working copies exist on shared machines)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Adapter plane + shared contracts from the canonical sibling checkout
# (Phase 1 workspace convention: <workspace>/diagnostic-clank).
DIAGNOSTIC = ROOT.parent / "diagnostic-clank"
for sub in ("clank-runtime/src", "clank-fleet/src"):
    p = DIAGNOSTIC / sub
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))
