"""
aether.invariants.guards
===========================

All invariant guard functions + exception types (Phase-0 Prompt
Section 8). Called explicitly throughout the codebase. Each raises a
specific exception type on violation and logs at ERROR level (or
WARNING for INV-08's soft-limit case) before raising.

Guard-function coverage note: Section 8's header says "All 10
invariant guard functions," but only defines concrete guard functions
for INV-01 through INV-05, INV-07, and INV-08 (7 functions). The
section's own inline comments state that INV-06, INV-09, and INV-10
are architectural and have no guard function here by design:
  - INV-06 (authority model not configurable at runtime): enforced by
    the fact that `user_confirmed=True` is only ever set in one place
    in the whole codebase (the `/approve` endpoint), verified by
    static analysis, not a runtime guard call.
  - INV-09 (skill invocation always logged): enforced entirely inside
    `invoke_skill()` (aether/skills/invoker.py, Step 12) — every
    invocation is logged there; there is nothing for a separate guard
    function to check.
  - INV-10 (Action Gateway is the only external-execution path):
    enforced by a CI import-linter rule (no external API client
    imports in agents/ or skills/, except master_agent.py's Anthropic
    SDK import) — an architectural constraint on the codebase's import
    graph, not a runtime assertion.
This file therefore implements 7 guard functions, matching the section
body exactly, and is flagged here the same way the enum-count
discrepancy was flagged at the models checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aether.models.enums import ConfidenceLevel, CreatedByAgent

logger = logging.getLogger("aether.invariants")


# ─── Exception Types ────────────────────────────────────────────────────────


class InvariantViolation(RuntimeError):
    """Base class for all runtime invariant violations."""

    def __init__(self, inv_code: str, description: str, caller: str = "unknown"):
        self.inv_code = inv_code
        self.description = description
        self.caller = caller
        super().__init__(f"[{inv_code}] in {caller}: {description}")


class ConfidenceViolationError(InvariantViolation):
    """INV-03: Agent attempted to write a node with confidence='explicit'."""

    def __init__(self, created_by: str, caller: str):
        super().__init__(
            "INV-03", f"created_by='{created_by}' cannot set confidence='explicit'", caller
        )


class LoopLimitExceeded(RuntimeError):
    """INV-08: Loop hit max_iterations or max_duration_ms."""

    def __init__(self, loop_name: str, current: int, maximum: int):
        self.loop_name = loop_name
        self.current = current
        self.maximum = maximum
        super().__init__(f"Loop '{loop_name}' exceeded limit ({current}/{maximum})")


class WatchdogNotRunningError(RuntimeError):
    """GoalLoop.start() precondition: the LoopWatchdog must be healthy."""


class NodeValidationError(ValueError):
    """NodeDraft failed validation in write_node() step 1."""


class SynthesisFromError(ValueError):
    """source='synthesis' node is missing metadata.synthesis_from."""


class AuthorityViolationError(InvariantViolation):
    """INV-05: Agent attempted an action beyond its granted authority level."""

    def __init__(self, agent: str, action_type: str, level: int, caller: str):
        super().__init__(
            "INV-05", f"Agent '{agent}' attempted '{action_type}' at L{level}", caller
        )


class SkillNotFoundError(RuntimeError):
    """Requested skill name is not present in SKILL_REGISTRY."""

    def __init__(self, skill_name: str):
        super().__init__(f"Skill not found in registry: {skill_name}")


class SkillTimeoutError(RuntimeError):
    """A skill invocation exceeded its configured timeout."""

    def __init__(self, skill_name: str, timeout_ms: int):
        super().__init__(f"Skill '{skill_name}' timed out after {timeout_ms}ms")


class SkillExecutionError(RuntimeError):
    """A skill invocation raised during execution."""

    def __init__(self, skill_name: str, detail: str):
        super().__init__(f"Skill '{skill_name}' execution error: {detail}")


class LLMUnavailableError(RuntimeError):
    """Anthropic API unavailable after all retries."""


class LLMOutputParseError(RuntimeError):
    """LLM returned invalid JSON after a retry."""


class ContextPacketValidationError(ValueError):
    """Context packet is missing one or more of the 6 required sections."""


# ─── Guard Functions ─────────────────────────────────────────────────────────


def compute_hash(data: dict) -> str:
    """SHA-256 hash utility for INV-09 (inputs_hash, outputs_hash)."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


async def log_before_act(
    session_id: UUID,
    agent: str,
    action_type: str,
    input_summary: str,
    db: AsyncSession,
) -> UUID:
    """
    INV-01: INSERT an ``action_log`` row BEFORE executing any agent action.

    This INSERT is committed even if the downstream action subsequently
    fails — the log entry documents that the action was *attempted*,
    which is the whole point of Human Oversight Above All.

    Returns the new ``action_log`` row's ID.
    """
    from aether.models.logs import ActionLog
    from aether.models.enums import ActionType, AgentName

    entry = ActionLog(
        session_id=session_id,
        agent=AgentName(agent),
        action_type=ActionType(action_type),
        input_summary=(input_summary or "")[:500],
        node_ids=[],
        user_confirmed=False,
    )
    db.add(entry)
    await db.flush()
    logger.info(
        "[INV-01] Action logged",
        extra={
            "action": action_type,
            "agent": agent,
            "session_id": str(session_id),
            "log_id": str(entry.id),
        },
    )
    return entry.id


