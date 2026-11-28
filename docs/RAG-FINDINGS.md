# RAG findings: preprocessing investigation (issue #13), PR #17

Question: could the retrieval failures come from how the markdown files are prepared and indexed, and should the files be reordered or normalized before ingestion?

## Short answer
- **Do not reorder or edit the files.** Retrieval is by similarity and BM25; file order has no effect, and the headers are clean (one `#` title, `##` sections, no tables).
- The problems were in **what we embed and what the search returns**, not in the files.
- A retrieval-only improvement was real (below) but did **not** become the default: with it, the full rehearsal regressed (M-S-02 t1 failed). It is opt-in until the agent's evidence step is fixed.

## Issues found
1. **Over-fragmentation of small documents.** Headings split a 107-word KB article into 5 chunks of 12-46 words. Average chunk size: it_kb 22 words, contracts 37, policies 45, memos 31; handbook/playbook 174-196. 284 of 740 chunks were under 80 words.
2. **Boilerplate duplicates.** 53 chunks were identical apart from the title ("Required Ticket Fields" and "Escalation" x15, "The user reports an issue related to X"), crowding the candidate pool.
3. **Missing context per chunk.** Only title, section and body were embedded. No document type or system name. Contracts, KB articles, policies and memos have no YAML front matter (their `key: value` lines were body text in one chunk). Policies never name the systems users ask about (ZoomInfo, Figma, Salesforce), so they ranked 13-50.
4. **Results were chunks, not documents.** One article could fill several of the 5 slots; the 5-document workflow cases could not reach 1.0 in 5 slots.
5. **Reranker preferred people profiles and handbook pages** over the governing policy or contract; the old "2 reserved policy slots" was a patch, and the policy was often chosen by search order (wrong policy).
6. **Rerank snippet of 350 characters** cut whole-document entries, so data changes alone made scores worse (A + old search 0.83 vs baseline 0.87).
7. **The model's `corpus` argument acted as a wall.** A guess of `policies` or `it_kb` hid the ZoomInfo contract and the Webex SLA contract.
8. **Evidence cap of 1500 characters** in the agent cut each of 8 whole documents to 120 characters, so "who signs off on AWS admin" came back as "manager approval only".
9. **Label problems.** SIMPLE_004 expects the Webex login article for an SLA question (it has no SLA text); WF_WEBEX_001 and WF_ONBOARD_001 need 5 documents in 5 slots.
10. **Evidence size is a trade-off.** 12000-character evidence fixed (8) but made M-S-02 t1 fail ("model did not return a valid action", 3/3) and t3 pending in the rehearsal.

## What was built (all behind env switches, defaults unchanged)
- `INGEST_VARIANT=a|b`: whole-document entries for docs <= 300 words, noise lines dropped, `doc_type`/`system` fields and `[doc_type | system] Title` prefix; `b` adds one Nova summary line per document (`scripts/gen_summaries.py`).
- `RETRIEVAL_VARIANT=c`: document-level results, whole-document rerank, best contract/KB/memo per named system as candidates, fixed slots (best contract per named system, 2 policies chosen by the model among policies only, 1 memo), `corpus` as a preference (audience filter stays in every query).
- Evals: `evals/extra_cases.json` (25 questions), `evals/label_overrides.json` (our corrected copy; golden file untouched), `evals/heldout_cases.json` (16 fresh questions), `retrieval_check.py --extra --heldout --k`, prints original and corrected scores.
- `scripts/ingest.py` refuses to overwrite the baseline index for a non-base variant.

## Measured (recall@k, 3 runs unless noted, separate indexes)
| setup | 5 expectations | 16 single-turn | 25 extra | overall (46) |
|---|---|---|---|---|
| original baseline @5 | 0.74 | 0.83 | 0.93 | 0.87 |
| A data + old search | 0.65 | 0.72 | 0.94 | 0.83 |
| B data + old search | 0.65 | 0.70 | 0.92 | 0.81 |
| B + C + fixed contract/policy slots @5 | 0.74 | 0.93 | 0.97 | 0.93 |
| + model-picked policies, memo slot @5 | 0.78 | 0.96 | 0.98 | 0.95 |
| same @8 | 0.82 | 0.98 | 1.00 | 0.97 |
| held-out, 16 fresh questions @8 (one run, labels unreviewed) | | | | 0.94 |

Corrected labels: 5 expectations 0.91 at k=5. `leak_check`: 0 leaks. Live answers fixed with the larger evidence and corpus-preference: AWS admin (IT Manager + Finance), ZoomInfo (contract cited), Webex SLA (P1 4h / P2 next business day).

## Why it is not the default
Full rehearsal (27 traces) with the new variant: all trace checks pass, but **M-S-02 t1 failed and t3 was pending** (both completed before). Smaller evidence avoids the failure but loses the rule. A rank-based cap (top 3 documents 1400 characters, rest 300) did not help and was reverted.

## Next
1. Fix the evidence step: pass only the relevant section of each document (not a cut-off prefix), then re-run the rehearsal; only then make `company-brain-docs-v2b` + variant c the default.
2. Review the held-out labels; add more fresh questions (46 labelled cases: one case moves a mean by about 0.02-0.05).
3. Decide on the label overrides with the course expectations in mind (the readme invites reading them critically).
4. Open indexes `company-brain-docs-v2a` and `-v2b` still exist in OpenSearch; delete `-v2a`.
5. Not touched: Langfuse follow-ups from issue #13.

Opt in: `OPENSEARCH_INDEX=company-brain-docs-v2b INGEST_VARIANT=b RETRIEVAL_VARIANT=c` (also needs evidence 12000 characters and k=8, not applied here).
