"""
Gemeinsame Freitext-Utilities für mehrere Handler (Vertragsanpassung,
Sandbox-Anfrage, ...).
"""
import html as _html
import re

# Slack liefert Links als <https://…|Anzeigetext> bzw. <https://…>, Erwähnungen als
# <@U123>, <!here>, <#C123|channel>. Ohne Normalisierung landen "|Anzeigetext>" und
# "%7C…" in URLs (Angebots-Link-Warnung bei jedem Link) und Link-Markup in
# Kundennamen ("…Wein am Limit> auf folgenden Vertrag").
_SLACK_LINK_RE = re.compile(r'<(https?://[^|>\s]+)(?:\|([^>]*))?>')
_SLACK_SPECIAL_RE = re.compile(r'<(?:@[A-Z0-9]+|![a-z]+(?:\^[^>]*)?|#[A-Z0-9]+(?:\|[^>]*)?)>')
_URL_ANY_RE = re.compile(r'https?://\S+')


def normalize_slack_text(text: str) -> str:
    """Wandelt Slack-Markup in lesbaren Klartext um.

    <url|Label> → "Label url" (Label für die Namenserkennung, URL für die Link-Erkennung),
    <url> → "url", Erwähnungen und <!here> werden entfernt. Idempotent.
    """
    if not text:
        return ''
    def _link(m):
        url, label = m.group(1), (m.group(2) or '').strip()
        if label and label != url:
            return f'{label} {url}'
        return url
    text = _SLACK_LINK_RE.sub(_link, text)
    text = _SLACK_SPECIAL_RE.sub('', text)
    return text


def strip_urls(text: str) -> str:
    """Entfernt URLs, damit URL-Bestandteile nicht als Namen, Daten oder Pläne erkannt werden."""
    return _URL_ANY_RE.sub(' ', text or '')

# Erkennt explizit gelabelte Kundennennungen ("Kunde: X", "Firma X").
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

# Nur explizit bekannte Füll-/Artikelwörter am Anfang eines Firmennamens überspringen.
# Wichtig: Firmen wie "wev Schmalkalden" fangen klein an → NICHT nach
# Großbuchstaben suchen, sondern nur SKIP-Wörter überspringen.
_SKIP_WORDS = {
    'der', 'die', 'das', 'den', 'dem', 'des', 'für', 'fur', 'fuer',
    'bitte', 'hi', 'hey', 'team', 'hallo', 'liebe', 'lieber',
    'the', 'a', 'an', 'please', 'hello',
    'kunden', 'kunde', 'kundschaft', 'firma', 'unternehmen',
    # Sandbox-Anfrage-Flow: erlaubt "Sandbox für X GmbH anlegen" → "X GmbH"
    'sandbox', 'spiegelung', 'testumgebung',
}


def _clean_company_name(name: str) -> str:
    """Extrahiert nur den eigentlichen Firmennamen (kürzt vorne und hinten)."""
    # Hinten: alles nach dem rechtlichen Suffix abschneiden
    # Wortgrenzen: ohne sie trifft z.B. "ag" in "Vertragsanpassung" (→ Kunde "Vertrag")
    suffix = re.search(
        r'\b(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|Inc\.?|SAS|NV|BV)\b(?:\s*&\s*Co\.?\s*KG)?',
        name, re.IGNORECASE,
    )
    if suffix:
        name = name[:suffix.end()].strip()
        # Der Suffix-Treffer beginnt oft am Satzanfang ("Bitte Vertragsanpassung für HNG
        # Digital GmbH") → nur den Teil nach der letzten Präposition/Kundennennung nehmen.
        parts = re.split(r'\b(?:für|fuer|bei|beim|von|vom|kunde[n]?|customer|zu)\s+', name, flags=re.IGNORECASE)
        if len(parts) > 1 and parts[-1].strip():
            name = parts[-1].strip()

    words = name.split()
    start = 0
    for i, w in enumerate(words):
        if w.lower().rstrip(',:') in _SKIP_WORDS:
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


def extract_company_name(text: str) -> str | None:
    """Extrahiert einen Firmennamen aus Freitext, z.B. 'Sandbox für Musterfirma GmbH anlegen'.

    Versucht zuerst ein explizit gelabeltes Muster ('Kunde: X', 'Firma X'), danach
    eine direkte Suche nach einem Rechtsform-Suffix (GmbH, AG, ...) im Text.
    Gibt None zurück, wenn kein Firmenname erkannt werden konnte.

    Slack liefert Nachrichtentext HTML-entity-escaped (z.B. '&' als '&amp;') —
    ohne Unescape würde "GmbH & Co. KG" an "&amp;" zerbrechen und die
    Rechtsform-Endung (KG) verlorengehen. `html.unescape` ist idempotent,
    daher unschädlich falls der Aufrufer (z.B. parse_vertragsanpassung) schon
    unescaped hat.
    """
    text = strip_urls(normalize_slack_text(_html.unescape(text)))
    m = _CUSTOMER_LABELED_RE.search(text)
    if m:
        raw = _STRIP_COMPANY_PREFIX_RE.sub('', m.group(1)).strip()
        return _clean_company_name(raw)

    m = _COMPANY_SUFFIX_RE.search(text)
    if m:
        raw = _STRIP_COMPANY_PREFIX_RE.sub('', m.group(1)).strip()
        return _clean_company_name(raw)

    return None
