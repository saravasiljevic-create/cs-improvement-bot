"""
Vertragsanpassungs-Flow: Intent-Erkennung, Parsing, Chargebee-Lookup (read-only),
und Zusammenfassungs-Builder.

Der Bot schreibt NICHTS in Chargebee — er erstellt nur eine strukturierte Zusammenfassung
mit Link zur Subscription und kontextuellen Hinweisen basierend auf dem IST-Zustand.
"""
import logging
import re
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

# CS Admin Slack User IDs für @-Mentions in der Subscription-Warnung
# Mirjam Köberlein, Linda Litzkow, Sara Vasiljevic
_CS_ADMIN_IDS = ('U07G83YH6RW', 'U092RN6D339', 'U07TRKK8BH9')

# ---------------------------------------------------------------------------
# Intent Detection
# ---------------------------------------------------------------------------

# Starke Signale — 3 Punkte je Treffer
_STRONG = [
    r'vertrags\s*anpassung',
    r'vertrags\s*[äa]nderung',
    r'vertrags\s*verlängerung',
    r'vertragswechsel',
    r'unterschriebene[snm]?\s+angebot',
    r'unterzeichnetes?\s+angebot',          # "unterzeichnete Angebot" (Synonym)
    r'angebot.{0,60}unterschrieben',
    r'unterschrieben.{0,60}angebot',
    r'angebot.{0,60}unterzeichnet',
    r'unterzeichnet.{0,60}angebot',         # "unterzeichnete Angebot: https://..."
    r'signed\s+(?:offer|contract|proposal)',
]

# Mittlere Signale — 1 Punkt je Treffer
_MEDIUM = [
    r'\bplan\b.{0,25}(?:wechsel|change|upgrade|downgrade|umstell|änder)',
    r'(?:wechsel|upgrade|downgrade|umstell|änder).{0,25}\bplan\b',
    r'add[\s\-]?on.{0,20}(?:hinzufüg|entfern|dazu|weg)',
    r'(?:jährlich|monatlich|annual|monthly).{0,30}(?:wechsel|umstell|zahlung)',
    r'\bramp\b',
    r'abo[\s\-](?:wechsel|änder|anpass)',
    r'anpassung\s+(?:vornehmen|vorgenommen|gemacht|rückgängig|zurück)',
    r'rückgängig\s+machen',
    r'(?:monatlich|jährlich)\w*\s+(?:miete|gebühr|preis|beitrag)',
    r'(?:subscription|abo|vertrag|konditionen)\s+(?:ändern|anpassen|wechseln|korrigieren)',
    r'könnt?\s+(?:ihr|sie).{0,30}(?:ändern|anpassen|korrigieren|umstellen)',
    # Upgrade/Downgrade-Signale
    r'\bupgrad\w*\b',                       # "upgrade", "upgraden", "upgradet"
    r'grad\w*\s+.{0,80}\s+up\b',           # "gradet ... auf ... up" (DE Anglizismus)
    r'(?:auf|zum?)\s+(?:das?\s+)?(?:pro|premium|enterprise|business|starter|growth)\s+(?:paket|plan|tarif|abo)',
    r'(?:paket|tarif|plan)\s+(?:up\b|upgrade)',
    # Verlängerungs-Signale
    r'verlängerung.{0,50}vertrags?',
    r'vertrags?.{0,30}verlänger\w*',
    r'\d+[\s\-]?jahres?[\s\-]?umstellung',
    r'\d+\s*(?:monats?|jahres?)\s*verlängerung',
    r'um\s+\d+\s+(?:jahre?|monate?)\s+verlänger',
    r'verlänger\w*\s+um\s+\d+',
]


