"""Tool logic behind the MCP server. The graph never imports this; it calls MCP tools.
All writes go through release_write(), the only place a write is committed."""
import contextvars
import difflib

from brain import config, db, gate, people, retrieval

# (request_id, trace_id) of the client call being served: set by the MCP server wrapper, stored on every write
# so a database row can be followed back to the trace that caused it. Empty when called directly (tests).
REQUEST: contextvars.ContextVar = contextvars.ContextVar("request", default=("", ""))

# ---------- reads ----------

def _cite(rows: list[dict], table: str, key: str) -> list[dict]:
    """Every database row carries the source an answer can cite, like a document path: db:<table>/<id>."""
    for r in rows:
        r["source"] = f"db:{table}/{r[key]}"
    return rows


CORPORA = ("contracts", "employment", "handbook", "internal_memos", "it_kb", "manager_playbook", "policies")


def search_documents(query: str, caller_id: str, corpus: str | None = None, k: int = 5) -> list[dict]:
    """Search company documents. Optional corpus (leave empty unless sure): contracts, employment, handbook, internal_memos, it_kb, manager_playbook, policies."""
    if corpus not in CORPORA:  # a made-up corpus name must not silently return nothing; the audience filter still applies
        corpus = None
    return retrieval.search(query, caller_id, k=k, corpus=corpus)


def get_employee(query: str) -> list[dict]:
    """Find employees by id, email or name; returns id, role, manager, group. Tolerates a typo in the name."""
    cols = ("employee_id, full_name, email, role, department_id, manager_id, location, "
            "employment_type, status, user_group_id")
    out = db.rows(f"SELECT {cols} FROM employees WHERE employee_id = ? OR lower(email) = lower(?) "
                  "OR lower(full_name) LIKE lower(?) ORDER BY full_name", (query, query, f"%{query}%"))
    if not out:  # 'rrachel stein': no exact hit, so accept ONLY a close full-name match (never a first-name guess)
        allp = db.rows(f"SELECT {cols} FROM employees ORDER BY full_name")
        close = difflib.get_close_matches(query.lower().strip(), [e["full_name"].lower() for e in allp], n=2, cutoff=0.85)
        out = [e for e in allp if e["full_name"].lower() in close]
    return _cite(out, "employees", "employee_id")

def list_direct_reports(manager_id: str) -> list[dict]:
    """List the employees who report to a manager id."""
    return _cite(db.rows("SELECT employee_id, full_name, role, status FROM employees "
                   "WHERE manager_id = ? ORDER BY full_name", (manager_id,)), "employees", "employee_id")

def check_software_subscription(software: str) -> list[dict]:
    """Seat limit, ACTIVE seats, over-limit flag, annual cost and renewal date for a software/vendor name."""
    like = f"%{software}%"
    out = db.rows(
        "SELECT sub.subscription_id, sys.system_name, v.vendor_name, sub.contract_id, sub.seat_limit, "
        "sub.active_seats, sub.status, sub.renewal_date, sub.annual_cost FROM software_subscriptions sub "
        "JOIN systems sys ON sub.system_id = sys.system_id JOIN vendors v ON sub.vendor_id = v.vendor_id "
        "WHERE lower(sys.system_name) LIKE lower(?) OR lower(v.vendor_name) LIKE lower(?) "
        "ORDER BY sys.system_name", (like, like))
    order = ["subscription_id", "system_name", "vendor_name", "contract_id", "seat_limit", "active_seats", "over_limit",
             "annual_cost", "annual_cost_above_threshold", "cost_per_seat", "seats_available", "status", "renewal_date"]
    for s in out:  # comparisons are made here, in code; the model is handed the answer (short rows: facts keep 240 characters)
        s["seats_available"] = s["seat_limit"] - s["active_seats"]
        s["over_limit"] = s["active_seats"] > s["seat_limit"]
        # the WHOLE contract cost against the threshold (a question about what a subscription costs). The memo applies the threshold
        # to an expansion's increase, so a question about adding seats uses cost_per_seat x seats added (KNOWN-TRAPS B2).
        s["annual_cost_above_threshold"] = (s["annual_cost"] or 0) > config.Q3_APPROVAL_THRESHOLD
        s["cost_per_seat"] = round((s["annual_cost"] or 0) / s["seat_limit"], 2) if s["seat_limit"] else None
    out = [{k: s[k] for k in order} for s in out]
    return _cite(out, "software_subscriptions", "subscription_id")


