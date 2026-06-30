"""
Productive → Planhat daily sync.

Fetches tracked hours and budget utilization per company from Productive
and upserts them as metrics in Planhat.

Triggered via POST /productive-sync (called by Google Cloud Scheduler).
"""

import logging
import os
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)

PRODUCTIVE_API_TOKEN = os.environ.get('PRODUCTIVE_API_TOKEN', '').strip()
PRODUCTIVE_ORG_ID = os.environ.get('PRODUCTIVE_ORG_ID', '').strip()
PRODUCTIVE_BASE_URL = 'https://api.productive.io/api/v2'

PLANHAT_API_TOKEN = os.environ.get('PLANHAT_API_KEY', '') or os.environ.get('PLANHAT_API_TOKEN', '')
PLANHAT_BASE_URL = 'https://api.planhat.com'

CSM_TEAM_NAMES = {'CSM T1', 'CSM T2', 'CSM T3', 'CSM T4'}
SC_TEAM_NAMES = {'Solution Consulting'}


def _productive_headers() -> dict:
    return {
        'X-Auth-Token': PRODUCTIVE_API_TOKEN,
        'X-Organization-Id': PRODUCTIVE_ORG_ID,
        'Content-Type': 'application/vnd.api+json',
    }


def _planhat_headers() -> dict:
    return {'Authorization': f'Bearer {PLANHAT_API_TOKEN}'}


def _fetch_all_pages(url: str, params: dict) -> list:
    """Fetches all pages from a Productive API endpoint."""
    results = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=_productive_headers(),
            params={**params, 'page[number]': page, 'page[size]': 200},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json().get('data', [])
        results.extend(data)
        meta = resp.json().get('meta', {})
        total_pages = meta.get('total_pages', 1)
        if page >= total_pages:
            break
        page += 1
    return results


def fetch_companies_with_planhat_id() -> dict:
    """Returns {productive_company_id: planhat_company_id} for all companies that have a Planhat ID set."""
    companies = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/companies', {})
    mapping = {}
    for c in companies:
        attrs = c.get('attributes') or {}
        custom_fields = attrs.get('custom_fields') or {}
        planhat_id = custom_fields.get('planhat_id') or attrs.get('planhat_id') or ''
        if planhat_id:
            mapping[c['id']] = str(planhat_id).strip()
    logger.info(f"Productive: {len(mapping)} companies with Planhat ID found")
    return mapping


def fetch_time_entries_for_period(after: str, before: str) -> list:
    """Fetches all time entries for a given date range (YYYY-MM-DD)."""
    return _fetch_all_pages(
        f'{PRODUCTIVE_BASE_URL}/time_entries',
        {
            'filter[after]': after,
            'filter[before]': before,
        },
    )


def fetch_teams() -> dict:
    """Returns {team_id: team_name}."""
    teams = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/teams', {})
    return {t['id']: t.get('attributes', {}).get('name', '') for t in teams}


def fetch_person_team_map(team_id_to_name: dict) -> dict:
    """Returns {person_id: 'csm' | 'sc' | None}."""
    people = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/people', {})
    mapping = {}
    for person in people:
        rels = person.get('relationships') or {}
        team_data = (rels.get('team') or {}).get('data') or {}
        team_id = team_data.get('id', '')
        team_name = team_id_to_name.get(team_id, '')
        if team_name in CSM_TEAM_NAMES:
            mapping[person['id']] = 'csm'
        elif team_name in SC_TEAM_NAMES:
            mapping[person['id']] = 'sc'
        else:
            mapping[person['id']] = None
    return mapping


def fetch_deals() -> list:
    """Fetches all deals (projects) including budget fields."""
    return _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/deals', {})


