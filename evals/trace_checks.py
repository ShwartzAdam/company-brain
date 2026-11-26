"""Is this trace followable without reading the code? Pure functions over one trace's observations (Langfuse v2
rows or plain dicts), so the same checks run offline on a fixture and live on a fetched trace.

verdict(obs) -> ("PASS" | "FAIL" | "INVALID", [reasons])
  INVALID = the evidence is not all readable yet (no root, or the root has no status): re-fetch, never score it low.

What a reader must find (spec 3.3 / M5): classify -> scope -> tools -> answer in order; caller, group, scope, tool
sequence, terminal status, request id on the root; the same request id on every tool span and on the server's span
for it; a server span for every write; model + usage on every generation; a silent fallback flagged; Maya's scope
never holding a write; a resume trace that names the request it resumes."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.scope import WRITE_TOOLS  # noqa: E402
from brain.status import STATUSES, answer_agrees  # noqa: E402

ROOT_KEYS = ("caller_id", "caller_group", "request_id", "as_of", "case_id", "turn", "scope", "intent",
             "visible_tools", "tool_sequence", "status")
RESUME_KEYS = ("caller_id", "request_id", "resume_of", "status")
# a write may only follow the step that justifies it
WRITE_ORDER = ["record_consent", "create_access_request", "request_approval", "release_access"]


def g(o, k, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


def meta(o) -> dict:
    m = g(o, "metadata") or {}
    return dict(m) if isinstance(m, dict) else {}


def as_list(v):
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v]
    return list(v) if isinstance(v, (list, tuple)) else ([] if v in (None, "") else [v])


def _t(o):
    return str(g(o, "start_time") or "")


def write_order(names: list[str]):
    """None when no write ran. Otherwise every write has its predecessors before it, and a release has a read_approval
    before it. Judged on the spans in start order, so workflow calls (not just gather's) are covered."""
    ran = [n for n in names if n in WRITE_ORDER]
    if not ran:
        return None
    bad = []
    for i, n in enumerate(names):
        if n == "create_access_request" and "record_consent" not in names[:i]:
            bad.append("create_access_request before record_consent")
        if n == "request_approval" and "create_access_request" not in names[:i] and "list_access_requests" not in names[:i]:
            bad.append("request_approval before any access request was read or created")
        if n == "release_access" and "read_approval" not in names[:i]:
            bad.append("release_access before read_approval")
    return (0.0, "; ".join(bad)) if bad else (1.0, f"writes in order: {' > '.join(ran)}")


def verdict(obs) -> tuple[str, list[str]]:
    obs = sorted(obs, key=_t)
    roots = [o for o in obs if not g(o, "parent_observation_id")]
    if len(roots) != 1:
        return "INVALID", [f"{len(roots)} root observations (expected 1): trace not fully readable yet"]
    root, rm = roots[0], meta(roots[0])
    resume = g(root, "name") == "agent.resume"
    if g(root, "name") not in ("agent.turn", "agent.resume"):
        return "FAIL", [f"root is {g(root, 'name')!r}, expected agent.turn or agent.resume"]
    if not rm.get("status"):
        return "INVALID", ["root has no status yet (still being written, or the turn crashed before finishing)"]
    f = [f"root metadata missing {k}" for k in (RESUME_KEYS if resume else ROOT_KEYS) if rm.get(k) in (None, "")]
    if rm["status"] not in STATUSES:
        f.append(f"status {rm['status']!r} is not one of the five")
    out = g(root, "output")
    if not out:
        f.append("root has no output (the answer)")
    elif not resume and not answer_agrees(rm["status"], str(out)):
        f.append(f"status {rm['status']!r} and the answer disagree")
    rid = rm.get("request_id")
    names = [g(o, "name") for o in obs]

    if not resume:  # classify -> scope -> (gather|handoff) -> answer, in start order
        order = [n for n in names if n in ("classify", "scope", "gather", "handoff", "answer")]
        want = ["classify", "answer"] if rm.get("intent") == "ambiguous" else ["classify", "scope", "answer"]
        pos = [order.index(n) if n in order else -1 for n in want]
        if -1 in pos or pos != sorted(pos):
            f.append(f"node spans out of order or missing: saw {order}, need {want} in that order")
        if as_list(rm.get("tool_sequence")) and "gather" not in order:
            f.append("tools were used but there is no gather span")

    tools = [o for o in obs if str(g(o, "name")).startswith("tool.")]
    servers = [o for o in obs if str(g(o, "name")).startswith("server.")]
    for t in as_list(rm.get("tool_sequence")):
        if not any(g(o, "name") == f"tool.{t}" for o in tools):
            f.append(f"tool_sequence names {t} but there is no tool.{t} span")
    for o in tools + servers:
        got = meta(o).get("request_id")
        if got != rid:
            f.append(f"{g(o, 'name')} carries request_id {got!r}, root has {rid!r}")
    for o in tools:
        name = g(o, "name")[5:]
        if name in WRITE_TOOLS or name in ("create_access_request", "create_ticket"):
            if not any(g(s, "parent_observation_id") == g(o, "id") and g(s, "name") == f"server.{name}" for s in servers):
                f.append(f"write {name} has no server.{name} span under it (the server side of the effect is not linked)")
    seq = write_order([g(o, "name")[5:] for o in tools])
    if seq and seq[0] == 0.0:
        f.append(f"write order: {seq[1]}")
    if rm.get("scope") == "maya" and any(g(o, "name")[5:] in WRITE_TOOLS for o in tools):
        f.append("Maya's scope has a write tool span")

    for o in obs:
        if g(o, "type") == "GENERATION":
            u = g(o, "usage_details") or {}
            if not g(o, "model"):
                f.append(f"generation {g(o, 'name')} has no model")
            if not (u.get("total") or u.get("input")):
                f.append(f"generation {g(o, 'name')} has no usage")
    cls = next((o for o in obs if g(o, "name") == "classify"), None)
    if cls is not None and meta(cls).get("classify_fallback") in (True, "true", "True"):
        if "classify_default_plan" not in as_list(rm.get("fallbacks")):
            f.append("classify fell back to the default plan but the root does not say so")
    if resume and rm.get("resume_of") and rm.get("resume_of") != rid:
        f.append(f"resume trace has request_id {rid!r} but resumes {rm.get('resume_of')!r}")
    return ("FAIL" if f else "PASS"), f


def summary(obs) -> str:
    """One line a human can read first: who, what, which tools, where it ended."""
    r = next((o for o in obs if not g(o, "parent_observation_id")), None)
    m = meta(r) if r is not None else {}
    return (f"{m.get('caller_id')}/{m.get('caller_group')} {m.get('scope')}:{m.get('intent')} "
            f"tools={as_list(m.get('tool_sequence'))} status={m.get('status')} request={m.get('request_id')}")
