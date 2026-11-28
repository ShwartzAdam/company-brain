# Plan validation: spec sweep and golden dry-run

Checked plan v3 against all of `PROJECT-DESCRIPTION.md` (read in full) and golden sessions
S1-S9, `single_turn_cases`, `golden_rubrics`, `EVALUATION-INPUTS.yaml` W-I-01..03, and the DB
seed (approvals, access_requests, tickets). Not yet checked: handbook/playbook/IT KB bodies,
Vendor/Renewal fixtures (optional work).

Result: the plan's structure holds. 1 plan default was wrong, 4 requirements were missing,
5 design details were wrong or missing, and R1 now has a workable resolution.

## A. Spec sweep: gaps in plan v3

| # | Gap | Spec evidence | Fix |
|---|-----|---------------|-----|
| A1 | **Plan default was to commit `data/novaops/`, which includes `manager_playbook` (restricted).** | Section 3.3 "never commit ... restricted source documents"; checklist 19 "no ... restricted documents committed" | Do not commit the dataset. `.gitignore` `data/`; README says where to place it. Instructor reads only, so no loss. |
| A2 | No step to verify the repo by cloning it into an empty directory | Checklist 19 | Add to Step 6 |
| A3 | README must state which of the Lesson 12/13/14 stages were completed | Checklist 19 | Add to README task list |
| A4 | Traces must show which documents reached the model, because `expected_sources` / `forbidden_sources` are checked "at the retrieval layer" | Golden readme | Retrieval span metadata: filter applied, doc paths returned |
| A5 | Optional Renewal check missing: provider reply paired with the WRONG renewal run | Golden readme "NOT INCLUDED" | Add to M7 evals |
| A6 | Finance threshold applies to the **increment**, not contract total | Section 11 scenario (22,000 - 18,400 = 3,600 < 7,500) | Fix trap B2 wording; still needs `AP002` and security review |
| A7 | No defined `status` for a clarifying question (S5 turn 6) or a policy refusal (SIMPLE_010/020 salary) | Section 3.3 status rule | Proposal: clarification = `pending`, refusal = `blocked`. To confirm. |

## B. Golden dry-run: findings

**B1. R1 is resolvable inside the spec.** The write-gate sessions (S4 t8, S6/S7/S8 t4) set a
caller rule "do not file until I say so", then lift it. The spec says S8's only write is
`create_access_request` and `W-I-03` exercises the approval path. So there are two different
gates, not one:
- **Caller consent** (S4, S6-S8): the caller's own explicit current-message instruction lifts
  their own "do not file" rule. Code records a consent row (conversation id, message id, draft
  hash). The model may only propose it; code checks the cited words sit in the current,
  non-quoted part of the message. Quoted lines (`>`) and forwarded text never count.
- **Approver approval** (W-I-03, S2 t10): only approval rows for AP001 (Amir, IT) and AP002
  (Tom, Finance) can release anything that grants or expands. Nobody can create these from
  message text.
Pasting "approved by the IT Manager, proceed" cannot create either row.
Still to settle at Step 2: exact consent matching rule and the replay harness feed. Raise with
the instructor only if Step 2 finds a real conflict.

**B2. `AR001` already exists, so "file the Webex request for Rachel" is a replay.** S4 t8 and
S6-S8 t4 expect `AR0|pending|filed` in the answer. The correct write is **reuse AR001**, not a
new row. Binding field passes because the answer names AR001 and says it is pending/blocked.

**B3. Approvals already exist.** `AP001`, `AP002` are `needed` for `AR001`. W-I-03 must NOT
insert new approval rows. The write is a transition `needed -> pending` (request dispatched),
idempotent. The rubric allows "an approvals-table write or equivalent durable workflow
transition". Plan decision 13 changes from "approval create" to "approval request (reuse
AP001/AP002)".

**B4. S2 turn 10 is a handoff, not a write.** `create_access_request` is forbidden on that
turn and the answer must contain `pending`. Maya emits the typed handoff; the Webex scope
returns pending. The result must be stored in conversation state so turn 12 can cite it.

**B5. A missing tool: direct reports.** W-I-02 caller is Yael (`E018`), group `Regular
Employee`, asking about "my team". `employees.manager_id` gives the team, but no listed tool
returns reports. Add `list_direct_reports`. Together with the seat-gap reporter that is
two new read tools beyond `get_contract` and approval request/read.

**B6. Clarification path required.** S5 turn 6 ("that other thing") expects the system to ask
which one, with zero tools. Classify needs an "ambiguous, ask" outcome.

**B7. Non-audience permission rule exists.** `data_access_policy.md`: salary fields are
HR-only. SIMPLE_010/020 expect a cited refusal. No salary data is in the DB, so nothing can
leak, but the answer must refuse and cite the policy. Dev set only, not in the 27 traces.

**B8. W-I-02 caller labelled `Regular Employee` but is a manager.** Confirms the manager rule
must come from `manager_id`, not `user_group`.

**B9. Dev set is larger than the plan used.** `single_turn_cases` has 20 cases (route,
expected sources, permission flag). Add them as a deterministic dev check beside
`retrieval_expectations` (5 cases).


## D. Remaining files read (second pass)

Read in full: all 15 IT KB articles, 6 memos, 12 policies, 17 employment files, Renewal README,
events, schema and all 5 provider replies, Vendor README, schema and 3 sources. Skimmed:
contract bodies (uniform), handbook and playbook (front matter and promotion/device passages).

