"""
Planhat Skill — Customer Health, CSM, Tasks, Activities
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "planhat_customer_info",
        "description": (
            "Gibt Informationen aus Planhat zu einem Kunden zurück: "
            "Health Score, Phase (Onboarding/Expansion/etc.), CSM-Owner, "
            "MRR, letzte Aktivität, offene Tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Firmenname des Kunden",
                }
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "planhat_open_tasks",
        "description": "Gibt offene Tasks eines Kunden in Planhat zurück.",
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Firmenname des Kunden",
                }
            },
            "required": ["company_name"],
        },
    },
]

_BASE = "https://api.planhat.com"


def _get_headers() -> dict:
    token = os.environ.get('PLANHAT_API_TOKEN', '')
    return {'Authorization': f'Bearer {token}'}


def _norm(n: str) -> str:
    import re as _re
    n = _re.sub(r'\b(?:gmbh|ag|kg|ug|se|ltd|llc|inc|gbr|ohg|e\.?\s?k\.?|co\.?)\b|[&.,()]', ' ', (n or '').lower())
    return ' '.join(n.split())


def _find_company(name: str) -> dict | None:
    """Planhat-Firma zu einem Namen. Die Planhat-API filtert nicht nach Namen (sie liefert einfach die
    ersten Firmen), daher zuerst über den CSM-MCP (`customer_lookup` → planhat_id), sonst nur einen
    Treffer, dessen Name wirklich passt. Nie eine beliebige erste Firma zurückgeben."""
    if not name:
        return None
    try:
        from sandbox_handler import csm_customer_lookup
        matches = csm_customer_lookup(name, limit=3)
        want = _norm(name)
        exact = [m for m in matches if want and (want in _norm(m.get('name')) or _norm(m.get('name')) in want)]
        pick = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        if pick and pick.get('planhat_id'):
            resp = requests.get(f"{_BASE}/companies/{pick['planhat_id']}", headers=_get_headers(), timeout=10)
            if resp.ok and isinstance(resp.json(), dict) and resp.json().get('_id'):
                return resp.json()
    except Exception as e:
        logger.warning(f"Planhat lookup via CSM failed: {e}")
    try:
        resp = requests.get(
            f"{_BASE}/companies",
            params={'companyName': name, 'limit': 50},
            headers=_get_headers(), timeout=10,
        )
        if resp.ok:
            want = _norm(name)
            hits = [c for c in (resp.json() or []) if want and want in _norm(c.get('name'))]
            if len(hits) == 1:
                return hits[0]
    except Exception as e:
        logger.warning(f"Planhat company search failed: {e}")
    return None


def execute(tool_name: str, params: dict, context: dict) -> str:
    if tool_name == "planhat_customer_info":
        return _customer_info(params.get('company_name', ''))
    if tool_name == "planhat_open_tasks":
        return _open_tasks(params.get('company_name', ''))
    return f"Unbekanntes Tool: {tool_name}"


def _customer_info(company_name: str) -> str:
    if not company_name:
        return "Kein Firmenname angegeben."
    company = _find_company(company_name)
    if not company:
        return f"Kein Kunde '{company_name}' in Planhat gefunden."

    ph_id = company.get('_id', '')
    result = {
        "planhat_id": ph_id,
        "name": company.get('name', ''),
        "phase": company.get('phase', ''),
        "health_score": company.get('health', ''),
        "csm_owner": company.get('owner', {}).get('name', '') if isinstance(company.get('owner'), dict) else company.get('owner', ''),
        "mrr": company.get('mrr', ''),
        "nrr": company.get('nrr', ''),
        "last_activity": company.get('lastActivity', ''),
        "churn_score": company.get('churnScore', ''),
        "planhat_url": f"https://ws.planhat.com/xentral/home/69941855813dcb5e78d08519?profile=Company.{ph_id}",
        "tags": company.get('tags', []),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _open_tasks(company_name: str) -> str:
    if not company_name:
        return "Kein Firmenname angegeben."
    company = _find_company(company_name)
    if not company:
        return f"Kein Kunde '{company_name}' in Planhat gefunden."

    company_id = company.get('_id', '')
    try:
        resp = requests.get(
            f"{_BASE}/tasks",
            params={'companyId': company_id, 'status': 'open', 'limit': 10},
            headers=_get_headers(), timeout=10,
        )
        tasks = resp.json() if resp.ok else []
        if not isinstance(tasks, list):
            tasks = tasks.get('data', []) if isinstance(tasks, dict) else []
    except Exception as e:
        return f"Fehler beim Laden der Tasks: {e}"

    result = []
    for t in tasks:
        result.append({
            "title": t.get('title', ''),
            "due_date": t.get('dueDate', ''),
            "owner": t.get('owner', {}).get('name', '') if isinstance(t.get('owner'), dict) else '',
            "status": t.get('status', ''),
            "type": t.get('type', ''),
        })
    return json.dumps(result or "Keine offenen Tasks.", ensure_ascii=False, indent=2)
