"""
Vertragsanpassungs-Flow: Intent-Erkennung, Parsing, Chargebee-Lookup (read-only),
und Zusammenfassungs-Builder.

Der Bot schreibt NICHTS in Chargebee — er erstellt nur eine strukturierte Zusammenfassung
mit Link zur Subscription.
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
    r'angebot.{0,60}unterschrieben',   # "Angebot für Pro 25 unterschrieben"
    r'unterschrieben.{0,60}angebot',   # auch umgekehrte Reihenfolge
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
    # Interne Formulierungen aus dem CS-Admin-Alltag
    r'anpassung\s+(?:vornehmen|vorgenommen|gemacht|rückgängig|zurück)',
    r'rückgängig\s+machen',
    r'(?:monatlich|jährlich)\w*\s+(?:miete|gebühr|preis|beitrag)',
    r'(?:subscription|abo|vertrag|konditionen)\s+(?:ändern|anpassen|wechseln|korrigieren)',
    r'könnt?\s+(?:ihr|sie).{0,30}(?:ändern|anpassen|korrigieren|umstellen)',
]


def detect_vertragsanpassung(text: str) -> bool:
    """Gibt True zurück wenn der Text mit hoher Konfidenz eine Vertragsanpassungs-Anfrage ist.

    Schwellwert: Score >= 3 (ein starkes Keyword genügt, oder mehrere mittlere).
    #improvement-Nachrichten werden explizit ausgeschlossen.
    """
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
    r'(?:kunde|kundschaft|customer|company|firma|unternehmen)\s*[:\-]\s*(.+?)(?:\n|,|$)',
    re.IGNORECASE,
)
_COMPANY_SUFFIX_RE = re.compile(
    r'([A-ZÄÖÜ][a-zA-ZäöüÄÖÜ\s&.\-]{1,40}'
    r'(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?)',
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

    # Kundenname — explizit gelabelter Wert hat Priorität
    m = _CUSTOMER_LABELED_RE.search(text)
    if m:
        result['customer_name'] = m.group(1).strip()
    else:
        m = _COMPANY_SUFFIX_RE.search(text)
        if m:
            result['customer_name'] = m.group(1).strip()

    # Angebots-Link (erste URL im Text)
    urls = _URL_RE.findall(text)
    if urls:
        result['offer_link'] = urls[0]

    # Effective Date (erstes Datum)
    m = _DATE_RE.search(text)
    if m:
        result['effective_date'] = (m.group(1) or m.group(2) or '').strip()

    # Zahlweise
    m = _PAYMENT_RE.search(text)
    if m:
        raw = m.group(1).lower()
        if any(x in raw for x in ('jähr', 'annual', 'yearly')):
            result['payment_type'] = 'jährlich'
        elif any(x in raw for x in ('monatl', 'monthly')):
            result['payment_type'] = 'monatlich'
        else:
            result['payment_type'] = 'quartalsweise'

    # Plan-Name
    m = _PLAN_RE.search(text)
    if m:
        result['new_plan'] = m.group(0).strip()

    # Berichtswesen-Tier mit Auto-Korrektur
    m = _BERICHTSWESEN_RE.search(text)
    if m:
        raw_tier = int(m.group(1))
        corrected = _TIER_CORRECTIONS.get(raw_tier, raw_tier)
        result['berichtswesen_tier'] = corrected if corrected in _VALID_TIERS else raw_tier
        if corrected != raw_tier and corrected in _VALID_TIERS:
            result['berichtswesen_tier_original'] = raw_tier  # für Hinweis in Summary

    # Add-Ons
    m = _ADDON_ADD_RE.search(text)
    if m:
        result['addons_add'] = m.group(1).strip()
    m = _ADDON_REMOVE_RE.search(text)
    if m:
        result['addons_remove'] = m.group(1).strip()

    # Discount / Rabatt erwähnt
    if re.search(r'\bdiscount\b|\brabatt\b|\bnachlass\b|\bgutschrift\b', text, re.IGNORECASE):
        result['has_discount'] = True

    return result


def missing_va_fields(parsed: dict) -> list[str]:
    """Gibt Liste der Labels fehlender Pflichtfelder zurück."""
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


def _format_found_fields(parsed: dict, subscription: dict | None = None) -> str:
    """Formatiert die bereits erkannten Felder für die Slack-Nachricht."""
    lines = []
    if parsed.get('customer_name'):
        lines.append(f"• *Kunde:* {parsed['customer_name']}")
    if subscription:
        lines.append(
            f"• *Chargebee:* <{subscription['url']}|{subscription['subscription_id']}>"
            f"  (`{subscription.get('plan_id') or '–'}`)"
        )
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
# Chargebee Lookup (read-only)
# ---------------------------------------------------------------------------

def lookup_chargebee_subscription(customer_name: str, api_key: str, site: str) -> dict | None:
    """Sucht die aktive Chargebee-Subscription eines Kunden anhand des Unternehmensnamens.

    Gibt None zurück wenn nichts gefunden oder API-Fehler. Kein Schreibzugriff.
    """
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')

    try:
        # Suche nach Unternehmensname
        resp = requests.get(
            f"{base}/customers",
            params={'company[contains]': customer_name, 'limit': 5},
            auth=auth, timeout=10,
        )
        customers = resp.json().get('list', []) if resp.ok else []

        # Fallback: Suche nach erstem Wort des Namens
        if not customers:
            first_word = customer_name.split()[0] if customer_name else customer_name
            resp = requests.get(
                f"{base}/customers",
                params={'first_name[contains]': first_word, 'limit': 5},
                auth=auth, timeout=10,
            )
            customers = resp.json().get('list', []) if resp.ok else []

        if not customers:
            logger.info(f"Kein Chargebee-Kunde gefunden für '{customer_name}'")
            return None

        customer = customers[0]['customer']
        customer_id = customer['id']
        company = (
            customer.get('company')
            or f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip()
        )

        # Subscriptions laden
        resp = requests.get(
            f"{base}/subscriptions",
            params={'customer_id': customer_id, 'limit': 10},
            auth=auth, timeout=10,
        )
        subs = resp.json().get('list', []) if resp.ok else []

        # Bevorzuge aktive Subscription
        active = [s['subscription'] for s in subs if s['subscription'].get('status') == 'active']
        sub = (active or [s['subscription'] for s in subs if 'subscription' in s])[0] if subs else None
        if not sub:
            return None

        sub_id = sub['id']
        addons = [a.get('id', '') for a in sub.get('addons', [])]

        return {
            'subscription_id': sub_id,
            'customer_id': customer_id,
            'company': company,
            'plan_id': sub.get('plan_id', ''),
            'addons': addons,
            'status': sub.get('status', ''),
            'url': f"https://{site}.chargebee.com/d/subscriptions/{sub_id}",
        }

    except Exception as e:
        logger.warning(f"Chargebee-Lookup Fehler für '{customer_name}': {e}")
        return None


# ---------------------------------------------------------------------------
# Summary Builder
# ---------------------------------------------------------------------------

def build_va_summary_blocks(parsed: dict, subscription: dict | None, requester: str) -> list[dict]:
    """Erstellt Slack Block Kit Blocks für die Vertragsanpassungs-Zusammenfassung."""
    customer = parsed.get('customer_name', 'Unbekannter Kunde')
    header = f"📋 Vertragsanpassung — {customer}"

    # IST-Zustand
    ist_lines = []
    if subscription:
        ist_lines.append(f"*Subscription:* <{subscription['url']}|{subscription['subscription_id']}>")
        ist_lines.append(f"*Aktueller Plan:* `{subscription.get('plan_id') or '–'}`")
        if subscription.get('addons'):
            ist_lines.append(f"*Aktive Add-Ons:* {', '.join(subscription['addons'])}")
        ist_lines.append(f"*Status:* `{subscription.get('status', '–')}`")
    else:
        ist_lines.append(
            "⚠️ Subscription nicht automatisch gefunden — "
            "bitte in Chargebee manuell suchen."
        )

    # SOLL-Zustand
    soll_lines = []
    if parsed.get('new_plan'):
        soll_lines.append(f"• Neuer Plan: `{parsed['new_plan']}`")
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

    # Hinweise / Warnungen
    warnings = []
    if parsed.get('berichtswesen_tier_original') is not None:
        orig = parsed['berichtswesen_tier_original']
        corr = parsed['berichtswesen_tier']
        warnings.append(
            f"⚠️ *Berichtswesen-Tier korrigiert:* `{orig}` → `{corr}` "
            f"(gültige Werte: 1, 31, 251, 501)"
        )

    # Ramp-Hinweis falls Datum in der Zukunft
    if parsed.get('effective_date'):
        for fmt in ('%d.%m.%Y', '%d.%m.%y', '%d-%m-%Y', '%d/%m/%Y'):
            try:
                dt = datetime.strptime(
                    parsed['effective_date'].replace(' ', '').split('(')[0], fmt
                )
                if dt.date() > datetime.now(timezone.utc).date():
                    warnings.append(
                        f"⚠️ *Ramp nötig:* Vertragsbeginn ({parsed['effective_date']}) liegt in der Zukunft "
                        "→ in Chargebee über Tab \"Ramps\" → \"Add Ramp\" anlegen"
                    )
                break
            except ValueError:
                continue

    if parsed.get('has_discount'):
        warnings.append(
            "⚠️ *Discount erkannt:* Manuell eintragen — erst Approval aus "
            "*#approval-discount-refunds* einholen. *Nie als Price Override!*"
        )

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
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(warnings)},
        })
    blocks += [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Nächste Schritte:*\n{next_steps}"}},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": (
                    f"Anfrage erkannt von {requester} · "
                    "Automatische Zusammenfassung · Kein Chargebee-Schreibzugriff"
                ),
            }],
        },
    ]
    return blocks