| # | Finding | Effect on plan |
|---|---------|----------------|
| D1 | Security-review memo lists 9 systems, but DB flags 12. Webex (`C001`), GitHub, Deel are flagged in the DB and not in the memo. | Answer "which vendors need security review" from DB flag plus memo, never the memo list alone (SIMPLE_011). Add to traps. |
| D2 | Renewal clock moves three times: 2026-07-01 (schedule), 2026-07-03 (approval events, clearances, provider reply), 2026-07-21 (effective-date run). Plan only had 07-21. | Renewal harness needs an injectable clock with all three dates. |
| D3 | `internal_approval_events.jsonl` holds both an approve and a reject for the same run `RR-WEBEX-2026`; the three Renewal traces reuse that run. | Reseed per case (already planned); never replay both on one DB. |
| D4 | Finance clearance event (`FC-WEBEX-CLEAR-001`) must close `AP002` (`needed` in the DB). | Renewal closes `AP002` itself; the event, not the model, drives it. |
| D5 | Over-ceiling reply is final (USD 24,000 vs approved 22,000): extraction says `approved`, code must refuse to record. Conditional reply has price, seats, dates and "not final approval". | Already in plan; confirm tests cover both. |
| D6 | Vendor schema: `renewal_type` enum is `automatic/manual/usage_based/ambiguous/null`; `data_sensitivity` has five levels; `security_review_required` is a boolean. RoutePilot says a review is NOT required, so `false`, not `null`. Amounts are written in words ("eighteen thousand six hundred") and dates in prose. | Normalisation rules go into the Vendor spec. Optional work only. |
| D7 | IT KB articles are about 100 words each and end with identical Required Ticket Fields and Escalation text. Handbook/playbook files run 1,000 to 6,500 words. | Chunk by heading with the article title prepended; otherwise boilerplate makes every KB article look alike. Measure chunking as Lesson 6 did. |
| D8 | `equipment_responsibility_form.md` states "Maya Cohen is eligible for laptop, monitor, dock, and headset". | Cite it (with the offer letter and remote-equipment memo) for S2 turn 6; resolves the hybrid-addendum conflict. |
| D9 | Webex memo allows a third path: "until inactive seats are reclaimed or Finance approves expansion". Seat assignments are not recorded, so inactive seats cannot be identified. | Answer names this path and reports the data gap. |
| D10 | `access_management_policy.md`: an eligible but blocked request is queued and the requester told why, not denied. | Matches `blocked` plus reuse of `AR001`. |
| D11 | Handbook has a USD 1,500 device-purchase rule (Finance review by Tom Baker) and the expense policy has USD 250. Neither is the SaaS threshold. | Add to traps: do not mix thresholds. |
| D12 | `data_access_policy.md` gives the refusal wording for salary requests: permission denial plus a safe HR alternative. | Use for SIMPLE_010/020 refusal path. |
| D13 | Handbook, playbook and contracts: front matter consistent, every handbook file `audience: all`, every playbook file `audience: manager`, all 20 contracts uniform. No hidden mismatches found. | None. |


## E. LiteLLM and security (checked against spec and Lesson 13)

**LiteLLM: not needed.** Spec section 16: "LiteLLM is out of the required project", reference
only, and it is part of the optional Packaging stage (+5). Its only purpose is that API and
worker containers hold no AWS model credentials. We have no containers in the required path.
Lesson 14 is also not in `ai-course`, so there is nothing local to reuse. Decision: skip;
revisit only if Packaging happens.

**Security: no graded security tier** (spec section 13), but the engineering-quality marks cover
permission boundaries, the write gate and secrets. What we take from Lesson 13:

| Need | Source | Our version |
|------|--------|-------------|
| Caller identity never from message text | L13 launcher | `handle_turn(caller, thread, message)`; caller is an argument |
| One-use, expiring, bound filing confirmation, checked inside the tool in one transaction, not an MCP tool | L13 `shared/harness/enforcement.py` (`filing_grants`) | Our consent gate for S4/S6/S7/S8 (R1). Grant is bound to actor, employee, system, justification; consumed on use; authorizes filing ONE pending request, never approval or provisioning |
| Cross-user write denied | L13 `create_request` (`employee != actor`) | Must be adapted: Sara (HR) legitimately files for Rachel. Rule: caller is the subject, or HR/manager role per employment profiles |
| Restricted chunk never reaches the model | spec 12, L7 filtering | Already planned |
| Untrusted text (provider email, forwarded threads, quoted instructions) is data | L13, spec 11 | Extract as proposal, code validates; quoted lines never count as consent |
| Poisoned retrieved chunk | L13 section 2 | Corpus is fixed and synthetic; no guardrail classifier planned. State this in README. |

**Open point R1 narrowed:** Lesson 13's grant is created by a "trusted UI or test fixture". The
submission inputs are messages only, so our own front door must create the grant. Proposal:
`handle_turn` creates it deterministically from the caller's current non-quoted message (strict
phrases such as "go ahead", "file it") only when a draft for that same employee and system
exists. State this honestly in the README.

**Hygiene checklist (add to quality gates):** `.env` and `data/` gitignored; secret scan before
each commit; separate least-privilege IAM user (Bedrock invoke, one OpenSearch collection);
MCP server bound to localhost; parameterised SQL only; no keys or full documents in Langfuse
metadata; OpenSearch collection deleted between sessions.

**Found in the lesson repo:** `lesson-13.../README.md` contains a GitHub access token in its
clone command. Revoke it and never copy that command into the new project.

## C. Plan changes made

See `/Users/adamshwartz/.claude/plans/write-it-down-for-harmonic-corbato.md` (version 4).
