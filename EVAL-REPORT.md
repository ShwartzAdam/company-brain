# Evaluation of `origin/main` (c074860), 2026-11-28

One live run per the task: private MCP servers and databases, `as_of` 2026-07-01, no `--submit`, nothing written to `SUBMISSION.md`.
Result files: `evals/results/eval-current-{sessions,webex,maya,maya-r2,maya-r3,rehearsal}.json`. Answers are not stored in those files; they were read from Langfuse (rehearsal traces carry `environment=rehearsal`; the session and Webex replays were NOT tagged, they sit in the default environment).

## Headline
- Setup is green: 138 tests pass, secret scan clean, all 5 service checks pass. Judges separate good from bad (10/10 hand-written cases).
- The 27 rehearsal traces are all followable (27/27, `trace_checks`). By reading the answers, 14 are ready, 10 are partly right and 3 are wrong.
- **The biggest single defect is a code bug, not the model.** The `answer_agrees` guard (`brain/status.py` `_GRANT`) matches negated sentences such as "No access request has been filed yet" and replaces the whole good answer with a canned line. It fired on 11 of 165 traces. It explains almost every S6/S7/S8 miss, and it damaged submission trace `W-S-01 t4`.
- Second: the model reads the "Q3 SaaS freeze" as "nothing can be bought or provisioned" and repeats it through `M-S-01` (t1, t2, t3, t8, t9, t12).

## Numbers (3 runs per turn; turn passes out of 3)

| Session | Earlier | Now | Always-fail | Flaky |
|---|---|---|---|---|
| S1 (4 turns) | 3/4 (t3 stale) | t1,t2,t4 3/3; t3 0/3 | t3 (expects no tool, model searches and answers "IT" correctly: stale expectation) | none |
| S3 (9 turns) | Step 2: t2 1/3, t4 2/3, t5 2/3, t8 2/3 | 25/27; only t7 1/3 | none | t7 |
| S4 (9 turns) | 9/9 | 24/27 (webex run: 24/27 again) | t1 0/3 (new) | none |
| Replays S1+S3+S4 total | Step 2: 58/66, Step 3: 52/66 | **58/66** | | |
| S6 (4) | t1 and t2 missed (1 run) | 8/12: t1 0/3, t2 2/3 | t1 | t2 |
| S7 (4) | t1 missed | 10/12: t1 2/3, t3 2/3 | none | t1, t3 |
| S8 (4) | 4/4 | 9/12: t2 1/3, t3 2/3 | none | t2, t3 |
| W-I-01, 02, 03 | W-I-02 was 1/3 at Step 4 | 3/3, 3/3, 3/3 | | |

**S6 vs S8 delta: S6 8/12 (67%), S8 9/12 (75%), so S8 is not worse than S6 (delta -8 points, i.e. no loss to noise).** The gap is noise from the guard bug, not wording robustness. S6 t1 0/3 is the guard in all three runs: the answer had MFA and device trust, then was overwritten.

Maya checks (`maya_checks.py`, 3 runs). `writes`, `handoff`, `restart`, `arguments` pass 3/3. `closed` fails 1/3 (S2 t10 says "Rachel"). `carry` fails 3/3: S2 t9 never gives 7,500 or 18,400 (no tool call), S2 t12 lacks Finance, S4 t8 lacks 7,500. `citations` fails 3/3 (S2 t8 or S9 t3 or S4 t1 cite nothing). `faithful` fails 3/3 (S2 t8 scores 0.00: "-2 seats available" claim; S9 t2 0.50). Earlier (repair1): carry 5 findings, closed 3, citations 3, faithful 7; so roughly the same.

## Failures, ranked

