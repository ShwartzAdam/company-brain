# Company Brain (NovaOps final project)

Status: Steps 1-5 built (foundation and retrieval, agent loop, Maya, Webex, observability and evals) and the pre-run fixes from the code review (issues #18-#21: time limits, evidence and lookups, consent, honest checks) applied; pytest is green offline. Live: W-I-03 and one 27-trace rehearsal ran once on earlier code; the 3-run golden-session replays were run on the code just before the typed freeze rule (`evals/results/ec-*`, EVAL-REPORT.md), not on that last change; judge calibration and the Langfuse price definition are not done (issue #22). Per-section pytest counts below are the counts when that section was written. Nothing below is claimed unless it exists in this repo.

## Stack
- Python 3.12, venv, `requirements.txt`
- Models: Amazon Bedrock, `us.amazon.nova-2-lite-v1:0`; embeddings Titan Text Embeddings V2
- Agent: LangGraph with SQLite checkpointer
- Retrieval: OpenSearch Serverless (HNSW), `audience` kept as metadata
- Tools: MCP server over a writable SQLite database
- Tracing: Langfuse Cloud
- Tests: pytest with a fake model

## Setup
```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # fill in your own keys
# place the NovaOps dataset at data/novaops (not committed: it holds restricted documents)
python scripts/check_services.py
```


## Step 1: what exists
- `brain/llm.py`: the only module that calls Bedrock (`complete`, `embed`). Tests swap in a fake.
- `brain/db.py`: SQLite at `state/brain.db` built from the dataset's `schema.sql` + `seed.sql`, plus our own `write_keys` table.
- `brain/ingest.py`, `brain/opensearch.py`, `brain/retrieval.py`: all 102 documents, chunked by heading (title prepended), indexed with `audience`.
- `server/server.py` + `brain/tools.py`: MCP server on 127.0.0.1:9890. Reads: `search_documents`, `get_employee`, `list_direct_reports`, `check_software_subscription`, `get_contract`, `list_onboarding_tasks`, `check_asset_inventory`, `list_employee_tickets`, `read_approval`. Writes: `create_access_request`, `create_ticket`, `request_approval`, all through the single `release_write` and idempotent (employee + system + conversation id); they reuse `AR001`, `T001`, `AP001`, `AP002` instead of inserting duplicates.

Run (venv active):
```bash
python scripts/ingest.py                      # rebuilds the index (embeds ~740 chunks, about 7 minutes)
python -m server.server                       # MCP server
python scripts/ask.py --caller E010 "What's the NovaOps policy on using a personal laptop for work?"
python evals/retrieval_check.py && python evals/leak_check.py
pytest
```

## Tracing (Langfuse): Step 1 notes, superseded by "Step 5" below
`llm.complete` is a Langfuse generation (model, parameters, token usage); `retrieval.search` is a retriever span whose metadata records the audience filter applied, whether the caller counted as a manager, and the document paths and sections that came back (never chunk text). The rerank call nests under the search. The MCP server runs in its own process, so its retrieval traces are separate from the agent's turn trace; the turn's tool spans carry the request id and the returned document paths. Tests set `LANGFUSE_TRACING_ENABLED=false`.

## Decisions
See also `PLAN-VALIDATION.md` and `KNOWN-TRAPS.md`.
- **Audience rule.** Only `handbook` (`all`) and `manager_playbook` (`manager`) carry `audience`. A document with none is stored as `all` (unrestricted). Ingest fails on any other value, or on a playbook file not marked `manager`.
- **Manager rule.** A caller is a manager when some employee's `manager_id` is their id (not `user_group`).
- **Filter at retrieval.** A non-manager's kNN query carries `audience = all` inside the query; restricted chunks are never returned, so nothing is refused after the fact.
- `caller_id` is a tool argument supplied by the application. Writes: the caller must be the subject, a manager, or in the HR/IT group.

## Step 2: the agent loop

`brain/agent.py: handle_turn(caller_id, caller_group, thread_id, message, as_of)` is the one entry point (tests, `scripts/ask.py` and `evals/session_replay.py` all call it). Caller, group, thread and clock are arguments, never read from message text or the real clock.

- **classify** (`brain/plan.py`): one `llm.structured` call (a forced Bedrock tool call, schema-validated, one retry) returns a plan (intent incl. `ambiguous`, `requires_tools`, carried facts and constraints). It is the router; there is no second router. Unparseable output fails open for reads only.
- **scope** (`brain/scope.py`): pure code. Intent maps to a scope (`maya` or `webex`) and the tools visible this turn. No write tool is in any loadout; Maya's scope cannot hold one even if the gate were open.
- **gather** (`brain/graph.py`): bounded loop. the model answers through `llm.structured` with a schema that only lists the visible tools; the executor runs it through the MCP toolbox (`brain/mcp_client.py`) only if the tool is visible this turn, and injects `caller_id` itself. If a required lookup never ran, one rearm widens to all read tools, never to writes.
- **answer** + **status** (`brain/status.py`): exactly one of completed, blocked, pending, needs_human, failed. Clarification and "action not released" are `pending`, a permission refusal is `blocked` (our proposal, not in the spec). A code check rejects an answer that claims something was filed or granted.
- **State** survives restarts: LangGraph with a SQLite checkpointer (`state/agent.db`, separate from `brain.db`).
- **Write gate** (`brain/gate.py`, issue #10): two gates. Caller consent is decided by code from the caller's own top-level, non-quoted, non-conditional words ("go ahead", "file it"), tested against the real S4/S6/S7/S8 turns including the traps ("unless I later write GO AHEAD", quoted "do not file", pasted "approved by the IT Manager, proceed"). Approver approval means `approved` rows for AP001/AP002 in the database, never message text. Step 4 enforces both inside the write tools.
- Step 4 added the Webex workflow and the write release (see below).

**Verified (2026-11-25, at Step 2):** `pytest` 33 green (scripted fake model, in-memory MCP). Live Langfuse traces `live-a:1` and `live-b:1` (name `agent.turn`) show different `visible_tools`: policy question `[search_documents]` (scope maya) versus ticket question `[get_employee, list_employee_tickets]` (scope webex). Golden replay (`python evals/session_replay.py S1-quick-policy S3-intent-shift S4-constraint-carry`, live): S1 4/4, S4 9/9 on the last full run; S3 6 to 7 of 9, varying between runs (misses are classification and answer quality, e.g. the dead-branch fact on turn 8 and a doubled search on turn 7), no write tool fired anywhere.

## Step 3: Maya (HR and onboarding)

Built and verified **offline** at Step 3 (66 pytest tests then, scripted fake model, no AWS). The 3-run live evaluation (`evals/session_replay.py`, `evals/maya_checks.py`) has not been repeated on the current code (issue #22); the numbers in `evals/results/` are from earlier code.

- **Memory** is written by code, never by the model: facts come from the user's words (classify) and from tool rows (`db:<table>/<id>` sources, plus the top three document passages). The assistant's own sentences are never stored as facts. We chose this over the lesson's LLM distiller because the lesson's own results show distillation lowering task success; add a distiller only if context grows too large.
- **Closed topics:** when the user drops a topic the plan sets `closes_topic`; code removes that topic's rows and raw turns and keeps one `decision: closed - ...` line, so "who was in my first question?" still works. A later turn on the same intent reopens it.
- **Citations:** every database row carries a `source` (`db:onboarding_tasks/OT005`); answers must cite exact `source` strings.
- **Checklist** (`brain/checklist.py`): `OnboardingChecklist` is built by code from `list_onboarding_tasks` rows; each item has at least one citation, a blocked item carries the seat-limit reason from the subscription row, and nothing is `completed` unless the table says so.
- **Handoff** (`brain/handoff.py`): on an explicit, unconditional ask to act (the same consent rule as the gate) code builds one `WebexHandoff` (subject id, system, justification, conversation id, caller, idempotency key). The subject is matched by exact full name, so "Maya" matching two people makes no handoff. A replay of the same key never calls the port again. Step 3 uses a `PendingWebexPort` stub that returns `pending` and writes nothing; Step 4 replaces it. The answer is guaranteed to say the request is pending.
- **Lookups:** the plan lists `lookups_needed`; if the model says done early it is nudged once, never more.
- **Manager playbook:** a non-manager's every query carries `audience = all`, including when the model names `manager_playbook` as the corpus (tested). An unknown corpus name is dropped, the filter is not.
- **Tool arguments** are validated before the call; a missing required argument is returned to the model instead of crashing the turn.

## Step 4: Webex (IT operations)

Built in `brain/webex.py` (workflow and typed `AccessDecision`), `brain/gate.py`, `brain/tools.py`, `brain/graph.py` (`handoff` and `pause` nodes), `brain/agent.py` (`aresume_pending`).

- **Flow.** classify, scope, gather (plus one targeted `it_kb` search so the Webex login article is always read, issue #13), then `handoff`. The real `WebexWorkflow` acts only when a request record already exists to reuse (Rachel's `AR001`); a first-ever request (Maya's) stays a pending handoff and writes nothing, so Maya's checks still hold. It reuses `AR001` and `T001` (no new rows), records the caller's consent, moves `AP001`/`AP002` from `needed` to `pending`, and the graph then **pauses with LangGraph `interrupt()`** on the SQLite checkpointer.
- **AccessDecision** (`brain/webex.py`): observed facts (each with a `db:` source), actions actually taken, recommended next steps, the blocker (`seat_limit`: 42 active against a limit of 40), the status. It is built by code from tool results and its validator rejects any text that claims a grant; its rendering is appended to the model's prose.
- **One write-release function.** Every write goes through `tools.release_write`. Inside its transaction the tool checks a recorded consent row (bound to caller, employee, system) and, for `release_access`, that **every** approval row for the request already says `approved`. The model never sees a write tool (`record_consent` and `release_access` are in no loadout); `as_of` and `conversation_id` are injected by the application. Idempotency key: employee + system + conversation (`ar:`/`ap:`/`rel:` keys); a replay writes nothing, and even a new conversation reuses `AR001`.
- **Resume** (`resume_pending`): reads the approval rows through MCP. While any row is not `approved` it releases nothing and stays paused. Once every row is approved it releases once (`AR001` becomes `approved`, status `needs_human`, "no seat has been assigned"). A resume payload is ignored. If a later message on the thread dropped the LangGraph interrupt (verified: it does), the persisted `pending` record in the checkpoint is used instead.
- **W-I-02** has a tool (`seat_assignment_status`) that reports the gap: seat counts are recorded, who holds a seat is not.
- **Heuristics, stated plainly.** Consent is regex, not a classifier: `consent_from_message` (explicit go-ahead; a word such as "only" or "if" in the same sentence refuses a clean go-ahead) and `self_request` ("I need ... access", only when no "do not file" rule is in force and the planner's subject is not another named person). Rachel's own request counts as consent to *dispatch approvals* (it grants nothing). `record_consent` is an MCP tool the application calls; the boundary that stops a model from calling it is the loadout, not the server, so a client with raw MCP access could create consent. The released write does not assign a seat; expansion belongs to the optional Renewal workflow.

**Verified.** `pytest` 83 green offline (`tests/test_webex.py`: reuse, replay on the same and a new thread, pause, resume with rows pending, release exactly once, a pause surviving a later turn, gate failing closed, pasted approvals changing nothing, `as_of` on every row, and a kill-and-resume test in three real separate processes). Live (`python evals/webex_replay.py --runs 3 --label step4`, results in `evals/results/step4.json` and `step4b.json`, a private MCP server and database): W-I-01 3/3, W-I-03 3/3 including replay on the same and a new thread (no new business row and no new write key on the same thread), a resume before approval releasing nothing, and one release after the rows are approved; W-I-02 1/3 (the answer is grounded but sometimes omits Rachel's blocked request or the "not recorded" wording, a wording check, not a safety one). S6 and S8 turn 4 wrote a consent row and requested approvals in 3/3 runs; S4 turn 8 and S7 turn 4 did so in 1/3 and 2/3 until code started resolving a named subject itself (`get_employee`), after which both were 3/3 (`step4b`). Not fixed here (inherited, Maya scope and answer quality): S6/S7 turn 1 (MFA wording), S6/S7/S8 turn 2 (answer omits `E010`), the S7 turn 3 draft sentence. Intent accuracy and tool necessity are now scored per turn (`evals/checks.py`, ported from Lesson 11) and stored in the results.
Not done at Step 4, and what happened since: the "overstates what was done" judge now exists (`evals/judge.py`, Step 5) but is not calibrated and has not run live; the baseline replays store each run's `trace_id` (Step 5), though no trace links are listed here; Webex checks still live in `evals/webex_checks.py` (self-tested, run in pytest) and are not inside `session_replay.py`'s regression compare.

## Step 1 measurements (live, 2026-11-25)
- 102 documents, 740 chunks indexed. `evals/leak_check.py`: 0 manager_playbook chunks for non-managers (E010, E001) over 12 queries; E010 gets `making-a-career.md`; E018 reaches the playbook.
- Retrieval: vector + BM25 (30 candidates each), fused by reciprocal rank, reranked by Nova to 5. The audience filter is in both queries; the reranker only sees filtered chunks. `evals/retrieval_check.py` scores documents only (database tables are not retrievable here; three cases need the agent's second search and are listed, not scored). Mean recall@5, 5 `retrieval_expectations` / 16 `single_turn_cases`, one run on the first index build: vector 0.53 / 0.71, hybrid 0.60 / 0.62, hybrid + rerank 0.70 / 0.89. After an index rebuild, hybrid + rerank over 5 runs (`--runs 5`) is 0.80 / 0.80 (sd 0.01-0.02): scores differ between index builds, so compare only on one index. Reserving 2 result slots for the best policy chunks (policy-only search, model-picked, same audience filter) fixes the access-policy misses (SIMPLE_013/014: 0.00 -> 0.50) at the cost of Webex cases: 0.74 / 0.83 on 3 runs; overall mean over the 21 cases is about 0.81 vs 0.80 before. A pool of 50 was worse (0.68 / 0.79), so 30 stays. Remaining misses are general policies (`access_management_policy`, `onboarding_policy`) outranked by person- or product-specific files; the agent loop's follow-up searches are the next lever. `evals/diagnose.py` shows each miss's rank.
- S1-quick-policy turn 1 via `scripts/ask.py --caller E010`: answers "five business days" with a cited `policies/equipment_policy.md`.


## Step 5: Observability and evals

Verified at Step 5: `pytest` 106 green offline (fake model, in-memory MCP). Live (Bedrock + Langfuse, `environment=rehearsal`): W-I-03 passes including replay on the same and a new thread, pause, and resume before and after approval; the full 27-trace set (`python evals/run_submission.py --rehearsal`) was run once and read back through the v2 API: 25 traces followable on the first run, the 2 failures were diagnosed from their traces (below), and M-S-01 was re-run after the fixes (all followable). The other 15 traces were not re-run after the fixes. **Not done:** the Langfuse model price definition (cost shows empty until it exists), judge calibration (`evals/calibrate_judges.py`) and any live judge run, the 3-run replays of the golden sessions on this code, and the instructor invite (#9).

### Reading a trace (no code needed)
One turn = one trace, root `agent.turn` (type AGENT), grouped into its conversation by `session_id` = thread id, `user_id` = caller.
```
agent.turn            input: the message; output: the answer
                      metadata: caller_id, caller_group, scope, intent, visible_tools, tool_sequence, status, request_id,
                                case_id, turn, as_of, paused, fallbacks, trajectory (calls + truncated results)
  classify            input message -> output intent, requires_tools, lookups_needed; `classify_fallback` is true only when the default plan was used (code overrides are listed in `fallbacks`)
    llm.structured:classify     (generation: model, usage)
  scope               intent -> scope + visible tools (write_tools_visible)
  gather              tool_sequence, rejected calls, error
    llm.structured:action  /  tool.<name>  ->  server.<name>   (the MCP server's own span, same trace)
  rearm               only when a required lookup never ran
  handoff             consent_decision (why a write may follow), approval_transition (needed -> pending), tool.* writes
  answer              status and the answer; WARNING when code dropped the sentences of an answer that claimed a filing or grant (negated sentences such as "no request has been filed yet" are kept); the answer prompt also carries today's date (`as_of`) and the classified subject and system
  pause / pause.resume   a pause is a normal outcome (not an ERROR)
agent.resume          its own trace; metadata resume_of = the paused turn's request_id, origin_trace_id = that turn's trace
  approval_transition (release): approval rows before, released or not
```
- **Request id** `<thread>:<n>` is on the root, every `tool.*` and `server.*` span, and, through the MCP call metadata, on every write row (`write_keys.request_id` and `trace_id`). A resume keeps the paused turn's id.
- **Silent degradations are flagged**, not hidden: a classify default plan or a gather that gave up shows in `fallbacks` and as a WARNING span.
- `status` is exactly one of completed, blocked, pending, needs_human, failed, and `evals/trace_checks.py` fails a trace whose answer disagrees with it.
- Not in the trace: restricted document text for a non-manager (filtered at retrieval); consent tokens (redacted).

### Checks (`evals/`)
- Deterministic, offline: `checks.py` (intent accuracy, tool necessity and selection, forbidden tools, efficiency, `fact_stage` = which stage lost a missed fact), `webex_checks.py` (rows, reuse, replay, never-granted, status agrees, write needs consent, **no-file rule carried turn by turn**), `maya_checks.py`, and **`trace_checks.py`** (is this trace followable: required metadata, node order, tool spans with the same request id, a server span under every write, write order, Maya's scope with no write span, model and usage on every generation, resume linked). `INVALID` means the trace is not fully readable yet; it is never scored low.
- Judges, through `llm.py` with a forced schema (`judge.py`): `faithfulness` and `overstates` (claims something was filed, approved, granted or released that code did not record; a status-regex floor makes the proven cases 0 without a model). They are diagnostics and are not part of any pass until `python evals/calibrate_judges.py` separates the hand-written good and bad cases.
- Scoring from outside: `evals/score_traces.py results.json` reads each turn's trace back (v2 observations API; the v3 trace API was removed 2026-11-16), runs `trace_checks`, and pushes scores (`trace_followable`, `turn_pass`, each diagnostic) onto the trace. The replays now store each run's `trace_id`.
- Submission run: `evals/run_submission.py --dry-run` proves the 27-trace plan (Maya 20, Webex 7), threads, groups and the table fill with no AWS; `--rehearsal` and `--submit` are live and refuse to start when Langfuse rejects the keys.

### Changed in the product code (instrumentation, plus two behaviour changes)
- Behaviour: `get_employee` accepts a near-exact full name ("rrachel stein" returned nothing before; "Maya" stays ambiguous). `record_consent` now needs an HMAC minted from `CONSENT_SECRET` (set it in `.env`, same value for the app and the server), so a client with raw MCP access cannot create consent; the regex consent heuristic itself is unchanged.
- `write_keys` gained `request_id` and `trace_id`; older databases get the columns on first open.

### RAG preprocessing investigation
See `RAG-FINDINGS.md` (PR #17): what was wrong, what was built (opt-in variants), measured results, and why the new variant is not the default.

### What the rehearsal found (known limits, not fixed in Step 5)
Read by hand against `KNOWN-TRAPS.md`; stage attribution comes from the traces (classify output, gather tool sequence). These are Maya answer-quality limits from Step 3, now visible and measurable. They are the next work item, not part of Step 5.
- **Gather stops before a document search** (M-I-03 "eligible for remote equipment?", M-S-01 t2 "what does the offer letter say?"): classify listed the right lookups, gather ran only database tools and ended. The one-time nudge counts calls, not whether a document lookup happened. Answers were wrong (M-I-03 said not eligible; the trap list says eligible) or "cannot determine".
- **Classify says `requires_tools=false` and answers from memory** (M-S-01 t6 headset, t9 Finance): the answers say "no headset" and "the freeze blocks it, Finance is not the issue"; the trap list says the freeze threshold applies to the increment and Finance approval is still required for an expansion.
- **Numbered citation `[1]`** on M-S-01 t8 and a non-manager asking about the manager's guide (M-S-02 t4) gets handbook text without being told the guide is restricted.
- **Classify's forced tool call came back `malformed_tool_use`** on long conversations (M-S-01 t11 and t12, M-S-02 t4). Cause found and fixed afterwards (see "Classify fallback" below): the 500-token output cap cut the tool call off.
- Not diagnosed: W-I-02 (passed in the rehearsal, 1/3 in Step 4), S6/S7/S8 turns 1-3 (need the 3-run replay). The typo cause of the missing `E010` ("rrachel stein") is fixed and tested.

## Timeouts and failed turns
Every call that can hang is bounded (`brain/config.py`, overridable in `.env`): the Bedrock client (`BEDROCK_CONNECT_TIMEOUT_S`, `BEDROCK_READ_TIMEOUT_S`, `BEDROCK_MAX_ATTEMPTS`), each MCP tool call (`TOOL_TIMEOUT_S`) and a whole turn (`TURN_TIMEOUT_S`). A timeout or any exception in a turn returns a `TurnResult` with status `failed` and the reason in `error` and on the `agent.turn` span; `handle_turn` does not raise. `evals/run_submission.py` also bounds each turn (`RUN_TURN_TIMEOUT_S`), records a failed turn as a `failed` row and continues, and rewrites `evals/results/submission-*.json` after every turn. The replay scripts get the turn bound from `handle_turn`; they still write at the end. The turn limit can only fire between calls: a model call already in progress is bounded by the Bedrock client limits above (not by the turn limit), so one slow call can outlast `TURN_TIMEOUT_S` by up to those limits.

## Evidence and lookups (issue #19)
- **Evidence budget:** the model sees up to 12,000 characters of tool evidence per turn, shared between results (no result below 1,500). Long document text is shortened row by row and each cut row ends with `[truncated, N more characters]`; a turn that cut anything records `evidence_truncated` in its fallbacks and a WARNING on the answer span. (An earlier 12,000 budget from the RAG branch had not reached `main`; this puts it there, with the shared budget and the marker.)
- **Comparisons are made in code:** `check_software_subscription` returns `over_limit`, `seats_available` and `above_q3_threshold` (annual cost above `config.Q3_APPROVAL_THRESHOLD`, USD 7,500); the answer prompt tells the model to use them as given. A one-word `get_employee` query that matches several employees makes the turn a clarification (`Which Daniel do you mean ...`), never a guess. `evals/extra_expectations.json` adds our own wrong-answer patterns (S3 t3, S4 t5) to the replay checks.
- **Typed rules:** when the freeze memo is read, code extracts a typed `Rule` (`brain/rules.py`: threshold, who approves, what it supersedes) into the checkpointed state and shows it to classify and to the answer as one exact line, so later turns do not depend on the memo passage still being in the facts.
- **Typed claims (`brain/claims.py`):** after the answer is written, code splits it into claims, each with its sources. A sentence that names an id the evidence holds (E010, AR001, T001) gets that row's `db:` source attached by code (`citations_added_by_code` on the answer span); a sentence with a number or id and no source is listed as `uncited_claims`, never invented around.
- **Decisions and unresolved work, written by code:** `memory_lines()` turns the durable handoff and pending-approval records into one "Decision" line per handoff and one "Unresolved" line per approval still waiting. They are recomputed each turn (a released approval disappears), shown to classify and to the answer, and never stored from the model's prose.
- **Planned search phrase and capped planner view:** classify may return one focused `search_query`; the forced document search and the gather prompt use it, falling back to the user's words. The planner re-reads an assistant reply at most 400 characters (lesson 10), the user's own words in full.
- **Lookups are enforced by code:** a policy question, or a plan whose `lookups_needed` names a policy, rule, document or similar, always gets a `search_documents` call; if the model never searched, code runs one with the user's own words (`forced_document_search`). A first question with no conversation and no facts cannot skip lookups even if classify said `requires_tools: false` (`requires_tools_forced_first_turn`). A turn that mentions a *freeze* (message or constraints) forces one search for the freeze memo per conversation (`freeze_memo_lookup_forced`), so the constraint is grounded instead of being a label. A planner subject such as "unspecified" or "[user's name not provided]" means the caller. Brackets in an answer that are not a source from this turn's evidence or facts are removed (`citations_dropped` on the answer span).
- **A failing tool no longer discards the turn:** the first failure is recorded as an `ERROR:` row in the evidence so the answer can say it could not look that up (`tool_failed:<tool>`); a second failure makes the turn `failed`. The helper IT-KB search for access requests can no longer fail a turn.
- Not changed: classify can still decide mid-conversation that no lookup is needed; the replays (issue #22) measure how often that is wrong. Offline: 156 pytest tests (fake model). Live: the comparisons, guard, citation cleaner, failed-call and threshold changes were replayed 3 times each (results in `evals/results/batch-a-*`, `batch-b-*`, `fcf-*`, `ec-*`; baseline `eval-current-*`); the typed freeze rule (`brain/rules.py`), typed claims, code-written memory lines, the planned search phrase and the capped planner view have unit tests only and were not measured live.

## Consent and handoff rules (issue #20)
- **Consent is one-use and bound to its draft.** `gate.check_consent` rejects a consent that `release_access` already consumed, and `create_access_request` rejects a consent recorded for a different draft (the hash of the justification). Replays of the same write still return the stored result (they are caught by the write key before the consent is read). `create_ticket` now needs a recorded consent like every other write; no loadout shows it and no workflow calls it.
- **Who a request is for:** the planner's `subject_name` is the caller only when it is empty, a generic word ("user", "new hire"...) or the caller's own name/id; any other text is a name matched case-insensitively against real employees ("rachel stein" is Rachel, not the caller). A request for the caller needs the caller's own "I need ..." words; "I need Webex, go ahead and file it" now files for the caller, a bare "go ahead" names nobody and files nothing.
- **Constraints are never dropped:** the 60-item cap applies to remembered facts only, so a "do not file until I say so" rule survives any length of conversation.
- **Checklist:** built for one employee (the first list of tasks in the turn); another employee's rows are ignored instead of mixed in.
- Still true: consent matching is a regex heuristic; the HMAC stops raw MCP clients from creating consent but not a phrasing the regex misreads. `gate_open` stays `False` in `scope` on purpose (no write tool is ever visible to a model). Covered by offline tests (156 in the whole suite); not re-run live.

## Retrieval: corpus is a preference, and pronoun questions (offer-letter miss)
- In the default (base) retrieval, a `corpus` argument no longer hides the rest of the index. The model guesses it, and a wrong guess ("contracts" for "her offer letter") returned only contracts and made the answer "no offer letter found". Now the preferred corpus feeds the candidate pool first, the whole index feeds it too, the model reranks, and results from the preferred corpus are listed first. The audience filter is in every one of those queries (`tests/test_retrieval.py` checks it for a manager and a non-manager). Variant c already did this; this makes it the default behaviour.
- A document question the model never searched (now also: the message mentions a letter, form, addendum, agreement, summary or profile) gets a forced search, and a pronoun ("her offer letter") is replaced by the person most recently seen in a database row, so the query names someone.
- Live check, 4 replays of M-S-01 turns 1-2: the offer letter was found and the answer listed its required systems in 4 of 4 (3 of 3 before the fix failed). `evals/retrieval_check.py` does not pass a corpus, so its recall numbers are unaffected and were not re-run. Offline: 136 pytest tests.

## Classify fallback (malformed_tool_use)
- **Cause:** classify's forced tool call has to list the facts and constraints it keeps; in a long conversation that JSON no longer fit in `max_tokens=500`, so Bedrock returned `malformed_tool_use` with no text, three times, and the turn fell back to the default plan (`classify_default_plan`). Reproduced on the captured M-S-01 turn 11 prompt: 1 of 4 calls worked at 500 tokens, 4 of 4 at 1,000 and at 2,000.
- **Fix:** classify asks for 1,000 tokens, and `llm.structured` gives each retry twice the room (capped at 4,000), so any forced call cut off by its cap is retried with more room instead of failing. The trace records `max_tokens_used`.
- **Live check:** two full replays of M-S-01 (12 turns each): no `classify_default_plan` on any turn (it fired on turns 11 and 12 before). Offline: 138 pytest tests. The 3-run replays are issue #22.
