"""
Chargebee Skill — Subscription & Kundendaten
"""
import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "chargebee_customer_lookup",
        "description": (
            "Sucht einen Kunden in Chargebee anhand des Firmennamens und gibt "
            "Subscription-Details zurück: Plan, Zahlweise, Laufzeit, Add-Ons, "
            "nächste Rechnung, aktiver Rabatt, Status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Firmenname des Kunden, z.B. 'Heavn Lights GmbH'",
                }
            },
            "required": ["company_name"],
        },
    },
    {
        "name": "chargebee_invoice_history",
        "description": "Gibt die letzten Rechnungen eines Kunden in Chargebee zurück.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {
                    "type": "string",
                    "description": "Chargebee Customer-ID (aus chargebee_customer_lookup)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Anzahl der Rechnungen (max 10, default 5)",
                    "default": 5,
                },
            },
            "required": ["customer_id"],
        },
    },
]


def _cb_get(path: str, params: dict = None) -> dict:
    api_key = os.environ.get('CHARGEBEE_API_KEY', '')
    site = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')
    from urllib.parse import quote as _q
    base = f"https://{site}.chargebee.com/api/v2"
    resp = requests.get(f"{base}/{path}", params=params or {}, auth=(api_key, ''), timeout=10)
    resp.raise_for_status()
    return resp.json()


def _search_customer(company_name: str) -> dict | None:
    from urllib.parse import quote as _q
    api_key = os.environ.get('CHARGEBEE_API_KEY', '')
    site = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')
    name_clean = company_name.strip()
    for fkey, fval in [
        (f"company[is]={_q(name_clean)}", None),
        (f"company[starts_with]={_q(name_clean)}", None),
    ]:
        try:
            resp = requests.get(f"{base}/customers?{fkey}&limit=5", auth=auth, timeout=10)
            if resp.ok:
                customers = resp.json().get('list', [])
                search_lower = name_clean.lower()
                for c in customers:
                    cname = (c['customer'].get('company') or '').strip().lower()
                    if cname and (search_lower in cname or cname in search_lower):
                        return c['customer']
        except Exception:
            pass
    return None


def execute(tool_name: str, params: dict, context: dict) -> str:
    if tool_name == "chargebee_customer_lookup":
        return _lookup_customer(params.get('company_name', ''))
    if tool_name == "chargebee_invoice_history":
        return _invoice_history(params.get('customer_id', ''), params.get('limit', 5))
    return f"Unbekanntes Tool: {tool_name}"


def _lookup_customer(company_name: str) -> str:
    if not company_name:
        return "Kein Firmenname angegeben."
    customer = _search_customer(company_name)
    if not customer:
        return f"Kein Kunde '{company_name}' in Chargebee gefunden."

    cid = customer['id']
    api_key = os.environ.get('CHARGEBEE_API_KEY', '')
    site = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')

    try:
        from urllib.parse import quote as _q
        resp = requests.get(f"{base}/subscriptions?customer_id[is]={_q(cid)}&limit=5", auth=auth, timeout=10)
        subs = resp.json().get('list', []) if resp.ok else []
    except Exception:
        subs = []

    result = {
        "customer_id": cid,
        "company": customer.get('company', ''),
        "email": customer.get('email', ''),
        "chargebee_url": f"https://{site}.chargebee.com/d/customers/{cid}",
        "subscriptions": [],
    }

    for s in subs:
        sub = s['subscription']
        # Plan-ID aus subscription_items (neues Modell) oder plan_id
        plan_id = sub.get('plan_id', '')
        if not plan_id:
            for item in sub.get('subscription_items', []):
                if item.get('item_type') == 'plan':
                    plan_id = item.get('item_price_id', '')
                    break
        addons = [
            item['item_price_id']
            for item in sub.get('subscription_items', [])
            if item.get('item_type') == 'addon' and item.get('unit_price', 0) > 0
        ]
        coupons = [c.get('coupon_id', '') for c in sub.get('coupons', []) if not c.get('exhausted')]
        result["subscriptions"].append({
            "id": sub['id'],
            "status": sub.get('status'),
            "plan_id": plan_id,
            "billing_period": f"{sub.get('billing_period', '')} {sub.get('billing_period_unit', '')}".strip(),
            "next_billing_at": sub.get('next_billing_at_formatted', ''),
            "current_term_end": sub.get('current_term_end_formatted', ''),
            "addons": addons,
            "active_coupons": [c for c in coupons if c],
            "mrr": sub.get('mrr_formatted', ''),
            "url": f"https://{site}.chargebee.com/d/subscriptions/{sub['id']}",
        })

    return json.dumps(result, ensure_ascii=False, indent=2)


def _invoice_history(customer_id: str, limit: int) -> str:
    if not customer_id:
        return "Keine Customer-ID angegeben."
    api_key = os.environ.get('CHARGEBEE_API_KEY', '')
    site = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')
    try:
        from urllib.parse import quote as _q
        resp = requests.get(
            f"{base}/invoices?customer_id[is]={_q(customer_id)}&limit={min(limit, 10)}&sort_by[desc]=date",
            auth=auth, timeout=10,
        )
        invoices = resp.json().get('list', []) if resp.ok else []
    except Exception as e:
        return f"Fehler beim Laden der Rechnungen: {e}"

    result = []
    for inv in invoices:
        i = inv['invoice']
        result.append({
            "id": i['id'],
            "date": i.get('date_formatted', ''),
            "status": i.get('status'),
            "total": i.get('total_formatted', ''),
            "url": f"https://{site}.chargebee.com/d/invoices/{i['id']}",
        })
    return json.dumps(result, ensure_ascii=False, indent=2)
