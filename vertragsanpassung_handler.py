"""
Vertragsanpassungs-Flow: Intent-Erkennung, Parsing, Chargebee-Lookup (read-only),
und Zusammenfassungs-Builder.
"""
import ipaddress
import logging
import os
import re
import socket
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from config import CS_ADMIN_USER_IDS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSRF guard for offer URL fetches
# ---------------------------------------------------------------------------

# Comma-separated list of allowed hostnames (exact or suffix match).
# Override via OFFER_ALLOWLIST_HOSTS env var if the offer portal ever moves.
_OFFER_ALLOWLIST: set[str] = {
    h.strip().lower()
    for h in os.environ.get('OFFER_ALLOWLIST_HOSTS', 'xentral.com').split(',')
    if h.strip()
}


def _validate_offer_url(url: str) -> None:
    """Raises ValueError if url is not on the offer allow-list or resolves to a private/loopback IP."""
    parsed = urlparse(url)
    if parsed.scheme not in ('https', 'http'):
        raise ValueError(f"Invalid scheme '{parsed.scheme}' — only https/http allowed")
    host = (parsed.hostname or '').lower()
    if not host:
        raise ValueError("No hostname in URL")
    if not any(host == a or host.endswith('.' + a) for a in _OFFER_ALLOWLIST):
        raise ValueError(f"Host '{host}' not in offer allow-list {_OFFER_ALLOWLIST}")
    try:
        addr_infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed for '{host}': {exc}") from exc
    for _, _, _, _, sockaddr in addr_infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"Host '{host}' resolves to non-routable IP {ip}")

# ---------------------------------------------------------------------------
# Chargebee Plan-ID Lookup
# ---------------------------------------------------------------------------

_PLAN_SLUG: dict[str, str] = {
    'pro 25': 'pro', 'pro25': 'pro', 'pro': 'pro',
    'pro 2025': 'pro-25', 'pro2025': 'pro-25',
    'pro 23': 'pro23', 'pro2023': 'pro23',
    'business 25': 'business', 'business25': 'business', 'business': 'business',
    'business 2025': 'business-25', 'business2025': 'business-25',
    'scale': 'scale',
    'starter': 'starter',
    'launch 25': 'launch', 'launch25': 'launch', 'launch': 'launch',
}
_DURATION_SLUG: dict[int, str] = {1: 'monthly', 12: 'annual', 24: 'biennial', 36: 'triennial'}
_PAYMENT_SLUG: dict[str, str] = {
    'jährlich': 'annual', 'jaehrlich': 'annual', 'annual': 'annual', 'yearly': 'annual',
    'monatlich': 'monthly', 'monthly': 'monthly',
    'quartalsweise': 'quarterly', 'quarterly': 'quarterly',
}

# Version-Suffix pro Service-Paket (v1–v9: Standard S/M/L, Growth S/M/L, Premium S/M/L).
# Gilt für Pro 25 und Business 25 Pläne. Andere Tier-Slugs haben kein Versions-Suffix.
_SERVICE_PACKAGE_VERSION: dict[str, str] = {
    'standard s': 'v1', 'standard m': 'v2', 'standard l': 'v3',
    'growth s': 'v4',   'growth m': 'v5',   'growth l': 'v6',
    'premium s': 'v7',  'premium m': 'v8',  'premium l': 'v9',
}
# Plan-Slugs die Versions-Suffixe unterstützen
_VERSIONED_SLUGS = {'pro', 'pro-25', 'business', 'business-25', 'scale', 'launch'}


def extract_service_package(plan_name: str) -> str:
    """Extrahiert das Service-Paket aus einem Plan-Namen (z.B. 'Premium L' aus 'Pro 25 | ... inkl. Premium L')."""
    m = re.search(r'inkl\.?\s+(.+?)$', plan_name.strip(), re.IGNORECASE)
    return m.group(1).strip() if m else ''


def fetch_item_price_name(item_price_id: str, api_key: str, site: str) -> str:
    """Lädt den offiziellen Chargebee-Namen einer item_price_id."""
    data = fetch_item_price(item_price_id, api_key, site)
    return data.get('name', '')


def fetch_item_price(item_price_id: str, api_key: str, site: str) -> dict:
    """Lädt Name und Preis einer item_price_id aus Chargebee."""
    from urllib.parse import quote as _q
    try:
        resp = requests.get(
            f"https://{site}.chargebee.com/api/v2/item_prices/{_q(item_price_id)}",
            auth=(api_key, ''),
            timeout=10,
        )
        if resp.ok:
            ip = resp.json().get('item_price', {})
            return {'name': ip.get('name', ''), 'price': ip.get('price')}
    except Exception as e:
        logger.warning(f"fetch_item_price({item_price_id}) failed: {e}")
    return {}


