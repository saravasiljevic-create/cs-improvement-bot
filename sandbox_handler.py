"""
Sandbox-Anfrage-Flow: Intent-Erkennung, Parsing, Chargebee/Planhat-Lookup (read-only),
Preis-/Text-Mapping und E-Mail-/Slack-Block-Builder.

Spiegelt bewusst die Struktur und Konventionen von vertragsanpassung_handler.py.
"""
import logging
import re

import requests

from text_utils import extract_company_name
from vertragsanpassung_handler import _chargebee_customer_search, detect_vertragsanpassung

logger = logging.getLogger(__name__)

PLANHAT_BASE_URL = 'https://api.planhat.com'

# ---------------------------------------------------------------------------
# Intent Detection
# ---------------------------------------------------------------------------

# Starke Signale — 3 Punkte je Treffer
_STRONG = [
    r'sandbox\s*einrichten',
    r'sandbox\s*anlegen',
    r'sandbox\s*für',
    r'spiegelung\s*einrichten',
    r'spiegelung\s*auf.*sandbox',
]

# Mittlere Signale — 1 Punkt je Treffer
_MEDIUM = [
    r'\bsandbox\b',
    r'\bspiegelung\b',
    r'\btestumgebung\b',
]


def detect_sandbox_request(text: str) -> bool:
    """Gibt True zurück wenn der Text mit hoher Konfidenz eine Sandbox-Anfrage ist.

    Feuert nie gleichzeitig mit dem Vertragsanpassungs-Flow — die beiden Intents
    schließen sich gegenseitig aus.
    """
    if detect_vertragsanpassung(text):
        return False
    t = text.lower()
    score = sum(3 for p in _STRONG if re.search(p, t))
    score += sum(1 for p in _MEDIUM if re.search(p, t))
    return score >= 3


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_SPIEGELUNG_MENTION_RE = re.compile(r'\bspiegelung\b|\bmirror\w*\b', re.IGNORECASE)
_CUSTOM_CODE_RE = re.compile(r'\bcustom\s*code\b', re.IGNORECASE)
_CUSTOM_CODE_NEGATIVE_RE = re.compile(
    r'\bcustom\s*code\b[^.\n]{0,40}?\b(?:kein|nicht|ohne|nein|no|not)\b'
    r'|\b(?:kein|nicht|ohne|nein|no|not)\b[^.\n]{0,40}?\bcustom\s*code\b',
    re.IGNORECASE,
)
_CUSTOM_CODE_POSITIVE_RE = re.compile(
    r'\bcustom\s*code\b[^.\n]{0,40}?\b(?:ja|auch|ebenfalls|mit|übertragen|migrieren|yes)\b'
    r'|\b(?:ja|auch|ebenfalls|mit|übertragen|migrieren|yes)\b[^.\n]{0,40}?\bcustom\s*code\b',
    re.IGNORECASE,
)


def parse_sandbox_request(text: str) -> dict:
    """Extrahiert strukturierte Felder aus einer Sandbox-Anfrage im Freitext."""
    wants_spiegelung = bool(_SPIEGELUNG_MENTION_RE.search(text))

    custom_code: bool | None = None
    if _CUSTOM_CODE_RE.search(text):
        if _CUSTOM_CODE_NEGATIVE_RE.search(text):
            custom_code = False
        elif _CUSTOM_CODE_POSITIVE_RE.search(text):
            custom_code = True
        else:
            custom_code = None

    return {
        'customer_name': extract_company_name(text),
        'wants_spiegelung': wants_spiegelung,
        'custom_code': custom_code,
    }


# ---------------------------------------------------------------------------
# Chargebee-Lookup (read-only)
# ---------------------------------------------------------------------------

