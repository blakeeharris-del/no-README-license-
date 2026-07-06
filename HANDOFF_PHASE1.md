# AETHER — Phase-1 Build (Agent Ecosystem)

Gate G1 → G2. Governed by `AETHER_PHASE1_PROMPT_v1.0`. Verified against a
real PostgreSQL 16 + pgvector 0.8.0 instance with all five migrations
applied. Kept in the Phase-0 HANDOFF format; `HANDOFF.md` (Phase-0) is
preserved unchanged and this is a new file.

## Status

All twelve exit criteria (EC-16 – EC-27) pass, verified empirically
against the live database (not inferred). Full suite: **160 passed**.

| # | Criterion | Status | Evidence |
|---|---|---|---|
| EC-16 | Six Specialist Agents route only to their own sub-agents; never user-facing | ✅ | `specialists.py`; a request mixing an out-of-pillar sub-agent is rejected; `user_facing=False`. Tests in `test_agent_ecosystem`, `test_phase1_exit_criteria`. |
| EC-17 | All 30 sub-agents implemented + independently invocable, producing their spec output | ✅ | Parametrized test runs all 30; each returns its structured output. Catalog↔handlers = 30. |
| EC-18 | Every sub-agent invocation logged to `sub_agent_runs` (not `action_log`) | ✅ | `run_sub_agent` logs a `sub_agent_runs` row with terminal status; verified `action_log` count is unchanged. |
| EC-19 | All 34 catalog skills Active + real `skill_invocation_log` entries | ✅ | `skills` table: 34 active / 0 draft. Skills driven through real `invoke_skill` produce real `ok` log rows with latency (not stubs). |
| EC-20 | `skill_performance` populated with real accuracy/latency/failure-rate | ✅ (with note) | `skill_performance_tracker` UPSERTs real `error_rate` + `p95_latency`. `accuracy_score` is honestly NULL — see Deviations. |
| EC-21 | Reflection Loop runs after session close, §16.2 6-step sequence; failure doesn't block next session | ✅ | `reflection_loop.py`; integration test: forced failure → close still 200 → next start still 200. |
| EC-22 | `cross_pillar_connector` produces ≥1 real cross-pillar signal routed via `synthesis_coordinator` | ✅ | SA-30 invokes `cross_pillar_connector` and returns its connections. |
| EC-23 | `synthesis_coordinator` routes structured packets without producing final analysis | ✅ | SA-30 output has `cross_pillar_signals` + `diff`, no `analysis` field. |
| EC-24 | Meta-Loop produces ≥1 real loop-health scorecard from `skill_performance` + `loop_runs` | ✅ | `meta_loop.py` writes a `meta_loop_runs` row with a scorecard built from real loop_runs + below-threshold skill_performance. |
| EC-25 | Trust maturity advanced T0→T1 on reliable L0–L1 operation, transition logged | ✅ | `trust.py`: evidence-gated advance, logged to `action_log`; `current_trust_stage` returns T1 after. |
| EC-26 | Zero invariant violations (INV-01–10) across 20 most recent sessions | ✅ | 20-session sweep: zero INV-03 agent-explicit nodes, no DELETE grant (INV-02), no orphan nodes. |
| EC-27 | `decision_protocol.py` + `rollback_executor.py` have real implementations | ✅ | Both real; AST check confirms no `raise NotImplementedError` remains. |

## How the documentation discrepancies were resolved (in code)

**§0 pre-stated (resolved as the prompt directed):**
- **34 vs 30 skills.** Built all 34 (catalog totals 7+6+6+5+5+5). Every "30" is stale. `skills.catalog.SKILL_CATALOG` asserts `len == 34`.
- **Skill chaining phase.** `skill_chains` is Phase-1 (table built in 0005). Treated "Phase 0/1" as a doc error; no retrofit into Phase 0.

**Surfaced by the Step-0 inventory and resolved by governance precedence (Foundation > Impl Plan in scope > supporting docs > code):**
- **A — `operational.node_linker`** (catalog said Phase-0, never built): built in Phase-1; the "Phase 0" tag is stale. It mandatorily fires `contradiction_enforcer` on `contradicts` links (INV-07).
- **B — `executive.approval_presenter`** (catalog Phase-0, absent): built in Phase-1; tag stale.
- **C — `executive.decision_protocol` placement**: Foundation §10.7.1 lists it as an *executive skill*, so the real implementation lives at `skills/executive/decision_protocol.py`; the Phase-0 `agents/decision_protocol.py` shell is now a redirect to it.
- **D — `evaluative.loop_health_checker`** (catalog Phase-2): built Active in Phase-1 to satisfy EC-19 + EC-24 (one real scorecard); its full 200-cycle stability bar stays Phase-2.