def assert_no_hard_delete(table: str, operation: str, caller: str) -> None:
    """INV-02: Raise if a DELETE is attempted on a memory table."""
    if operation.strip().upper().startswith("DELETE"):
        msg = f"Hard DELETE attempted on '{table}'. Use status='archived'."
        logger.error(f"[INV-02] {msg}")
        raise InvariantViolation("INV-02", msg, caller)


def assert_confidence_not_explicit_from_agent(
    created_by: CreatedByAgent, confidence: ConfidenceLevel, caller: str
) -> None:
    """
    INV-03: Raise ``ConfidenceViolationError`` if an agent tries to set
    ``confidence='explicit'``. Only ``created_by='user'`` (or, per
    ``NodeSource``, a ``sync_import``-sourced node) may carry
    ``confidence='explicit'``. Called in ``write_node()`` step 2.
    """
    if created_by != CreatedByAgent.USER and confidence == ConfidenceLevel.EXPLICIT:
        logger.error(
            "[INV-03] Agent attempted explicit confidence",
            extra={"created_by": created_by.value},
        )
        raise ConfidenceViolationError(created_by.value, caller)


def assert_no_speculative_pending_in_context(nodes: list, caller: str) -> None:
    """
    INV-04: Raise if any node with ``confidence='speculative'`` AND
    ``status='pending_review'`` is present in a context-packet node
    list. Called by ``context_assembler`` before returning the packet.

    NOTE: speculative nodes that are ``active`` (not
    ``pending_review``) are allowed in contradiction scans. This guard
    applies to context packets only.
    """
    violations = [
        n
        for n in nodes
        if (n.confidence == ConfidenceLevel.SPECULATIVE and str(n.status) == "pending_review")
    ]
    if violations:
        ids = [str(n.id) for n in violations]
        msg = f"Speculative pending_review nodes in context: {ids}"
        logger.error(f"[INV-04] {msg}")
        raise InvariantViolation("INV-04", msg, caller)


async def assert_has_user_approval(session_id: UUID, db: AsyncSession, caller: str) -> None:
    """
    INV-05: Raise if no ``action_log`` row with ``user_confirmed=True``
    exists for the current session. Called inside ``action_gateway()``
    before any execution attempt.
    """
    from sqlalchemy import select
    from aether.models.logs import ActionLog
    from aether.models.enums import ActionType

    result = await db.execute(
        select(ActionLog).where(
            ActionLog.session_id == session_id,
            ActionLog.action_type == ActionType.CONFIRM,
            ActionLog.user_confirmed == True,  # noqa: E712 - SQLAlchemy comparison, not identity
        )
    )
    if not result.scalars().first():
        msg = "No user_confirmed=True action_log entry for this session."
        logger.error(f"[INV-05] {msg}", extra={"session_id": str(session_id)})
        raise InvariantViolation("INV-05", msg, caller)


# INV-06 is ARCHITECTURAL — no guard function.
# user_confirmed=True is set ONLY in api/routes/session.py's /approve endpoint.
# No function in agents/ or skills/ ever sets user_confirmed=True.
# Verified by: test_inv06_user_confirmed_only_set_by_approve_endpoint (static analysis)


def assert_contradiction_not_silently_resolved(
    node_a_id: UUID,
    node_b_id: UUID,
    resolution_attempted: bool,
    resolved_by_user: bool,
    caller: str,
) -> None:
    """INV-07: Raise if a contradiction was resolved without user confirmation."""
    if resolution_attempted and not resolved_by_user:
        msg = (
            f"Contradiction between {node_a_id} and {node_b_id} "
            f"resolved without user confirmation."
        )
        logger.error(f"[INV-07] {msg}")
        raise InvariantViolation("INV-07", msg, caller)


def assert_loop_within_bounds(
    current_iter: int,
    max_iter: int,
    elapsed_ms: float,
    max_dur_ms: int,
    loop_name: str,
    caller: str,
) -> None:
    """
    INV-08: Raise ``LoopLimitExceeded`` if a loop has hit its iteration
    or duration limit. Called at the top of every loop iteration.
    Also enforced externally by the ``LoopWatchdog`` every
    ``WATCHDOG_CHECK_INTERVAL_MS`` (default 1000ms).
    """
    if current_iter >= max_iter:
        logger.warning(
            f"[INV-08] Loop '{loop_name}' hit iteration limit",
            extra={"current": current_iter, "max": max_iter},
        )
        raise LoopLimitExceeded(loop_name, current_iter, max_iter)

    if elapsed_ms >= max_dur_ms:
        logger.warning(
            f"[INV-08] Loop '{loop_name}' hit duration limit",
            extra={"elapsed_ms": elapsed_ms, "max_ms": max_dur_ms},
        )
        raise LoopLimitExceeded(loop_name, int(elapsed_ms), max_dur_ms)


# INV-09 is enforced entirely by invoke_skill() in skills/invoker.py.
# Skills do not self-log. The wrapper logs on START, COMPLETE, ERROR, TIMEOUT.

# INV-10 is ARCHITECTURAL.
# CI lint rule: no external API client imports in agents/ or skills/
# (exception: master_agent.py imports the anthropic SDK only).
# Verified by: test_inv10_no_external_imports_in_agents_or_skills_dirs
