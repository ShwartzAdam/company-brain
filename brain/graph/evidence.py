"""Evidence: tool results as the model sees them (shrunk to one budget), and what code reads back out of them
(sources, row facts, an ambiguous first name). Pure functions, no model and no I/O."""
import json
import re

EVIDENCE_CHARS = 12000  # the whole evidence block. 8 whole-document results are about 1400 characters each


MIN_ITEM_CHARS = 1500   # no single result is squeezed below this, however many there are


def shrink(result, limit: int = EVIDENCE_CHARS):
    """-> (result, chars_cut). Cap a tool result near `limit` characters by shortening each row's `text`, never by
    cutting the JSON string (a cut string once ended mid-`source` and the model cited '[documents/handbook/getting-.]').
    A shortened row ends with a marker, so the model knows the text is partial."""
    if isinstance(result, str):
        return (result, 0) if len(result) <= limit else (result[:limit] + " ...[truncated]", len(result) - limit)
    if not isinstance(result, list) or len(json.dumps(result)) <= limit:
        return result, 0
    rows = [r for r in result if isinstance(r, dict) and isinstance(r.get("text"), str)]
    if not rows:
        return result, 0
    room = limit - (len(json.dumps(result)) - sum(len(r["text"]) for r in rows))
    cap = max(120, room // len(rows))
    cut, out = 0, []
    for r in result:
        if isinstance(r, dict) and isinstance(r.get("text"), str) and len(r["text"]) > cap:
            cut += len(r["text"]) - cap
            r = {**r, "text": r["text"][:cap] + f" ...[truncated, {len(r['text']) - cap} more characters]"}
        out.append(r)
    return out, cut


def render_evidence(ev: list) -> tuple[str, int]:
    """-> (prompt text, characters cut). One budget for the whole block, shared between the results."""
    each = max(MIN_ITEM_CHARS, EVIDENCE_CHARS // max(1, len(ev)))
    lines, cut = [], 0
    for i, e in enumerate(ev, 1):
        res, c = shrink(e["result"], each)
        cut += c
        lines.append(f"[{i}] {e['tool']}({json.dumps(e['args'])}) -> {json.dumps(res)}")
    return "\n".join(lines) or "(none yet)", cut


def ambiguous_first_name(ev: list):
    """(name, [(id, full name)...]) when a one-word get_employee query matched more than one employee, else None."""
    for i, e in enumerate(ev):
        q = str((e.get("args") or {}).get("query", "")).strip()
        rows = [r for r in (e["result"] if isinstance(e["result"], list) else []) if isinstance(r, dict) and r.get("employee_id")]
        if e["tool"] == "get_employee" and q and " " not in q and not re.fullmatch(r"E\d+|\S+@\S+", q) and len(rows) > 1:
            ids = {r["employee_id"] for r in rows}
            if any(x["tool"] == "get_employee" and len(found := [r for r in (x["result"] if isinstance(x["result"], list) else [])
                                                                  if isinstance(r, dict) and r.get("employee_id")]) == 1
                   and found[0]["employee_id"] in ids for x in ev[i + 1:]):
                continue  # a later lookup returned exactly one of the ambiguous people: the model resolved it from the conversation
            return q, [(r["employee_id"], r["full_name"]) for r in rows]
    return None


def live_evidence(ev: list) -> list:
    """A failed or rejected call is dropped once a later call of the same tool returned a result: the answer must not
    say 'could not look it up' about something the retry found."""
    bad = lambda e: isinstance(e["result"], str) and e["result"].startswith(("ERROR:", "REJECTED:"))  # noqa: E731
    return [e for i, e in enumerate(ev) if not (bad(e) and any(x["tool"] == e["tool"] and not bad(x) for x in ev[i + 1:]))]


def evidence_text(ev: list) -> str:
    return render_evidence(ev)[0]


def sources_of(result) -> list[str]:
    out = []
    for part in result if isinstance(result, list) else [result]:
        if isinstance(part, dict) and part.get("source"):
            out.append(part["source"])
    return out


def row_facts(evidence: list) -> list[str]:
    """Memory of what the tools returned, written by code: one compact line per database row. Never the
    model's own prose, so a wrong sentence cannot become a fact. (Top document passages are kept too, so a follow-up needs no new search.)"""
    out = []
    for e in evidence:
        docs = 0
        for part in e["result"] if isinstance(e["result"], list) else []:
            if not isinstance(part, dict):
                continue
            src = str(part.get("source", ""))
            if src.startswith("db:"):
                body = ", ".join(f"{k}={v}" for k, v in part.items() if k != "source" and v not in (None, ""))
                out.append(f"[{src}] {body}"[:240])
            elif src.startswith("documents/") and part.get("text") and docs < 3:  # top passages, so follow-ups need no new search
                docs += 1
                out.append(f"[{src}] {part.get('section', '')}: {' '.join(part['text'].split())[:300]}")
    return out
