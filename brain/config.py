"""Settings from .env. The clock is never read here: callers pass `as_of`."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATASET_ROOT = (ROOT / os.getenv("DATASET_ROOT", "data/novaops")).resolve()
DOCS_DIR = DATASET_ROOT / "documents"
DB_SCHEMA = DATASET_ROOT / "database" / "schema.sql"
DB_SEED = DATASET_ROOT / "database" / "seed.sql"
DB_PATH = Path(os.getenv("BRAIN_DB_PATH", ROOT / "state" / "brain.db"))

REGION = os.getenv("AWS_REGION", "us-east-1")
MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
EMBED_MODEL_ID = os.getenv("BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = 1024
OPENSEARCH_COLLECTION = os.getenv("OPENSEARCH_COLLECTION", "company-brain")
INDEX_NAME = os.getenv("OPENSEARCH_INDEX", "company-brain-docs")
MCP_HOST, MCP_PORT = "127.0.0.1", int(os.getenv("MCP_PORT", "9890"))
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}/mcp"

# Bounds on every call that can hang (seconds). Overridable from .env.
TOOL_TIMEOUT_S = float(os.getenv("TOOL_TIMEOUT_S", "60"))        # one MCP tool call
TURN_TIMEOUT_S = float(os.getenv("TURN_TIMEOUT_S", "240"))       # one whole graph run (a turn)
RUN_TURN_TIMEOUT_S = float(os.getenv("RUN_TURN_TIMEOUT_S", str(TURN_TIMEOUT_S + 60)))  # eval runner's outer bound
BEDROCK_CONNECT_TIMEOUT_S = float(os.getenv("BEDROCK_CONNECT_TIMEOUT_S", "10"))
BEDROCK_READ_TIMEOUT_S = float(os.getenv("BEDROCK_READ_TIMEOUT_S", "60"))
BEDROCK_MAX_ATTEMPTS = int(os.getenv("BEDROCK_MAX_ATTEMPTS", "3"))