def get_chargebee_contact_email(customer_name: str, api_key: str, site: str) -> dict | None:
    """Sucht den Chargebee-Kunden und liefert die Decider-Kontakt-E-Mail + Vorname.

    Gibt None zurück wenn kein Treffer gefunden wurde, oder
    {'ambiguous': True, 'candidates': [...]} bei mehreren Treffern — niemals raten.
    """
    base = f"https://{site}.chargebee.com/api/v2"
    auth = (api_key, '')
    try:
        customers = _chargebee_customer_search(base, auth, customer_name)
    except Exception as e:
        logger.warning(f"get_chargebee_contact_email({customer_name!r}) failed: {e}")
        return None

    if not customers:
        return None
    if len(customers) > 1:
        return {
            'ambiguous': True,
            'candidates': [c['customer'].get('company') for c in customers],
        }

    customer = customers[0]['customer']
    billing_address = customer.get('billing_address') or {}
    return {
        'email': customer.get('email'),
        'first_name': billing_address.get('first_name'),
        'company': customer.get('company'),
        'customer_id': customer.get('id'),
        'debit_number': customer.get('cf_debit_number'),
    }


# ---------------------------------------------------------------------------
# Planhat-Lookup (read-only)
# ---------------------------------------------------------------------------

def _planhat_find_by_external_id(debit_number, api_token: str) -> dict | None:
    """Direkter Lookup über Planhats extid-Shortcut (GET /companies/extid-{externalId}) —
    ein einzelner, exakter Call statt seitenweisem Scannen. Siehe git-Historie
    (Commit 10d2921 'Fix Planhat lookup: use extid- direct endpoint instead of
    full scan', ursprünglich in opos_handler.py, jetzt im ausgelagerten
    opos-sperrpruefung-bot-Repo) — dieselbe Erkenntnis gilt hier 1:1."""
    headers = {'Authorization': f'Bearer {api_token}'}
    try:
        resp = requests.get(
            f'{PLANHAT_BASE_URL}/companies/extid-{debit_number}',
            headers=headers, timeout=15,
        )
        if resp.ok and isinstance(resp.json(), dict) and resp.json().get('_id'):
            return resp.json()
    except Exception as e:
        logger.warning(f"Planhat extid lookup failed for {debit_number}: {e}")
    return None


def _planhat_find_by_name_scan(customer_name: str, api_token: str) -> list:
    """Fallback nur wenn keine Debitorennummer vorliegt: Planhats /companies-
    Listenendpoint unterstützt KEINEN serverseitigen Namensfilter (Doku-Recherche
    2026-08-14, siehe Commit 10d2921) — 'companyName'-Query-Parameter wird
    schlicht ignoriert und liefert immer dieselbe Default-Seite zurück. Deshalb
    hier client-seitiger Abgleich über die komplette Liste (limit=5000/Seite,
    Planhats Maximum) statt eines (nicht-funktionierenden) Serverfilters."""
    headers = {'Authorization': f'Bearer {api_token}'}
    matches = []
    for offset in range(0, 20000, 5000):
        try:
            resp = requests.get(
                f'{PLANHAT_BASE_URL}/companies',
                params={'limit': 5000, 'offset': offset},
                headers=headers, timeout=20,
            )
            if not resp.ok:
                break
            page = resp.json()
        except Exception as e:
            logger.warning(f"Planhat name-scan page (offset={offset}) failed: {e}")
            break
        if not page:
            break
        matches.extend(c for c in page if c.get('name', '').lower() == customer_name.lower())
        if len(page) < 5000:
            break
    return matches


