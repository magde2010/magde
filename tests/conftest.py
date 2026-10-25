"""Ensure repository modules are importable under both `pytest` and `python -m pytest`."""
import sys
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
