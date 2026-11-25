# Best practices from the ai-course, applied to Company Brain

Read from the lesson docs (lessons 2, 3, 5-13) and the lesson 10 code. `ai-course` is read-only: we adapt a copy.
"Status" says what our code does today. Update it when the code changes.

## 1. Structured output: force it, then validate (L3, L6, L7)
- Lessons force JSON through a **forced tool call** (Bedrock `toolConfig`), validate it with `jsonschema`, and retry only on failures worth retrying. Planner, tagger, reranker and judges all work this way.
- **Status: done for classify and gather** (uncommitted). `llm.structured()` uses a forced Bedrock tool call and returns schema-valid JSON or raises; a backend without it falls back to `_json_from_text`. Free-text `llm.complete` remains for the answer and the retrieval rerank. No distil step exists yet.

## 2. The planner (L10 stage 1)
- One structured plan per turn: intent, facts, constraints, `requires_tools`. A field's description is sent to the model like prompt text, so write descriptions as carefully as prompts.
- `requires_tools` is False only when every needed fact is already in this conversation. "You know nothing about NovaOps otherwise, so a first turn is almost always True." "Only an answer you could quote from THIS conversation is False."
- A different topic needs a different document; having retrieved one article does not answer a new question.
- Drop what the user resolved or abandoned. Plan for the request inside a pasted email, not the signature.
- **Status: mostly done** (uncommitted). `CLASSIFY_SYSTEM` now has the first-turn/policy/person/record rule, the quotable-answer rule, the DB-facts-are-a-snapshot rule and the pasted-text rule. Still missing: tool results are not truncated in the planner's view (`plan.window` keeps 2 user turns verbatim).

## 3. Tool loadouts (L10 stage 2, L12)
- **Relevance fails open, permission fails closed.** Which read tools are visible is an optimisation; which writes may run is a safety property.
- Each loadout row is deliberately a little wider than needed so a nearby classification error can recover. `other` opens the whole read catalogue.
- Rearm: if the plan needed a lookup and none ran, widen to all reads once. Never add a write.
- L12 found a score stuck through five wording changes because `LOADOUTS` never offered the tool. **When a lever does nothing three times, look at what the code makes possible.**
- **Status: done** (uncommitted). `scope.LOADOUTS['other']` is `READ_TOOLS`; `rearm` in `graph.py` widens to all reads once and never adds a write.

## 4. Context and memory (L10 stage 3, L12)
- Graph state is not model context. Build the model's context per node from state.
- Keep the last two turns verbatim, drop old tool traffic from them, cap tool results in the planner's view (400 chars in the lesson).
- **More raw context made recall worse** in two separate L12 runs (longer tool output, three verbatim turns). Do not widen context to fix recall.
- Memory is named fields (facts, constraints, decisions, unresolved), not a summary string. Never silently drop a constraint. A rejected memory keeps the raw history. Memory values are a snapshot: "what is it now" needs a tool.
- Lesson's own result: distillation saves tokens (-12%) but strict task success was lower than the baseline. Treat it as a trade.
- **Status:** facts are written by code from tool rows (`_row_facts` in `graph.py`), never from the model's prose; classify takes facts and constraints from the user's words only. Facts and constraints still only grow (cap 30). No `memory.py`, distil node or closed-topic handling yet (Step 3).

## 5. Retrieval (L6, L7)
- Access control is a **metadata filter at retrieval**, from the caller's role, never a refusal after the fact.
- Plan then retrieve: route the question to the corpora it needs and rewrite a focused query per corpus. Nova 2 Lite reliably makes only one tool call per turn, so a multi-source question needs a forced plan step.
- Retrieve wide, rerank narrow. A subject filter fails open when unsure. Measure every lever (faithfulness, context relevance, completeness).
- Unrestricted means missing audience; only `manager_playbook` is fail-closed.
- **Status:** hybrid retrieval, audience filter and rerank done. No query-rewrite or multi-source plan step; the gather model writes the query itself.

## 6. Tools and the server boundary (L8, L9)
- Tools live behind an MCP server; the agent discovers them at runtime. Read and write tools differ: writes file something pending approval, they never act on their own ("propose, don't act").
- LangGraph earns its place through runtime capability: durable state, checkpoints, `interrupt()`/resume for approval. A supervisor with parallel specialists is overkill unless subtasks are truly independent.
- Keep the model call behind one seam (`llm.py`). **Status: done.**

## 7. Observability (L11)
- Observability says what happened; evaluation says whether it was right. Neither replaces the other.
- Three lines instrument the agent; node names are span names. The caller must supply `session_id`, `user_id`, tags. One root span per turn, with the trajectory digest copied onto it.
- Use LLM judges only where code cannot decide. Make them explain themselves. Rubrics that count parts and divide give a gradient. Evaluators do not backfill old traces.
- **Status:** root span, node spans and tool spans exist; retrieval metadata shows the filter and returned paths (paths only).

## 8. Evals (L10, L11)
- Assert what you can, judge the rest. Eight deterministic checks (tool selection recall, tool necessity precision, argument correctness, sequence, call efficiency, forbidden avoided, fact recall, intent accuracy) plus judges (faithfulness, session faithfulness, context relevance, completeness, correctness).
- Roles: outcome metrics feed the score, diagnostics explain, hard gates (forbidden writes, runtime failures) cannot be averaged away. Strict task success asks how many turns cleared every requirement.
- Route checks by turn kind (retrieval, lookup, reasoning, action, summary, trivial). Grade retrieval whenever it happens. A summary is judged against the whole session's evidence. Missing expected evidence scores zero.
- Repeat runs; report medians and ranges. A baseline that is not 100% is the honest starting point.
- **Status:** `evals/session_replay.py` (binding fields, args, saved results, regression compare), `evals/maya_checks.py`, `evals/judge.py`; baselines in `evals/results/`. No intent-accuracy or tool-necessity metrics yet (Step 5).

## 9. Improvement loop (L12)
- The agent does not decide when it is good enough; a gate it cannot edit does. Eval scores give a distance, not pass/fail; the budget ends the loop.
- Every real win is a trade; price it. Not one score improves monotonically. Read mechanisms, not the third decimal: two runs of the identical agent differed by 0.03.
- Record what did not work. Keep forbidden-action scores untouched.
- **Our habit:** diagnose from traces first, change one thing, re-run, compare to the saved baseline.

## 10. Security (L13)
- Ask which part of the application can still say no after the model says yes. A classifier judges text and can be wrong; a tool checks permission and confirmation before changing a record.
- Caller identity comes from the harness, never the message. Filing needs a one-use, bound confirmation checked inside the tool. A tool's real semantics decide its risk, whatever the user calls it.
- An audience filter does not stop a poisoned chunk. Deleting a chunk does not clean a conversation checkpoint.
- Several lesson READMEs contain GitHub tokens in their clone commands. Never copy them into this repo, and have them revoked.

## Priority list for Step 3
1. ~~Restore `other` to all read tools.~~ Done (uncommitted).
2. ~~Add the planner's first-turn and quotable-answer rules.~~ Done (uncommitted).
3. ~~Forced tool-call JSON with validation.~~ Done for classify and gather (uncommitted).
4. Bounded planner transcript with truncated tool results. Open.
5. Closed-topic handling, typed handoff, checklist (Step 3 plan).
6. Re-run `evals/session_replay.py` and `evals/maya_checks.py` against the saved baselines after each change.
