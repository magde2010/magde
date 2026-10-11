"""Compatibility import for the shared root Flow Engine."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lotfe_flow_engine import FlowEngine, FlowSettings, FlowSignal, get_flow_engine, scan_flow  # noqa: F401,E402
