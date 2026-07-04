"""tests.agents.test_master_agent — Phase-0 Prompt Section 23."""

from __future__ import annotations

import json

import pytest

from aether.agents.master_agent import MasterAgent


def _intent_json(**overrides):
    base = {
        "action_type": "query", "subject": "test", "implied_pillars": ["legal"],
        "urgency": "standard", "time_horizon": None, "entities": [],
    }
    base.update(overrides)
    return json.dumps(base)


def _reasoning_json(text="Response.", **overrides):
    base = {
        "response": text, "write_proposals": [], "action_requests": [],
        "confidence": "explicit", "source_node_ids": [], "warnings": [],
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.asyncio
async def test_agent_routes_direct_mode_single_pillar(db_session, test_session_row, mock_llm_client):
    from aether.schemas.agent import UserIntent

    intent = UserIntent(raw_input="x", action_type="query", subject="x", implied_pillars=["legal"], urgency="standard", ambiguity_flag=False)
    assert MasterAgent._select_routing_mode(intent) == "direct"


@pytest.mark.asyncio
async def test_agent_routes_orchestrated_mode_multi_pillar():
    from aether.schemas.agent import UserIntent

    intent = UserIntent(raw_input="x", action_type="query", subject="x", implied_pillars=["legal", "business"], urgency="standard", ambiguity_flag=False)
    assert MasterAgent._select_routing_mode(intent) == "orchestrated"


@pytest.mark.asyncio
async def test_agent_routes_synthesis_mode():
    from aether.schemas.agent import UserIntent

    intent = UserIntent(raw_input="x", action_type="synthesize", subject="x", implied_pillars=["legal"], urgency="standard", ambiguity_flag=False)
    assert MasterAgent._select_routing_mode(intent) == "synthesis"


@pytest.mark.asyncio
async def test_agent_routes_direct_write_mode():
    from aether.schemas.agent import UserIntent

    intent = UserIntent(raw_input="x", action_type="write", subject="x", implied_pillars=["legal"], urgency="standard", ambiguity_flag=False)
    assert MasterAgent._select_routing_mode(intent) == "direct_write"


def test_estimate_authority_mapping():
    """
    Direct test of the new _estimate_authority() design (closes the
    EC-09 gap). Grounded in Foundation §9.1's L0-L5 levels — see the
    function's own docstring for the full justification per action_type.
    """
    from aether.schemas.agent import UserIntent

    cases = {
        "query": 0, "review": 0, "clarify": 0,
        "synthesize": 1, "write": 2, "task": 3,
    }
    for action_type, expected_level in cases.items():
        intent = UserIntent(raw_input="x", action_type=action_type, subject="x", implied_pillars=["legal"], urgency="standard", ambiguity_flag=False)
        assert MasterAgent._estimate_authority(intent) == expected_level, action_type


@pytest.mark.asyncio
async def test_agent_routes_challenge_and_prepare_mode(db_session, test_session_row):
    """
    Previously only reachable by bypassing MasterAgent and passing
    routing_mode to context_assembler explicitly (the
    estimated_authority gap — see MasterAgent._estimate_authority()).
    Now verifies MasterAgent's own routing logic reaches this mode
    directly for any task-type intent.
    """
    from aether.schemas.agent import UserIntent

    intent = UserIntent(raw_input="x", action_type="task", subject="x", implied_pillars=["legal"], urgency="standard", ambiguity_flag=False)
    assert MasterAgent._select_routing_mode(intent) == "challenge_and_prepare"

    # And confirm context_assembler actually honors it end-to-end when
    # MasterAgent passes it through.
    from aether.skills.operational.context_assembler import assemble_context

    intent_dict = {"raw_input": "x", "action_type": "task", "subject": "x", "implied_pillars": ["legal"],
                   "urgency": "standard", "entities": [], "ambiguity_flag": False}
    routing_mode = MasterAgent._select_routing_mode(intent)
    packet = await assemble_context(
        {"intent": intent_dict, "session_id": test_session_row.id, "routing_mode": routing_mode}, db_session
    )
    assert packet["active_pillar"]["routing_mode"] == "challenge_and_prepare"


@pytest.mark.asyncio
async def test_agent_returns_approval_request_not_gateway_call(db_session, test_session_row, mock_llm_client, monkeypatch):
    """
    Patches the SKILL_REGISTRY entry directly, not the source module
    attribute. Real gotcha found running the full suite together:
    invoke_skill()'s import of SKILL_REGISTRY is deferred (inside the
    function body, to avoid a circular import), so it only actually
    executes the *first* time invoke_skill() is ever called in the
    whole test process. Patching
    ``aether.skills.operational.action_gateway.action_gateway_skill``
    directly is unsafe: if that first real invoke_skill() call happens
    while such a patch is active, registry.py's own
    ``from action_gateway import action_gateway_skill`` permanently
    captures the *mocked* function into SKILL_REGISTRY for the rest of
    the process — contaminating every later test that relies on the
    real skill. Patching the dict entry itself (as done here, and in
    the INV-09 tests) avoids this entirely.
    """
    from aether.skills.registry import SKILL_REGISTRY

    called = {"gateway": False}

    async def fake_gateway(inputs, db):
        called["gateway"] = True
        return {}

    monkeypatch.setitem(SKILL_REGISTRY, "operational.action_gateway", fake_gateway)

    mock_llm_client.set_responses([
        _intent_json(action_type="task"),
        _reasoning_json(
            text="Approval needed.",
            action_requests=[{"action": "send_email", "target": "x", "amount_or_consequence": "n/a",
                                "timing": "now", "authority_level": 3, "risk_level": "low"}],
        ),
    ])
    response = await MasterAgent().process("please send this email", test_session_row.id, db_session)
    assert len(response.pending_approvals) == 1
    assert called["gateway"] is False


@pytest.mark.asyncio
async def test_agent_surfaces_contradiction_in_response_flagged_items(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([
        _intent_json(),
        _reasoning_json(
            text="Here's a proposal.",
            write_proposals=[{"proposed_node": {"type": "fact", "title": "x", "content": "y"}, "reason": "stated"}],
        ),
    ])
    response = await MasterAgent().process("remember this", test_session_row.id, db_session)
    assert len(response.flagged_items) == 1
    assert response.nodes_written == []


@pytest.mark.asyncio
async def test_agent_returns_clarification_on_ambiguous_input(db_session, test_session_row, mock_llm_client):
    mock_llm_client.set_responses([
        json.dumps({
            "action_type": "clarify", "subject": "unclear", "implied_pillars": ["legal"],
            "urgency": "standard", "time_horizon": None, "entities": [],
            "ambiguity_flag": True, "clarification": "Which matter?",
        }),
    ])
    response = await MasterAgent().process("do the thing", test_session_row.id, db_session)
    assert response.text == "Which matter?"