def detect_vertragsanpassung(text: str) -> bool:
    """Gibt True zurück wenn der Text mit hoher Konfidenz eine Vertragsanpassungs-Anfrage ist."""
    if '#improvement' in text.lower():
        return False
    t = text.lower()
    score = sum(3 for p in _STRONG if re.search(p, t))
    score += sum(1 for p in _MEDIUM if re.search(p, t))
    return score >= 3


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(
    r'\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\b'
    r'|\b(\d{1,2}\.\s*'
    r'(?:januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember'
    r'|jan|feb|mär|apr|jun|jul|aug|sep|okt|nov|dez)\w*\.?\s*\d{2,4})\b',
    re.IGNORECASE,
)
_URL_RE = re.compile(r'https?://\S+')
_PAYMENT_RE = re.compile(
    r'\b(jährlich|monatlich|annual(?:ly)?|yearly|monthly|quarterly|quartalsweise)\b',
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    r'\b(?:growth\s*[mlxs]?|pro\s*(?:25|2025)?(?:\s*legacy)?|starter|enterprise'
    r'|basic|premium|scale|connect(?:\s*only)?)\b',
    re.IGNORECASE,
)
_CUSTOMER_LABELED_RE = re.compile(
    # Colon/dash optional: matches both "Kunde: X" and "Kunde X"
    r'\b(?:kunde[n]?|kundschaft|customer|company|firma|unternehmen)\b'
    r'\s*[:\-]?\s*'
    r'(.+?)(?=\s+(?:soll|hat|möchte|will|kann|wünscht|bittet|muss|ist|wurde|werden)|\n|,|$)',
    re.IGNORECASE,
)
_COMPANY_SUFFIX_RE = re.compile(
    r'([A-ZÄÖÜ][a-zA-ZäöüÄÖÜ\s&.\-]{1,40}'
    r'(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?)',
)
# Nur "Artikel + häufiges Nomen" am Anfang entfernen ("Der Kunde X" → "X")
# "The Glow GmbH" bleibt unverändert — "The" ohne folgendes Nomen wird NICHT gestripped
_STRIP_COMPANY_PREFIX_RE = re.compile(
    r'^(?:der|die|das|den|dem|des|ein|eine|the)\s+'
    r'(?:kunde[n]?|kund|klient|unternehmen|firma|company|client)\s+',
    re.IGNORECASE,
)
_BERICHTSWESEN_RE = re.compile(r'berichtswesen\s*(?:tier)?\s*[:\-]?\s*(\d+)', re.IGNORECASE)
_ADDON_ADD_RE = re.compile(
    r'add[\s\-]?ons?\s+(?:hinzufügen?|dazunehmen?|add)[:\s]+([^\n;,]+)',
    re.IGNORECASE,
)
_ADDON_REMOVE_RE = re.compile(
    r'add[\s\-]?ons?\s+(?:entfernen?|weg|remove|raus)[:\s]+([^\n;,]+)',
    re.IGNORECASE,
)

_VALID_TIERS = {1, 31, 251, 501}
_TIER_CORRECTIONS = {250: 251, 500: 501, 30: 31, 0: 1}


def parse_vertragsanpassung(text: str) -> dict:
    """Extrahiert strukturierte Felder aus einer Vertragsanpassungs-Anfrage im Freitext."""
    result: dict = {}

    m = _CUSTOMER_LABELED_RE.search(text)
    if m:
        raw = m.group(1).strip()
        result['customer_name'] = _STRIP_COMPANY_PREFIX_RE.sub('', raw).strip()
    else:
        m = _COMPANY_SUFFIX_RE.search(text)
        if m:
            raw = m.group(1).strip()
            result['customer_name'] = _STRIP_COMPANY_PREFIX_RE.sub('', raw).strip()

    urls = _URL_RE.findall(text)
    if urls:
        result['offer_link'] = urls[0]

    m = _DATE_RE.search(text)
    if m:
        result['effective_date'] = (m.group(1) or m.group(2) or '').strip()

    m = _PAYMENT_RE.search(text)
    if m:
        raw = m.group(1).lower()
        if any(x in raw for x in ('jähr', 'annual', 'yearly')):
            result['payment_type'] = 'jährlich'
        elif any(x in raw for x in ('monatl', 'monthly')):
            result['payment_type'] = 'monatlich'
        else:
            result['payment_type'] = 'quartalsweise'

    m = _PLAN_RE.search(text)
    if m:
        result['new_plan'] = m.group(0).strip()

    m = _BERICHTSWESEN_RE.search(text)
    if m:
        raw_tier = int(m.group(1))
        corrected = _TIER_CORRECTIONS.get(raw_tier, raw_tier)
        result['berichtswesen_tier'] = corrected if corrected in _VALID_TIERS else raw_tier
        if corrected != raw_tier and corrected in _VALID_TIERS:
            result['berichtswesen_tier_original'] = raw_tier

    m = _ADDON_ADD_RE.search(text)
    if m:
        result['addons_add'] = m.group(1).strip()
    m = _ADDON_REMOVE_RE.search(text)
    if m:
        result['addons_remove'] = m.group(1).strip()

    if re.search(r'\bdiscount\b|\brabatt\b|\bnachlass\b|\bgutschrift\b', text, re.IGNORECASE):
        result['has_discount'] = True

    return result


