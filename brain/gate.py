"""The write gate (issue #10, R1). Two separate gates:
  1. Caller consent: the caller's OWN current top-level message lifts their own 'do not file' rule.
     Decided here by code, never by the model. Recorded as a one-use row bound to caller, employee,
     system, draft hash and message id.
  2. Approver approval: only approvals rows (AP001/AP002) with status 'approved' release anything that
     grants or expands. Read-only from the graph; message text can never create them.
Both checks run INSIDE tools.release_write's transaction (the one write-release function), fail closed.
consent_from_message is a regex heuristic, not a classifier: it can refuse a clean go-ahead that contains
a word like "only" or "if", and it cannot understand intent. It never fails open on quoted or forwarded text."""
import hashlib
import hmac
import os
import re
import sqlite3

DDL = """CREATE TABLE IF NOT EXISTS consents (
  message_id TEXT PRIMARY KEY, caller_id TEXT NOT NULL, employee_id TEXT NOT NULL, system TEXT NOT NULL,
  draft_hash TEXT NOT NULL, created_at TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0)"""

_CUT = re.compile(r"^\s*(-{2,}\s*(forwarded|copied|original)|from:|on .{0,80} wrote:|sent from|"
                  r"old template|confidential)", re.I)
_YES = re.compile(r"go ahead|file it|i'?m saying so|i am saying so|you may file|"
                  r"file (?:the |her |his |\w+'?s? ).{0,40}request", re.I)
_NO = re.compile(r"\b(unless|until|till|if|when|before|wait|not|don'?t|dont|never|later|text only|only)\b", re.I)


def consent_token(caller_id: str, employee_id: str, system: str, message_id: str, draft: str) -> str:
    """Proof that APPLICATION code, not a raw MCP client, asks for this consent row: an HMAC over exactly what the row
    binds, keyed by CONSENT_SECRET (in .env, never in a prompt, loadout or trace). The server recomputes it."""
    key = os.environ.get("CONSENT_SECRET", "")
    if not key:
        raise RuntimeError("CONSENT_SECRET is not set: consent cannot be recorded (fail closed)")
    msg = "\x1f".join([caller_id, employee_id, system.lower(), message_id, hashlib.sha256(draft.encode()).hexdigest()])
    return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()


def check_consent_token(token: str, *args) -> None:
    if not token or not hmac.compare_digest(token, consent_token(*args)):
        raise PermissionError("consent token missing or invalid: only the application can record consent")


def top_level(message: str) -> str:
    """The caller's current words: drop quoted lines and everything from a forward marker on."""
    keep = []
    for line in message.splitlines():
        if _CUT.match(line):
            break
        if not line.lstrip().startswith(">"):
            keep.append(line)
    return "\n".join(keep)


def consent_from_message(message: str) -> bool:
    """Affirmative imperative in a sentence with no conditional/negating word."""
    for s in re.split(r"[.!?\n]+", top_level(message)):
        if _YES.search(s) and not _NO.search(s):
            return True
    return False


_BLOCK = re.compile(r"(do not|don'?t|dont|never|not)\b.{0,30}\b(file|create|submit|execute)|\bno filing\b|until i (say|write|tell)", re.I)


_SELF = re.compile(r"\b(i need|i want|i'?d like|i require|please (?:get|request|give|set)|can you (?:get|request|give|set)|"
                   r"need[^.]{0,30} access)\b", re.I)


def self_request(message: str) -> bool:
    """Heuristic: the caller, in their own current words, asks for access for themselves."""
    return bool(_SELF.search(top_level(message)))


def blocks_filing(constraints) -> bool:
    """A 'do not file until I say so' rule is still in force (the user's own constraints, kept by the planner)."""
    return any(_BLOCK.search(c) for c in constraints or [])


def insert_consent(c, message_id: str, caller_id: str, employee_id: str, system: str, draft: str, as_of: str) -> None:
    c.execute(DDL)
    c.execute("INSERT OR IGNORE INTO consents VALUES (?,?,?,?,?,?,0)",
              (message_id, caller_id, employee_id, system.lower(), hashlib.sha256(draft.encode()).hexdigest(), as_of))


def record_consent(path, message_id: str, caller_id: str, employee_id: str, system: str,
                   draft: str, as_of: str) -> None:
    c = sqlite3.connect(path)
    try:
        insert_consent(c, message_id, caller_id, employee_id, system, draft, as_of)
        c.commit()
    finally:
        c.close()


def check_consent(c, consent_id: str, caller_id: str, employee_id: str, system: str) -> None:
    """Fail closed: a recorded consent bound to this caller, employee and system, or PermissionError."""
    r = c.execute("SELECT caller_id, employee_id, system FROM consents WHERE message_id = ?", (consent_id,)).fetchone()
    if r is None or tuple(r) != (caller_id, employee_id, system.lower()):
        raise PermissionError("no recorded consent for this caller, employee and system")


def approvals_recorded(c, request_id: str) -> tuple[bool, list]:
    """True only if the request has approval rows and EVERY one has status 'approved'. Message text never counts."""
    rows = [tuple(r) for r in c.execute("SELECT approval_id, status FROM approvals WHERE request_id = ? "
                                        "ORDER BY approval_id", (request_id,)).fetchall()]
    return bool(rows) and all(st == "approved" for _, st in rows), rows
