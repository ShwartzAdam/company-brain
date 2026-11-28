# Where our system differs from the ai-course design, and which failures that explains

Written 2026-11-28 from a read-only pass. Nothing was run. Sources read in `ai-course`: lesson 10 (README, RESULTS.md,
WALKTHROUGH.md, `stage01_policy.py`, `stage03_policy.py`), lesson 12 README, lesson 7 README, and the project spec
(`PROJECT-DESCRIPTION.md` sections 2, 6.4-6.6, Milestones 2-3, Maya hints). Lessons 6, 8, 9, 11 and 13 were only skimmed.
Failure numbers come from `EVAL-REPORT.md` (PR #27) and PRs #28-#32. "Course" below means those files, not the lecture.

## Short answer
Some open failures do sit in places where we left the course design. Others come from our own bugs, and two come from
limits the course itself has. The deviations were sometimes deliberate and sometimes an old gap we never closed.

## A. Failures that trace to a real gap against the course

| # | Course says (file) | We do | Failures it explains |
|---|---|---|---|
| 1 | The answer call receives the plan: `answer_messages` = `ASSISTANT_PROMPT + render_plan(plan)` + last two turns (L10 `stage01_policy.py`, WALKTHROUGH stop 3) | Before #28 the answer prompt had no plan, subject, system or date (EVAL-REPORT root cause 3). #28 added subject, system and `as_of` | S8 t3 wrong person, M-S-02 t5 date. Mostly closed by #28 |
| 2 | Memory is five named fields: `important_facts`, `active_constraints`, `decisions`, `unresolved_items`, `current_intent`, updated by a distil node, with `memory_is_safe` (L10 `stage03_policy.py`) | `facts` and `constraints` only. No `decisions`, no `unresolved_items`, no distil node (BEST-PRACTICES section 4) | M-S-01 t12 omits Finance, Israel, hybrid and the handoff; carry misses on S2 t9, S4 t8 |
| 3 | Retrieval is planned: question to subjects/corpora, a focused query per corpus (L7 README; our BEST-PRACTICES section 5) | The gather model writes its own query. BEST-PRACTICES says there is no query rewrite or multi-source plan | Made-up `corpus` argument, rejected calls (9 of 114 gather spans), weak evidence for M-S-02 t5 |
| 4 | Planner sees tool results capped at 400 characters (`render_transcript`) | Still open (BEST-PRACTICES priority item 4) | Noisy-session variance; not measured as a cause |
| 5 | Spec 6.5: typed returns. `OnboardingChecklist` has a citation per item. `AccessDecision` keeps observed facts, actions taken and next steps apart | The answer is free text of at most six sentences. `AccessDecision` and the checklist are appended to it. #31 lists "structured answers" as not done | Maya `citations` and `carry` checks fail 3/3; citation clutter (#6 in EVAL-REPORT); long W-I-03 answer |
| 6 | Spec 6.6: "newer or more specific guidance overrides older. A memo has quietly changed a threshold... resolving that is your job" | We carried the user's word "freeze" as a label; the 7,500 rule was read only if a search happened. #29 forces one search; #32 types the rule; #31 computes the flag | The whole freeze misread (M-S-01 t1, t2, t3, t8, t9, t12). This is the spec's planted trap |

## B. Gaps the course has too (our fix goes beyond it)
- `requires_tools` is a model field in the course planner as well. The course relies on prompt rules (first turn, quotable answer, snapshot).
  Our mid-conversation misses (M-S-01 t9, S3 t7) are the same limit. Code-side lookup enforcement is our own addition, and the `needed_facts`
  version of it measured worse.
- The course's own measured results do not show the engineered stages beating the baseline on strict task success
  (baseline 70.8%, planning 75.0%, tool loadout 68.8%, distillation 66.7%; L10 RESULTS.md). Distillation lowered it, and the course names the
  trade. So closing gap 2 is not a sure win. It is a trade to measure.
- The course says rearm, loadouts and distillation cost real tokens: planner and distiller are about 45% of workflow tokens at stages 02-03.
  Each forced lookup and each extra planner field we add has the same cost.

## C. Not course gaps: our own defects
- The answer guard regex that rewrote correct answers (EVAL-REPORT #1; fixed in #28, but see the negation-exemption risk in the review).
- `[n]` citation format printed by our evidence renderer, and the missing bracket check.
- `classify_fallback` flag shared by two meanings (S4 t1 trace check).
- Check wording (W-I-02) and weak wrong-answer checks (S3 t3). Not stale expectations: the golden README lists `should_use_tools` as BINDING and forbids relaxing it, so S1 t3 and S4 t1 (the model searches when no tool is needed) are real defects, and EVAL-REPORT item 10 was wrong to call them stale.
- Status that follows intent rather than outcome (EVAL-REPORT root cause 8).

## D. Where we deviated on purpose and were right to
- Facts are written by code from tool rows, never from model prose (LESSONS-LEARNED item 3). The course's distiller stores model-written
  `decisions`. We keep our rule. It is also why gap 2 cannot be closed by copying the course: `decisions` would have to come from code.
- `other` loadout opens all read tools and the write gate stays closed. This matches the course.

## E. Cost note (relevant to the budget)
The L10 README says a full four-stage comparison is "a few hundred model calls" and tells students to use one session in class
(`--session S2`). Our 3-run replays across S1, S3, S4, S6, S7, S8 plus the Webex file plus the rehearsal are the large cost.
Course habits that would have cut it: one session first, repeats only for the turns that moved, offline unit tests as the first gate
(L12: "the agent does not decide when it is good enough; a gate it cannot edit does").

## F. Suggested order if we act on this (nothing started)
1. Gap 5 / 6 are largest for the grade (citations per claim, typed answer). Smallest step: a typed answer object with `claims[{text, sources}]`
   validated in code, rendered to text. Needs one live run to confirm.
2. Gap 2: add `decisions` and `unresolved` as code-written fields (from handoff, ticket and approval rows), shown to classify and answer.
   Offline-testable; live effect unmeasured.
3. Gap 3: a planned query per corpus. Larger change; skip unless time allows.
4. Gap 4: cap tool results in the planner view at 400 characters. Small, offline-testable.

## G. Offline work done after this note (unit tests only, not measured live)
- Gap 5: `brain/claims.py` types the answer as claims with sources; ids the evidence holds are cited by code; uncited numbers are listed.
  The answer is still free text from the model, not a structured model output.
- Gap 2: `memory_lines()` writes "Decision" and "Unresolved" lines from handoffs and pending approvals (code, recomputed each turn).
  Not stored as facts; no distil node.
- Gap 4: the planner's window never held tool results (messages are user and assistant text; rows live in facts, cut at 240 characters),
  so the gap was smaller than stated. Assistant replies are now capped at 400 characters in the classify prompt.
- Gap 3: classify can return a `search_query`; the forced search and the gather prompt use it. The `corpus` argument was already normalised in
  `tools.search_documents` (an unknown or list value becomes no filter). No per-corpus plan.
Risk to read live before trusting: the capped replies and the new classify field change classify's input and schema.

## H. Review fixes made offline (unit tests only)
- The subscription flag is now `annual_cost_above_threshold` (whole contract) next to `cost_per_seat`; the answer prompt says an expansion is judged by its increase.
- The freeze-memo note is stored only after a document search succeeded, so a failed search is retried on the next turn.
- A first name is no longer "ambiguous" once a later lookup by full name or id found the person.
- `clean_citations` keeps a `#section` form of a real source; rule extraction accepts reworded memos and no longer claims more than the memo says.
