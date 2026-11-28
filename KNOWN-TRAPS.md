# Known traps in the NovaOps dataset

Findings from reviewing `novaops-enterprise-agent-dataset` (database, contracts, policies,
memos, employment files, IT KB, golden datasets). The data is used as supplied and was not
edited. Dataset `as_of_date` is 2026-07-02, but the evaluation run pins the clock to **2026-07-01** (spec section 13); never use the real clock. Renewal-deadline day counts below were computed from 07-02 and shift by one day.

Reviewed in the second pass: IT KB, memos, policies, employment files, Vendor and Renewal
fixtures, security-review flags. Only skimmed: handbook and playbook bodies (78,000 words) and
contract bodies beyond header fields.

## A. Deliberate contradictions (the agent must resolve them)

| # | Trap | Evidence | Correct handling |
|---|------|----------|------------------|
| 1 | Webex over its seat limit | `SUB001`: 42 active vs limit 40; `C001`, Webex contract, Webex memo, audit `EV002` agree | Block new provisioning. Reuse `T001` and `AR001`. Pause for approval. Never say access was granted. |
| 2 | Approval threshold | `saas_procurement_policy.md` says USD 5,000; `saas_renewal_freeze_q3.md` sets USD 7,500 for Q3 2026 | Newer memo wins: USD 7,500. |
| 3 | Contract owners | DB: Figma (`C013`) = Engineering, SupportDesk (`C020`) = IT. Contract docs: Product, Customer Success. `contract_owner_cleanup_memo.md` | Contract documents are the source of truth. |
| 4 | Restricted vs open HR docs | `manager_playbook/*` is `audience: manager`; `handbook/making-a-career.md` is `audience: all`; both answer "how do I get promoted" | Filter at retrieval for non-managers. A refusal after retrieval is a leak. |
| 5 | Duplicate names | Maya Cohen `E001` / Maya Rosen `E020`; Rachel Green `E009` / Rachel Stein `E010`; Sara Ben-David `E004` / Sara Cohen `E005`; Daniel Levi `E002` / Daniel Cohen `E017` | Resolve by employee id or email, never first name. |
| 6 | Approval states | `AP001`, `AP002` = needed; `AR001` = blocked; `AP003`, `AP004` = pending | Only a recorded approved status counts as approval, never inferred text. |
| 7 | Stale manager | Noam Sharon (`E003`): DB manager `E018`, should be `E013` (Emma Wilson); addendum and audit `EV001` agree | Treat Emma as current manager. |

## B. Judgment calls and subtle conflicts

1. **Equipment rules.** `equipment_policy.md`: monitor and dock for remote or hybrid staff with an
   approved addendum. `remote_work_policy.md`: remote staff need no addendum, only hybrid do. The
   newer `remote_equipment_budget_update.md` says "when role or addendum supports it". Maya Cohen is
   hybrid, no addendum file, but her offer letter lists monitor and dock. Eligible, citing the offer
   letter and the memo (question `M-I-03`).
2. **Webex expansion vs USD 7,500.** Two extra seats cost roughly USD 920 (18,400 / 40 per seat),
   under the threshold. The Q3 memo applies the threshold to the **increment**, not the contract total (expansion to 50 seats adds 3,600, still under 7,500). Yet the Webex memo, `AP002` and the KB require Finance approval for any
   expansion. Do not apply the threshold mechanically.
3. **Outdated runbook line.** `it_kb/webex_license_assignment.md` mentions an old "add temporary
   seats and reconcile later" practice and marks it invalid. Never present it as current advice.
4. **Approvers.** `AR001` names Yael Romano (`E018`). `AP001` and `AP002` need Amir Haddad (`E006`,
   IT Manager) and Tom Baker (`E011`, Finance). Yael can approve business eligibility, not expansion.
5. **Maya's checklist.** `OT005` Webex stays blocked until the seat issue is resolved. `AR004`
   Salesforce is planned, do not activate before 2026-08-01. `OT002` Okta is pending. Never mark a
   task complete unless the onboarding table says complete.
6. **Renewals at 2026-07-02** (policy: review at 90 days; urgent if notice deadline within 45 days):
   - Past the notice deadline: Webex, Salesforce, MailGuard.
   - Urgent (deadline within 45 days): Slack, Okta, SecureSign, CloudBackup.
   - Miro: ambiguous renewal terms go to Finance for a ruling.
7. **Authority order when documents conflict.** Security policy over access policy; data access
   policy over access management; contract limits over general policy; newer memo over older policy.

8. **Security review list.** The security memo names 9 systems; the DB flags 12 contracts
   (adds Webex `C001`, GitHub `C004`, Deel `C009`). Use the DB flag plus the memo.
