"""python scripts/gen_summaries.py -- one summary line per small document (variant b), cached in
state/doc_summaries.json (gitignored). Only the five unrestricted small corpora; never the manager playbook."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import config, ingest, llm  # noqa: E402

SYSTEM = ("Write ONE sentence (max 35 words) saying what this document is about and which questions it answers: "
          "name the systems, roles or processes it covers. For a general policy, say what kinds of requests it "
          "governs and name the systems it applies to (any system, including those named in the other documents). "
          "Use only facts in the document. Output only the sentence.")
SKIP = ingest.LONG_CORPORA
out = json.loads(ingest.SUMMARIES_PATH.read_text()) if ingest.SUMMARIES_PATH.exists() else {}
for folder in sorted(p for p in config.DOCS_DIR.iterdir() if p.is_dir() and p.name not in SKIP):
    for path in sorted(folder.glob("*.md")):
        key = f"documents/{folder.name}/{path.name}"
        if key in out:
            continue
        body = path.read_text(encoding="utf-8")
        out[key] = llm.complete([{"role": "user", "content": body}], SYSTEM, max_tokens=120,
                                temperature=0).strip().replace("\n", " ")
        print(key, "->", out[key], flush=True)
ingest.SUMMARIES_PATH.parent.mkdir(exist_ok=True)
ingest.SUMMARIES_PATH.write_text(json.dumps(out, indent=1, sort_keys=True))
print(len(out), "summaries")