def missing_va_fields(parsed: dict) -> list[str]:
    out = []
    if not parsed.get('customer_name'):
        out.append('*Kundenname* — welches Unternehmen?')
    if not parsed.get('new_plan'):
        out.append('*Neuer Plan* — z.B. "Pro 25 – 12 Monate jährlich"')
    if not parsed.get('payment_type'):
        out.append('*Zahlweise* — jährlich oder monatlich?')
    if not parsed.get('effective_date'):
        out.append('*Vertragsbeginn / Effective Date* — ab wann gilt die Änderung?')
    # offer_link ist optional — wird in der Summary angezeigt wenn vorhanden
    return out


# ---------------------------------------------------------------------------
# Chargebee + Planhat Lookup (read-only)
# ---------------------------------------------------------------------------

def _ts_to_date(ts: int | None) -> str:
    if not ts:
        return ''
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%d.%m.%Y')


def _chargebee_customer_search(base: str, auth: tuple, customer_name: str) -> list:
    """Versucht mehrere Suchstrategien um einen Chargebee-Kunden zu finden.

    Chargebee v2 unterstützt nur 'company[is]' und 'company[starts_with]' —
    KEIN 'company[contains]'. Deshalb nutzen wir starts_with als Hauptstrategie.
    """
    name_no_suffix = re.sub(
        r'\s*(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?\s*$',
        '', customer_name, flags=re.IGNORECASE,
    ).strip()

    first_word = customer_name.split()[0] if customer_name else ''
    strategies = [
        ('company[is]', customer_name),
        ('company[starts_with]', customer_name),
        ('company[starts_with]', name_no_suffix),
        ('company[starts_with]', first_word),   # Fallback auf erstes Wort (z.B. "wev")
    ]
    search_lower = customer_name.lower()
    seen: set = set()
    for filter_key, filter_val in strategies:
        if not filter_val or filter_val in seen:
            continue
        seen.add(filter_val)
        try:
            # params={} lässt requests die Brackets als %5B%5D encodieren
            # (identisches Format wie Chargebee-SDK und MCP-Tools verwenden)
            resp = requests.get(
                f"{base}/customers",
                params={filter_key: filter_val, 'limit': 100},  # limit=100 um wev Schmalkalden sicher zu treffen
                auth=auth,
                timeout=10,
            )
            total = len(resp.json().get('list', [])) if resp.ok else 'error'
            logger.info(f"Chargebee [{filter_key}={filter_val!r}]: status={resp.status_code} total={total}")
            if resp.ok:
                candidates = resp.json().get('list', [])
                # company MUSS nicht-leer sein UND zum Suchnamen passen
                # ("" in "any string" == True in Python → explizit prüfen)
                # Exakter Company-Name-Vergleich (case-insensitive)
                # Verhindert falsche Treffer wenn Chargebee den Filter ignoriert
                exact = [
                    c for c in candidates
                    if (c['customer'].get('company') or '').strip().lower() == search_lower
                ]
                if exact:
                    logger.info(f"Chargebee exakter Treffer: {exact[0]['customer']['id']} ({exact[0]['customer'].get('company')})")
                    return exact
        except Exception as e:
            logger.warning(f"Chargebee search [{filter_key}={filter_val!r}] failed: {e}")
    return []


