# AETHER — Phase-2 Build (Loop Engine & Dashboard) — WORK IN PROGRESS

Gate G2 → G3. Governed by `AETHER_PHASE2_PROMPT_v1.0`. Verified against a
live PostgreSQL instance. This is a running ledger accumulated as Phase-2
work lands; it becomes the final HANDOFF at `phase-2-complete`.

## Loop layer — status so far

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
2. **§16 "trust maturity status" is not a dashboard zone.** The Dashboard
   Spec's Section 2 defines exactly six zones (no trust-maturity zone) —
   true in both v2.3 and v2.8. Built to the spec's six zones; adding a
   trust display would exceed the spec. Observation for Blake if he wants
   §16's trust-maturity line reflected (would be a spec amendment).
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

## Open seams (from the consolidation checkpoint — not yet wired)

- `/close` performs §16.6 STEP 3–5 but not STEP 1–2 (Shutdown Loop not
  yet invoked by session close).
- Goal Loop's §16.1 STEP 6 correction hook not wired to the Correction Loop.
- Correction STEP 6 "trigger escalation_loop" is a queue handoff, not a call.
- Sub-loops (correction/escalation/safety/shutdown) don't re-check
  `LoopWatchdog.is_healthy()` (enforced at GoalLoop entry).
- Shutdown STEP 2 inline force-termination deviates from §16.6's
  signal/wait/clean-exit protocol (no detached processes in Phase 2)
  (INV-09 inline force-termination deviation).
- Escalation `max_duration_ms=10000` and Shutdown `max_duration_ms=15000`
  are documented choices — §16.5/§16.6 state no duration bound; values
  chosen to sit inside the system's 10s safety envelope / EC-31's 60s.