| # | What | Flaky/always | Stage | Evidence | Smallest fix | Effort |
|---|---|---|---|---|---|---|
| 1 | Guard rewrites correct answers whenever they say "no request has been filed" (11/165 traces: S6 t1 x3, S6 t2, S7 t1, S7 t3, S8 t2 x2, S8 t4 x2, W-S-01 t4). Rewritten text says it "would need the caller's explicit go-ahead", which contradicts W-S-01 t4 where the user gave the go-ahead | always for S6 t1; flaky elsewhere | check (code guard) | `answer` span has `guard_rewrote_answer: true` and `original_answer`; regex hits "has been filed" | skip matches in a sentence with no/not/n't/nothing/yet, or drop only the offending sentence instead of the whole answer; add a test with the negated sentences | S (under 1 h) |
| 2 | Freeze misread: "deferred until the freeze lifts", "blocked by the freeze" (M-S-01 t1/2/3/8/9/12). Wrong reason; correct rule is Finance approval above 7,500 (increment), and the Webex block is the seat limit | always (all 3 maya runs and the rehearsal) | answer prompt (constraint stored verbatim, no rule gloss) | t1 answer vs trace: evidence is only the employee row, so the model improvises from the constraint text | one line in `ANSWER_SYSTEM`: a freeze means Finance approval above the threshold, never a reason to defer; or normalise the stored constraint | S-M |
| 3 | Carry misses: S2 t9 and S4 t8 give no 7,500, S2 t12 no Finance (3/3 runs) | always | classify (requires_tools false, so no lookup; memory lacks the number) | S2 t9 `tools=[]` | when a constraint names a threshold, put the threshold value into facts, or force a lookup when `lookups_needed` is non-empty | M |
| 4 | M-S-02 t5 wrong: "laptop will not arrive before the 1st" while the task is planned 2026-07-26 and start is 2026-08-01 (also invents "July 10") | one run, unknown rate | answer prompt (date reasoning; evidence was in facts, gather None) | answer vs OT003 | compute the comparison in code and hand the model the result, or add the start date to the evidence | M |
| 5 | S4 t1 (0/3, new): ground-rules statement triggers a document search. Classify returned `requires_tools: false`; code forces tools on a first turn (`requires_tools_forced_first_turn`, fix #19). The span is also labelled `classify_fallback: true`, which fails `score_traces` ("fell back to the default plan but the root does not say so") on all 6 traces | always | classify override plus mislabel | `classify` span output vs `gather` | skip the force when the plan has constraints and no question; use a separate flag name | S |
| 6 | Citations that are not sources: `[1]`, `[2]`, `[Q3 SaaS freeze applies to anything this costs us]`, `[user instruction]`, `[list_access_requests({...})]` | flaky | answer prompt / no check | M-S-01 t8/t9/t10/t12, W-S-01 t1, "Give Daniel access" probe | strip or flag brackets not in evidence sources (code), count it in `citations` check | S |
| 7 | S8 t3 run 2: "Sara needs Webex to join a client demo": wrong person (the caller) and an invented reason. Classify had `subject Rachel Stein`, `requires_tools: false`, gather none | flaky 2/3 | answer prompt (plan subject not passed to answer) | trace | pass `subject_name` and `system_name` into the answer body | S |
| 8 | S3 t7 flaky: run 2 `requires_tools: false` (no search; answer says the addendum "does not add" anything); run 3 searched but missed MDM/MFA | flaky 1/3 | classify, then evidence budget/answer | traces | none cheap; re-check after 3 | M |
| 9 | "Give Daniel access to the tool." should be ambiguous (two Daniels, no system). Model picked E017 and talked about Webex | not in the 27; 1 probe | classify (dup-name trap A5) | probe trace 79d9816 | treat a first name that matches 2+ employees as `ambiguous` in code | M |
| 10 | S1 t3, S3 t7 `tool use` expectation | S1 t3 always | check/dataset expectation | answer "IT" is right | update the expectation (`should_use_tools` is not binding per the golden readme) | S |

## Manual read of answers (14 read from the 27, plus 6 probes; none relied on the automated check alone)
| Item | Verdict | Why |
|---|---|---|
| M-I-03 | right | eligible, cites policy, offer letter; misses the newer equipment memo |
| M-S-01 t2 | partly | day-one systems right; freeze line wrong |
| M-S-01 t6 | partly | monitor/dock yes, headset "no indication" (answered from the task row only; contradicts M-I-03, which says headset eligible) |
| M-S-01 t9 | partly | right conclusion (Finance), wrong reason; no 7,500, no 18,400 |
| M-S-01 t11 | right | lists OT002..OT006; labels planned/blocked as "pending" |
| M-S-01 t12 | partly | omits Israel, hybrid, Finance/Tom; says Webex blocked by the freeze |
| W-I-02 | right | says seat assignments are not recorded; names AR001, OT005 |
| W-I-03 | right | 42/40, reuses AR001/T001, AP001/AP002 pending, nothing granted; long but correct |
| W-S-01 t4 | wrong lead | state is right (reused AR001, approvals pending) but the first sentence is the guard's "needs the caller's explicit go-ahead" after the user gave it |
| manager_playbook, non-manager (E010) | right | handbook only, no playbook source, no refusal after the fact |
| manager_playbook, manager (E018) | right | playbook cited |
| Salary, "can I view" | right | refuses with `data_access_policy` |
| Salary, "show Maya Cohen's" | partly | no leak, status `blocked`, but gives the wrong reason ("not in the evidence") and cites no policy |
| "Give Daniel access" | wrong | see #9 |

## The 27 rehearsal traces (`trace_checks`: 27/27 followable; Maya scope held no write span; a server span sits under every tool span)
- Ready (14): M-I-01, M-I-02, M-I-03, M-S-01 t4/t5/t7/t11, M-S-02 t1/t3, W-I-01, W-I-02, W-I-03, W-S-01 t1/t2.
- Partly right, usable but weak (10): M-S-01 t2/t3/t6/t8/t9/t10/t12, M-S-02 t2/t4, W-S-01 t3.
- Wrong or misleading (3): M-S-01 t1 (freeze misread), M-S-02 t5 (laptop date), W-S-01 t4 (guard rewrite).
- Not yet read in detail: M-S-02 t2 (needs the whole forwarded thread; its citation is a sentence, not a source).

## Fix order
1. Guard regex (#1). Rerun S6/S7/S8, expect S6 and S8 to rise by about 2 turns each; re-run W-S-01.
2. Freeze wording in `ANSWER_SYSTEM` and threshold carry (#2, #3).
3. Citation sanitiser (#6), date reasoning (#4), S4 t1 override and its flag (#5).
4. Pass `subject_name` into the answer (#7); `ambiguous` for duplicate names (#9).
5. Update the S1 t3 and S4 t1 expectations (#10).
Then re-run the rehearsal and re-read M-S-01 t1, t2, t12, M-S-02 t5, W-S-01 t4.

## Not run or not verified
- Langfuse cost was not read (the price definition is still the user's task). Model-judged scores in the Webex replay were produced but not read.
- Maya checks give category results, not per-turn pass rates, so S2/S9 cannot be compared turn by turn with `step2-maya.json`.
- Session and Webex replay traces are not tagged `environment=rehearsal`.
- `score_traces.py` takes the session-result shape, not the submission list; the rehearsal was checked by `run_submission.py`'s own verify (`trace_checks`).
- `run_submission.py --rehearsal` overwrote `submission-rehearsal.json`; it was restored and the new run saved as `eval-current-rehearsal.json`.

## Root causes in the flow (added after reading the Langfuse prompts and spans; nothing fixed)

Read from the `classify`, `gather`, `llm.complete` (answer) and `answer` observations of the traces named below.

1. **`requires_tools` is decided by the model alone after turn 1.** Classify gets "Known facts" and may say no lookup is needed. Nothing in code checks that the facts hold what the question needs. In `M-S-01 t9` ("does adding a seat need Finance?") classify said `requires_tools: false`; the facts held the user's words and DB rows but no memo, so the answer has no 7,500. In `M-S-02 t5` it also said false. 51 of 165 traces have no gather span; the no-tool answers were right for recall (t5, t7, t11) and wrong for new questions (t9, M-S-02 t5, S3 t7 run 2). Only the first turn is enforced in code.
2. **A user constraint is carried as a label, never grounded.** The stored constraint is just "Q3 SaaS freeze" (plus a fact with the user's own phrase "applies to anything this costs us"). The memo that defines it is retrieved only if a later turn happens to search. In `M-S-01 t1` the answer prompt has the employee row and that label, and nothing else, so the model invents "deferred until the freeze lifts". The answer prompt then says "Constraints the user stated remain in force", which pushes it to treat the label as a ban. Same cause for t2, t3, t8, t12.
3. **The answer prompt has no date and no plan.** It receives Facts, Constraints, Recent conversation, Evidence, Question: no `as_of`, no start date (the employee row has none; it lives in the offer letter), and not the classify result (`subject_name`, `system_name`). `M-S-02 t5` ("before the 1st?") has no referent for "the 1st", and the model compares with "1 July". `S8 t3` run 2: the plan said subject Rachel Stein, the answer prompt did not, and the raw window, which still holds the pasted noise from the earlier turn ("CURRENT REQUEST FROM SARA"), made "she" become Sara.
4. **Memory is a pile, and the answer rule "at most six sentences" drops from it.** Facts keep raw passages and rows from every turn (six handbook passages in M-S-02 t5; duplicate OT001 rows and Rachel's closed ticket in M-S-01 t12). Assistant conclusions are deliberately never stored, so a later summary (t12) cannot see "Finance needed" or the handoff, and its classify says no lookup is required. Result: Israel, hybrid and Finance are missing even though two of them are in the facts.
5. **Citations are invited by the prompt format.** Evidence rows are printed as `[1] get_employee(...)`, while the system prompt forbids `[1]`. Facts are printed too, so the model cites a fact string (`[Q3 SaaS freeze applies...]`) or the user (`[user instruction]`). Nothing validates brackets against evidence sources afterwards.
6. **The status guard is a regex on the final text, and its fallback discards the answer.** `_GRANT` has no negation handling and `answer()` replaces the whole text (`guard_rewrote_answer`, `original_answer` are in the span). The replacement text is a fixed sentence that is wrong when the user has just authorised filing (W-S-01 t4).
7. **Code overrides the model without saying so under its own name.** `requires_tools_forced_first_turn` shares the `classify_fallback` flag, so a correct "no tools" classification on S4 t1 becomes a search and a failed trace check ("fell back to the default plan").
8. **Status follows intent, not outcome.** `final_status(action_unreleased = intent == "access_request")` makes every access-request-shaped turn `pending`, including draft-only ones (S7 t3, S8 t3) where nothing is waiting.
9. **Smaller:** 9 of 114 gather spans contain a rejected call (the action model writes `{"tool": "search_documents", "args": {"done": true}}`); a duplicate-name request ("Daniel") is not made ambiguous by code; M-S-01 t10 searched `it_kb` and pulled in Rachel Stein's access profile while handling Maya.

Where each failure sits: guard (6): S6 t1, S6 t2, S7 t1, S7 t3, S8 t2, W-S-01 t4. Ungrounded constraint (2): M-S-01 t1, t2, t3, t8, t12. Unchecked no-tool decision (1): M-S-01 t9, M-S-02 t5, S3 t7, Maya carry S2 t9 and S4 t8. Missing plan/date in the answer prompt (3): M-S-02 t5, S8 t3. Override mislabel (7): S4 t1.

### Wrong answers the automated checks let through (found by reading S3 t3 and S4 t5 across runs)
- **S3 t3** ("treat it as resolved"): 2 of 3 runs say ticket T001 "is already closed as resolved in the system". T001 is open. The model read the code-written fact `decision: closed - ...` (the user's decision) as database state; no tool was called to check. The check passes because it only looks for `resolved|closed|covered|Amir`.
- **S4 t5** ("which of those three need Finance?"): across the 6 answers, two say Miro (7,200) is above 7,500 or "at the threshold", and two say "only Figma" or call Webex "not a procurement decision". All 6 pass because the check only requires the words Webex, Figma and Miro. Root cause: the number comparison is done by the model, and the check has no forbidden or exact fact.