def get_planhat_sandbox_fields(customer_name: str, api_token: str, debit_number=None) -> dict | None:
    """Sucht den Planhat-Kunden und liefert die Sandbox-Preis-Custom-Fields.

    Bevorzugt den exakten extid-Lookup über die Chargebee-Debitorennummer
    (`debit_number` == Planhat `externalId`) — zuverlässig und schnell.
    Nur wenn keine Debitorennummer vorliegt, wird auf einen vollständigen
    Namens-Scan zurückgegriffen (siehe `_planhat_find_by_name_scan`).

    Gibt None zurück wenn kein Treffer gefunden wurde, oder
    {'ambiguous': True, 'candidates': [...]} bei mehreren Treffern — niemals raten.
    """
    company = None
    if debit_number:
        company = _planhat_find_by_external_id(debit_number, api_token)

    if not company:
        try:
            companies = _planhat_find_by_name_scan(customer_name, api_token)
        except Exception as e:
            logger.warning(f"get_planhat_sandbox_fields({customer_name!r}) failed: {e}")
            return None
        if not companies:
            return None
        if len(companies) > 1:
            return {
                'ambiguous': True,
                'candidates': [c.get('name') for c in companies],
            }
        company = companies[0]

    custom = company.get('custom') or {}
    sandbox_raw = custom.get('Sandbox')
    spiegelung_raw = custom.get('Sandbox Spiegelung')
    cs_package = custom.get('CS Package')
    return {
        'planhat_id': company.get('_id'),
        'name': company.get('name'),
        'sandbox_raw': sandbox_raw,
        'spiegelung_raw': spiegelung_raw,
        'cs_package': cs_package,
        'planhat_url': f"https://app.planhat.com/customer/{company.get('_id')}",
    }


# ---------------------------------------------------------------------------
# Preis-/Text-Mapping
# ---------------------------------------------------------------------------

_SANDBOX_PARAGRAPH_MAP = {
    "130€/99€": "kein_neues_servicepaket_oder_standard_sm",
    "free": "growth_s_premium_m",
    "49€": "standard_l",
}
_SPIEGELUNG_PARAGRAPH_MAP = {
    "149€": "kein_servicepaket",
    "299€": "standard_s_l",
    "199€": "growth_s_l",
    "99€": "premium_s_m",
}
_FREE_EQUIVALENT = {"free", "0€", "0", "kostenfrei"}


def resolve_paragraph_variants(
    sandbox_raw: str | None, spiegelung_raw: str | None, cs_package: str | None
) -> dict:
    """Löst die Preis-/Text-Variante für Template 1 (neue Sandbox) auf.

    Reihenfolge (siehe Plan, live gegen Produktionsdaten verifiziert):
    1. Success-Servicepaket-Override (Datenqualitäts-Fix) — geht IMMER vor.
    2. "Beide Felder free-äquivalent" → kombiniertes Premium-L-Sondertemplate.
    3. Ansonsten unabhängige Map-Lookups pro Feld.
    4. Unbekannter Wert in einem der beiden Felder → 'unresolved', niemals raten.
    """
    if cs_package and 'success' in cs_package.lower():
        return {
            'mode': 'standard',
            'sandbox_key': 'kein_neues_servicepaket_oder_standard_sm',
            'spiegelung_key': 'kein_servicepaket',
            'override_reason': 'success_package',
        }

    sandbox_norm = (sandbox_raw or '').strip().lower()
    spiegelung_norm = (spiegelung_raw or '').strip().lower()

    if sandbox_norm in _FREE_EQUIVALENT and spiegelung_norm in _FREE_EQUIVALENT:
        return {'mode': 'combined_premium_l'}

    sandbox_key = _SANDBOX_PARAGRAPH_MAP.get(sandbox_raw or '')
    if sandbox_key is None:
        return {'mode': 'unresolved', 'unresolved_field': 'Sandbox', 'raw_value': sandbox_raw}

    spiegelung_key = _SPIEGELUNG_PARAGRAPH_MAP.get(spiegelung_raw or '')
    if spiegelung_key is None:
        return {'mode': 'unresolved', 'unresolved_field': 'Sandbox Spiegelung', 'raw_value': spiegelung_raw}

    return {'mode': 'standard', 'sandbox_key': sandbox_key, 'spiegelung_key': spiegelung_key}