def _planhat_company_search(customer_name: str, api_token: str) -> dict | None:
    """Sucht einen Kunden in Planhat und gibt Subscription-ID + Planhat-Link zurück.

    Planhat speichert die Chargebee-Subscription-ID im Feld 'externalId' (serial).
    Mit dieser ID kann die Chargebee-Subscription direkt geladen werden.
    """
    headers = {'Authorization': f'Bearer {api_token}'}
    name_no_suffix = re.sub(
        r'\s*(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?\s*$',
        '', customer_name, flags=re.IGNORECASE,
    ).strip()
    first_word = customer_name.split()[0] if customer_name else ''

    name_no_suffix_ph = re.sub(
        r'\s*(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?\s*$',
        '', customer_name, flags=re.IGNORECASE,
    ).strip()
    for search_term in dict.fromkeys([customer_name, name_no_suffix_ph]):  # dedup, preserve order
        if not search_term:
            continue
        try:
            resp = requests.get(
                'https://api.planhat.com/companies',
                params={'companyName': search_term, 'limit': 5},
                headers=headers,
                timeout=10,
            )
            logger.info(
                f"Planhat search [companyName={search_term!r}]: "
                f"status={resp.status_code}, "
                f"results={len(resp.json()) if resp.ok else 'error'}"
            )
            if resp.ok:
                companies = resp.json()
                if companies:
                    company = companies[0]
                    ph_id = company.get('_id', '')
                    name = company.get('name', customer_name)
                    import json as _json
                    logger.info(f"Planhat company FULL: {_json.dumps(company, default=str)[:3000]}")

                    # 1) Chargebee-Link direkt aus Planhat-Links extrahieren
                    # (sichtbar in Planhat UI unter "Links & Tags" → "Chargebee")
                    chargebee_url = ''
                    for link_field in ['links', 'tags', 'customLinks', 'integrations']:
                        items = company.get(link_field) or []
                        if isinstance(items, list):
                            for item in items:
                                url = ''
                                if isinstance(item, dict):
                                    url = (item.get('url') or item.get('href')
                                           or item.get('link') or item.get('value') or '')
                                elif isinstance(item, str):
                                    url = item
                                if url and 'chargebee.com' in url:
                                    chargebee_url = url
                                    logger.info(f"Planhat Chargebee-Link gefunden: {chargebee_url}")
                                    break
                        if chargebee_url:
                            break

                    # 2) Debitorennummer (externalId) für Chargebee cf_debit_number-Suche
                    external_id = company.get('externalId', '')
                    debit_number = str(external_id).strip() if str(external_id).strip().isdigit() else ''
                    if debit_number:
                        logger.info(f"Planhat externalId (Debitnr): {debit_number}")

                    logger.info(f"Planhat: chargebee_url={chargebee_url!r} debit_number={debit_number!r}")
                    return {
                        'planhat_id': ph_id,
                        'name': name,
                        'chargebee_url': chargebee_url,   # direkter Chargebee-Link aus Planhat
                        'debit_number': debit_number,      # externalId = Debitorennummer
                        'planhat_url': f"https://app.planhat.com/customer/{ph_id}",
                    }
        except Exception as e:
            logger.warning(f"Planhat search [{search_term!r}] failed: {e}")
    return None


def _fetch_subscription_by_id(subscription_id: str, api_key: str, site: str) -> dict | None:
    """Lädt eine Chargebee-Subscription direkt per Subscription-ID."""
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')
    try:
        resp = requests.get(f"{base}/subscriptions/{subscription_id}", auth=auth, timeout=10)
        logger.info(f"Chargebee subscription/{subscription_id}: status={resp.status_code}")
        if not resp.ok:
            return None
        sub = resp.json().get('subscription', {})
        if not sub:
            return None
        return _build_subscription_result(sub, site)
    except Exception as e:
        logger.warning(f"Chargebee subscription/{subscription_id} fetch failed: {e}")
        return None


