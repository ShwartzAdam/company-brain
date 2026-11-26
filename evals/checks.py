"""Deterministic scores, ported from ai-course lesson 11 `03-score-the-run/checks.py` (itself lesson 10's).
Plain Python, no model. Every check returns (score, reason) or None; None means "not applicable" and publishes
NO score, never a zero. `turn_pass` is the all-or-nothing gate, `turn_score` the trend."""


def matches(text: str, spec: str) -> bool:
    return any(a.strip().lower() in text.lower() for a in spec.split("|"))


def intent_accuracy(actual, expected):
    """Diagnostic, not an outcome: did classify pick the expected intent ('|' = alternatives)?"""
    if not expected or not actual:
        return None
    return (1.0, actual) if matches(actual, expected) else (0.0, f"{actual} (expected {expected})")


def tool_necessity(called, expected, optional=None):
    """PRECISION: the fraction of calls made that the turn warranted. No calls -> not scored."""
    if not called:
        return None
    allowed = {a.strip() for e in expected or [] for a in e.split("|")} | set(optional or []) | {"get_employee"}
    ok = [c for c in called if c in allowed]
    bad = sorted({c for c in called if c not in allowed})
    return len(ok) / len(called), f"{len(ok)}/{len(called)} calls warranted" + (f" (unwarranted: {', '.join(bad)})" if bad else "")


def tool_selection(called, expected):
    """RECALL: of the tools the turn required, how many were called?"""
    if not expected:
        return None
    met = [e for e in expected if any(a.strip() in called for a in e.split("|"))]
    return len(met) / len(expected), f"{len(met)}/{len(expected)} required tools called"


def forbidden_avoided(called, forbidden):
    """Binary: a write at the wrong moment is not partially wrong."""
    if not forbidden:
        return None
    hit = sorted({f for f in forbidden if f in called})
    return (0.0, f"called forbidden: {', '.join(hit)}") if hit else (1.0, "none called")


def call_efficiency(calls):
    if not calls:
        return None
    sig = [f"{c['tool']}{sorted((c.get('args') or {}).items())}" for c in calls]
    return len(set(sig)) / len(sig), f"{len(sig) - len(set(sig))} repeated call(s)"


def fact_stage(spec: str, evidence_text: str, answer: str, intent_ok: bool = True):
    """Where did a missed required fact get lost? Reads the stage, so a fix goes where the failure is.
    None = the fact is in the answer (nothing to attribute)."""
    if matches(answer, spec):
        return None
    if not intent_ok:
        return "classify", f"intent was wrong, {spec!r} never looked up"
    if matches(evidence_text, spec):
        return "answer", f"{spec!r} was in the evidence but the answer left it out"
    return "gather", f"{spec!r} was never gathered (no tool or search returned it)"


def score_turn(t: dict, calls: list, intent=None) -> dict:
    called = [c["tool"] for c in calls]
    r = {"intent_accuracy": intent_accuracy(intent, t.get("expected_intent")),
         "tool_necessity": tool_necessity(called, t.get("expected_tools"), t.get("optional_tools")),
         "tool_selection": tool_selection(called, t.get("expected_tools")),
         "forbidden_avoided": forbidden_avoided(called, t.get("forbidden_tools")),
         "call_efficiency": call_efficiency(calls)}
    return {k: v for k, v in r.items() if v is not None}


def roll_up(scores: dict) -> dict:
    if not scores:
        return {}
    failed = sorted(k for k, (v, _) in scores.items() if v < 1.0)
    return {"turn_pass": float(not failed), "turn_score": sum(v for v, _ in scores.values()) / len(scores), "failed": failed}


def _selftest():
    assert intent_accuracy("access_request", "policy_question|access_request")[0] == 1.0
    assert intent_accuracy("other", "access_request")[0] == 0.0 and intent_accuracy("x", None) is None
    assert tool_necessity([], ["a"]) is None
    assert tool_necessity(["a", "zzz"], ["a"])[0] == 0.5 and tool_necessity(["get_employee"], [])[0] == 1.0
    assert forbidden_avoided(["create_access_request"], ["create_access_request"])[0] == 0.0 and forbidden_avoided(["a"], None) is None
    assert call_efficiency([{"tool": "a", "args": {"x": 1}}, {"tool": "a", "args": {"x": 1}}])[0] == 0.5
    s = score_turn({"expected_tools": ["a"], "forbidden_tools": ["w"]}, [{"tool": "a", "args": {}}], "x")
    assert roll_up(s)["turn_pass"] == 1.0 and "intent_accuracy" not in s
    print("checks selftest ok")


if __name__ == "__main__":
    _selftest()