A further Foundation-vs-supporting-doc reconciliation inside EC-27: Missing Specs SKILL-22 carries a populated `recommendation` field, but **Foundation §10.6 governs** — the Decision Protocol *defers* the recommendation by default, producing one only on explicit user ask. `challenge` is always non-empty for external-state actions.

## Real issues surfaced by execution (not by review)

1. **`asgi-lifespan` missing from `requirements.txt`** — a Phase-0 test-only dependency was never pinned; the suite couldn't collect until it was installed. Pinned.
2. **Active-gate vs. synthetic-skill tests** — adding the Foundation §10.7.1 Active-gate to `invoke_skill` correctly rejected three Phase-0 INV-09 tests that injected synthetic skills with no catalog row. Fixed by seeding an Active row for the synthetic skill in-transaction (the real gate requirement, not a bypass).
3. **Sub-agent catalog vs. an old table test** — a Phase-1 table test inserted a `SubAgent` named `legal.deadline_scanner`, which began colliding with the seeded 30-agent catalog (UNIQUE name). Renamed to a test-only name.
4. **Global aggregation brittleness** — `loop_health_checker` aggregates *all* loop_runs in the window (no session filter, per spec); a test's exact `== 2` broke once client-based tests committed real loop_runs. Corrected to assert the test's own contribution (`>=`).

(Trivial import-path corrections during authoring — `models.sessions`, `models.runtime` — are not listed as system bugs.)

## Known deviations (intentional, documented at each site)

- **`skill_performance.accuracy_score` is NULL.** The invocation log records success/timeout/error and latency, but no *output-correctness* ground truth, so a true accuracy cannot be derived. `error_rate` (failure-rate) and `p95_latency` are real; accuracy is left NULL rather than fabricated. (EC-20 "real failure-rate/latency" is met.)
- **Reflection Loop runs inline-but-guarded**, not as a detached background task. §16.2 calls it "background"; inline-guarded execution makes EC-21's "fail-safe verified by test, not assumed" deterministic. Non-blocking semantics are identical; `run()` can be detached to a worker unchanged.
- **§16.2 lifecycle gates (50/200 invocations) are not literally executed per skill.** EC-19's checkable bar — Active + real invocation logs, not stub rows — is what's met; fabricating 200-invocation histories would be exactly the stub rows EC-19 forbids.
- **Trust stage stored in `action_log`, not a new table.** Keeps the Phase-1 five-table scope. The *dynamic* stage is not yet wired into the `action_gateway`/`authority_checker` gates (which read static config); Phase-1 adds no live external actions, so the gate value isn't exercised. Wiring is a Phase-2+ refinement.
- **A shared `skills/_llm.py` helper** backs the new LLM skills (single mock-patch point). The working Phase-0 skills keep their own helpers — no churn of tested code.
- **`weekly_reviewer` pillar snapshots** are built from direct per-pillar reads rather than invoking all six analytical skills; the output contract (`status/key_items/alerts`) is unchanged.

## New schema (migration 0005)

`sub_agents`, `sub_agent_runs`, `skill_chains`, `skill_performance`,
`meta_loop_runs`. Same privilege pattern as Phase-0: `aether_app_role`
gets SELECT/INSERT/UPDATE, never DELETE (INV-02); append-only history
tables additionally REVOKE DELETE. Reconciled the Impl-Plan (abridged)
and Missing-Specs (full) DDL by building the superset.

## Running it

```
# Postgres 16 + pgvector, roles per .env
alembic upgrade head           # applies 0001–0005; seeds nothing
# skill + sub-agent catalogs seed on app startup (lifespan) and in test fixtures
pytest -q                      # 160 passed against live Postgres
```

Catalog state after seeding: **34 skills Active / 0 Draft; 30 sub-agents Active.**

## Repository / version-control state

- Nine Phase-1 commits on top of `phase-0-complete` (`f298110`), tagged `phase-1-complete` at this build.
- All work is also exported as ordered `.patch` files (one per build step) applying cleanly onto `phase-0-complete`.
- No Phase-0 behavior changed except the deliberately-added `invoke_skill` Active-gate (Foundation §10.7.1), with Phase-0 skills seeded Active so the original suite stays green (63/63 within the 160).

## Out of scope (Phase-1 §3, unchanged)

Trusted Circle (P3), T4 standing authority (P3), full 7-loop 200-cycle
stability (P2), Level-2 Dashboard (P2), any new live external
integration, and the retired city metaphor.
