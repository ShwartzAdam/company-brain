"""SQLite built from the dataset's schema.sql + seed.sql. Parameterised SQL only."""
import sqlite3

from brain import config, gate

# Our own table (dataset untouched): one row per idempotent write.
EXTRA = """CREATE TABLE IF NOT EXISTS write_keys (
  key TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL,
  request_id TEXT NOT NULL DEFAULT '', trace_id TEXT NOT NULL DEFAULT '');
""" + gate.DDL + ";"


def reset_db(path=None) -> None:
    """(Re)build the database from the dataset. Destroys existing writes."""
    p = path or config.DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.unlink(missing_ok=True)
    c = sqlite3.connect(p)
    c.executescript(config.DB_SCHEMA.read_text(encoding="utf-8"))
    c.executescript(config.DB_SEED.read_text(encoding="utf-8"))
    c.executescript(EXTRA)
    c.commit()
    c.close()


def conn(path=None) -> sqlite3.Connection:
    p = path or config.DB_PATH
    if not p.exists():
        reset_db(p)
    c = sqlite3.connect(p, check_same_thread=False)
    c.row_factory = sqlite3.Row
    have = {r[1] for r in c.execute("PRAGMA table_info(write_keys)")}
    for col in ("request_id", "trace_id"):  # a database built before Step 5 gains the trace-link columns
        if have and col not in have:
            c.execute(f"ALTER TABLE write_keys ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            c.commit()
    return c


def rows(sql: str, args=(), path=None) -> list[dict]:
    c = conn(path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()