# Erweiterte Spiegelung-Map nur für Template 2 (Spiegelung auf bereits bestehender Sandbox) —
# ergänzt um den eigenständigen, kostenfreien Premium-L-Bucket. Template 1's Map bleibt
# unverändert (dort wird "beide Felder free" separat über combined_premium_l abgefangen).
_SPIEGELUNG_ONLY_PARAGRAPH_MAP = {
    **_SPIEGELUNG_PARAGRAPH_MAP,
    **{val: "premium_l" for val in _FREE_EQUIVALENT},
}


def resolve_spiegelung_only_variant(spiegelung_raw: str | None, cs_package: str | None) -> dict:
    """Löst die Preis-/Text-Variante für Template 2 (Spiegelung auf bestehender Sandbox) auf.

    Spiegelt resolve_paragraph_variants' Success-Override-Logik, betrachtet aber
    ausschließlich das Sandbox-Spiegelung-Feld.
    """
    if cs_package and 'success' in cs_package.lower():
        return {
            'mode': 'standard',
            'spiegelung_key': 'kein_servicepaket',
            'override_reason': 'success_package',
        }

    spiegelung_norm = (spiegelung_raw or '').strip().lower()
    if spiegelung_norm in _FREE_EQUIVALENT:
        return {'mode': 'standard', 'spiegelung_key': 'premium_l'}

    spiegelung_key = _SPIEGELUNG_ONLY_PARAGRAPH_MAP.get(spiegelung_raw or '')
    if spiegelung_key is None:
        return {'mode': 'unresolved', 'unresolved_field': 'Sandbox Spiegelung', 'raw_value': spiegelung_raw}

    return {'mode': 'standard', 'spiegelung_key': spiegelung_key}


# ---------------------------------------------------------------------------
# E-Mail-Templates — wortgleich aus Saras Vorlage übernommen, NICHT umformulieren.
# ---------------------------------------------------------------------------

_SANDBOX_PARAGRAPHS = {
    "kein_neues_servicepaket_oder_standard_sm": (
        "Die Sandbox kostet im Monatsvertrag 130€ im Monat, im Jahresvertrag 99€ im Monat. "
        "Monatsverträge sind mit einer Frist von 30 Tagen zum Ende der monatlichen Laufzeit "
        "kündbar, Jahresverträge mit einer Frist von 3 Monaten zum Ende der jährlichen "
        "Laufzeit (siehe Punkt 13 unserer AGB)."
    ),
    "standard_l": (
        "Im Rahmen eures Standard L-Servicepakets erhaltet ihr die Sandbox zum reduzierten "
        "Preis von 49€ pro Monat. Sie ist mit einer Frist von 30 Tagen zum Ende der "
        "monatlichen Laufzeit kündbar (siehe Punkt 13 unserer AGB)."
    ),
    "growth_s_premium_m": (
        "Im Rahmen eures Servicepakets stellen wir euch die Sandbox kostenfrei zur Verfügung."
    ),
}

_SPIEGELUNG_PARAGRAPHS = {
    "kein_servicepaket": "Die optionale einmalige Spiegelung der Daten kostet 149€.",
    "standard_s_l": "Die optionale einmalige Spiegelung kostet 299€.",
    "growth_s_l": "Die optionale einmalige Spiegelung der Daten kostet 199€.",
    "premium_s_m": "Die optionale einmalige Spiegelung der Daten kostet 99€.",
    "premium_l": "Im Rahmen deines Premium L Service-Pakets ist die Spiegelung für dich kostenfrei.",
}

