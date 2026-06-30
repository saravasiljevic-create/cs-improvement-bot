"""
Productive → Planhat daily sync.

Fetches tracked hours (CSM/SC split) and budget utilization per company from Productive
and upserts them as custom fields in Planhat.

Data chain: time_entry → service → deal → company → Planhat ID

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

PRODUCTIVE_PLANHAT_ID_FIELD = '122091'  # Custom field "Planhat ID" in Productive


def _productive_headers() -> dict:
    return {
        'X-Auth-Token': PRODUCTIVE_API_TOKEN,
        'X-Organization-Id': PRODUCTIVE_ORG_ID,
        'Content-Type': 'application/vnd.api+json',
    }


def _planhat_headers() -> dict:
    return {'Authorization': f'Bearer {PLANHAT_API_TOKEN}'}


def _fetch_all_pages(url: str, params: dict) -> list:
    results = []
    page = 1
    while True:
        resp = requests.get(
            url,
            headers=_productive_headers(),
            params={**params, 'page[number]': page, 'page[size]': 200},
            timeout=60,
        )
        resp.raise_for_status()
        body = resp.json()
        data = body.get('data', [])
        results.extend(data)
        total_pages = (body.get('meta') or {}).get('total_pages', 1)
        if page >= total_pages:
            break
        page += 1
    return results


def fetch_companies_with_planhat_id() -> dict:
    """Returns {productive_company_id: planhat_company_id}."""
    companies = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/companies', {})
    mapping = {}
    for c in companies:
        attrs = c.get('attributes') or {}
        custom_fields = attrs.get('custom_fields') or {}
        planhat_id = custom_fields.get(PRODUCTIVE_PLANHAT_ID_FIELD) or ''
        if planhat_id:
            mapping[c['id']] = str(planhat_id).strip()
    logger.info(f"Companies with Planhat ID: {len(mapping)}")
    return mapping


def fetch_teams() -> dict:
    """Returns {team_id: team_name}."""
    teams = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/teams', {})
    return {t['id']: (t.get('attributes') or {}).get('name', '') for t in teams}


def fetch_person_team_map(team_id_to_name: dict) -> dict:
    """Returns {person_id: 'csm' | 'sc' | None}."""
    people = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/people', {'include': 'teams'})
    mapping = {}
    for person in people:
        # 'teams' is plural and an array — a person can belong to multiple teams
        teams_data = ((person.get('relationships') or {}).get('teams') or {}).get('data') or []
        person_type = None
        for team_ref in teams_data:
            team_name = team_id_to_name.get(team_ref.get('id', ''), '')
            if team_name in CSM_TEAM_NAMES:
                person_type = 'csm'
                break
            elif team_name in SC_TEAM_NAMES:
                person_type = 'sc'
                break
        mapping[person['id']] = person_type
    return mapping


def fetch_service_to_deal_map() -> dict:
    """Returns {service_id: deal_id} by fetching all services."""
    services = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/services', {})
    mapping = {}
    for s in services:
        deal_id = (((s.get('relationships') or {}).get('deal') or {}).get('data') or {}).get('id', '')
        if deal_id:
            mapping[s['id']] = deal_id
    logger.info(f"Services mapped to deals: {len(mapping)}")
    return mapping


def fetch_deal_to_company_map_and_budgets() -> tuple[dict, dict]:
    """
    Returns:
      deal_to_company: {deal_id: company_id}
      budget_by_company: {company_id: budget_utilization_pct}  (aggregated across all deals)

    Budget is time-based: worked_time / budgeted_time * 100.
    Requires include=company to get company ID from deal relationship.
    """
    deals = _fetch_all_pages(f'{PRODUCTIVE_BASE_URL}/deals', {'include': 'company'})
    deal_to_company = {}
    company_budgets = {}  # {company_id: [worked_minutes, budgeted_minutes]}

    for deal in deals:
        attrs = deal.get('attributes') or {}
        rels = deal.get('relationships') or {}
        company_id = ((rels.get('company') or {}).get('data') or {}).get('id', '')
        if not company_id:
            continue

        deal_to_company[deal['id']] = company_id

        # Use time-based budget: budgeted_time (planned) vs worked_time (actual), both in minutes
        budgeted_time = attrs.get('budgeted_time') or 0
        worked_time = attrs.get('worked_time') or 0

        if budgeted_time > 0:
            if company_id not in company_budgets:
                company_budgets[company_id] = [0, 0]
            company_budgets[company_id][0] += worked_time
            company_budgets[company_id][1] += budgeted_time

    budget_by_company = {}
    for cid, (worked, budgeted) in company_budgets.items():
        if budgeted > 0:
            budget_by_company[cid] = round((worked / budgeted) * 100, 1)

    logger.info(f"Deals mapped to companies: {len(deal_to_company)}, companies with budget: {len(budget_by_company)}")
    return deal_to_company, budget_by_company


def fetch_time_entries_for_period(after: str, before: str) -> list:
    return _fetch_all_pages(
        f'{PRODUCTIVE_BASE_URL}/time_entries',
        {'filter[after]': after, 'filter[before]': before, 'include': 'person,service'},
    )


def aggregate_hours_by_company(
    time_entries: list,
    person_team_map: dict,
    service_to_deal: dict,
    deal_to_company: dict,
) -> dict:
    """Returns {company_id: {'csm': hours, 'sc': hours}}."""
    totals = {}
    skipped = 0
    for entry in time_entries:
        attrs = entry.get('attributes') or {}
        rels = entry.get('relationships') or {}

        person_id = ((rels.get('person') or {}).get('data') or {}).get('id', '')
        service_id = ((rels.get('service') or {}).get('data') or {}).get('id', '')

        team_type = person_team_map.get(person_id)
        if team_type not in ('csm', 'sc'):
            continue

        deal_id = service_to_deal.get(service_id, '')
        company_id = deal_to_company.get(deal_id, '') if deal_id else ''

        if not company_id:
            skipped += 1
            continue

        minutes = attrs.get('time', 0) or 0
        if company_id not in totals:
            totals[company_id] = {'csm': 0, 'sc': 0}
        totals[company_id][team_type] += minutes

    if skipped:
        logger.info(f"Time entries skipped (no company chain): {skipped}")

    return {
        cid: {k: round(v / 60, 2) for k, v in hours.items()}
        for cid, hours in totals.items()
    }


def run_productive_sync() -> dict:
    if not PRODUCTIVE_API_TOKEN or not PRODUCTIVE_ORG_ID:
        logger.error("PRODUCTIVE_API_TOKEN or PRODUCTIVE_ORG_ID not set")
        return {'error': 'missing credentials'}
    if not PLANHAT_API_TOKEN:
        logger.error("PLANHAT_API_TOKEN not set")
        return {'error': 'missing planhat token'}

    today = date.today()
    yesterday = today - timedelta(days=1)
    period_start = today.replace(day=1).isoformat()
    period_end = yesterday.isoformat()

    logger.info(f"Productive sync: {period_start} → {period_end}")

    try:
        company_map = fetch_companies_with_planhat_id()
        team_id_to_name = fetch_teams()
        person_team_map = fetch_person_team_map(team_id_to_name)
        service_to_deal = fetch_service_to_deal_map()
        deal_to_company, budget_by_company = fetch_deal_to_company_map_and_budgets()
        time_entries = fetch_time_entries_for_period(period_start, period_end)
    except Exception as e:
        logger.exception("Productive API fetch failed")
        return {'error': str(e)}

    logger.info(f"Time entries fetched: {len(time_entries)}")

    hours_by_company = aggregate_hours_by_company(
        time_entries, person_team_map, service_to_deal, deal_to_company
    )

    logger.info(f"Companies with hours: {len(hours_by_company)}, companies with budget: {len(budget_by_company)}")

    synced = 0
    errors = 0
    skipped_no_data = 0

    for productive_id, planhat_id in company_map.items():
        hours = hours_by_company.get(productive_id, {})
        budget_pct = budget_by_company.get(productive_id)

        updates = {}
        csm_h = hours.get('csm', 0)
        sc_h = hours.get('sc', 0)

        # Always write hours if there are any CSM/SC entries for this company
        if productive_id in hours_by_company:
            updates['Productive: CSM Hours'] = csm_h
            updates['Productive: SC Hours'] = sc_h
        if budget_pct is not None:
            updates['Productive: Budget Utilization %'] = budget_pct

        if not updates:
            skipped_no_data += 1
            continue

        resp = requests.put(
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
        'companies_with_hours': len(hours_by_company),
        'companies_with_budget': len(budget_by_company),
        'synced': synced,
        'skipped_no_data': skipped_no_data,
        'errors': errors,
    }
    logger.info(f"Productive sync done: {summary}")
    return summary
