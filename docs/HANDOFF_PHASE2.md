# AETHER — Phase-2 Build (Loop Engine & Dashboard) — COMPLETE

Gate G2 → G3. Governed by `AETHER_PHASE2_PROMPT_v1.0`. Verified against a
live PostgreSQL instance. This is the final HANDOFF at `phase-2-complete`:
all twelve exit criteria (EC-28–EC-39) verified on a fresh image (229
tests passing, import-linter contract kept), DATA_SCHEMA regenerated
(v2.1, 17 application tables), thresholds locked (below).

> ## ⚠️ CARRIED G3 GATE CONDITIONS — READ FIRST
>
> **EC-37 and EC-39 are satisfied by HONEST SIMULATION, not by real
> elapsed evidence.** This is the tag point (`phase-2-complete`); the fact
> travels with it. Two named G3 gate-conditions remain **NOT YET
> PERFORMED**, to be run before the G3 gate is passed — the mechanisms
> exist and run unchanged on real data:
>
> - **EC-37 (three consecutive weekly Meta scorecards, no degradation):**
>   re-confirm with **three real weekly `MetaLoop.run()` runs** at
>   `window_end=now` (one per real elapsed week), not the three backdated
>   windows the Phase-2 simulation used.
> - **EC-39 (zero invariant violations across 30 sessions):** re-confirm by
>   running the **invariant sweep over the most recent 30 REAL sessions**
>   accrued in operation, not the 30 synthetic sessions the Phase-2
>   simulation built.
>
> Until both are performed on real accumulated data, EC-37 and EC-39 are
> "**satisfied-by-simulation, pending-G3-real-data**" — never report them
> as unqualified "met." (Related Phase-0 gap: EC-14's 100-session stress
> harness was never committed — see the ledger.)

## Loop layer — status

| Loop | Status | Key EC / evidence |
|---|---|---|
| Goal | operational + tested (Phase 0) | bounds force-tested (`test_goal_loop`) |
| Reflection | operational + tested | EC-21; single-pass bound now tested (`test_reflection_single_pass_bound`) |
| Correction | operational + tested | EC-30 + EC-29 (`test_correction_loop`) |
| Safety | operational + tested | EC-29 duration fail-safe forced (`test_safety_loop`) |
| Escalation | operational + tested | EC-31 measured; EC-29 (`test_escalation_loop`) |
| Shutdown | operational + tested | EC-32 forced (`test_shutdown_loop`) |
| Meta | operational (Phase 1); Path-B for EC-29 (below) | EC-24; `meta_loop_runs` |

## EC-29 interpretation rulings (cited)

### Meta — Path B (satisfied in substance; no build)