def _build_subscription_result(sub: dict, site: str) -> dict:
    """Wandelt ein Chargebee-Subscription-Objekt in unser einheitliches Format um."""
    sub_id = sub['id']
    if sub.get('subscription_items'):
        addons = [
            item['item_price_id']
            for item in sub['subscription_items']
            if item.get('item_type') == 'addon' and item.get('unit_price', 0) > 0
        ]
    else:
        addons = [a.get('id', '') for a in sub.get('addons', []) if a.get('id')]

    period = sub.get('billing_period', 1)
    period_unit = sub.get('billing_period_unit', '')
    if period_unit == 'month':
        billing_cycle = 'monatlich' if period == 1 else f'alle {period} Monate'
    elif period_unit == 'year':
        billing_cycle = 'jährlich' if period == 1 else f'alle {period} Jahre'
    else:
        billing_cycle = period_unit or ''

    coupons = [c.get('coupon_id', '') for c in sub.get('coupons', []) if c.get('coupon_id')]
    if not coupons and sub.get('coupon'):
        coupons = [sub['coupon']]

    return {
        'subscription_id': sub_id,
        'customer_id': sub.get('customer_id', ''),
        'plan_id': sub.get('plan_id', ''),
        'addons': addons,
        'status': sub.get('status', ''),
        'billing_cycle': billing_cycle,
        'billing_period_unit': period_unit,
        'next_billing_at': _ts_to_date(sub.get('next_billing_at')),
        'current_term_end': _ts_to_date(sub.get('current_term_end')),
        'trial_end': _ts_to_date(sub.get('trial_end')),
        'coupons': coupons,
        'url': f"https://{site}.chargebee.com/d/subscriptions/{sub_id}",
    }


def _search_by_debit_number(debit_number: str, base: str, auth: tuple,
                             site: str, company_name: str) -> dict | None:
    """Sucht Chargebee-Kunden eindeutig über cf_debit_number (Debitorennummer)."""
    from urllib.parse import quote as _q
    url = f"{base}/customers?cf_debit_number[is]={_q(debit_number)}&limit=5"
    try:
        resp = requests.get(url, auth=auth, timeout=10)
        logger.info(f"Chargebee [cf_debit_number[is]={debit_number}]: status={resp.status_code}")
        if resp.ok:
            customers = resp.json().get('list', [])
            if customers:
                customer_id = customers[0]['customer']['id']
                logger.info(f"Chargebee Debit-Match: customer_id={customer_id}")
                return _fetch_subscriptions_for_customer(
                    customer_id, base, auth, site, company_name
                )
    except Exception as e:
        logger.warning(f"Chargebee debit_number search failed: {e}")
    return None


def _fetch_subscriptions_for_customer(customer_id: str, base: str, auth: tuple,
                                       site: str, company_name: str) -> dict | None:
    """Lädt Subscriptions für eine bekannte Chargebee Customer-ID.

    Gibt die aktive Standard-Subscription zurück. Wenn mehrere aktive
    Subscriptions existieren, wird ein Hinweis in 'multiple_note' gesetzt.
    """
    try:
        from urllib.parse import quote as _q
        # customer_id[is] mit literalen Klammern — params={} encodiert sie und
        # Chargebee ignoriert den Filter ggf., daher direkter URL-Build
        url = f"{base}/subscriptions?customer_id[is]={_q(customer_id)}&limit=10"
        resp = requests.get(url, auth=auth, timeout=10)
        logger.info(f"Chargebee subscriptions?customer_id={customer_id}: status={resp.status_code}")
        if not resp.ok:
            return None
        subs = resp.json().get('list', [])
        active = [s['subscription'] for s in subs if s['subscription'].get('status') in ('active', 'non_renewing')]
        candidates = active or [s['subscription'] for s in subs if 'subscription' in s]
        _STD = re.compile(r'^\d{4}-\d{4}-\d{4}-\d{4}$')
        standard = [s for s in candidates if _STD.match(s.get('id', ''))]
        # Prefer standard-format IDs (current Xentral subscriptions)
        ordered = standard or candidates
        if not ordered:
            return None
        sub = ordered[0]
        result = _build_subscription_result(sub, site)
        result['company'] = company_name
        # Build clickable links for all subscriptions
        if len(ordered) > 1:
            links = ' '.join(
                f"<https://{site}.chargebee.com/d/subscriptions/{s['id']}|{s['id']}>"
                for s in ordered
            )
            result['multiple_subs'] = ordered
            result['multiple_links'] = links
        return result
    except Exception as e:
        logger.warning(f"_fetch_subscriptions_for_customer({customer_id}) failed: {e}")
    return None


