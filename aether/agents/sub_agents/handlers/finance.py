"""Finance pillar sub-agent handlers (SA-05, 06, 07, 08, 21, 27)."""

from __future__ import annotations

import json
import logging

from aether.agents.sub_agents.handlers._common import call_skill, days_until, nodes_for, priority_for_days
from aether.models.enums import NodeType, PillarName
from aether.skills._llm import call_json

logger = logging.getLogger("aether.agents.sub_agents.finance")


def _num(raw):
    try:
        return float(str(raw).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


async def net_worth_calculator(inputs: dict, db) -> dict:
    """SA-05: dated net worth snapshot (wraps analytical.financial_net_worth)."""
    return await call_skill("analytical.financial_net_worth",
                            {"session_id": inputs["session_id"],
                             "as_of_date": inputs.get("as_of_date")},
                            db, inputs["session_id"], "finance.net_worth_calculator")


async def cash_flow_monitor(inputs: dict, db) -> dict:
    """SA-06: income vs expense summary + anomalies + upcoming bills."""
    nodes = await nodes_for(PillarName.PERSONAL_FINANCE, db)
    income = expense = 0.0
    upcoming_bills, anomalies = [], []
    for n in nodes:
        meta = n.metadata_ or {}
        amt = _num(meta.get("amount"))
        cat = meta.get("category")
        if cat == "income" and amt:
            income += amt
        elif cat == "expense" and amt:
            expense += amt
        if meta.get("deadline") and cat in ("bill", "expense"):
            du = days_until(meta.get("deadline"))
            if du is not None and du >= 0:
                upcoming_bills.append({"node_id": str(n.id), "title": n.title,
                                       "deadline": meta.get("deadline"), "days_until": du})
        if meta.get("anomaly") is True:
            anomalies.append({"node_id": str(n.id), "description": n.title})
    return {"income_total": income, "expense_total": expense,
            "net_cash_flow": income - expense, "upcoming_bills": upcoming_bills,
            "anomalies": anomalies, "confidence": "medium" if nodes else "low"}


async def deadline_scanner(inputs: dict, db) -> dict:
    """SA-07: finance deadlines (tax/payment/insurance), scored."""
    out = await call_skill("operational.deadline_monitor",
                           {"session_id": inputs["session_id"], "pillars": ["personal_finance"]},
                           db, inputs["session_id"], "finance.deadline_scanner")
    return {"deadlines": out.get("deadline_list", []),
            "p0_count": out.get("p0_count", 0), "p1_count": out.get("p1_count", 0)}


async def projection_builder(inputs: dict, db) -> dict:
    """SA-08: financial projection — ALWAYS speculative, with disclaimer."""
    horizon = int(inputs.get("projection_horizon_months", 12))
    nw = await call_skill("analytical.financial_net_worth",
                          {"session_id": inputs["session_id"]},
                          db, inputs["session_id"], "finance.projection_builder")
    system = (
        "Build a conservative financial projection. Return ONLY JSON: "
        '{"projection_periods":[{"period":str,"net_worth_estimate":float,'
        '"cash_flow_estimate":float}],"assumptions":[str]}'
    )
    parsed = await call_json(system, json.dumps({"current_net_worth": nw.get("net_worth"),
                                                 "horizon_months": horizon}),
                             logger=logger, max_tokens=1024) or {}
    return {
        "projection_periods": parsed.get("projection_periods", []),
        "assumptions": parsed.get("assumptions", ["Based on current nodes only."]),
        "confidence": "speculative",  # ALWAYS
        "disclaimer": "This is a financial estimate, not advice. Assumptions shown.",
    }


async def tax_deadline_scanner(inputs: dict, db) -> dict:
    """SA-21: tax filing deadlines; P1<60d, P0<14d."""
    nodes = await nodes_for(PillarName.PERSONAL_FINANCE, db, category={"tax"})
    items = []
    for n in nodes:
        du = days_until((n.metadata_ or {}).get("deadline"))
        pc = "p0" if (du is not None and du < 14) else ("p1" if (du is not None and du < 60) else "p3")
        items.append({"node_id": str(n.id), "title": n.title,
                      "deadline": (n.metadata_ or {}).get("deadline"),
                      "days_until": du, "priority_class": pc})
    items.sort(key=lambda x: (x["days_until"] is None, x["days_until"]))
    return {"tax_deadlines": items, "total": len(items)}


async def insurance_expiry_scanner(inputs: dict, db) -> dict:
    """SA-27: insurance policies approaching renewal/expiry."""
    nodes = await nodes_for(PillarName.PERSONAL_FINANCE, db, category={"insurance"})
    items = []
    for n in nodes:
        meta = n.metadata_ or {}
        du = days_until(meta.get("expiry_date") or meta.get("deadline"))
        items.append({"policy_name": meta.get("policy_name") or n.title,
                      "expiry_date": meta.get("expiry_date") or meta.get("deadline"),
                      "days_until": du, "p_class": priority_for_days(du, p1_within=45)})
    items.sort(key=lambda x: (x["days_until"] is None, x["days_until"]))
    return {"policies": items, "total": len(items)}
