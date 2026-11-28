"""Read the 7 document folders, keep `audience`, chunk, embed, index.
INGEST_VARIANT (env, default base = chunk every heading, index company-brain-docs): a = whole-document entries for
small docs + context prefix + doc_type/system fields; b = a + one summary line per document
(state/doc_summaries.json, from scripts/gen_summaries.py). RETRIEVAL_VARIANT=c is the matching document-level search
(opt-in: it needs a larger evidence budget in the agent that currently breaks M-S-02 t1, see PR #17).
Opt-in run: OPENSEARCH_INDEX=company-brain-docs-v2b INGEST_VARIANT=b RETRIEVAL_VARIANT=c."""
import json
import os
import re
import time

from brain import config, llm

ALLOWED_AUDIENCE = {"all", "manager"}
WINDOW, OVERLAP = 250, 50
WHOLE_DOC_WORDS = 300   # variant a+: a doc this short is one entry, not one chunk per heading
MIN_CHUNK_WORDS = 40    # variant a+: a smaller piece of a long doc is merged into its neighbour
LONG_CORPORA = {"handbook", "manager_playbook"}
DOC_TYPE = {"contracts": "contract", "employment": "employment", "handbook": "handbook",
            "internal_memos": "memo", "it_kb": "kb_article", "manager_playbook": "manager_playbook",
            "policies": "policy"}
NOISE = re.compile(r"^(Raw inspiration:.*|The user reports an issue related to .*)$")
SUMMARIES_PATH = config.ROOT / "state" / "doc_summaries.json"


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta = {}
    for line in parts[1].strip().splitlines():
        k, sep, v = line.partition(":")
        if sep:
            meta[k.strip()] = v.strip()
    return meta, parts[2].lstrip("\n")


def _windows(text: str):
    words = text.split()
    start = 0
    while start < len(words):
        yield " ".join(words[start:start + WINDOW])
        if start + WINDOW >= len(words):
            break
        start += WINDOW - OVERLAP


def chunk(title: str, body: str) -> list[tuple[str, str]]:
    """(section, text) pairs: split on markdown headings, title prepended to every chunk
    (KB articles share boilerplate); long sections fall back to a word window."""
    sections, cur, name = [], [], ""
    for line in body.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            if "".join(cur).strip():
                sections.append((name, "\n".join(cur)))
            name, cur = m.group(1).strip(), []
        else:
            cur.append(line)
    if "".join(cur).strip():
        sections.append((name, "\n".join(cur)))
    out = []
    for name, text in sections:
        for w in _windows(text):
            out.append((name, f"{title}\n{name}\n{w}" if name and name != title else f"{title}\n{w}"))
    return out


def variant() -> str:
    return os.getenv("INGEST_VARIANT", "base")


def _inline_fields(body: str) -> dict:
    """`key: value` lines near the top of a doc without front matter (contracts, KB articles, ...)."""
    out = {}
    for line in body.splitlines()[:30]:
        if line.startswith("##"):
            break
        m = re.match(r"^([A-Za-z_ ]{2,30}):\s*(.+)$", line)
        if m:
            out.setdefault(m.group(1).strip().lower().replace(" ", "_"), m.group(2).strip())
    return out


def known_systems() -> set[str]:
    """System names the contracts declare (system_name); used to tag memos and policies by title."""
    out = set()
    d = config.DOCS_DIR / "contracts"
    for path in d.glob("*.md") if d.is_dir() else []:
        v = _inline_fields(parse_frontmatter(path.read_text(encoding="utf-8"))[1]).get("system_name")
        if v:
            out.add(v)
    return out


def _systems(fields: dict, title: str, known: set[str]) -> list[str]:
    explicit = fields.get("system_name") or fields.get("related_systems") or ""
    found = [x.strip() for x in explicit.split(",") if x.strip()]
    found += [k for k in known if k.lower() in title.lower() and k not in found]
    return found