def get_contract(query: str) -> list[dict]:
    """By contract id, vendor name or system name. Includes the contract document path."""
    like = f"%{query}%"
    return _cite(db.rows(
        "SELECT c.contract_id, v.vendor_name, sys.system_name, c.contract_name, c.document_path, "
        "c.start_date, c.end_date, c.renewal_type, c.renewal_notice_days, c.annual_cost, "
        "c.owner_department_id, c.status, c.security_review_required FROM contracts c "
        "JOIN vendors v ON c.vendor_id = v.vendor_id JOIN systems sys ON c.system_id = sys.system_id "
        "WHERE c.contract_id = ? OR lower(v.vendor_name) LIKE lower(?) OR lower(sys.system_name) LIKE lower(?) "
        "ORDER BY c.contract_id", (query, like, like)), "contracts", "contract_id")

def list_onboarding_tasks(employee_id: str) -> list[dict]:
    """Onboarding checklist tasks and their status for an employee id."""
    return _cite(db.rows("SELECT task_id, task_type, description, owner_group, status, due_date, related_system_id "
                   "FROM onboarding_tasks WHERE employee_id = ? ORDER BY due_date, task_id", (employee_id,)), "onboarding_tasks", "task_id")

def check_asset_inventory(asset_type: str = "", location: str = "") -> list[dict]:
    """Hardware assets by type and location with status."""
    return _cite(db.rows("SELECT asset_id, asset_type, model, status, location, employee_id, notes FROM assets "
                   "WHERE lower(asset_type) LIKE lower(?) AND lower(location) LIKE lower(?) "
                   "ORDER BY asset_type, asset_id", (f"%{asset_type}%", f"%{location}%")), "assets", "asset_id")

def list_employee_tickets(employee_id: str, status: str = "") -> list[dict]:
    """Tickets for an employee id, optionally by status."""
    return _cite(db.rows("SELECT ticket_id, category, subcategory, priority, status, subject, created_at, "
                   "assigned_team, related_system_id FROM tickets "
                   "WHERE employee_id = ? AND lower(status) LIKE lower(?) ORDER BY created_at DESC",
                   (employee_id, f"%{status}%")), "tickets", "ticket_id")

def read_approval(request_id: str) -> list[dict]:
    """Approvals attached to an access request (e.g. AR001 -> AP001, AP002)."""
    return _cite(db.rows("SELECT approval_id, request_type, request_id, approver_id, status, decision_reason, "
                   "created_at FROM approvals WHERE request_id = ? ORDER BY approval_id", (request_id,)), "approvals", "approval_id")


def list_access_requests(employee_id: str) -> list[dict]:
    """Access requests already filed for an employee id, with system and status (e.g. AR001 for Webex)."""
    return _cite(db.rows("SELECT r.request_id, r.employee_id, r.system_id, s.system_name, r.status, "
                         "r.approver_id, r.decision_reason, r.created_at FROM access_requests r "
                         "JOIN systems s ON r.system_id = s.system_id WHERE r.employee_id = ? "
                         "ORDER BY r.request_id", (employee_id,)), "access_requests", "request_id")


def seat_assignment_status(system: str) -> list[dict]:
    """Who holds a seat on a system? The database stores seat COUNTS only, never which employee holds one.
    Returns that gap, the counts, and the blocked requests and onboarding tasks that do exist."""
    like = f"%{system}%"
    subs = check_software_subscription(system)
    sid = [s["subscription_id"] for s in subs]
    sysid = db.rows("SELECT system_id FROM systems WHERE lower(system_name) LIKE lower(?)", (like,))
    ids = [r["system_id"] for r in sysid]
    out = [{"gap": "Seat assignments are NOT recorded: the database holds seat counts, not which employee holds a seat. "
                   "Entitlement by role is not a seat assignment.", "subscriptions": sid,
            "source": f"db:software_subscriptions/{sid[0]}" if sid else "db:software_subscriptions"}]
    for i in ids:
        out += _cite(db.rows("SELECT request_id, employee_id, status, decision_reason FROM access_requests "
                             "WHERE system_id = ? AND status IN ('blocked','pending_approval') ORDER BY request_id", (i,)),
                     "access_requests", "request_id")
        out += _cite(db.rows("SELECT task_id, employee_id, status, description FROM onboarding_tasks "
                             "WHERE related_system_id = ? AND status = 'blocked' ORDER BY task_id", (i,)),
                     "onboarding_tasks", "task_id")
    return out

# ---------- writes ----------