def lookup_chargebee_subscription(customer_name: str, api_key: str, site: str,
                                   planhat_token: str = '') -> dict | None:
    """Sucht Chargebee-Subscription.

    Reihenfolge:
    1. Planhat (zuverlässigste Quelle — speichert den direkten Chargebee-Customer-Link)
    2. Chargebee-Namenssuche als Fallback
    """
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')

    # Direkte Chargebee-Namenssuche (Planhat deaktiviert — Links dort fehlerhaft)
    # MCP-Test bestätigt: company[starts_with]=wev findet exakt BTcLSNTO7WSZBjCd
    try:
        customers = _chargebee_customer_search(base, auth, customer_name)
        if customers:
            customer = customers[0]['customer']
            company_name = (
                customer.get('company')
                or f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
            )
            result = _fetch_subscriptions_for_customer(
                customer['id'], base, auth, site, company_name
            )
            if result:
                return result
        else:
            logger.info(f"Chargebee: kein Treffer für '{customer_name}'")
    except Exception as e:
        logger.warning(f"Chargebee-Suche Fehler für '{customer_name}': {e}")

    return None


# ---------------------------------------------------------------------------
# IST-Zustand formatieren (für Nachfrage-Nachricht)
# ---------------------------------------------------------------------------

def _format_found_fields(parsed: dict, subscription: dict | None = None) -> str:
    lines = []
    if parsed.get('customer_name'):
        lines.append(f"• *Kunde:* {parsed['customer_name']}")
    if subscription and not subscription.get('multiple_links') and subscription.get('url'):
        # Nur eindeutige Subscription anzeigen — mehrere werden erst nach Infos abgefragt
        lines.append(f"• *Chargebee:* <{subscription['url']}|{subscription['subscription_id']}>")
        if subscription.get('plan_id'):
            plan_info = f"`{subscription['plan_id']}`"
            if subscription.get('billing_cycle'):
                plan_info += f" · {subscription['billing_cycle']}"
            lines.append(f"• *Aktueller Plan:* {plan_info}")
        if subscription.get('addons'):
            lines.append(f"• *Aktive Add-Ons:* {', '.join(subscription['addons'])}")
        if subscription.get('coupons'):
            lines.append(f"• *Aktiver Rabatt:* {', '.join(subscription['coupons'])}")
        if subscription.get('next_billing_at'):
            lines.append(f"• *Nächste Rechnung:* {subscription['next_billing_at']}")
    if parsed.get('new_plan'):
        lines.append(f"• *Neuer Plan:* {parsed['new_plan']}")
    if parsed.get('payment_type'):
        lines.append(f"• *Zahlweise:* {parsed['payment_type']}")
    if parsed.get('effective_date'):
        lines.append(f"• *Vertragsbeginn:* {parsed['effective_date']}")
    if parsed.get('offer_link'):
        lines.append(f"• *Angebots-Link:* {parsed['offer_link']}")
    if parsed.get('addons_add'):
        lines.append(f"• *Add-Ons hinzufügen:* {parsed['addons_add']}")
    if parsed.get('addons_remove'):
        lines.append(f"• *Add-Ons entfernen:* {parsed['addons_remove']}")
    if parsed.get('berichtswesen_tier'):
        lines.append(f"• *Berichtswesen-Tier:* `{parsed['berichtswesen_tier']}`")
    return '\n'.join(lines)


def ask_for_va_info_blocks(
    user_id: str,
    missing: list[str],
    parsed: dict | None = None,
    subscription: dict | None = None,
) -> list[dict]:
    parsed = parsed or {}
    found = _format_found_fields(parsed, subscription)
    missing_items = '\n'.join(f'• {m}' for m in missing)

    text = f"Hey <@{user_id}> :wave: Ich habe eine *Vertragsanpassungs-Anfrage* erkannt.\n\n"
    if found:
        text += f"*Bereits erkannt:*\n{found}\n\n"
    if missing_items:
        text += f"*Mir fehlen noch:*\n{missing_items}\n\nBitte ergänze diese Informationen hier im Thread."
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