def _clean(body: str) -> str:
    text = "\n".join(l for l in body.splitlines() if not NOISE.match(l.strip()))
    return re.sub(r"^#{2,6} .*\n\s*(?=#{2,6} |\Z)", "", text, flags=re.M)  # a heading left with no body


def _prefix(doc_type: str, systems: list[str], title: str) -> str:
    return f"[{doc_type}{' | ' + ', '.join(systems) if systems else ''}] {title}"


def _merge_small(pieces: list[tuple[str, str]]) -> list[tuple[str, str]]:
    out = []
    for name, text in pieces:
        if out and len(text.split()) < MIN_CHUNK_WORDS:
            out[-1] = (out[-1][0], out[-1][1] + "\n" + text)
        else:
            out.append((name, text))
    if len(out) > 1 and len(out[0][1].split()) < MIN_CHUNK_WORDS:  # a small first piece joins the second
        out[1] = (out[1][0], out[0][1] + "\n" + out[1][1])
        out.pop(0)
    return out


def _variant_pieces(corpus, title, body, doc_type, systems, summary):
    """(section, text) pairs for variants a/b/c."""
    pre = _prefix(doc_type, systems, title)
    body = _clean(re.sub(r"^#\s+.*\n?", "", body, count=1, flags=re.M)).strip()
    extra = f"\nSummary: {summary}" if summary else ""
    if corpus not in LONG_CORPORA and len(body.split()) <= WHOLE_DOC_WORDS:
        return [(title, f"{pre}{extra}\n{body}")]
    pieces = _merge_small([(n, t.split("\n", 1)[-1]) for n, t in chunk(title, body)])
    return [(n, f"{pre}\n{n}\n{t}" if n and n != title else f"{pre}\n{t}") for n, t in pieces]


def load_chunks() -> list[dict]:
    recs = []
    v = variant()
    known = known_systems() if v != "base" else set()
    summaries = json.loads(SUMMARIES_PATH.read_text()) if v in ("b", "c") and SUMMARIES_PATH.exists() else {}
    for folder in sorted(p for p in config.DOCS_DIR.iterdir() if p.is_dir()):
        for path in sorted(folder.glob("*.md")):
            meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
            audience = meta.get("audience", "all")
            if audience not in ALLOWED_AUDIENCE:
                raise ValueError(f"{path}: unknown audience {audience!r}")
            if folder.name == "manager_playbook" and audience != "manager":
                raise ValueError(f"{path}: playbook file must be audience: manager")
            m = re.search(r"^#\s+(.*)", body, re.M)
            title = m.group(1).strip() if m else path.stem
            source = f"documents/{folder.name}/{path.name}"
            if v == "base":
                pieces, extra = chunk(title, body), {}
            else:
                systems = _systems(_inline_fields(body), title, known)
                pieces = _variant_pieces(folder.name, title, body, DOC_TYPE[folder.name], systems,
                                         summaries.get(source, ""))
                extra = {"doc_type": DOC_TYPE[folder.name], "system": systems}
            for section, text in pieces:
                recs.append({"text": text, "source": source, "corpus": folder.name, "audience": audience,
                             "title": title, "section": section, **extra})
    return recs


def run(recreate: bool = True) -> int:
    from opensearchpy import helpers

    from server import opensearch
    c = opensearch.client()
    if recreate:
        opensearch.recreate_index(c)
    recs = load_chunks()
    for i, r in enumerate(recs, 1):
        r["vector"] = llm.embed(r["text"])
        if i % 50 == 0:
            print(f"embedded {i}/{len(recs)}", flush=True)
    helpers.bulk(c, [{"_index": config.INDEX_NAME, "_source": r} for r in recs])
    for _ in range(30):  # aoss has no manual refresh
        try:
            if c.count(index=config.INDEX_NAME)["count"] >= len(recs):
                break
        except Exception:
            pass
        time.sleep(2)
    return len(recs)