_BASE_TEMPLATE = """{GREETING}

danke für deine Nachricht! Gerne richten wir dir eine Sandbox ein. Diese wird standardmäßig leer bereitgestellt, auf Wunsch können wir aber gerne die Daten aus deiner Hauptinstanz auf die Sandbox spiegeln.

{SANDBOX_PARAGRAPH}

{SPIEGELUNG_PARAGRAPH}

Bitte beachte, dass der initiale Versand der Sandbox-Zugangsdaten aus technischen Gründen nur an die E-Mail-Adresse des vertraglichen Entscheidungsträgers möglich ist, in eurem Fall also an {DECIDER_EMAIL}. Natürlich könnt ihr nach Erhalt aber beliebige weitere Nutzer hinzufügen.

Bitte bestätige mir kurz, dass du damit einverstanden bist und ob du eine Spiegelung der Daten wünschst. Wenn ihr eine Spiegelung wollt, teile uns bitte auch mit, ob euer Custom Code (falls vorhanden) ebenfalls übertragen werden soll. Sobald wir diese Infos haben, gebe ich die Erstellung der Sandbox umgehend in Auftrag."""

_COMBINED_PREMIUM_L_TEMPLATE = """{GREETING}

danke für deine Nachricht! Gerne richten wir dir eine Sandbox ein. Diese wird standardmäßig leer bereitgestellt, auf Wunsch können wir aber gerne die Daten aus deiner Hauptinstanz auf die Sandbox spiegeln.

Im Rahmen eures Premium L-Servicepakets ist sowohl die Bereitstellung der Sandbox als auch die optionale einmalige Spiegelung der Daten für euch kostenfrei.

Bitte beachtet, dass der initiale Versand der Sandbox-Zugangsdaten nur an die E-Mail-Adresse des vertraglichen Entscheidungsträgers möglich ist, in eurem Fall also an {DECIDER_EMAIL}. Natürlich könnt ihr nach Erhalt aber beliebige weitere Nutzer hinzufügen.

Bitte bestätige mir kurz, ob du eine Spiegelung der Daten wünschst. Wenn ja, teile mir bitte auch mit, ob euer Custom Code (falls vorhanden) dabei ebenfalls übertragen werden soll. Sobald wir diese Infos haben, gebe ich die Erstellung der Sandbox umgehend in Auftrag.

Gib mir gerne Bescheid, wenn Du weitere Infos brauchst."""

_EXISTING_SANDBOX_MIRRORING_TEMPLATE = """{GREETING}

danke für deine Nachricht! Gerne spiegeln wir die Daten aus deiner Hauptinstanz auf deine Sandbox.

{SPIEGELUNG_PARAGRAPH}

Bitte beachte, dass bei der Spiegelung alle Daten und Einstellungen aus der Hauptinstanz übernommen werden. Das bedeutet, dass die bestehenden Daten und Einstellungen der Sandbox gelöscht beziehungsweise überschrieben werden.

Zudem sind nach der Spiegelung alle Prozessstarter in der Sandbox standardmäßig deaktiviert und müssen zum Testen gegebenenfalls wieder aktiviert werden.

Bitte bestätige mir kurz, ob du damit einverstanden bist, bevor ich unsere Entwickler mit der Spiegelung beauftrage. Wenn ihr Custom Code nutzt, teile uns bitte auch mit, ob dieser ebenfalls in die Sandbox übertragen werden soll."""


def _greeting(first_name: str | None) -> str:
    """Anrede-Regel: immer nur Vorname ('Hallo Tamara,'), sonst Fallback ohne Namen ('Hallo,')."""
    return f"Hallo {first_name}," if first_name else "Hallo,"


def build_customer_email(first_name: str | None, decider_email: str, variant: dict) -> str:
    """Baut den fertigen Kunden-E-Mail-Text für die Neu-Sandbox-Anfrage (Template 1).

    variant['mode'] muss 'standard' oder 'combined_premium_l' sein — 'unresolved'
    darf hier nie ankommen (der Aufrufer muss stattdessen eine Rückfrage stellen).
    """
    greeting = _greeting(first_name)
    if variant['mode'] == 'combined_premium_l':
        return _COMBINED_PREMIUM_L_TEMPLATE.format(GREETING=greeting, DECIDER_EMAIL=decider_email)
    if variant['mode'] == 'standard':
        sandbox_paragraph = _SANDBOX_PARAGRAPHS[variant['sandbox_key']]
        spiegelung_paragraph = _SPIEGELUNG_PARAGRAPHS[variant['spiegelung_key']]
        return _BASE_TEMPLATE.format(
            GREETING=greeting,
            SANDBOX_PARAGRAPH=sandbox_paragraph,
            SPIEGELUNG_PARAGRAPH=spiegelung_paragraph,
            DECIDER_EMAIL=decider_email,
        )
    raise ValueError(f"build_customer_email: unerwarteter variant['mode']={variant.get('mode')!r}")


