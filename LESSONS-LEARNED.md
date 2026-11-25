# Lessons learned (Step 2 and Step 3 prep)

Plain language. Read this before editing `brain/graph.py`, `brain/scope.py`, `brain/tools.py`, `brain/llm.py`, `brain/plan.py`.
Design reference: `BEST-PRACTICES.md` (from the ai-course lessons).

## What went wrong

1. **Weak "done when" (Step 2).** Step 2 was called done because two questions showed different tool sets. The first full 66-turn replay then found real bugs. A step is only done when its golden session passes end to end.
2. **Made-up corpus bug.** The model passed a `corpus` argument that does not exist, so every document search came back empty. No unit test or two-question check would have caught it.
3. **Model sentences stored as facts.** Answer digests were saved as `facts`. A wrong answer ("checklist is empty", S2 t11) became truth and was reused on later turns. A closed branch (Rachel's ticket) also leaked back in (S2 t8, t12).
4. **Narrow `other` loadout.** We gave `other` only two tools. A misclassified ticket question (S2 t4) then had no ticket tool and the model searched documents instead. The lesson design says relevance fails open: `other` sees every read tool.
5. **Regex JSON parsing.** Classify and gather pulled JSON out of free text. A fenced or malformed reply silently became intent `other`. The lessons force a tool call and validate against a schema.
6. **Missing planner rules.** No "a first turn needs tools" rule and no "only quote-able answers skip tools" rule, so S9 t1 and S2 t9/t11 skipped lookups because memory "already had" the answer.
7. **Unreliable checks.** The replay's source check never matched (path prefix differed), the judge flagged text the user had quoted, and a word-match check false-alarmed. A score from a broken check is worse than no score.
8. **Uncited database claims.** DB rows had no `source`, so citations like [1] could not be verified.

## What we changed (uncommitted in the main checkout until the live comparison is read)

- `llm.structured`: forced tool call plus schema validation; classify and gather use it.
- `other` = all read tools; `widen_reads` still never adds a write.
- Planner prompt: first turn / policy / person / record means tools; skip tools only if the answer is quotable from this conversation; DB facts are an old snapshot.
- Facts are written by code from tool rows, never from model prose. Every DB row carries a `db:table/id` source.
- Argument validation on tool calls; judge ignores user-quoted text.
- Evals: `evals/session_replay.py` (saved results, regression compare), `evals/maya_checks.py`, `evals/judge.py`, results in `evals/results/`.

## Checklist: a step is not done until every line is true

- [ ] Full multi-turn replay of the step's golden sessions, 3 runs, results saved in `evals/results/` as the baseline.
- [ ] Failures diagnosed from Langfuse traces first (classify output, tool calls, answer per failing turn), not guessed.
- [ ] MCP server restarted after any tool change (it is a separate long-running process).
- [ ] Before/after compared per turn against the saved baseline. A change that moves nothing or regresses recall turns is reverted or tuned.
- [ ] A few real answers read by a human for each check, so the check itself is trusted.
- [ ] No model sentence stored as a fact; every DB-backed claim has a `db:` source.
- [ ] Zero writes where the scope has no write tools (row counts unchanged).
- [ ] `pytest` green, `scripts/secret_scan.sh` clean, README matches code, one commit saying what was verified.
- [ ] Report what was measured and what was only inferred.

## Habits

- Keep the lesson design unless there is a measured reason to deviate.
- When a lever does nothing three times, look at what the code makes possible (lesson 12).
- Do not widen raw context to fix recall; it made recall worse twice in the lessons.
- Repeat runs; read mechanisms, not the third decimal.
