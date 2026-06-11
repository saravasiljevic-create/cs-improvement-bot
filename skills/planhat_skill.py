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


def _find_company(name: str) -> dict | None:
    try:
        resp = requests.get(
            f"{_BASE}/companies",
            params={'companyName': name, 'limit': 5},
            headers=_get_headers(), timeout=10,
        )
        if resp.ok:
            companies = resp.json()
            if isinstance(companies, list) and companies:
                return companies[0]
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
