import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
GOLD = json.loads((ROOT / "data" / "GOLDEN-DATASETS.json").read_text())
