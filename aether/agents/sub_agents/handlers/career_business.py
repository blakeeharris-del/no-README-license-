"""Career (SA-09..11, 24) and Business (SA-12..14, 23) sub-agent handlers."""

from __future__ import annotations

import json
import logging

from aether.agents.sub_agents.handlers._common import call_skill, days_until, nodes_for, priority_for_days
from aether.models.enums import NodeStatus, PillarName
from aether.skills._llm import call_json

logger = logging.getLogger("aether.agents.sub_agents.career_business")


# ============================ CAREER ============================

async def credential_tracker(inputs: dict, db) -> dict:
    """SA-09: credentials with renewal dates; P1<60d, P0<7d."""
    nodes = await nodes_for(PillarName.CAREER, db)
    creds = []
    for n in nodes:
        meta = n.metadata_ or {}
        if meta.get("type") != "credential" and meta.get("category") != "credential":
            continue
        du = days_until(meta.get("expiry_date"))
        pc = "p0" if (du is not None and du < 7) else ("p1" if (du is not None and du < 60) else "p3")
        creds.append({"credential": n.title, "expiry_date": meta.get("expiry_date"),
                      "days_until": du, "priority_class": pc})
    return {"credentials": creds, "total": len(creds)}


async def opportunity_ranker(inputs: dict, db) -> dict:
    """SA-10: rank active opportunities by fit/urgency/alignment."""
    traj = await call_skill("analytical.career_trajectory",
                            {"session_id": inputs["session_id"]},
                            db, inputs["session_id"], "career.opportunity_ranker")
    nodes = await nodes_for(PillarName.CAREER, db, category={"opportunity"})
    if not nodes:
        return {"opportunities": []}
    system = ("Rank these career opportunities. Return ONLY JSON: "
              '{"opportunities":[{"node_id":str,"title":str,"fit_score":float,'
              '"urgency":str,"strategic_alignment":str,"rationale":str}]}')
    user = json.dumps({"trajectory": traj.get("trajectory_summary"),
                       "opportunities": [{"node_id": str(n.id), "title": n.title,
                                          "content": (n.content or "")[:300]} for n in nodes]})
    parsed = await call_json(system, user, logger=logger, max_tokens=1024) or {}
    return {"opportunities": parsed.get("opportunities", [])}


async def trajectory_assessor(inputs: dict, db) -> dict:
    """SA-11: career trajectory (wraps analytical.career_trajectory)."""
    return await call_skill("analytical.career_trajectory",
                            {"session_id": inputs["session_id"]},
                            db, inputs["session_id"], "career.trajectory_assessor")


async def skill_gap_identifier(inputs: dict, db) -> dict:
    """SA-24: credentials vs opportunity requirements (inferred gaps)."""
    nodes = await nodes_for(PillarName.CAREER, db)
    opps = [n for n in nodes if (n.metadata_ or {}).get("category") == "opportunity"]
    if not opps:
        return {"gaps": [], "confidence": "inferred"}
    system = ("Identify skill gaps between credentials and opportunity requirements. "
              'Return ONLY JSON: {"gaps":[{"opportunity_title":str,'
              '"missing_skills":[str],"evidence_nodes":[str]}]}')
    user = json.dumps({"nodes": [{"id": str(n.id), "title": n.title,
                                  "content": (n.content or "")[:300]} for n in nodes]})
    parsed = await call_json(system, user, logger=logger, max_tokens=1024) or {}
    return {"gaps": parsed.get("gaps", []), "confidence": "inferred"}


# ============================ BUSINESS ============================

async def pipeline_monitor(inputs: dict, db) -> dict:
    """SA-12: client pipeline status + revenue forecast."""
    nodes = await nodes_for(PillarName.BUSINESS, db, category={"client"})
    active = at_risk = closing = 0
    at_risk_reasons = []
    forecast = 0.0
    for n in nodes:
        meta = n.metadata_ or {}
        status = meta.get("client_status") or meta.get("stage", "active")
        if status == "active":
            active += 1
        elif status == "at_risk":
            at_risk += 1
            at_risk_reasons.append({"client": meta.get("name") or n.title,
                                    "reason": meta.get("risk_reason", "unspecified")})
        elif status == "closing":
            closing += 1
        amt = meta.get("expected_value")
        try:
            forecast += float(amt) if amt else 0.0
        except (ValueError, TypeError):
            pass
    return {"active_clients": active, "at_risk_clients": at_risk, "closing": closing,
            "revenue_forecast": forecast or None,
            "confidence": "medium" if nodes else "low", "at_risk_reasons": at_risk_reasons}


async def obligation_tracker(inputs: dict, db) -> dict:
    """SA-13: business obligation deadlines (same structure as SA-04)."""
    out = await call_skill("operational.deadline_monitor",
                           {"session_id": inputs["session_id"], "pillars": ["business"]},
                           db, inputs["session_id"], "business.obligation_tracker")
    return {"obligations": out.get("deadline_list", []),
            "p0_count": out.get("p0_count", 0), "p1_count": out.get("p1_count", 0)}


async def health_scorecard(inputs: dict, db) -> dict:
    """SA-14: business health scorecard (wraps analytical.business_health)."""
    return await call_skill("analytical.business_health",
                            {"session_id": inputs["session_id"],
                             "business_name": inputs.get("business_name")},
                            db, inputs["session_id"], "business.health_scorecard")


async def vendor_obligation_tracker(inputs: dict, db) -> dict:
    """SA-23: vendor contract renewals + payment obligations."""
    nodes = await nodes_for(PillarName.BUSINESS, db, category={"vendor"})
    items = []
    for n in nodes:
        meta = n.metadata_ or {}
        du = days_until(meta.get("deadline") or meta.get("renewal_date"))
        items.append({"vendor": meta.get("vendor_name") or n.title,
                      "deadline": meta.get("deadline") or meta.get("renewal_date"),
                      "days_until": du, "priority_class": priority_for_days(du, p1_within=30)})
    items.sort(key=lambda x: (x["days_until"] is None, x["days_until"]))
    return {"vendor_obligations": items, "total": len(items)}
