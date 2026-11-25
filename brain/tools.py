"""Tool logic behind the MCP server. The graph never imports this; it calls MCP tools.
All writes go through release_write(), the only place a write is committed."""
from brain import db, people, retrieval

# ---------- reads ----------

def search_documents(query: str, caller_id: str, corpus: str | None = None, k: int = 5) -> list[dict]:
    return retrieval.search(query, caller_id, k=k, corpus=corpus)


def get_employee(query: str) -> list[dict]:
    """Find employees by id, email or name; returns id, role, manager, group."""
    like = f"%{query}%"
    return db.rows(
        "SELECT employee_id, full_name, email, role, department_id, manager_id, location, "
        "employment_type, status, user_group_id FROM employees "
        "WHERE employee_id = ? OR lower(email) = lower(?) OR lower(full_name) LIKE lower(?) "
        "ORDER BY full_name", (query, query, like))


def list_direct_reports(manager_id: str) -> list[dict]:
    """List the employees who report to a manager id."""
    return db.rows("SELECT employee_id, full_name, role, status FROM employees "
                   "WHERE manager_id = ? ORDER BY full_name", (manager_id,))


def check_software_subscription(software: str) -> list[dict]:
    """Seat limit, ACTIVE seats, over-limit flag, annual cost and renewal date for a software/vendor name."""
    like = f"%{software}%"
    out = db.rows(
        "SELECT sub.subscription_id, sys.system_name, v.vendor_name, sub.contract_id, sub.seat_limit, "
        "sub.active_seats, sub.status, sub.renewal_date, sub.annual_cost FROM software_subscriptions sub "
        "JOIN systems sys ON sub.system_id = sys.system_id JOIN vendors v ON sub.vendor_id = v.vendor_id "
        "WHERE lower(sys.system_name) LIKE lower(?) OR lower(v.vendor_name) LIKE lower(?) "
        "ORDER BY sys.system_name", (like, like))
    for s in out:
        s["seats_available"] = s["seat_limit"] - s["active_seats"]
        s["over_limit"] = s["active_seats"] > s["seat_limit"]
    return out


def get_contract(query: str) -> list[dict]:
    """By contract id, vendor name or system name. Includes the contract document path."""
    like = f"%{query}%"
    return db.rows(
        "SELECT c.contract_id, v.vendor_name, sys.system_name, c.contract_name, c.document_path, "
        "c.start_date, c.end_date, c.renewal_type, c.renewal_notice_days, c.annual_cost, "
        "c.owner_department_id, c.status, c.security_review_required FROM contracts c "
        "JOIN vendors v ON c.vendor_id = v.vendor_id JOIN systems sys ON c.system_id = sys.system_id "
        "WHERE c.contract_id = ? OR lower(v.vendor_name) LIKE lower(?) OR lower(sys.system_name) LIKE lower(?) "
        "ORDER BY c.contract_id", (query, like, like))


def list_onboarding_tasks(employee_id: str) -> list[dict]:
    """Onboarding checklist tasks and their status for an employee id."""
    return db.rows("SELECT task_id, task_type, description, owner_group, status, due_date, related_system_id "
                   "FROM onboarding_tasks WHERE employee_id = ? ORDER BY due_date, task_id", (employee_id,))


def check_asset_inventory(asset_type: str = "", location: str = "") -> list[dict]:
    """Hardware assets by type and location with status."""
    return db.rows("SELECT asset_id, asset_type, model, status, location, employee_id, notes FROM assets "
                   "WHERE lower(asset_type) LIKE lower(?) AND lower(location) LIKE lower(?) "
                   "ORDER BY asset_type, asset_id", (f"%{asset_type}%", f"%{location}%"))


def list_employee_tickets(employee_id: str, status: str = "") -> list[dict]:
    """Tickets for an employee id, optionally by status."""
    return db.rows("SELECT ticket_id, category, subcategory, priority, status, subject, created_at, "
                   "assigned_team, related_system_id FROM tickets "
                   "WHERE employee_id = ? AND lower(status) LIKE lower(?) ORDER BY created_at DESC",
                   (employee_id, f"%{status}%"))


def read_approval(request_id: str) -> list[dict]:
    """Approvals attached to an access request (e.g. AR001 -> AP001, AP002)."""
    return db.rows("SELECT approval_id, request_type, request_id, approver_id, status, decision_reason, "
                   "created_at FROM approvals WHERE request_id = ? ORDER BY approval_id", (request_id,))

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
        c.execute("INSERT INTO write_keys (key, kind, entity_id, created_at) VALUES (?,?,?,?)",
                  (key, kind, entity_id, as_of))
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


def _allowed_actor(caller_id: str, employee_id: str) -> bool:
    if caller_id == employee_id or people.is_manager(caller_id):
        return True
    r = db.rows("SELECT user_group_id FROM employees WHERE employee_id = ?", (caller_id,))
    return bool(r) and r[0]["user_group_id"] in ("UG_HR", "UG_IT")


def create_access_request(caller_id: str, employee_id: str, system: str, justification: str,
                          conversation_id: str, as_of: str) -> dict:
    if not _allowed_actor(caller_id, employee_id):
        raise PermissionError(f"{caller_id} may not file a request for {employee_id}")
    sysrow = db.rows("SELECT system_id, system_name FROM systems WHERE lower(system_name) LIKE lower(?) "
                     "ORDER BY system_name LIMIT 1", (f"%{system}%",))
    if not sysrow:
        raise ValueError(f"no system matching {system!r}")
    sid = sysrow[0]["system_id"]

    def fn(c):
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
                  description: str, system: str, conversation_id: str, as_of: str) -> dict:
    if not _allowed_actor(caller_id, employee_id):
        raise PermissionError(f"{caller_id} may not open a ticket for {employee_id}")
    sysrow = db.rows("SELECT system_id FROM systems WHERE lower(system_name) LIKE lower(?) LIMIT 1", (f"%{system}%",))
    sid = sysrow[0]["system_id"] if sysrow else None

    def fn(c):
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


def request_approval(caller_id: str, approval_id: str, conversation_id: str, as_of: str) -> dict:
    """Dispatch an existing approval: needed -> pending. No new rows; never approves."""
    if not (people.is_manager(caller_id) or _allowed_actor(caller_id, "")):
        raise PermissionError(f"{caller_id} may not dispatch approvals")

    def fn(c):
        r = c.execute("SELECT status FROM approvals WHERE approval_id = ?", (approval_id,)).fetchone()
        if r is None:
            raise ValueError(f"no approval {approval_id!r}")
        if r["status"] == "needed":
            c.execute("UPDATE approvals SET status = 'pending' WHERE approval_id = ?", (approval_id,))
            ev = _next_id(c, "audit_log", "event_id", "EV")
            c.execute("INSERT INTO audit_log (event_id, entity_type, entity_id, event_type, actor_employee_id, "
                      "created_at, notes) VALUES (?,?,?,?,?,?,?)",
                      (ev, "approval", approval_id, "approval_requested", caller_id, as_of, conversation_id))
            return approval_id, {"status": "pending", "transitioned": True}
        return approval_id, {"status": r["status"], "transitioned": False}
    return release_write("approval_request", f"ap:{approval_id}:{conversation_id}", as_of, fn)