def release_write(kind: str, key: str, as_of: str, fn) -> dict:
    """The single write-release function. `fn(conn)` runs inside one transaction and returns
    (entity_id, result). The same key replays the stored result and writes nothing."""
    c = db.conn()
    try:
        done = c.execute("SELECT entity_id FROM write_keys WHERE key = ?", (key,)).fetchone()
        if done:
            return {"replayed": True, "entity_id": done["entity_id"]}
        entity_id, result = fn(c)
        rid, tid = REQUEST.get()
        c.execute("INSERT INTO write_keys (key, kind, entity_id, created_at, request_id, trace_id) VALUES (?,?,?,?,?,?)",
                  (key, kind, entity_id, as_of, rid, tid))
        c.commit()
        return {"replayed": False, "entity_id": entity_id, **result}
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _next_id(c, table: str, col: str, prefix: str) -> str:
    n = c.execute(f"SELECT MAX(CAST(SUBSTR({col}, {len(prefix) + 1}) AS INTEGER)) FROM {table}").fetchone()[0] or 0
    return f"{prefix}{n + 1:03d}"


def _system(name: str) -> dict:
    r = db.rows("SELECT system_id, system_name FROM systems WHERE lower(system_name) LIKE lower(?) "
                "ORDER BY system_name LIMIT 1", (f"%{name}%",))
    if not r:
        raise ValueError(f"no system matching {name!r}")
    return r[0]


def _allowed_actor(caller_id: str, employee_id: str) -> bool:
    if caller_id == employee_id or people.is_manager(caller_id):
        return True
    r = db.rows("SELECT user_group_id FROM employees WHERE employee_id = ?", (caller_id,))
    return bool(r) and r[0]["user_group_id"] in ("UG_HR", "UG_IT")


def record_consent(caller_id: str, employee_id: str, system: str, message_id: str, draft: str, as_of: str,
                   consent_token: str = "") -> dict:
    """CODE-ONLY (never visible to a model): the application records the caller's own current-message consent.
    Needs the HMAC the application mints with CONSENT_SECRET, so a client with raw MCP access cannot create consent."""
    gate.check_consent_token(consent_token, caller_id, employee_id, system, message_id, draft)
    sysrow = _system(system)

    def fn(c):
        gate.insert_consent(c, message_id, caller_id, employee_id, sysrow["system_name"], draft, as_of)
        return message_id, {"consent_id": message_id}
    return release_write("consent", f"consent:{message_id}", as_of, fn)