**EC-29 (Meta):** Satisfied in substance. The Meta-Loop's run record is
`meta_loop_runs`, **not** `loop_runs`, by deliberate design — Missing
Specs Vol 3 **LOOP-07** (outputs line 3022: "meta_loop_run record
(meta_loop_runs table)"; step 6 lines 3058–3059: "INSERT meta_loop_runs")
writes `meta_loop_runs` and only *reads* `loop_runs` (step 1, line 3028:
"Query loop_runs WHERE start_time > now()-lookback_days"). Meta is the
only one of the seven LOOP-0x configs with no `INSERT loop_runs`; a grep
across the specs for any `loop_type='meta'` `loop_runs` write returns
none. DATA_SCHEMA_v2.0.md (`### meta_loop_runs`, line 64) and Phase-1
(EC-24) built this table as first-class. No Foundation (§10.7.2 line 518)
or Implementation-Plan text requires Meta to emit `loop_runs`. EC-29's
"produces `loop_runs`" therefore does not literally apply to Meta; it is
met via `meta_loop_runs`. **Caveat:** Meta's config bounds
(`max_iterations: 1`, `max_duration_ms: 600000`, `blocks_other_loops:
false` — LOOP-07 lines 3005–3008) are **not** enforced via the
`loop_runs`/watchdog mechanism, an accepted consequence of the
dedicated-table modeling.

### Safety — Path A (built as a per-trigger dispatcher)

Built per Missing Specs Vol 3 **LOOP-04** + Implementation Plan §16.4:
`aether/loops/safety_loop.py`. Emits `loop_runs(loop_type='safety',
trigger=risk_type)` on each of the five risk triggers (LOOP-04 line 2835),
executes the §16.4 risk responses, and enforces the 10 s per-response
budget with a fail-safe force-termination (LOOP-04 lines 2828–2832).
Safety has **no iteration limit** by design (`max_iterations: null`, line
2768); EC-29 for Safety is proven by forcing the **duration** bound.

## Deviations flagged for the final HANDOFF

1. **Safety config-vs-schema (`max_iterations`).** LOOP-04 line 2768:
   `max_iterations: null`. But `loop_runs` has
   `CHECK (max_iterations IS NOT NULL …)` (migration 0004 /
   `ck_loop_runs_bounds_required`). Reconciliation: the safety `loop_runs`
   row stores placeholder `max_iterations = 1` (one response per trigger);
   the real, forceable bound is `max_duration_ms = 10000`. Documented at
   `safety_loop.py` module docstring.

2. **Safety escalation-type (`safety_timeout`).** LOOP-04 names a
   `type='safety_timeout'` escalation, but `EscalationType` has no such
   member. Mapped to `SAFETY_ALERT` with
   `content['safety_event']='safety_timeout'`. Documented at fix site.

3. **Safety P0 de-duplication (bug found by force, fixed).** The P0 index
   `uq_pe_p0_pending_per_node` is `NULLS NOT DISTINCT` (migration 0004
   line 235), so a session may hold at most **one node-less pending P0**.
   A runaway/invariant response escalates a node-less P0, then the
   fail-safe escalates a second (safety_timeout) — a raw insert crashed
   the fail-safe (the one path LOOP-04 says must never fail). Fixed:
   Safety P0 escalations pre-check (null-safe) and dedupe; the second
   collapses into the first, with the timeout recorded in
   `loop_run.notes` + the `action_log` surface (not lost). This is also a
   LOOP-04-vs-schema note: LOOP-04 says "INSERT p0 safety_timeout"; the
   schema permits only one node-less pending P0 per session. Regression:
   `test_failsafe_dedupes_p0_instead_of_crashing`.

## Level 2 Dashboard (AETHER_DASHBOARD_SPEC_v1.0)

Built as a live view over the Memory Layer (the spec's own framing, §1):
`aether/schemas/dashboard.py` (six-zone model + four-state vocabulary),
`aether/api/routes/dashboard.py` (`build_dashboard` assembler +
`GET /dashboard` JSON + `GET /dashboard/view` server-rendered HTML).

- **Cross-reference (v2.3 → v2.8):** the spec cites Foundation v2.3 §16;
  v2.8 §16 (line 1025) is identical — "pillar health scores, active
  deadline timelines, trust maturity status, and action queues" — and
  exit criterion #18 (line 1241) matches. No divergence; built against
  current requirements.
- **EC-34 (forced):** `test_ec34_...` seeds live nodes/approvals, asserts
  the dashboard's values equal the seed verbatim, then mutates
  (resolve approval, add node) and confirms a reload reflects the change —
  proving no caching/placeholders.
- **EC-33:** six zones present + fixed pillar order asserted; Section 7
  exclusions asserted on the rendered DOM where the medium allows (exactly
  one command input + one search input, no `<img>`/avatar, no
  `@keyframes`/animation, `overflow:hidden` desktop / no scroll, no
  box-shadow/gradient/blur, one accent color). **Flagged for Blake's
  visual review** (not claimable in a test): actual dark-theme/contrast
  appearance, that there is genuinely no idle motion when rendered in a
  browser, and true unscrolled fit at real desktop viewport sizes.

### Dashboard rulings / deviations (flagged for close-out)

1. **"New" status not emitted (ruling, Section 5).** New is defined by
   revert-on-view ("not yet seen … Reverts to On Track or Attention once
   viewed"), which needs per-item view-tracking the schema lacks. Emitting
   New as "created since last close" would make everything permanently New
   with no revert — a broken state. The assembler produces
   Attention/Scheduled/On Track from live deadline state; New stays
   reserved in the vocabulary (`DashboardStatus.NEW`) pending a
   `viewed`/`last_seen` field. Cited at `dashboard.py` `_node_status`.
2. **§16 "trust maturity status" is not a dashboard zone (divergence,
   flagged for the G3 Foundation-interpretation call).** Foundation §16
   (line 1025) lists "trust maturity status" among the Level 2 Dashboard's
   contents, but the Dashboard Spec's Section 2 defines exactly six zones
   with **no** trust-maturity zone — a divergence present in both v2.3 and
   v2.8. Built to the spec's six zones (adding a trust display would exceed
   the spec). This is a genuine Foundation-vs-supporting-doc divergence:
   resolving it (amend the spec to add a trust indicator, or amend §16 to
   drop the line) is a **G3 Foundation-interpretation decision for Blake**,
   not something to resolve silently in code.
3. **Desktop layout overflow — found in visual review, fixed.** The
   initial CSS sized `.grid` with hardcoded pixel math
   (`calc(100vh - 53px - 26px)`) that omitted the chips row and the Files
   strip's real height, so at 1440×900 the Files zone was clipped below
   the viewport (a Section-7 "no scrolling / all six zones on one
   unscrolled viewport" violation). Fixed with a flex-column layout
   (header/chips/grid[flex:1]/files) so the grid fills remaining space and
   Files is always visible. Confirmed by screenshot at 1440×900.
4. **Accent on the pending-reviews badge (borderline, flag for Blake).**
   Rule Theme: accent "only for the Attention state — nowhere else." The
   accent renders on the two Attention items (Today row, Legal tile) and
   on the header pending-reviews count badge. The badge is defensible as an
   Attention signal (pending approvals "require the user's judgment or
   action within the current session" = the Section 5 Attention
   definition), but it is the one accent use that is a count, not a
   Section-5 dot. Left as-is (not a clear violation); Blake to confirm
   whether the badge should be neutral instead.
3. **Source-freshness proxy.** No external connectors in Phase-2, so the
   header freshness indicator is bound to the nearest live checkable
   signal (a `skill_invocation_log` marked `timeout`). True connector
   freshness is Phase-3.
4. **Today is deadline-driven.** 6.3's "Executive Brief" is bound to live
   deadline nodes in deadline order (the brief's dominant priority
   signal). Full session-briefer integration (flagged nodes, synthesis
   signals) needs a session-scoped load — deferred.
5. **Memory "Entities: N" = linked-node count** (`node_links` from the
   node) as the live proxy for related items.
6. **Bug fixed (stored XSS).** `render_dashboard_html` interpolated user
   content (node titles/descriptions) into HTML unescaped. Fixed with
   `html.escape` on all user-controlled values; regression test
   `test_user_content_is_html_escaped`.

## EC-38 — Decision Protocol exercised on ≥5 decisions with confirmed accuracy

`executive.decision_protocol` confirmed real and **unchanged** (Phase-1
EC-27; Sense→Analyze→Challenge→Recommend, recommendation deferred by
default per Foundation §10.6). Exercised on five real, distinct decisions
(one per pillar) against live Postgres; each produces a real
`decision_journal` row and a genuine, sourced confirmation.

- **Accuracy-confirmation mechanism (documented schema addition,
  migration 0006):** a new `decision_journal` table — the "Decision
  Journal" the Dashboard Spec Memory zone already names, so not an
  invented concept. It records the full protocol run plus
  `confirmed_correct` / `confirmed_by` / `confirmed_at`, NULL until an
  explicit confirmation. `memory.decision_journal.confirm_decision` is the
  only path that sets them, and requires a non-empty `confirmed_by`; a DB
  CHECK (`confirmed_correct IS NULL OR confirmed_by IS NOT NULL`) forbids
  an anonymous/back-filled outcome. No accuracy score is fabricated and no
  synthetic ground-truth is generated (EC-19 preserved). App role gets
  SELECT/INSERT/UPDATE (UPDATE only for the confirmation transition),
  never DELETE. **DATA_SCHEMA_v2.0.md should be regenerated to list the
  16th application table.**
- **Spec question ruled (with citation):** "confirmed accuracy" reads as
  an **explicit, sourced confirmation act**, not an inferred outcome
  signal. Basis: Foundation §10.6 (the user confirms/asks) and Impl Plan
  §18.1 ("Only an explicit response … constitutes authorization; silence,
  inaction, and implied consent are not approval"), consistent with the
  INV-06 discipline that `user_confirmed` is set only by an explicit
  `/approve`. In production the confirming source is Blake; the test
  stands in for that user. **Alternative reading (an objective outcome
  signal) is left for Blake if he prefers it** — flagged, not silently
  chosen.
- **Observation (out of scope, decision_protocol not modified this turn):**
  `_wants_recommendation` uses a naive substring match, so an action that
  merely *describes* something as "recommended" (or contains "advise")
  falsely triggers the recommendation path instead of deferring. Minor
  correctness looseness; flagged for a future decision_protocol pass.
- **Forced assertions (`test_decision_journal_ec38.py`, 3 tests):** five
  distinct `decision_journal` rows; each has non-empty
  sense/analysis/challenge (full sequence), `deferred=True` +
  `recommendation == _DEFER` (§10.6), `approval_required=True` (task→L3),
  and `confirmed_correct=True` with `confirmed_by='blake'`; ≥5 real
  `executive.decision_protocol` `ok` invocation logs (not stubs). Plus:
  records are unconfirmed until the explicit act, and the source
  requirement is enforced both in code and by the DB CHECK.

## EC-35 — global trust-advancement ladder (T0→T1→T2→T3)

Built to Blake's approved rulings. `aether/memory/trust_state.py`
(evidence + advance, memory layer so the skills-layer gate can consume
it), `aether/agents/trust.py` (`evaluate_and_advance` converted),
migration 0007 (action_log sourced-marker CHECK).

**The three rulings, recorded with citations:**
1. **Trust is global** — one system-wide stage. The Foundation-internal
   tension is resolved in favor of **§9.2** (line 292: trust maturity is a
   single accumulated stage governing standing-permission scope; the stage
   table has no pillar dimension). **§18 criterion 20** (line 1249, "T3 in
   at least two pillars based on audited performance") is the **evidence
   basis** — the T3 bar requires confirmed accuracy in ≥2 pillars — not a
   per-pillar stage.
2. **No stage advances automatically** — T0→T1 included (no low-stakes
   exception). AETHER surfaces evidence (read-only); Blake executes.
   Basis: **DP-10** (line 211, "never granted automatically; confirmed by
   Blake").
3. **Real signals only** — no computed "trust score". Every number traces
   to real rows.

**Evidence bars — BLAKE-CONFIRMED (settled 2026-07-07; RAISABLE, not
lowerable by future ruling):**
- **T0→T1** (existing Phase-1 bar, now surfaced): ≥3 closed sessions, ≥5
  ok skill invocations.
- **T1→T2** *(Blake-confirmed — §9.2 T2 "reliable L2 operation and a record
  of accurate L3 staging")*: ≥1 Blake-confirmed decision, ≥5 clean closed
  sessions, ≥5-session zero-violation streak.
- **T2→T3** (Blake-confirmed): ≥3 `decision_journal` rows with
  `confirmed_correct=true AND confirmed_by='blake'` in **each of ≥2 distinct
  pillars**; ≥10 clean closed sessions; ≥10-session zero-violation streak;
  0 degraded skills (`skill_performance.below_threshold`). "Clean session"
  = closed with none of `loop_runs.status='forced_termination'`,
  `pending_escalations.escalation_type IN ('safety_alert','correction_exhaust')`,
  `action_log.output_summary LIKE 'authority_violation%'`.
- **`decision_journal` (EC-38) is the audited-accuracy source** for the
  per-pillar L3 evidence.
- **Threshold status (2026-07-07): the T1→T2 and T2→T3 bars are
  Blake-confirmed** — promoted from proposed to the settled operative bars.
  They remain RAISABLE, not lowerable, by future ruling. **G3 open-items
  ledger group F (Blake-tunable thresholds awaiting confirmation) is now
  CLOSED.** Only the confirmed/proposed status changed; the numbers, the
  "clean session" definition, and the real signals are unchanged, so the
  EC-35 trust-ladder tests are unaffected.

**Mechanism:** `surface_advancement_evidence(target, db)` (read-only,
advances nothing) and `execute_trust_advance(from, to, confirmed_by,
session_id, db)` (the ONLY writer) are **separate functions, separate
roles**. `execute` structurally requires a non-empty `confirmed_by`
(ValueError) AND the migration-0007 CHECK `ck_action_log_trust_marker_sourced`
makes an unsourced `trust_maturity` marker impossible at the DB level
(mirrors the `decision_journal` discipline). It refuses to advance unless
the evidence bar is met and the step is a single valid rung. The gate
reads the new stage live with no further wiring (trust-wiring 5e686de).

**Documented change to Phase-1 behavior:** `evaluate_and_advance`
(trust.py) previously **auto-advanced** T0→T1 when evidence passed. Per
ruling #2 it is now **sign-off-gated**: without `confirmed_by` it surfaces
only (returns the live stage, advances nothing); with `confirmed_by` it
delegates to `execute_trust_advance`. No production path called it
(it was never wired into session close), so no runtime regression; the
EC-25 test was adjusted to the sign-off flow (kept green), not deleted.

**Trust remains global** per ruling #1 — the stage is one action_log
marker read by `current_trust_stage`; there is no per-pillar stage.

**Open seam (not a bug):** the advance functions exist but are **not yet
exposed via an API endpoint** for Blake to invoke in a running system
(analogous to `/approve` for actions). Surfacing evidence + executing an
advance from the live app is a follow-up.

## EC-36 — standing L4 authority

Built per approved Proposal 3. `standing_authorities` table (migration
0008 — the **17th application table; DATA_SCHEMA_v2.0.md regeneration
debt**), `aether/memory/standing_authority.py` (propose/grant/check),
gateway integration in `action_gateway.py` STEP 3.

**Schema (`standing_authorities`):** `pillar` (enum), `action_type` (the
specific action the grant scopes), `bounds` (JSONB), `reversible` (BOOL,
`CHECK reversible=true`), `rationale` (NOT NULL — the written rule),
`granted_by` (NOT NULL — anti-inference guard), `granted_at`,
`renewal_date` (NOT NULL — §9.2 periodic renewal), `status`
(active/lapsed/revoked). App role SELECT/INSERT/UPDATE, never DELETE
(revoke = `status='revoked'`, INV-02).

**Propose-then-approve + structural anti-inference guarantee:**
`propose_standing_authorities` derives candidates from `_STANDING_ELIGIBLE`
— a static, hand-authored routine/bounded/reversible classification —
minus existing grants. It reads **nothing else**; the module imports
neither `action_log` nor `pending_escalations`, so approval frequency
**structurally cannot** become a proposal or a grant. A grant row exists
only via `grant_standing_authority` (requires non-empty `granted_by` +
`reversible=true`, both also schema-enforced). Reversibility is **Blake's
per-grant judgment recorded in `rationale`** (DP-08 reversibility-by-
default), CHECK-enforced.

**Gateway integration (the critical path):** STEP 3 skips per-action
approval **only** if the full conjunction holds — an `active` grant
covering `(action_name, pillar)` AND live `current_trust_stage ≥ T3` AND
within bounds AND `now() < renewal_date`. The abstract `action_type`
(read/write/confirm) still passes STEP 2's authority-level check; the
**specific `action_name`** is what a grant scopes and the T3 gate lives
entirely in the standing check. Any miss → the existing per-action
approval path, unchanged.

**INV-05 ↔ §9.2 reconciliation (flagged tension, cited):** INV-05
(line 227) says "no exception, no bypass, no implicit authorization" for
external actions; §9.2 permits standing action "**without per-action
confirmation**." Reconciled: a standing grant **IS** the "logged,
user-approved authorization" INV-05 requires — explicit (Blake-authored,
never inferred), permanent (never deleted; revoke-not-delete), user-
approved. §9.2 removes only the *per-action* step; the gateway still
**logs every execution under a grant** (the permanent per-execution
record). So standing authority is not an unauthorized/implicit bypass —
it is authorization granted in advance, and Blake's EC-36 directive +
§9.2 govern the reconciliation.

**Bug found in adversarial review, fixed at the site:** `_within_bounds`
originally **ignored** any bound key that wasn't `max_<field>` — a
**fail-open** on an authorization gate (an unenforceable bound would
silently authorize). Fixed to **fail closed**: an unrecognized bound key
denies. Regression: `test_unrecognized_bound_fails_closed`.

**Forced tests (`test_standing_authority_ec36.py`, 12):** the positive
case (valid grant + T3 + in-bounds + before-renewal → auto-approved,
proceeded with **no** per-action approval, execution logged) and **seven**
adversarial fall-throughs (lapsed, revoked, wrong pillar, wrong action,
out-of-bounds, unrecognized-bound fail-closed, trust<T3) — each blocks to
`no_approval` (falls through, never auto-approves). Plus: 3 real grants
each traceable to a written rule (granted_by=blake, reversible, rationale,
renewal); "repeated approvals create no standing authority"; and the
grant guards (empty source / non-reversible rejected in code AND by DB
CHECK).

## EC-37 / EC-39 — satisfied by honest simulation, re-confirm on real evidence at G3

Both are time/scale-dependent. Per the approved method rulings, each is
**satisfied by an honest simulation now (Phase 2), to be re-confirmed on
real elapsed evidence at G3** — the SAME mechanism runs unchanged on real
accumulated data (the hybrid re-confirmation path).

- **EC-37** (`test_ec37_meta_scorecards`): three separate `MetaLoop.run()`
  calls over three distinct non-overlapping backdated windows, each seeded
  with different sessions/loop-mixes/volumes (3/5/4 runs) — not one
  scorecard relabeled. Mechanism addition: `window_end` on
  `check_loop_health` (window `[window_end-lookback, window_end]`, default
  `now` → existing behavior unchanged). "No degradation" proven **both
  ways**: no CRITICAL anomaly in any cycle AND the key rates
  (forced_termination/safety/correction) don't worsen window-over-window.
  **Re-confirm at G3:** three real weekly runs with `window_end` defaulting
  to `now`.
- **EC-39** (`test_ec39_invariant_sweep`): 30 sessions of genuinely varied
  activity — 30 distinct (pillar, loop-trigger) pairs, mixed actions and
  confidences (stronger than EC-26's homogeneous precedent). INV-01–INV-10
  each checked by a **real** query/constraint/enforcement signal, scoped to
  the 30 swept ids (ambient-safe). Structural detectors justified as
  faithful: INV-04 = derives_from→speculative; INV-07 = contradicts link
  lacking the enforcer's "Contradiction detected" escalation; INV-09 =
  closed session still owning a RUNNING loop / spawned sub-agent. **Two
  detector bugs found and fixed while building** (a proxy would have passed
  falsely): INV-07 wrong escalation type; INV-10 substring-matched the
  gateway docstring → switched to AST. **Re-confirm at G3:** point the same
  detectors at the most recent 30 real sessions.
- ⚠️ **EC-14 has no preserved test (flagged for G3):** the Phase-0
  100-session stress harness that found the watchdog bug (HANDOFF.md) was a
  verification-time run, not committed. EC-39's committed sweep is the
  standing precedent going forward; if a 100-session real-data stress is
  wanted at G3, it must be (re)built, not inherited.

## Seams wired (Phase 2) and deferred (Phase 3)

**Wired this phase:**
- **`/close` → Shutdown STEP 1–2** — `close_session` calls
  `ShutdownLoop.terminate_active_tree` after the goal loop completes, so
  nested sub-loops/sub_agent_runs are terminated at close (INV-09 orphan
  gap closed). Verified via the real `/close` endpoint.
- **Correction STEP 6 → Escalation auto-drain** — Correction calls
  `EscalationLoop.run_for_pending` on the correction_exhaust row (accurate
  retries flow through; the EC-31 seam), replacing the manual handoff.

**Deferred to Phase 3 (with rationale):**
- **Trust-advance API endpoint (EC-35)** — the advance functions exist and
  are tested, but no `/advance` endpoint is exposed. Rationale: at T3 the
  gateway still returns `mock_executed` (no real external action until
  Phase 3), so an endpoint unlocks nothing operationally yet. A **read-only
  trust-evidence view** is added to the Dashboard Memory zone this phase
  (surfaces `surface_advancement_evidence`; advances nothing) — no new
  zone, no Section-7 exclusion violated.
- **Goal §16.1 STEP 6 → Correction hook** — NOT wired (per ruling). Wiring
  entangles the validated goal-loop/`MasterAgent` path; deferred to Phase 3.

## Schema / DATA_SCHEMA regeneration debt

Phase 2 added two application tables — **`decision_journal` (16th, EC-38)**
and **`standing_authorities` (17th, EC-36)** — plus `action_log` CHECK
`ck_action_log_trust_marker_sourced` (0007). **DATA_SCHEMA_v2.0.md must be
regenerated** (was 15 tables) at close-out. The Unit-D trust-evidence view
adds no table.

## Open seams (superseded above; retained for history)

- `/close` performs §16.6 STEP 3–5 but not STEP 1–2 (Shutdown Loop not
  yet invoked by session close). *(WIRED — see "Seams wired" above.)*
- Goal Loop's §16.1 STEP 6 correction hook not wired to the Correction Loop.
  *(Deferred to Phase 3 — see above.)*
- Correction STEP 6 "trigger escalation_loop" is a queue handoff, not a call.
- Sub-loops (correction/escalation/safety/shutdown) don't re-check
  `LoopWatchdog.is_healthy()` (enforced at GoalLoop entry).
- Shutdown STEP 2 inline force-termination deviates from §16.6's
  signal/wait/clean-exit protocol (no detached processes in Phase 2)
  (INV-09 inline force-termination deviation).
- Escalation `max_duration_ms=10000` and Shutdown `max_duration_ms=15000`
  are documented choices — §16.5/§16.6 state no duration bound; values
  chosen to sit inside the system's 10s safety envelope / EC-31's 60s.
