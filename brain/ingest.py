"""Read the 7 document folders, keep `audience`, chunk by heading, embed, index."""
import re
import time

from brain import config, llm

ALLOWED_AUDIENCE = {"all", "manager"}
WINDOW, OVERLAP = 250, 50


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


def load_chunks() -> list[dict]:
    recs = []
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
            for section, text in chunk(title, body):
                recs.append({"text": text, "source": f"documents/{folder.name}/{path.name}",
                             "corpus": folder.name, "audience": audience,
                             "title": title, "section": section})
    return recs


def run(recreate: bool = True) -> int:
    from opensearchpy import helpers

    from brain import opensearch
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
