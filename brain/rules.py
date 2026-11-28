"""Typed rules, written by code from documents the agent actually read. A user's word ('the Q3 freeze applies') is only a
label; the rule it names is read from the memo, kept as typed fields, and shown to the model as one exact line."""
import re

from pydantic import BaseModel

_THRESHOLD = re.compile(r"(?P<what>SaaS\s+(?:expansions?|renewals?|purchases?|spend))\s+(?:above|over|exceeding)\s+(?:USD\s*|\$\s*)(?P<usd>[\d,]+)\s+"
                        r"(?P<basis>annuali[sz]ed|annual|per\s+year|a\s+year)\s+(?:requires?|needs?)\s+"
                        r"(?P<who>[A-Za-z ]+?)\s+(?:approval|sign-?\s?off)", re.I)
_OLDER = re.compile(r"older\s+(?:USD\s*|\$\s*)(?P<usd>[\d,]+)\s+threshold", re.I)


class Rule(BaseModel):
    name: str
    threshold_usd: int
    applies_to: str
    needs: str
    supersedes_usd: int | None = None
    source: str

    def render(self) -> str:
        sup = f" It supersedes the older USD {self.supersedes_usd:,} threshold." if self.supersedes_usd else ""
        return (f"{self.name} [{self.source}]: {self.applies_to} above USD {self.threshold_usd:,} need {self.needs} approval."
                f"{sup} The memo sets an approval threshold, not a ban: it does not cover amounts below it.")


def extract_rules(evidence: list) -> list[dict]:
    out = []
    for e in evidence:
        for part in e["result"] if isinstance(e.get("result"), list) else []:
            if not isinstance(part, dict) or "saas_renewal_freeze" not in str(part.get("source", "")):
                continue
            text = " ".join(str(part.get("text", "")).split())
            m = _THRESHOLD.search(text)
            if m:
                old = _OLDER.search(text)
                out.append(Rule(name="Q3 SaaS freeze", threshold_usd=int(m["usd"].replace(",", "")),
                                applies_to=f"{m['what']} ({m['basis']})", needs=m["who"].strip(),
                                supersedes_usd=int(old["usd"].replace(",", "")) if old else None,
                                source=str(part["source"])).model_dump())
    return out