def aggregate_hours_by_company(time_entries: list, person_team_map: dict) -> dict:
    """Returns {productive_company_id: {'csm': hours, 'sc': hours}}."""
    totals = {}
    for entry in time_entries:
        attrs = entry.get('attributes') or {}
        rels = entry.get('relationships') or {}
        company_id = ((rels.get('company') or {}).get('data') or {}).get('id', '')
        person_id = ((rels.get('person') or {}).get('data') or {}).get('id', '')
        if not company_id:
            continue
        team_type = person_team_map.get(person_id)
        if team_type not in ('csm', 'sc'):
            continue
        minutes = attrs.get('time', 0) or 0
        if company_id not in totals:
            totals[company_id] = {'csm': 0, 'sc': 0}
        totals[company_id][team_type] += minutes
    return {
        cid: {k: round(v / 60, 2) for k, v in hours.items()}
        for cid, hours in totals.items()
    }


def aggregate_budget_utilization_by_company(deals: list) -> dict:
    """Returns {productive_company_id: budget_utilization_pct}."""
    company_budget = {}  # {company_id: [budget_used, budget_total]}
    for deal in deals:
        attrs = deal.get('attributes') or {}
        rels = deal.get('relationships') or {}
        company_id = ((rels.get('company') or {}).get('data') or {}).get('id', '')
        if not company_id:
            continue
        budget_total = attrs.get('budget', 0) or 0
        budget_used = attrs.get('budget_spent', 0) or 0
        if budget_total <= 0:
            continue
        if company_id not in company_budget:
            company_budget[company_id] = [0, 0]
        company_budget[company_id][0] += budget_used
        company_budget[company_id][1] += budget_total

    result = {}
    for cid, (used, total) in company_budget.items():
        if total > 0:
            result[cid] = round((used / total) * 100, 1)
    return result



def run_productive_sync() -> dict:
    """Main sync function. Returns a summary dict."""
    if not PRODUCTIVE_API_TOKEN or not PRODUCTIVE_ORG_ID:
        logger.error("PRODUCTIVE_API_TOKEN or PRODUCTIVE_ORG_ID not set — skipping sync")
        return {'error': 'missing credentials'}
    if not PLANHAT_API_TOKEN:
        logger.error("PLANHAT_API_TOKEN not set — skipping sync")
        return {'error': 'missing planhat token'}

    today = date.today()
    yesterday = today - timedelta(days=1)
    # Sync current month from day 1 to yesterday — gives Planhat fresh running totals daily
    period_start = today.replace(day=1).isoformat()
    period_end = yesterday.isoformat()
    metric_date = yesterday.isoformat()

    logger.info(f"Productive sync: {period_start} → {period_end}")

    try:
        company_map = fetch_companies_with_planhat_id()
        team_id_to_name = fetch_teams()
        person_team_map = fetch_person_team_map(team_id_to_name)
        time_entries = fetch_time_entries_for_period(period_start, period_end)
        deals = fetch_deals()
    except Exception as e:
        logger.exception("Productive API fetch failed")
        return {'error': str(e)}

    hours_by_company = aggregate_hours_by_company(time_entries, person_team_map)
    budget_by_company = aggregate_budget_utilization_by_company(deals)

    synced = 0
    errors = 0

    for productive_id, planhat_id in company_map.items():
        hours = hours_by_company.get(productive_id, {})
        budget_pct = budget_by_company.get(productive_id)

        updates = {}
        if hours.get('csm') is not None:
            updates['Productive: CSM Hours'] = hours['csm']
        if hours.get('sc') is not None:
            updates['Productive: SC Hours'] = hours['sc']
        if budget_pct is not None:
            updates['Productive: Budget Utilization %'] = budget_pct

        if not updates:
            continue

        resp = requests.patch(
            f'{PLANHAT_BASE_URL}/companies/{planhat_id}',
            headers=_planhat_headers(),
            json={'custom': updates},
            timeout=15,
        )
        if resp.ok:
            synced += 1
        else:
            logger.warning(f"Planhat update failed for {planhat_id}: {resp.status_code} {resp.text[:200]}")
            errors += 1

    summary = {
        'period': f"{period_start} → {period_end}",
        'companies_mapped': len(company_map),
        'synced': synced,
        'errors': errors,
    }
    logger.info(f"Productive sync done: {summary}")
    return summary
