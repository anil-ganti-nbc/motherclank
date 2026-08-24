"""Ensure tests exercise THIS checkout's sources, not whichever editable
motherclank install last won site-packages (multiple operator/agent working
copies exist on shared machines)."""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
