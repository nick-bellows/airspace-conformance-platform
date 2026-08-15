"""Put the repository root on `sys.path` for the test run.

`acp` is installed (src layout), but `scripts/` deliberately is not packaged --
those are developer tools, not shipped code. The contract tests still need to
import the drift gate so the same check runs locally under pytest and in CI.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