def create_access_request(caller_id: str, employee_id: str, system: str, justification: str,
                          conversation_id: str, as_of: str, consent_id: str) -> dict:
    if not _allowed_actor(caller_id, employee_id):
        raise PermissionError(f"{caller_id} may not file a request for {employee_id}")
    sysrow = [_system(system)]
    sid = sysrow[0]["system_id"]

    def fn(c):
        gate.check_consent(c, consent_id, caller_id, employee_id, sysrow[0]["system_name"], draft=justification)
        ex = c.execute("SELECT request_id, status FROM access_requests WHERE employee_id = ? AND system_id = ? "
                       "AND status NOT IN ('rejected','denied','cancelled') ORDER BY request_id LIMIT 1",
                       (employee_id, sid)).fetchone()
        if ex:  # reuse AR001 etc.: never a second request
            return ex["request_id"], {"status": ex["status"], "reused": True}
        rid = _next_id(c, "access_requests", "request_id", "AR")
        c.execute("INSERT INTO access_requests (request_id, employee_id, system_id, access_level, "
                  "business_justification, status, approver_id, created_at, decision_reason) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (rid, employee_id, sid, "standard_user", justification, "pending_approval", None, as_of, None))
        return rid, {"status": "pending_approval", "reused": False}
    return release_write("access_request", f"ar:{employee_id}:{sid}:{conversation_id}", as_of, fn)


def create_ticket(caller_id: str, employee_id: str, category: str, subcategory: str, subject: str,
                  description: str, system: str, conversation_id: str, as_of: str, consent_id: str) -> dict:
    """A write like any other: needs the caller's recorded, unused consent for this employee and system. No loadout
    shows it and no workflow calls it today; the gate is here so that wiring it up later cannot skip consent."""
    if not _allowed_actor(caller_id, employee_id):
        raise PermissionError(f"{caller_id} may not open a ticket for {employee_id}")
    sysrow = db.rows("SELECT system_id, system_name FROM systems WHERE lower(system_name) LIKE lower(?) LIMIT 1", (f"%{system}%",))
    sid = sysrow[0]["system_id"] if sysrow else None

    def fn(c):
        if not sysrow:
            raise PermissionError("no consent can exist for an unknown system")
        gate.check_consent(c, consent_id, caller_id, employee_id, sysrow[0]["system_name"])
        ex = c.execute("SELECT ticket_id, status FROM tickets WHERE employee_id = ? AND related_system_id IS ? "
                       "AND category = ? AND status NOT IN ('closed','resolved') ORDER BY ticket_id LIMIT 1",
                       (employee_id, sid, category)).fetchone()
        if ex:  # reuse T001 etc.
            return ex["ticket_id"], {"status": ex["status"], "reused": True}
        tid = _next_id(c, "tickets", "ticket_id", "T")
        c.execute("INSERT INTO tickets (ticket_id, employee_id, category, subcategory, priority, status, subject, "
                  "description, created_at, assigned_team, related_system_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (tid, employee_id, category, subcategory, "medium", "open", subject, description, as_of, "IT", sid))
        return tid, {"status": "open", "reused": False}
    return release_write("ticket", f"t:{employee_id}:{sid}:{category}:{conversation_id}", as_of, fn)


def request_approval(caller_id: str, approval_id: str, consent_id: str, conversation_id: str, as_of: str) -> dict:
    """Dispatch an existing approval: needed -> pending. No new rows; never approves. Needs the caller's recorded
    consent for the request's employee and system; the caller must be that employee, a manager, HR or IT."""
    def fn(c):
        r = c.execute("SELECT a.status, a.request_id, q.employee_id, s.system_name FROM approvals a "
                      "JOIN access_requests q ON a.request_id = q.request_id JOIN systems s ON q.system_id = s.system_id "
                      "WHERE a.approval_id = ?", (approval_id,)).fetchone()
        if r is None:
            raise ValueError(f"no approval {approval_id!r}")
        if not _allowed_actor(caller_id, r["employee_id"]):
            raise PermissionError(f"{caller_id} may not dispatch approvals for {r['employee_id']}")
        gate.check_consent(c, consent_id, caller_id, r["employee_id"], r["system_name"])
        if r["status"] == "needed":
            c.execute("UPDATE approvals SET status = 'pending' WHERE approval_id = ?", (approval_id,))
            ev = _next_id(c, "audit_log", "event_id", "EV")
            c.execute("INSERT INTO audit_log (event_id, entity_type, entity_id, event_type, actor_employee_id, "
                      "created_at, notes) VALUES (?,?,?,?,?,?,?)",
                      (ev, "approval", approval_id, "approval_requested", caller_id, as_of, conversation_id))
            return approval_id, {"status": "pending", "transitioned": True}
        return approval_id, {"status": r["status"], "transitioned": False}
    return release_write("approval_request", f"ap:{approval_id}:{conversation_id}", as_of, fn)


def release_access(caller_id: str, request_id: str, consent_id: str, conversation_id: str, as_of: str) -> dict:
    """Release an access request ONLY against recorded approvals: every approval row for it must already say
    'approved' in the database. Moves the request to 'approved' (it does not assign a seat) and consumes the consent."""
    def fn(c):
        r = c.execute("SELECT q.employee_id, q.status, s.system_name FROM access_requests q "
                      "JOIN systems s ON q.system_id = s.system_id WHERE q.request_id = ?", (request_id,)).fetchone()
        if r is None:
            raise ValueError(f"no access request {request_id!r}")
        if not _allowed_actor(caller_id, r["employee_id"]):
            raise PermissionError(f"{caller_id} may not release a request for {r['employee_id']}")
        gate.check_consent(c, consent_id, caller_id, r["employee_id"], r["system_name"])
        ok, rows = gate.approvals_recorded(c, request_id)
        if not ok:
            raise PermissionError(f"approvals not recorded as approved: {rows}")
        if r["status"] != "approved":
            c.execute("UPDATE access_requests SET status = 'approved', decision_reason = ? WHERE request_id = ?",
                      ("Approvals recorded; seat not assigned.", request_id))
            ev = _next_id(c, "audit_log", "event_id", "EV")
            c.execute("INSERT INTO audit_log (event_id, entity_type, entity_id, event_type, actor_employee_id, "
                      "created_at, notes) VALUES (?,?,?,?,?,?,?)",
                      (ev, "access_request", request_id, "access_request_released", caller_id, as_of, conversation_id))
        c.execute("UPDATE consents SET consumed = 1 WHERE message_id = ?", (consent_id,))
        return request_id, {"status": "approved", "approvals": dict(rows)}
    return release_write("access_release", f"rel:{request_id}:{conversation_id}", as_of, fn)
