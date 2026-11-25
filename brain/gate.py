"""The write gate (issue #10, R1). Two separate gates:
  1. Caller consent: the caller's OWN current top-level message lifts their own 'do not file' rule.
     Decided here by code, never by the model. Recorded as a one-use row bound to caller, employee,
     system, draft hash and message id.
  2. Approver approval: only approvals rows (AP001/AP002) with status 'approved' release anything that
     grants or expands. Read-only from the graph; message text can never create them.
In Step 2 release_allowed() is always False: nothing is released yet."""
import hashlib
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


def record_consent(path, message_id: str, caller_id: str, employee_id: str, system: str,
                   draft: str, as_of: str) -> None:
    c = sqlite3.connect(path)
    try:
        c.execute(DDL)
        c.execute("INSERT OR IGNORE INTO consents VALUES (?,?,?,?,?,?,0)",
                  (message_id, caller_id, employee_id, system,
                   hashlib.sha256(draft.encode()).hexdigest(), as_of))
        c.commit()
    finally:
        c.close()


def release_allowed(*_args, **_kw) -> bool:
    # Step 2: releases nothing. Step 3/4 will require a consent row AND, for grants, approved AP rows.
    return False
