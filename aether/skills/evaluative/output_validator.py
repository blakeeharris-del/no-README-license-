"""
aether.skills.evaluative.output_validator
=============================================

SKILL-25 (Phase-0 Prompt Section 15).
"""

from __future__ import annotations

from aether.schemas.skills import ValidationResult

_VALIDATORS = {
    "free_text": lambda o: isinstance(o.get("response"), str) and isinstance(
        o.get("source_node_ids"), list
    ),
    "structured_data": None,  # handled specially below (needs required_fields)
    "write_proposal": lambda o: isinstance(o.get("proposed_node"), dict)
    and isinstance(o.get("reason"), str),
    "synthesis_diff": lambda o: isinstance(o.get("diff_report"), dict)
    and all(k in o["diff_report"] for k in ("new", "updated", "contradictions")),
    "clarification": lambda o: isinstance(o.get("question"), str) and bool(o.get("question")),
}


async def validate_output(inputs: dict, db) -> dict:
    output = inputs["output"]
    format_spec = inputs["format_spec"]
    format_type = format_spec.get("format_type")

    if output is None:
        return ValidationResult(valid=False, rejection_reason="null_output").model_dump(mode="json")

    if format_type not in _VALIDATORS:
        return ValidationResult(valid=False, rejection_reason="unknown_format_type").model_dump(
            mode="json"
        )

    if format_type == "structured_data":
        required = format_spec.get("required_fields") or []
        valid = all(output.get(f) is not None for f in required)
    else:
        valid = _VALIDATORS[format_type](output)

    if valid and format_spec.get("confidence_disclosure"):
        valid = "confidence" in output

    if valid and format_spec.get("max_length") is not None:
        valid = len(str(output)) <= format_spec["max_length"]

    if not valid:
        return ValidationResult(
            valid=False, rejection_reason=f"format_validation_failed:{format_type}"
        ).model_dump(mode="json")

    return ValidationResult(valid=True).model_dump(mode="json")
