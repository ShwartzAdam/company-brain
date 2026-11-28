"""python scripts/ingest.py  -- (re)build the OpenSearch index from the dataset."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import config  # noqa: E402
from server import ingest

if ingest.variant() != "base" and config.INDEX_NAME == "company-brain-docs":
    sys.exit("refusing to overwrite the baseline index: set OPENSEARCH_INDEX to a new name for INGEST_VARIANT != base")

print("indexed chunks:", ingest.run(recreate=True))
