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

# ---------------------------------------------------------------------------
# Intent Detection
# ---------------------------------------------------------------------------

# Starke Signale — 3 Punkte je Treffer
_STRONG = [
    r'vertrags\s*anpassung',
    r'vertrags\s*[äa]nderung',
    r'vertragswechsel',
    r'unterschriebene[snm]?\s+angebot',
    r'angebot.{0,60}unterschrieben',
    r'unterschrieben.{0,60}angebot',
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
    r'(?:kunde|kundschaft|customer|company|firma|unternehmen|kund)\s*[:\-]\s*'
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
    if not parsed.get('offer_link'):
        out.append('*Link zum unterschriebenen Angebot*')
    return out


# ---------------------------------------------------------------------------
# Chargebee Lookup (read-only)
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

    # Reihenfolge: exakt → starts_with (ohne Suffix) → starts_with (erstes Wort)
    strategies = [
        ('company[is]', customer_name),
        ('company[starts_with]', name_no_suffix),
        ('company[starts_with]', customer_name),
        ('company[starts_with]', first_word),
    ]
    seen = set()
    for param_key, param_val in strategies:
        if not param_val or param_val in seen:
            continue
        seen.add(param_val)
        try:
            resp = requests.get(
                f"{base}/customers",
                params={param_key: param_val, 'limit': 5},
                auth=auth, timeout=10,
            )
            logger.info(
                f"Chargebee search [{param_key}={param_val!r}]: "
                f"status={resp.status_code}, "
                f"results={len(resp.json().get('list', [])) if resp.ok else 'error'}"
            )
            if resp.ok:
                customers = resp.json().get('list', [])
                if customers:
                    return customers
        except Exception as e:
            logger.warning(f"Chargebee search [{param_key}={param_val!r}] failed: {e}")
    return []


def lookup_chargebee_subscription(customer_name: str, api_key: str, site: str) -> dict | None:
    """Sucht die aktive Chargebee-Subscription und lädt relevante Details. Kein Schreibzugriff."""
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')

    try:
        customers = _chargebee_customer_search(base, auth, customer_name)

        if not customers:
            logger.info(f"Kein Chargebee-Kunde gefunden für '{customer_name}' (alle Strategien erschöpft)")
            return None

        customer = customers[0]['customer']
        customer_id = customer['id']
        company = (
            customer.get('company')
            or f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        )

        # Subscription laden
        resp = requests.get(
            f"{base}/subscriptions",
            params={'customer_id': customer_id, 'limit': 10},
            auth=auth, timeout=10,
        )
        subs = resp.json().get('list', []) if resp.ok else []

        active = [s['subscription'] for s in subs if s['subscription'].get('status') == 'active']
        sub = (active or [s['subscription'] for s in subs if 'subscription' in s])[0] if subs else None
        if not sub:
            return None

        sub_id = sub['id']

        # Add-Ons: neues Item-Model (subscription_items) hat Vorrang, Fallback auf altes addons-Feld
        if sub.get('subscription_items'):
            addons = [
                item['item_price_id']
                for item in sub['subscription_items']
                if item.get('item_type') == 'addon' and item.get('unit_price', 0) > 0
            ]
        else:
            addons = [a.get('id', '') for a in sub.get('addons', []) if a.get('id')]

        # Billing-Zyklus lesbar machen
        period = sub.get('billing_period', 1)
        period_unit = sub.get('billing_period_unit', '')
        if period_unit == 'month':
            billing_cycle = f"monatlich" if period == 1 else f"alle {period} Monate"
        elif period_unit == 'year':
            billing_cycle = 'jährlich' if period == 1 else f"alle {period} Jahre"
        else:
            billing_cycle = period_unit or ''

        # Gutscheine / Discounts
        coupons = [c.get('coupon_id', '') for c in sub.get('coupons', []) if c.get('coupon_id')]
        if not coupons and sub.get('coupon'):
            coupons = [sub['coupon']]

        return {
            'subscription_id': sub_id,
            'customer_id': customer_id,
            'company': company,
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

    except Exception as e:
        logger.warning(f"Chargebee-Lookup Fehler für '{customer_name}': {e}")
        return None


# ---------------------------------------------------------------------------
# IST-Zustand formatieren (für Nachfrage-Nachricht)
# ---------------------------------------------------------------------------

def _format_found_fields(parsed: dict, subscription: dict | None = None) -> str:
    lines = []
    if parsed.get('customer_name'):
        lines.append(f"• *Kunde:* {parsed['customer_name']}")
    if subscription:
        cb_info = f"<{subscription['url']}|{subscription['subscription_id']}>"
        if subscription.get('plan_id'):
            cb_info += f"  (`{subscription['plan_id']}`"
            if subscription.get('billing_cycle'):
                cb_info += f", {subscription['billing_cycle']}"
            cb_info += ")"
        lines.append(f"• *Chargebee:* {cb_info}")
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
    text += f"*Mir fehlen noch:*\n{missing_items}\n\nBitte ergänze diese Informationen hier im Thread."

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
    if parsed.get('has_discount'):
        warnings.append(
            "⚠️ *Discount erkannt:* Manuell eintragen — erst Approval aus "
            "*#approval-discount-refunds* einholen. *Nie als Price Override!*"
        )

    # Kontextuelle Hinweise aus Chargebee-Daten
    suggestions = _build_suggestions(parsed, subscription) if subscription else []

    next_steps = (
        "1. Subscription in Chargebee öffnen (Link oben)\n"
        "2. Plan & Add-Ons laut Zusammenfassung eintragen\n"
        "3. Bei Discount: erst Approval in #approval-discount-refunds\n"
        "4. Im Thread als ✅ done markieren"
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