def build_existing_sandbox_mirroring_email(first_name: str | None, decider_email: str, spiegelung_key: str) -> str:
    """Baut den fertigen Kunden-E-Mail-Text für Template 2 (Spiegelung auf bestehender Sandbox).

    decider_email wird hier bewusst NICHT in den Text eingesetzt (keine neuen
    Zugangsdaten bei diesem Szenario) — Parameter bleibt aus Konsistenzgründen
    mit der übrigen Aufruf-Konvention (z.B. für künftiges Logging/Audit) erhalten.
    """
    greeting = _greeting(first_name)
    spiegelung_paragraph = _SPIEGELUNG_PARAGRAPHS[spiegelung_key]
    return _EXISTING_SANDBOX_MIRRORING_TEMPLATE.format(
        GREETING=greeting,
        SPIEGELUNG_PARAGRAPH=spiegelung_paragraph,
    )


# ---------------------------------------------------------------------------
# Slack Block Kit Builder
# ---------------------------------------------------------------------------

_VARIANT_EXPLANATIONS = {
    "kein_neues_servicepaket_oder_standard_sm": "Planhat-Feld `Sandbox` = '130€/99€' → kein neues Servicepaket / Standard S–M",
    "standard_l": "Planhat-Feld `Sandbox` = '49€' → Standard L",
    "growth_s_premium_m": "Planhat-Feld `Sandbox` = 'free' → Growth S – Premium M",
}
_SPIEGELUNG_EXPLANATIONS = {
    "kein_servicepaket": "Planhat-Feld `Sandbox Spiegelung` = '149€' → kein Servicepaket",
    "standard_s_l": "Planhat-Feld `Sandbox Spiegelung` = '299€' → Standard S–L",
    "growth_s_l": "Planhat-Feld `Sandbox Spiegelung` = '199€' → Growth S–L",
    "premium_s_m": "Planhat-Feld `Sandbox Spiegelung` = '99€' → Premium S–M",
    "premium_l": "Planhat-Feld `Sandbox Spiegelung` = 'free'/'kostenfrei' → Premium L (kostenfrei)",
}


def build_sandbox_lookup_result_blocks(
    customer_name: str, chargebee_result: dict, planhat_result: dict, variant: dict, email_text: str
) -> list[dict]:
    """Recap der Lookups + fertiger E-Mail-Entwurf zum 1:1-Kopieren."""
    lines = [f"*Kunde:* {customer_name}"]
    if chargebee_result.get('email'):
        lines.append(f"*Chargebee-Kontakt (Decider):* {chargebee_result['email']}")
    if planhat_result.get('planhat_url'):
        lines.append(f"*Planhat:* <{planhat_result['planhat_url']}|{planhat_result.get('name', customer_name)}>")

    if variant.get('override_reason') == 'success_package':
        lines.append(":warning: Servicepaket enthält 'Success' → Basis-Variante erzwungen (Datenqualitäts-Fix)")
    elif variant.get('mode') == 'combined_premium_l':
        lines.append("*Preis-Variante:* Beide Felder free-äquivalent → kombiniertes Premium-L-Sondertemplate")
    else:
        sandbox_key = variant.get('sandbox_key')
        spiegelung_key = variant.get('spiegelung_key')
        if sandbox_key in _VARIANT_EXPLANATIONS:
            lines.append(f"*Preis-Variante Sandbox:* {_VARIANT_EXPLANATIONS[sandbox_key]}")
        if spiegelung_key in _SPIEGELUNG_EXPLANATIONS:
            lines.append(f"*Preis-Variante Spiegelung:* {_SPIEGELUNG_EXPLANATIONS[spiegelung_key]}")

    text = '\n'.join(lines)
    text += f"\n\n```\n{email_text}\n```"
    text += "\n\nPasst das so? Antworte mit *ja* oder beschreibe kurz, was nicht passt."
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


