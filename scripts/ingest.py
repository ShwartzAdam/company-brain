"""python scripts/ingest.py  -- (re)build the OpenSearch index from the dataset."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import ingest  # noqa: E402

print("indexed chunks:", ingest.run(recreate=True))
