"""
aether.schemas.skills
=======================

Pydantic v2 schemas for the skills runtime (Phase-0 Prompt Section 6,
``schemas/skills.py``).

``SkillResult`` is the universal envelope every skill invocation
returns through ``invoke_skill()`` (Section 14 / ``aether/skills/invoker.py``).
``log_id`` always points at the corresponding ``skill_invocation_log``
row (INV-09).
"""

from __future__ import annotations

from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from aether.models.enums import PriorityClass


class SkillResult(BaseModel):
    """Universal return envelope from ``invoke_skill()``."""

    status: Literal["ok", "error", "timeout"]
    output: Optional[dict] = None
    latency_ms: int = 0
    error_detail: Optional[str] = None
    log_id: UUID


class ScoredSignal(BaseModel):
    """Output of ``cognitive.signal_scorer`` for a single signal."""

    signal: dict
    impact: int = Field(ge=0, le=5)
    time_sensitivity: int = Field(ge=0, le=5)
    confidence: int = Field(ge=0, le=5)
    noise_penalty: int = Field(ge=0, le=5)
    priority_class: PriorityClass


class ValidationResult(BaseModel):
    """Output of ``evaluative.output_validator``."""

    valid: bool
    rejection_reason: Optional[str] = None