def build_sandbox_clarification_blocks(reason: str) -> list[dict]:
    """Rückfrage bei Mehrdeutigkeit/keinem Match/unbekanntem Feldwert — niemals raten."""
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': f":thinking_face: {reason}"}}]


def build_sandbox_come_back_later_blocks() -> list[dict]:
    """Bestätigt den Entwurf und bittet den CSM, nach Kundenantwort in den Thread zurückzukommen."""
    text = (
        "Perfekt! Schick die E-Mail an den Kunden. Sobald du eine Antwort hast, "
        "schreib einfach hier im Thread weiter — ich frage dich dann nach den nächsten Schritten."
    )
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


def build_sandbox_scope_question_blocks() -> list[dict]:
    """Fragt per Buttons, welches der drei Szenarien final umgesetzt werden soll."""
    return [
        {
            'type': 'section',
            'text': {'type': 'mrkdwn', 'text': 'Was soll final umgesetzt werden?'},
        },
        {
            'type': 'actions',
            'elements': [
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': 'Nur neue Sandbox anlegen'},
                    'action_id': 'sandbox_scope_new_only',
                    'value': 'new_only',
                },
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': 'Neue Sandbox + Spiegelung'},
                    'action_id': 'sandbox_scope_new_plus_mirror',
                    'value': 'new_plus_mirror',
                },
                {
                    'type': 'button',
                    'text': {'type': 'plain_text', 'text': 'Spiegelung auf bestehender Sandbox'},
                    'action_id': 'sandbox_scope_mirror_existing',
                    'value': 'mirror_existing',
                },
            ],
        },
    ]


def build_sandbox_step2_stub_blocks(scope: str) -> list[dict]:
    """Platzhalter für Schritt 2 (Chargebee-Umsetzung).

    TODO: replace with real, detailed guidance (+ screenshots) once Sara provides the content.
    """
    if scope == 'new_only':
        text = (
            "Als nächstes: Sandbox-Item in Chargebee hinzufügen (Namenskonvention `sandbox-*`, "
            "z.B. `sandbox-monthly-contract-monthly-payment`). Eine ausführliche Anleitung mit "
            "Screenshots folgt hier bald."
        )
    elif scope == 'new_plus_mirror':
        text = (
            "Als nächstes: Sandbox-Item in Chargebee hinzufügen (Namenskonvention `sandbox-*`, "
            "z.B. `sandbox-monthly-contract-monthly-payment`). Eine ausführliche Anleitung mit "
            "Screenshots folgt hier bald.\n\n"
            "Zusätzlich wird für die Spiegelung ein Jira-Ticket angelegt, sobald die Instanz-Infos "
            "vorliegen (siehe nächste Nachricht)."
        )
    elif scope == 'mirror_existing':
        text = (
            "Als nächstes: Spiegelung auf der bestehenden Sandbox-Subscription in Chargebee "
            "vermerken. Eine ausführliche Anleitung mit Screenshots folgt hier bald.\n\n"
            "Zusätzlich wird für die Spiegelung ein Jira-Ticket angelegt, sobald die Instanz-Infos "
            "vorliegen (siehe nächste Nachricht)."
        )
    else:
        text = "Unbekanntes Szenario — bitte manuell in Chargebee prüfen."
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


# ---------------------------------------------------------------------------
# Instanz-Info-Parsing (Prod/Sandbox URL + Serial, vom CSM per Freitext erfragt)
# ---------------------------------------------------------------------------