def resolve_chargebee_plan_id(
    plan_name: str,
    contract_months: int,
    payment_type: str,
    service_package: str = '',
) -> str | None:
    """Leitet die Chargebee item_price_id ab inkl. Versions-Suffix für das Service-Paket.

    Beispiel: ('Pro 25', 24, 'monatlich', 'Growth S') → 'pro-biennial-contract-monthly-payment-v4'
    """
    slug = _PLAN_SLUG.get(plan_name.lower().strip())
    if not slug:
        slug = re.sub(r'\s+', '-', plan_name.lower().strip())
    duration = _DURATION_SLUG.get(contract_months)
    payment = _PAYMENT_SLUG.get(payment_type.lower().strip())
    if not duration or not payment:
        return None
    base = f"{slug}-{duration}-contract-{payment}-payment"
    if slug in _VERSIONED_SLUGS and service_package:
        version = _SERVICE_PACKAGE_VERSION.get(service_package.lower().strip())
        if version:
            return f"{base}-{version}"
    return base


# CS Admin Slack User IDs für @-Mentions in der Subscription-Warnung
# Loaded from CS_ADMIN_USER_IDS env var (set in GCP Secret Manager).
_CS_ADMIN_IDS = CS_ADMIN_USER_IDS

# ---------------------------------------------------------------------------
# Intent Detection
# ---------------------------------------------------------------------------

