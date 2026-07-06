"""Health (SA-15..17, 25) and Relationships (SA-18..20, 26) sub-agent handlers."""

from __future__ import annotations

from aether.agents.sub_agents.handlers._common import call_skill, days_until, nodes_for
from aether.models.enums import NodeStatus, NodeType, PillarName

_MED_DISCLAIMER = "Not medical advice. Consult your provider."
_CADENCE = {"close": 14, "professional": 60, "extended": 180}


# ============================ HEALTH ============================

async def medication_monitor(inputs: dict, db) -> dict:
    """SA-15: medication refills + appointment proximity; P0/P1 alerts."""
    nodes = await nodes_for(PillarName.HEALTH, db)
    medications, appointments = [], []
    alerts = 0
    for n in nodes:
        meta = n.metadata_ or {}
        cat = meta.get("category")
        if cat == "medication":
            du = days_until(meta.get("refill_date"))
            pc = "p0" if (du is not None and du <= 3) else ("p1" if (du is not None and du <= 7) else "p3")
            if pc in ("p0", "p1"):
                alerts += 1
            medications.append({"name": meta.get("name") or n.title,
                                "refill_date": meta.get("refill_date"),
                                "days_until_refill": du, "priority_class": pc})
        elif cat == "appointment":
            du = days_until(meta.get("date"))
            if du is not None and du >= 0:
                appointments.append({"title": n.title, "date": meta.get("date"), "days_until": du})
    return {"medications": medications, "upcoming_appointments": appointments,
            "alerts_created": alerts, "disclaimer": _MED_DISCLAIMER}


async def pattern_detector(inputs: dict, db) -> dict:
    """SA-16: health patterns (wraps analytical.health_pattern_detector)."""
    return await call_skill("analytical.health_pattern_detector",
                            {"session_id": inputs["session_id"], "lookback_days": 180},
                            db, inputs["session_id"], "health.pattern_detector")


async def provider_mapper(inputs: dict, db) -> dict:
    """SA-17: providers by specialty, last visit, next appointment."""
    nodes = await nodes_for(PillarName.HEALTH, db, category={"provider"})
    providers = [{
        "name": (n.metadata_ or {}).get("name") or n.title,
        "specialty": (n.metadata_ or {}).get("specialty"),
        "last_visit": (n.metadata_ or {}).get("last_visit"),
        "next_appointment": (n.metadata_ or {}).get("next_appointment"),
        "node_id": str(n.id),
    } for n in nodes]
    return {"providers": providers, "disclaimer": _MED_DISCLAIMER}


async def appointment_reminder(inputs: dict, db) -> dict:
    """SA-25: medical appointments within 7 days; P0<24h, P1<3d."""
    nodes = await nodes_for(PillarName.HEALTH, db, category={"appointment"})
    appts = []
    for n in nodes:
        meta = n.metadata_ or {}
        du = days_until(meta.get("date"))
        if du is None or du < 0 or du > 7:
            continue
        pc = "p0" if du < 1 else ("p1" if du < 3 else "p3")
        appts.append({"appointment_title": n.title, "date": meta.get("date"),
                      "provider": meta.get("provider"), "days_until": du, "priority_class": pc})
    appts.sort(key=lambda x: x["days_until"])
    return {"appointments": appts, "total": len(appts), "disclaimer": _MED_DISCLAIMER}


# ============================ RELATIONSHIPS ============================

async def commitment_tracker(inputs: dict, db) -> dict:
    """SA-18: open commitments; flag overdue."""
    nodes = await nodes_for(PillarName.RELATIONSHIPS, db, node_types=[NodeType.TASK])
    open_commitments = []
    overdue_count = 0
    for n in nodes:
        if n.status != NodeStatus.ACTIVE:
            continue
        meta = n.metadata_ or {}
        du = days_until(meta.get("deadline"))
        overdue = du is not None and du < 0
        if overdue:
            overdue_count += 1
        open_commitments.append({"node_id": str(n.id), "title": n.title,
                                 "person": meta.get("related_person"),
                                 "deadline": meta.get("deadline"), "overdue": overdue})
    return {"open_commitments": open_commitments, "overdue_count": overdue_count}


async def contact_cadence(inputs: dict, db) -> dict:
    """SA-19: people due for contact by tier cadence."""
    nodes = await nodes_for(PillarName.RELATIONSHIPS, db, node_types=[NodeType.FACT])
    contacts_due = []
    for n in nodes:
        meta = n.metadata_ or {}
        if meta.get("entity_type") != "person":
            continue
        tier = meta.get("relationship_tier", "extended")
        ds = days_until(meta.get("last_contact_date"))
        days_since = -ds if ds is not None else None
        threshold = _CADENCE.get(tier, 180)
        if days_since is not None and days_since > threshold:
            contacts_due.append({"name": meta.get("name") or n.title,
                                 "days_since": days_since, "tier": tier, "node_id": str(n.id)})
    return {"contacts_due": contacts_due, "total_due": len(contacts_due)}


async def learning_progress(inputs: dict, db) -> dict:
    """SA-20: active learning items with progress + alignment."""
    nodes = await nodes_for(PillarName.RELATIONSHIPS, db, node_types=[NodeType.GOAL],
                            category={"learning"})
    items = [{"title": n.title, "progress": (n.metadata_ or {}).get("progress"),
              "goal_alignment": (n.metadata_ or {}).get("goal_alignment"), "node_id": str(n.id)}
             for n in nodes if n.status == NodeStatus.ACTIVE]
    return {"learning_items": items, "total_active": len(items)}


async def key_date_reminder(inputs: dict, db) -> dict:
    """SA-26: important dates within 14 days."""
    nodes = await nodes_for(PillarName.RELATIONSHIPS, db)
    dates = []
    for n in nodes:
        meta = n.metadata_ or {}
        date_val = meta.get("key_date")
        if not date_val:
            continue
        du = days_until(date_val)
        if du is None or du < 0 or du > 14:
            continue
        pc = "p0" if du < 1 else ("p1" if du < 7 else "p3")
        dates.append({"person": meta.get("related_person") or meta.get("name"),
                      "date_type": meta.get("date_type", "event"), "date": date_val,
                      "days_until": du, "priority_class": pc})
    dates.sort(key=lambda x: x["days_until"])
    return {"key_dates": dates, "total": len(dates)}