_INSTANCE_INFO_PATTERNS = {
    'prod_url': r'prod[\s\-_]*url\s*[:\-]\s*(\S+)',
    'prod_serial': r'prod[\s\-_]*serial\s*[:\-]\s*(\S+)',
    'sandbox_url': r'sandbox[\s\-_]*url\s*[:\-]\s*(\S+)',
    'sandbox_serial': r'sandbox[\s\-_]*serial\s*[:\-]\s*(\S+)',
}


def parse_instance_info(text: str) -> dict:
    """Parst die 4 gelabelten Instanz-Info-Zeilen aus einer CSM-Antwort.

    Toleriert kleinere Formatierungsabweichungen (Bindestrich/Unterstrich im Label,
    ':' oder '-' als Trenner, zusätzliche Leerzeichen, Groß-/Kleinschreibung) — mehr
    braucht es nicht, da der CSM lediglich das vorgegebene Format grob nachtippt.
    Gibt nur die tatsächlich gefundenen Keys zurück (fehlende Keys sind schlicht
    nicht im Dict enthalten, nicht None).
    """
    result = {}
    for key, pattern in _INSTANCE_INFO_PATTERNS.items():
        match = re.search(pattern, text or '', re.IGNORECASE)
        if match:
            result[key] = match.group(1)
    return result


def build_sandbox_instance_info_request_blocks() -> list[dict]:
    """Fragt den CSM nach den 4 Instanz-Werten, bevor das Jira-Ticket angelegt wird."""
    text = (
        "Bevor ich das Jira-Ticket für die Spiegelung anlege, brauche ich noch die "
        "Instanz-Infos. Bitte antworte mit allen vier Zeilen (Format beibehalten):\n\n"
        "Prod URL: ...\n"
        "Prod Serial: ...\n"
        "Sandbox URL: ...\n"
        "Sandbox Serial: ..."
    )
    return [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}]


# ---------------------------------------------------------------------------
# Jira-Ticket (Spiegelung) — reales, verifiziertes Format (siehe CCS-1956).
# ---------------------------------------------------------------------------

def create_sandbox_mirroring_ticket(
    customer_name: str, prod_url: str, prod_serial: str, sandbox_url: str, sandbox_serial: str
) -> dict | None:
    """Legt das Jira-Ticket für die Sandbox-Spiegelung im CCS-Projekt an.

    Format 1:1 aus dem realen Produktions-Ticket CCS-1956 übernommen: Projekt CCS,
    Issue-Type Task, feste generische Summary, Description mit Kundenname und den
    4 vom CSM eingetragenen Instanz-Werten (keine Platzhalter im finalen Ticket).

    Gibt None zurück wenn die Ticket-Erstellung fehlschlägt (Fehler wird geloggt) —
    ein Fehlschlag beim Ticket-Anlegen darf den Bot nicht crashen lassen. Der Aufrufer
    muss ein None-Ergebnis dem CSM verständlich melden statt es stillschweigend zu
    ignorieren.
    """
    from jira_handler import create_ccs_mirroring_ticket

    summary = "Mirror data from Prod to Sandbox"
    description = (
        "Hi Team,\n\n"
        f"for the customer **{customer_name}**, could you please mirror the data from "
        "their Prod instance to their Sandbox?\n\n"
        "**Prod/Source:**\n"
        f"URL: {prod_url}\n"
        f"Serial: {prod_serial}\n\n"
        "**Sandbox/Target:**\n"
        f"URL: {sandbox_url}\n"
        f"Serial: {sandbox_serial}\n\n"
        "* Please see Zendesk Support tab for further comments and attachments."
    )
    try:
        return create_ccs_mirroring_ticket(summary, description)
    except Exception as e:
        logger.warning(f"create_sandbox_mirroring_ticket({customer_name!r}) failed: {e}")
        return None