# Starke Signale — 3 Punkte je Treffer
_STRONG = [
    r'vertrags\s*anpassung',
    r'vertrags\s*[äa]nderung',
    r'vertrags\s*verlängerung',
    r'vertragswechsel',
    r'vertrag\s+anlegen',                   # "Vertrag anlegen"
    r'\d+[\s\-]?(?:monats|jahres)vertrag',  # "24-Monatsvertrag", "12-Jahresvertrag"
    r'unterschriebene[snm]?\s+angebot',
    r'unterzeichnetes?\s+angebot',
    r'angebot.{0,60}unterschrieben',
    r'unterschrieben.{0,60}angebot',
    r'angebot.{0,60}unterzeichnet',
    r'unterzeichnet.{0,60}angebot',
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
    r'\bupgrad\w*\b',
    r'grad\w*\s+.{0,80}\s+up\b',
    r'(?:auf|zum?)\s+(?:das?\s+)?(?:pro|premium|enterprise|business|starter|growth)\s+(?:paket|plan|tarif|abo)',
    r'(?:paket|tarif|plan)\s+(?:up\b|upgrade)',
    # Neue Vertragserstellung
    r'\bjahresrechnung\b',                  # "Jahresrechnung mit monatlichem Zahlungsplan"
    r'\bzahlungsplan\b',                    # "monatlicher Zahlungsplan"
    r'(?:folgenden?|neuen?)\s+vertrag',     # "folgenden Vertrag anlegen"
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

_ASAP_RE = re.compile(
    r'\b(?:asap|sofort|ab\s+sofort|umgehend|so\s+schnell\s+wie\s+m[öo]glich'
    r'|immediately|jetzt|now|baldm[öo]glichst)\b',
    re.IGNORECASE,
)

_DATE_RE = re.compile(
    r'\b(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4})\b'          # DD.MM.YYYY
    r'|\b(\d{1,2}[.\-/]\d{1,2}\.?)\b'                     # DD.MM. ohne Jahr (z.B. "1.7.")
    r'|\b(\d{1,2}\.\s*'
    r'(?:januar|februar|märz|april|mai|juni|juli|august|september|oktober|november|dezember'
    r'|jan|feb|mär|apr|jun|jul|aug|sep|okt|nov|dez)\w*\.?\s*\d{2,4})\b',
    re.IGNORECASE,
)
_URL_RE = re.compile(r'https?://[^\s<>"\'>\])]+')
_PAYMENT_RE = re.compile(
    r'\b(jährlich|monatlich|annual(?:ly)?|yearly|monthly|quarterly|quartalsweise)\b',
    re.IGNORECASE,
)
_PLAN_RE = re.compile(
    # "pro" MUSS eine Zahl dahinter haben (verhindert Match auf "pro Paket")
    r'\b(?:growth\s*[mlxs]?|pro\s*\d+(?:\s*legacy)?|starter|enterprise'
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

    def _clean_company_name(name: str) -> str:
        """Extrahiert nur den eigentlichen Firmennamen (kürzt vorne und hinten)."""
        # Hinten: alles nach dem rechtlichen Suffix abschneiden
        suffix = re.search(
            r'(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)(?:\s*&\s*Co\.?\s*KG)?',
            name, re.IGNORECASE,
        )
        if suffix:
            name = name[:suffix.end()].strip()

        # Vorne: nur explizit bekannte Füll-/Artikel-Wörter überspringen.
        # Wichtig: Firmen wie "wev Schmalkalden" fangen klein an → NICHT nach
        # Großbuchstaben suchen, sondern nur SKIP-Wörter überspringen.
        _SKIP = {'der', 'die', 'das', 'den', 'dem', 'des', 'für', 'fur', 'fuer',
                 'bitte', 'hi', 'hey', 'team', 'hallo', 'liebe', 'lieber',
                 'the', 'a', 'an', 'please', 'hello',
                 'kunden', 'kunde', 'kundschaft', 'firma', 'unternehmen'}
        words = name.split()
        start = 0
        for i, w in enumerate(words):
            if w.lower().rstrip(',:') in _SKIP:
                start = i + 1  # dieses Wort überspringen
            else:
                break  # erstes Nicht-SKIP-Wort → Firmenname beginnt hier
        name = ' '.join(words[start:]).strip() or name.strip()

        # Zusätzlich: bei Kontext-Präpositionen abschneiden
        # "Heavn Lights ab dem 1.7. auf einen 2-Jahresvertrag" → "Heavn Lights"
        ctx_match = re.search(
            r'\s+(?:ab|seit|bis|zum?|auf\s+(?:einen?|das?)|mit|von|nach|in)\s+',
            name, re.IGNORECASE,
        )
        if ctx_match:
            name = name[:ctx_match.start()].strip()

        return name

    _trim_to_company_suffix = _clean_company_name  # alias

    m = _CUSTOMER_LABELED_RE.search(text)
    if m:
        raw = _STRIP_COMPANY_PREFIX_RE.sub('', m.group(1)).strip()
        result['customer_name'] = _trim_to_company_suffix(raw)
    else:
        m = _COMPANY_SUFFIX_RE.search(text)
        if m:
            raw = _STRIP_COMPANY_PREFIX_RE.sub('', m.group(1)).strip()
            result['customer_name'] = _trim_to_company_suffix(raw)

    urls = _URL_RE.findall(text)
    if urls:
        result['offer_link'] = urls[0]

    m = _DATE_RE.search(text)
    if m:
        result['effective_date'] = (m.group(1) or m.group(2) or m.group(3) or '').strip()
    elif _ASAP_RE.search(text):
        result['effective_date'] = 'ASAP'  # wird durch next_billing_at aus Chargebee ersetzt

    m = _PAYMENT_RE.search(text)
    if m:
        raw = m.group(1).lower()
        if any(x in raw for x in ('jähr', 'annual', 'yearly')):
            result['payment_type'] = 'jährlich'
        elif any(x in raw for x in ('monatl', 'monthly')):
            result['payment_type'] = 'monatlich'
        else:
            result['payment_type'] = 'quartalsweise'

    # Plan: erst "Plan | N-Monatsvertrag [Zahlung] inkl. X"-Format (wie im Angebot)
    _inline_plan = re.compile(
        r'([\w][\w \-]*?\d+[\w \-]*?)'          # Plan-Name mit Zahl (z.B. "Pro 25")
        r'[ ]*\|[ ]*'                             # Pipe
        r'([\d]+[ \-]?(?:Monats|Jahres)vertrag[^\[]*)'  # Vertragstyp
        r'\[([^\]]+)\]'                           # [Zahlweise]
        r'([^\n]*)',                               # inkl. X (optional)
        re.IGNORECASE,
    )
    inline = _inline_plan.search(text)
    if inline:
        plan_base = inline.group(1).strip()
        contract_raw = inline.group(2).strip()
        payment_raw = inline.group(3).strip()
        inkl = inline.group(4).strip()
        # Vollständiger Plan-Name wie im Angebot
        result['new_plan'] = plan_base
        result['plan_full_name'] = (
            f"{plan_base} | {contract_raw} [{payment_raw}]"
            + (f" {inkl}" if inkl else '')
        ).strip()
        if inkl:
            result['service_package'] = inkl.strip()  # z.B. "Premium L"
        if 'monatl' in payment_raw.lower():
            result['payment_type'] = 'monatlich'
        elif 'jährl' in payment_raw.lower() or 'annual' in payment_raw.lower():
            result['payment_type'] = 'jährlich'
        # Laufzeit in Monaten
        _dur = re.search(r'(\d+)', contract_raw)
        if _dur:
            n = int(_dur.group(1))
            result['contract_months'] = n * 12 if 'Jahres' in contract_raw else n
        # Chargebee plan_id auflösen (inkl. Versions-Suffix aus Service-Paket)
        if result.get('payment_type') and result.get('contract_months'):
            pid = resolve_chargebee_plan_id(
                plan_base, result['contract_months'], result['payment_type'],
                service_package=result.get('service_package', ''),
            )
            if pid:
                result['chargebee_plan_id'] = pid
    else:
        m = _PLAN_RE.search(text)
        if m:
            result['new_plan'] = m.group(0).strip()

    # Fallback: Laufzeit aus "N Monate" / "N Jahre" / "N-Jahres-..." extrahieren
    if not result.get('contract_months'):
        # Monate: "24 Monate", "24-monatig", "24-Monatsvertrag"
        _dur_m = re.search(
            r'\b(\d+)\s*[-\s]?\s*monat(?:e[ns]?|ig|svertrag)?', text, re.IGNORECASE
        )
        # Jahre: "2 Jahre", "2-Jahres-...", "2jährig", "2 jährlich"
        _dur_y = re.search(
            r'\b(\d+)\s*[-\s]?\s*(?:jahr(?:e[ns]?|ig)?|jährig)', text, re.IGNORECASE
        )
        if _dur_m:
            result['contract_months'] = int(_dur_m.group(1))
        elif _dur_y:
            result['contract_months'] = int(_dur_y.group(1)) * 12
        # Chargebee Plan-ID neu auflösen falls jetzt alle Felder vorhanden
        if result.get('contract_months') and result.get('new_plan') and result.get('payment_type'):
            pid = resolve_chargebee_plan_id(
                result['new_plan'], result['contract_months'], result['payment_type'],
                service_package=result.get('service_package', ''),
            )
            if pid:
                result['chargebee_plan_id'] = pid

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

    # Leistungsbeschreibung YYYY (z.B. "Leistungsbeschreibung 2026")
    lb_match = re.search(r'leistungsbeschreibung\s*(\d{4})', text, re.IGNORECASE)
    if lb_match:
        result['leistungsbeschreibung'] = lb_match.group(1)

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
        out.append('*Vertragsbeginn / Effective Date* — ab wann gilt die Änderung? (oder: ASAP/sofort)')
    # Service-Paket Pflicht wenn neuer Plan ein versionierter Slug ist (Pro 25, Business 25 etc.)
    plan_name = (parsed.get('new_plan') or '').lower()
    is_versioned_plan = any(s in plan_name for s in ('pro 25', 'pro25', 'business 25', 'business25', 'scale', 'launch'))
    if is_versioned_plan and not parsed.get('service_package'):
        out.append('*Service-Paket* — z.B. "Standard S", "Growth M", "Premium L" (bestimmt die Preis-Variante!)')
    return out


# ---------------------------------------------------------------------------
# Offer-Page Parser
# ---------------------------------------------------------------------------

def enrich_from_jira_tickets(parsed: dict, tickets: list[dict]) -> tuple[dict, list[dict]]:
    """Versucht fehlende Felder aus gefundenen Jira-Tickets zu ergänzen.

    Gibt (updated_parsed, relevant_tickets) zurück. relevant_tickets sind
    die Tickets aus denen Informationen extrahiert wurden.
    """
    if not tickets or not parsed.get('customer_name'):
        return parsed, []

    relevant = []
    for ticket in tickets:
        text = f"{ticket.get('summary', '')} {ticket.get('description', '')}"
        if not text.strip():
            continue

        # Parsen des Ticket-Textes mit demselben Parser
        ticket_parsed = parse_vertragsanpassung(text)
        found_something = False

        for key in ('new_plan', 'payment_type', 'effective_date', 'contract_months',
                    'service_package', 'plan_full_name', 'chargebee_plan_id'):
            if ticket_parsed.get(key) and not parsed.get(key):
                parsed[key] = ticket_parsed[key]
                found_something = True
                logger.info(f"Jira {ticket['key']}: {key}={ticket_parsed[key]!r} übernommen")

        if found_something:
            relevant.append(ticket)

    return parsed, relevant


def fetch_offer_data(url: str) -> dict:
    """Lädt eine Xentral-Angebots-URL und extrahiert Vertragsinformationen.

    Liest aus der HTML-Seite:
    - Firmenname (oben auf der Seite)
    - Plan + Zahlweise aus "Produkte & Services" (Format: "Plan | 12-Monatsvertrag [monatliche Zahlung]")
    """
    try:
        _validate_offer_url(url)
    except ValueError as exc:
        logger.warning(f"fetch_offer_data blocked (SSRF guard): {exc}")
        return {}
    try:
        resp = requests.get(
            url,
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (compatible; CS-Admin-Bot/1.0)'},
        )
        if not resp.ok:
            logger.warning(f"Offer URL {url}: HTTP {resp.status_code}")
            return {}

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp.text, 'html.parser')
        result: dict = {}

        # Normalisiere Non-Breaking-Spaces und andere Unicode-Whitespace
        full_text = soup.get_text(separator='\n', strip=True)
        full_text = full_text.replace('\xa0', ' ').replace(' ', ' ').replace(' ', ' ')

        # Log ersten 800 Zeichen für Debugging
        logger.info(f"Offer page text (first 800): {full_text[:800]!r}")

        company_candidates = []

        # Heading-Tags
        for tag in soup.find_all(['h1', 'h2', 'h3']):
            t = tag.get_text(strip=True)
            if 5 < len(t) < 120:
                company_candidates.append(t)

        # Elemente mit typischen Klassen für Empfänger-/Kundenname
        if not company_candidates:
            for sel in ['.company', '.customer', '.recipient', '.empfaenger',
                        '[class*="company"]', '[class*="recipient"]', '[class*="address"]']:
                el = soup.select_one(sel)
                if el:
                    t = el.get_text(strip=True)
                    if 5 < len(t) < 120:
                        company_candidates.append(t)
                        break

        # GmbH/AG-Suffix-Suche im Text (erstes Vorkommen)
        if not company_candidates:
            m = re.search(
                r'([A-ZÄÖÜ][a-zA-ZäöüÄÖÜ\s&.\-]{2,60}'
                r'(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|GbR)(?:\s*&\s*Co\.?\s*KG)?)',
                full_text,
            )
            if m:
                company_candidates.append(m.group(1).strip())

        if company_candidates:
            # Nur bis zum ersten GmbH/AG/... kürzen
            name = company_candidates[0]
            suffix = re.search(
                r'(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|GbR)(?:\s*&\s*Co\.?\s*KG)?',
                name, re.IGNORECASE,
            )
            if suffix:
                name = name[:suffix.end()].strip()
            result['customer_name'] = name

        # --- Plan + Zahlweise aus Produkte & Services ---
        # Format: "Pro 25 | 12-Monatsvertrag [monatliche Zahlung] inkl. ..."
        # Auch: "Pro 25 | 1-Jahresvertrag [jährliche Zahlung]"
        # Plan-Pattern: "Pro 25 | 12-Monatsvertrag [monatliche Zahlung]"
        plan_re = re.compile(
            r'([\w][\w \-]*?\d+[\w \-]*?)'
            r'[ \t]*[|｜][ \t]*'
            r'([\d]+[ \-]?(?:Monats|Jahres)vertrag[^\[]*)'
            r'[ \t]*\[([^\]]+)\]',
            re.IGNORECASE,
        )

        # Strategie 1: Jede einzelne <td>-Zelle prüfen
        plan_match = None
        for td in soup.find_all('td'):
            cell = td.get_text(separator=' ', strip=True).replace('\xa0', ' ')
            m = plan_re.search(cell)
            if m:
                plan_match = m
                logger.info(f"Plan found in td: {repr(cell[:120])}")
                break

        # Strategie 2: gesamter Text
        if not plan_match:
            plan_match = plan_re.search(full_text)
            logger.info(f"Plan regex in full_text: {bool(plan_match)}")

        # Strategie 3: roher HTML-Text (ohne BeautifulSoup)
        if not plan_match:
            import html as _html
            raw = _html.unescape(resp.text).replace('\xa0', ' ')
            plan_match = plan_re.search(raw)
            logger.info(f"Plan regex in raw HTML: {bool(plan_match)}")
        if plan_match:
            plan_raw = plan_match.group(1).strip()
            contract_raw = plan_match.group(2).strip()
            payment_raw = plan_match.group(3).strip()
            # "inkl. X" nach dem Match
            after = full_text[plan_match.end():plan_match.end() + 60].split('\n')[0].strip()
            inkl = after if after.lower().startswith('inkl') else ''

            result['new_plan'] = plan_raw
            result['plan_full_name'] = (
                f"{plan_raw} | {contract_raw} [{payment_raw}]"
                + (f" {inkl}" if inkl else '')
            ).strip()
            if inkl:
                result['service_package'] = inkl.strip()

            payment_lower = payment_raw.lower()
            if 'monatl' in payment_lower:
                result['payment_type'] = 'monatlich'
            elif 'jährl' in payment_lower or 'annual' in payment_lower:
                result['payment_type'] = 'jährlich'

            dur = re.search(r'(\d+)', contract_raw)
            if dur:
                n = int(dur.group(1))
                months = n * 12 if 'Jahres' in contract_raw else n
                result['contract_months'] = months
                if result.get('payment_type'):
                    pid = resolve_chargebee_plan_id(
                        plan_raw, months, result['payment_type'],
                        service_package=result.get('service_package', ''),
                    )
                    if pid:
                        result['chargebee_plan_id'] = pid

        logger.info(f"Offer data from {url}: {result}")
        return result

    except Exception as e:
        logger.warning(f"fetch_offer_data({url}) failed: {e}")
        return {}


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

    # Plan-ID: zuerst plan_id-Feld, dann aus subscription_items (neues Item-Modell)
    plan_id = sub.get('plan_id', '')
    if not plan_id and sub.get('subscription_items'):
        plan_item = next(
            (it for it in sub['subscription_items'] if it.get('item_type') == 'plan'),
            None,
        )
        if plan_item:
            plan_id = plan_item.get('item_price_id', '')

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
        'plan_id': plan_id,
        'addons': addons,
        'status': sub.get('status', ''),
        'billing_cycle': billing_cycle,
        'billing_period_unit': period_unit,
        'next_billing_at': _ts_to_date(sub.get('next_billing_at')),
        'current_term_end': _ts_to_date(sub.get('current_term_end')),
        'trial_end': _ts_to_date(sub.get('trial_end')),
        'coupons': coupons,
        'url': f"https://{site}.chargebee.com/d/subscriptions/{sub_id}",
        'cf_debit_number': str(sub.get('cf_debit_number', '')),
        '_raw_sub': sub,  # für IST-Plan-Fallback
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
        lines.append(f"• *Chargebee:* <{subscription['url']}|{subscription['subscription_id']}>")
        if subscription.get('service_package'):
            lines.append(f"• *Service-Paket (IST):* {subscription['service_package']}")
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
        date_val = parsed['effective_date']
        if date_val == 'ASAP' and subscription and subscription.get('next_billing_at'):
            date_val = f"Ab sofort _(vorgeschlagen: nächste Rechnung {subscription['next_billing_at']})_"
        elif date_val == 'ASAP':
            date_val = 'Ab sofort / ASAP'
        lines.append(f"• *Vertragsbeginn:* {date_val}")
    if parsed.get('offer_link'):
        lines.append(f"• *Angebots-Link:* {parsed['offer_link']}")
    if parsed.get('addons_add'):
        lines.append(f"• *Add-Ons hinzufügen:* {parsed['addons_add']}")
    if parsed.get('addons_remove'):
        lines.append(f"• *Add-Ons entfernen:* {parsed['addons_remove']}")
    if parsed.get('berichtswesen_tier'):
        lines.append(f"• *Berichtswesen-Tier:* `{parsed['berichtswesen_tier']}`")
    return '\n'.join(lines)


_SERVICE_PACKAGE_OPTIONS = [
    'Standard S', 'Standard M', 'Standard L',
    'Growth S', 'Growth M', 'Growth L',
    'Premium S', 'Premium M', 'Premium L',
]

_PLAN_DURATION_OPTIONS = [
    ('Pro 25 | 12M monatlich', 'Pro 25|12|monatlich'),
    ('Pro 25 | 12M jährlich',  'Pro 25|12|jährlich'),
    ('Pro 25 | 24M monatlich', 'Pro 25|24|monatlich'),
    ('Pro 25 | 24M jährlich',  'Pro 25|24|jährlich'),
    ('Scale | 12M monatlich',  'Scale|12|monatlich'),
    ('Scale | 24M monatlich',  'Scale|24|monatlich'),
    ('Launch | 24M monatlich', 'Launch|24|monatlich'),
]


def ask_for_va_info_blocks(
    user_id: str,
    missing: list[str],
    parsed: dict | None = None,
    subscription: dict | None = None,
) -> list[dict]:
    parsed = parsed or {}
    found = _format_found_fields(parsed, subscription)

    text = f"Hey <@{user_id}> :wave: Ich habe eine *Vertragsanpassungs-Anfrage* erkannt.\n\n"
    if found:
        text += f"*Bereits erkannt:*\n{found}\n\n"

    jira_sources = parsed.get('_jira_sources', [])
    if jira_sources:
        links = ', '.join(f"<{t['url']}|{t['key']}>" for t in jira_sources[:3])
        text += f"_Infos aus Jira ergänzt: {links}_\n\n"

    # Textfelder die kein Dropdown haben
    text_missing = [m for m in missing if 'Service-Paket' not in m and 'Plan' not in m]
    dropdown_missing_pkg = any('Service-Paket' in m for m in missing)
    dropdown_missing_plan = any('Plan' in m for m in missing)

    if text_missing:
        text += f"*Mir fehlen noch:*\n" + '\n'.join(f'• {m}' for m in text_missing)
        text += '\n\nBitte ergänze diese Informationen hier im Thread.'

    blocks: list[dict] = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]

    # Dropdown für Service-Paket
    if dropdown_missing_pkg:
        blocks.append({
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '*Service-Paket wählen* _(bestimmt die Preis-Variante)_'},
            'accessory': {
                'type': 'static_select',
                'placeholder': {'type': 'plain_text', 'text': 'Service-Paket auswählen…'},
                'action_id': 'va_select_service_package',
                'options': [
                    {'text': {'type': 'plain_text', 'text': pkg}, 'value': pkg}
                    for pkg in _SERVICE_PACKAGE_OPTIONS
                ],
            },
        })

    # Dropdown für Plan + Laufzeit
    if dropdown_missing_plan:
        blocks.append({
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': '*Plan & Laufzeit wählen*'},
            'accessory': {
                'type': 'static_select',
                'placeholder': {'type': 'plain_text', 'text': 'Plan auswählen…'},
                'action_id': 'va_select_plan',
                'options': [
                    {'text': {'type': 'plain_text', 'text': label}, 'value': value}
                    for label, value in _PLAN_DURATION_OPTIONS
                ],
            },
        })

    return blocks


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
        # Plan aus plan_id ODER subscription_items (neues Item-Modell)
        plan_id_ist = subscription.get('plan_id', '')
        if not plan_id_ist and subscription.get('_raw_sub'):
            for it in subscription.get('_raw_sub', {}).get('subscription_items', []):
                if it.get('item_type') == 'plan':
                    plan_id_ist = it.get('item_price_id', '')
                    break
        plan_info = f"`{plan_id_ist}`" if plan_id_ist else '`–`'
        if subscription.get('billing_cycle'):
            plan_info += f"  ·  {subscription['billing_cycle']}"
        ist_lines.append(f"*Aktueller Plan:* {plan_info}")
        if subscription.get('service_package'):
            ist_lines.append(f"*Service-Paket (IST):* {subscription['service_package']}")
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
        # Vollständiger Planname (mit Vertragslaufzeit + Zahlweise + inkl.)
        plan_display = parsed.get('plan_full_name') or parsed['new_plan']
        arrow = f" _(war: `{old}`)_" if old and old.lower() not in (parsed['new_plan'].lower(), plan_display.lower()) else ''
        soll_lines.append(f"• Neuer Plan: {plan_display}{arrow}")
        # Chargebee item_price_id + Listenpreis
        if parsed.get('chargebee_plan_id'):
            price_line = f"  → `item_price_id`: `{parsed['chargebee_plan_id']}`"
            list_price = parsed.get('chargebee_plan_list_price')
            negotiated_price = parsed.get('negotiated_price')
            if list_price is not None:
                eur_list = f"EUR {list_price / 100:,.2f}"
                price_line += f"  ·  Listenpreis: *{eur_list}/mo*"
                if negotiated_price is not None and negotiated_price != list_price:
                    eur_neg = f"EUR {negotiated_price / 100:,.2f}"
                    price_line += f"\n  ⚠️ *Verhandelter Preis: {eur_neg}/mo* — Price Override nötig!"
            soll_lines.append(price_line)
        # Service-Paket (IST → SOLL Vergleich) — Pflichtfeld für versionierte Pläne
        soll_pkg = parsed.get('service_package', '')
        ist_pkg = subscription.get('service_package', '') if subscription else ''
        plan_name_lower = (parsed.get('new_plan') or '').lower()
        is_versioned = any(s in plan_name_lower for s in ('pro 25', 'pro25', 'business 25', 'business25', 'scale', 'launch'))
        if soll_pkg:
            pkg_line = f"• Service-Paket: *{soll_pkg}*"
            if ist_pkg and ist_pkg.lower() != soll_pkg.lower():
                pkg_line += f" _(war: {ist_pkg})_"
            soll_lines.append(pkg_line)
        elif ist_pkg:
            soll_lines.append(f"• Service-Paket: {ist_pkg} _(unverändert — bitte prüfen)_")
        elif is_versioned:
            soll_lines.append("• Service-Paket: ⚠️ *nicht angegeben* — bestimmt die Preis-Variante! (Standard S/M/L, Growth S/M/L, Premium S/M/L)")
    if parsed.get('payment_type'):
        pay_line = f"• Zahlweise: {parsed['payment_type']}"
        if parsed.get('payment_type_inherited'):
            pay_line += ' _(aus IST übernommen)_'
        soll_lines.append(pay_line)
    if parsed.get('effective_date'):
        date_val = parsed['effective_date']
        if date_val == 'ASAP' and subscription and subscription.get('next_billing_at'):
            date_val = f"Ab sofort _(vorgeschlagen: nächste Rechnung {subscription['next_billing_at']})_"
        elif date_val == 'ASAP':
            date_val = 'Ab sofort / ASAP'
        soll_lines.append(f"• Vertragsbeginn: {date_val}")
    if parsed.get('addons_add'):
        soll_lines.append(f"• Add-Ons *hinzufügen:* {parsed['addons_add']}")
    if parsed.get('addons_remove'):
        soll_lines.append(f"• Add-Ons *entfernen:* {parsed['addons_remove']} _(laut SOP explizit prüfen!)_")
    if parsed.get('berichtswesen_tier'):
        soll_lines.append(f"• Berichtswesen-Tier: `{parsed['berichtswesen_tier']}`")
    if parsed.get('offer_link'):
        soll_lines.append(f"• Angebot: {parsed['offer_link']}")
    if parsed.get('leistungsbeschreibung'):
        soll_lines.append(f"• Leistungsbeschreibung: *{parsed['leistungsbeschreibung']}* _(Kunde wechselt in die aktuelle Leistungsbeschreibung)_")

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
        if dt:
            today = datetime.now(timezone.utc).date()
            if dt.date() > today:
                warnings.append(
                    f"⚠️ *Ramp nötig:* Vertragsbeginn ({parsed['effective_date']}) liegt in der Zukunft "
                    "→ wird automatisch über *✅ Geprüft — bitte ausführen* angelegt"
                )
            elif dt.date() <= today:
                warnings.append(
                    f"⚠️ *Effective Date heute oder Vergangenheit ({parsed['effective_date']})* "
                    "→ Ramp kann nicht automatisch angelegt werden — bitte *manuell in Chargebee* eintragen"
                )
    # Discount-Hinweis bewusst weggelassen (wird manuell behandelt)

    # Kontextuelle Hinweise aus Chargebee-Daten
    suggestions = _build_suggestions(parsed, subscription) if subscription else []

    # Nächste Schritte — immer beide Optionen anzeigen
    effective_date = parsed.get('effective_date')
    dt = _try_parse_date(effective_date) if effective_date else None
    is_future = dt and dt.date() > datetime.now(timezone.utc).date()
    if is_future:
        next_steps = (
            "*Option A — Automatisch:*\n"
            "1. Zusammenfassung prüfen\n"
            "2. *✅ Geprüft — bitte ausführen* klicken → Ramp wird automatisch angelegt\n"
            "3. Bei Preisabweichung: vorher Price Override in Chargebee manuell setzen\n\n"
            "*Option B — Manuell:*\n"
            "1. Subscription in Chargebee öffnen (Link oben)\n"
            "2. *🙋 Mache ich — ich übernehme* klicken\n"
            "3. Ramp manuell in Chargebee anlegen & im Thread als ✅ done markieren"
        )
    else:
        next_steps = (
            "⚠️ Effective Date heute → kein automatischer Ramp möglich\n\n"
            "1. Subscription in Chargebee öffnen (Link oben)\n"
            "2. Änderungen manuell eintragen\n"
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