def build_cs_admin_subscription_blocks(subscription: dict) -> list[dict]:
    """Postet die CS-Admin-Warnung. Bei ≤3 Subscriptions alle zeigen, sonst nur erste 3 + Hinweis."""
    admin_mentions = ' '.join(f'<@{uid}>' for uid in _CS_ADMIN_IDS)
    subs = subscription.get('multiple_subs', [])
    site = subscription.get('url', '').split('/d/')[0]  # e.g. https://xentral-dach.chargebee.com

    MAX_SHOWN = 3
    shown = subs[:MAX_SHOWN]
    rest = len(subs) - MAX_SHOWN

    lines = []
    for s in shown:
        sub_id = s.get('id', '')
        url = f"{site}/d/subscriptions/{sub_id}"
        lines.append(f"• <{url}|{sub_id}>")
    if rest > 0:
        lines.append(f"_…und {rest} weitere — bitte in Chargebee prüfen_")

    text = (
        f"⚠️ {admin_mentions} *Mehrere Subscriptions gefunden* — "
        f"bitte die richtige Chargebee-URL in den Thread schreiben:\n"
        + '\n'.join(lines)
    )
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


# ---------------------------------------------------------------------------
# Summary Builder
# ---------------------------------------------------------------------------

def _try_parse_date(date_str: str) -> datetime | None:
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(date_str.replace(' ', '').split('(')[0], fmt)
        except ValueError:
            continue
    return None


def _build_suggestions(parsed: dict, subscription: dict) -> list[str]:
    """Generiert kontextuelle Hinweise basierend auf IST-Zustand vs. gewünschten Änderungen."""
    hints = []

    # Billing-Zyklus-Wechsel
    current_unit = subscription.get('billing_period_unit', '')
    requested = parsed.get('payment_type', '')
    if current_unit and requested:
        current_is_annual = current_unit == 'year'
        requested_is_annual = requested == 'jährlich'
        if current_is_annual and not requested_is_annual:
            hints.append(
                "💡 *Zyklus-Wechsel:* Aktuell jährlich → wechselt auf monatlich. "
                "Bitte prüfe ob eine Prorata-Gutschrift für den Restbetrag nötig ist."
            )
        elif not current_is_annual and requested_is_annual:
            hints.append(
                "💡 *Zyklus-Wechsel:* Aktuell monatlich → wechselt auf jährlich. "
                "Prorata-Abrechnung für den laufenden Monat möglich."
            )

    # Aktive Add-Ons die im Request nicht erwähnt werden
    current_addons = set(subscription.get('addons', []))
    mentioned_remove = parsed.get('addons_remove', '').lower()
    mentioned_add = parsed.get('addons_add', '').lower()
    if current_addons:
        unmentioned = [
            a for a in current_addons
            if a.lower() not in mentioned_remove and a.lower() not in mentioned_add
        ]
        if unmentioned:
            hints.append(
                f"💡 *Aktive Add-Ons nicht erwähnt:* `{'`, `'.join(unmentioned)}` — "
                "bitte prüfe ob sie nach der Anpassung weiter gelten sollen "
                "_(laut SOP: alte Add-Ons explizit entfernen wenn nicht mehr gewünscht)_."
            )

    # Bestehender Rabatt
    if subscription.get('coupons'):
        coupon_list = ', '.join(subscription['coupons'])
        hints.append(
            f"💡 *Aktiver Rabatt gefunden:* `{coupon_list}` — "
            "bitte klären ob er nach der Vertragsanpassung weiterhin gelten soll."
        )

    # Nächstes Billing-Datum
    if subscription.get('next_billing_at') and parsed.get('effective_date'):
        hints.append(
            f"💡 *Nächste Rechnung:* {subscription['next_billing_at']} — "
            "Änderungen vor diesem Datum können eine Prorata-Abrechnung auslösen."
        )

    # Trial läuft noch
    if subscription.get('trial_end'):
        hints.append(
            f"💡 *Trial läuft bis:* {subscription['trial_end']} — "
            "prüfe ob die Anpassung vor oder nach Trial-Ende greifen soll."
        )

    return hints


