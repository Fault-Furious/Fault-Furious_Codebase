import sys
from pathlib import Path

# belief.py sits in the repo root and the agent in agent/, so plain `pytest` finds both
ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "agent")]