9. **Other thresholds.** Handbook: device purchases over USD 1,500 need Finance review. Expense
   policy: USD 250. Neither is the SaaS expansion threshold (USD 7,500 incremental).
10. **Webex memo third path.** New seats wait until inactive seats are reclaimed or Finance
    approves expansion. Seat assignments are not recorded, so reclaim cannot be computed.

## C. Build implications

- About 70 of 102 documents have no `audience` front matter (policies, contracts, employment,
  memos, `it_kb`). Default a missing audience to `all`, and record that decision in the README.
  Only `handbook` (`all`) and `manager_playbook` (`manager`) carry it.
- Citations must be able to point at database rows as well as documents. The golden datasets cite
  the tables `contracts`, `software_subscriptions` and `onboarding_tasks`.
- Every document named in the golden datasets exists under `documents/`.
- Database and contract documents agree on seat limit, cost, dates, renewal type and notice days
  for all 20 contracts. The only mismatches are the two owners in trap A3.

## D. Which traps the 27 submission questions can reach, and what covers each (offline read, nothing run live)
Questions are in `data/EVALUATION-INPUTS.yaml` (Maya 20, Webex 7). "Covered" means a unit test or an eval check fails if the
behaviour breaks. It does not mean the live model gets it right; no live run has measured commits after PR #31.

| Trap | Questions that can hit it | What covers it | Verdict |
|---|---|---|---|
| A1 Webex over limit | M-S-01 t8-t10, t12; W-I-03; W-S-01 t4 | `over_limit` computed in code; `tests/test_webex.py` reuse of `T001`/`AR001`; `check_never_granted` | covered |
| A2 USD 7,500 threshold | M-S-01 t1, t9, t12; M-S-02 t3 | typed `Rule` read from the memo (`brain/rules.py`); carry checks S2 t9, t12, S4 t8 | covered offline, carry still failing in the last live run |
| A3 contract owners | none of the 27 | nothing | not reachable by the 27; no check |
| A4 restricted vs open HR docs | M-S-02 t1, t4 (caller E001 is not a manager) | audience filter at retrieval; `tests/test_retrieval.py::test_playbook_unreachable_for_non_manager...` | covered |
| A5 duplicate names | M-S-01 (Maya Cohen vs Rosen), M-S-02 t2 (Sara), W-S-01 t1-t2 (Rachel, Sara), M-I-03 | exact-name subject resolution, `ambiguous_first_name`, `arg_checks` with `equals: E001` | covered |
| A6 approval states | M-S-01 t10, W-I-03, W-S-01 t4 | `ap()` state asserts in `tests/test_webex.py`; handoff never grants | covered |
| A7 stale manager (E003 under E018, should be E013) | W-I-02 (E018 asks "my team members"); `list_direct_reports(E018)` returns Noam Sharon from the database as supplied | only `seat_assignment_status` leaves E003 out | **gap**: no code or check handles it; an answer that lists Noam as Yael's report is not caught |
| B1 equipment rules | M-I-03, M-S-01 t6 | golden `required_facts`; retrieval | model-dependent |
| B2 threshold applies to the increment | M-S-01 t9 | `cost_per_seat` and `annual_cost_above_threshold` flags; answer prompt line; test | covered offline, unmeasured live |
| B3 outdated "reconcile later" runbook line | W-I-03 (the IT KB article `webex_license_assignment.md` carries it) | nothing | **gap**: no check. A forbidden-words check would also fail an answer that correctly calls the line invalid, so it needs a sentence-level rule |
| B4 approvers differ | M-S-01 t9-t10, W-S-01 t4 | handoff names approval ids; `request_approval` reuse | partly covered; who approves what is the model's wording |
| B5 Maya's checklist (`OT005` blocked, `AR004` planned, `OT002` pending) | M-S-01 t3, t11, t12 | `tests/test_agent.py` and `tests/test_maya_checks.py` use `OT005`; `AR004` and the 2026-08-01 activation date appear in no test | partly covered |
| B6 renewal deadlines | none of the 27 | nothing | not reachable; day counts shift by one at as_of 07-01 |
| B7 authority order | M-S-02 t3 | prompt line "newer or more specific guidance overrides older" | model-dependent |
| B8, B9 security list, other thresholds | none of the 27 | nothing | not reachable |
| B10 seat assignments not recorded | W-I-02 | `test_seat_assignments_are_reported_as_a_gap`; W-I-02 wording test | covered |

Gaps worth closing offline, smallest first: (1) a W-I-02 check that Noam Sharon is not listed as E018's report, plus a code note on
`list_direct_reports` rows when an employment addendum names a different manager; (2) a sentence-level B3 check for W-I-03.
Neither is done: they change what the golden-adjacent checks fail on, so they need your call.
