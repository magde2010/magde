"""Railway entrypoint: run the isolated MEXC Grid Bot."""
from pathlib import Path
import sys

GRID_ROOT = Path(__file__).resolve().parent / "mexc-grid-bot"
sys.path.insert(0, str(GRID_ROOT))

from gridbot.main import main

if __name__ == "__main__":
    main()
