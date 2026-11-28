"""The answer as a typed object: each sentence with the sources that back it (spec 6.5, 'every claim needs a source').
Written by code from the model's text and the allowed sources (this turn's evidence plus facts from earlier turns): a sentence that names an id the evidence holds (E010,
AR001, T001) gets that row's source attached by code, never by the model. A sentence with a number or id and no source
is counted as uncited, not invented around."""
import re

from pydantic import BaseModel, Field

_ID = re.compile(r"\b[A-Z]{1,3}\d{3}\b")
_CITE = re.compile(r"\[([^\[\]]+)\]")
_CLAIM = re.compile(r"\d|\b[A-Z]{1,3}\d{3}\b")
_LEAD_CITES = re.compile(r"(?:\s*\[[^\[\]]+\])+")


class Claim(BaseModel):
    text: str
    sources: list[str] = Field(default_factory=list)
    added_by_code: list[str] = Field(default_factory=list)


class AnswerClaims(BaseModel):
    claims: list[Claim]

    @property
    def uncited(self) -> list[str]:
        return [c.text for c in self.claims if not c.sources and _CLAIM.search(c.text)]


def ground(text: str, allowed: set) -> tuple[str, AnswerClaims]:
    """-> (text with code-attached citations, the typed claims). `allowed` = the same source set clean_citations used.
    Original whitespace (paragraphs, bullet lines) is kept; a segment made only of citations belongs to the sentence before."""
    by_id = {}
    for s in sorted(allowed):
        if s.startswith("db:") and "/" in s:
            by_id.setdefault(s.rsplit("/", 1)[1], []).append(s)
    parts = re.split(r"((?<=[.!?])[ \t]+|\s*\n\s*)", text.strip()) + [""]
    segs = []  # [sentence, separator after it]
    for sent, sep in zip(parts[::2], parts[1::2]):
        m = _LEAD_CITES.match(sent) if segs else None
        if m:  # "E010. [src] She starts" -> the citation closes the previous sentence
            rest = sent[m.end():]
            if rest.strip() in ("", ".", "!", "?"):
                segs[-1] = [segs[-1][0] + segs[-1][1] + sent, sep]
                continue
            segs[-1] = [segs[-1][0] + segs[-1][1] + m.group(0), rest[:len(rest) - len(rest.lstrip())] or " "]
            sent = rest.lstrip()
        segs.append([sent, sep])
    out, claims = [], []
    for sent, sep in segs:
        if not sent:
            out.append(sep)
            continue
        have = [c for c in _CITE.findall(sent) if c in allowed or c.split("#")[0].strip() in allowed]
        extra = [] if have else [s for i in dict.fromkeys(_ID.findall(sent)) for s in by_id.get(i, [])][:3]
        if extra:  # the sentence names an id the evidence holds: attach that row, before the closing punctuation
            tail = re.search(r"[.!?]+$", sent)
            sent = (sent[:tail.start()] if tail else sent) + "".join(f" [{s}]" for s in extra) + (tail.group(0) if tail else "")
        out.append(sent + sep)
        claims.append(Claim(text=sent, sources=[*have, *extra], added_by_code=extra))
    return "".join(out), AnswerClaims(claims=claims)


def clean_citations(text: str, allowed: set, numbered: dict | None = None) -> tuple[str, list]:
    """[n] is the model pointing at the n-th evidence row printed in its prompt: it becomes that row's real sources.
    Any other bracket that is not a source in this turn's evidence or facts ([constraints], [user instruction]) is removed."""
    for n, srcs in (numbered or {}).items():
        text = text.replace(f"[{n}]", "".join(f"[{x}]" for x in srcs))
    dropped = [b for b in re.findall(r"\[([^\[\]]+)\]", text) if b not in allowed and b.split("#")[0].strip() not in allowed]  # innermost brackets only
    for b in dropped:
        text = text.replace(f"[{b}]", "")
    return re.sub(r"\s+([.,;])", r"\1", re.sub(r" {2,}", " ", text)).strip(), dropped