def build_va_summary_blocks(parsed: dict, subscription: dict | None, requester: str) -> list[dict]:
    """Erstellt Slack Block Kit Blocks für die Vertragsanpassungs-Zusammenfassung."""
    customer = parsed.get('customer_name', 'Unbekannter Kunde')
    header = f"📋 Vertragsanpassung — {customer}"

    # IST-Zustand
    ist_lines = []
    if subscription:
        ist_lines.append(f"*Subscription:* <{subscription['url']}|{subscription['subscription_id']}>")
        plan_info = f"`{subscription.get('plan_id') or '–'}`"
        if subscription.get('billing_cycle'):
            plan_info += f"  ·  {subscription['billing_cycle']}"
        ist_lines.append(f"*Aktueller Plan:* {plan_info}")
        if subscription.get('addons'):
            ist_lines.append(f"*Aktive Add-Ons:* {', '.join(subscription['addons'])}")
        if subscription.get('coupons'):
            ist_lines.append(f"*Aktiver Rabatt:* {', '.join(subscription['coupons'])}")
        if subscription.get('next_billing_at'):
            ist_lines.append(f"*Nächste Rechnung:* {subscription['next_billing_at']}")
        if subscription.get('current_term_end'):
            ist_lines.append(f"*Vertragsende:* {subscription['current_term_end']}")
        ist_lines.append(f"*Status:* `{subscription.get('status', '–')}`")
    else:
        ist_lines.append(
            "⚠️ Subscription nicht automatisch gefunden — bitte in Chargebee manuell suchen."
        )

    # SOLL-Zustand
    soll_lines = []
    if parsed.get('new_plan'):
        old = subscription.get('plan_id', '') if subscription else ''
        arrow = f" _(war: `{old}`)_" if old and old.lower() != parsed['new_plan'].lower() else ''
        soll_lines.append(f"• Neuer Plan: `{parsed['new_plan']}`{arrow}")
    if parsed.get('payment_type'):
        soll_lines.append(f"• Zahlweise: {parsed['payment_type']}")
    if parsed.get('effective_date'):
        soll_lines.append(f"• Vertragsbeginn: {parsed['effective_date']}")
    if parsed.get('addons_add'):
        soll_lines.append(f"• Add-Ons *hinzufügen:* {parsed['addons_add']}")
    if parsed.get('addons_remove'):
        soll_lines.append(f"• Add-Ons *entfernen:* {parsed['addons_remove']} _(laut SOP explizit prüfen!)_")
    if parsed.get('berichtswesen_tier'):
        soll_lines.append(f"• Berichtswesen-Tier: `{parsed['berichtswesen_tier']}`")
    if parsed.get('offer_link'):
        soll_lines.append(f"• Angebot: {parsed['offer_link']}")

    # Warnungen
    warnings = []
    if parsed.get('berichtswesen_tier_original') is not None:
        orig = parsed['berichtswesen_tier_original']
        corr = parsed['berichtswesen_tier']
        warnings.append(
            f"⚠️ *Berichtswesen-Tier korrigiert:* `{orig}` → `{corr}` (gültige Werte: 1, 31, 251, 501)"
        )
    if parsed.get('effective_date'):
        dt = _try_parse_date(parsed['effective_date'])
        if dt and dt.date() > datetime.now(timezone.utc).date():
            warnings.append(
                f"⚠️ *Ramp nötig:* Vertragsbeginn ({parsed['effective_date']}) liegt in der Zukunft "
                "→ in Chargebee über Tab \"Ramps\" → \"Add Ramp\" anlegen"
            )
    # Discount-Hinweis bewusst weggelassen (wird manuell behandelt)

    # Kontextuelle Hinweise aus Chargebee-Daten
    suggestions = _build_suggestions(parsed, subscription) if subscription else []

    next_steps = (
        "1. Subscription in Chargebee öffnen (Link oben)\n"
        "2. Plan & Add-Ons laut Zusammenfassung eintragen\n"
        "3. Im Thread als ✅ done markieren"
    )

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*IST-Zustand (Chargebee):*\n" + "\n".join(ist_lines)},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*Geplante Änderungen (SOLL):*\n" + ("\n".join(soll_lines) or "_Keine Details erkannt_"),
            },
        },
    ]
    if warnings:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(warnings)}})
    if suggestions:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Hinweise aus dem IST-Zustand:*\n" + "\n".join(suggestions)},
        })
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Nächste Schritte:*\n{next_steps}"}},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": f"Anfrage erkannt von {requester} · Automatische Zusammenfassung · Kein Chargebee-Schreibzugriff",
            }],
        },
    ]
    return blocks
