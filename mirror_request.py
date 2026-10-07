"""
Spiegelungs-Anfrage für eine BESTEHENDE Sandbox ("Sandbox ist jetzt da, bitte Spiegelung veranlassen").

Ablauf: Nachricht erkennen → Prod/Sandbox-URL und -Serial aus dem Text lesen → mit dem
Instance Manager (CSM-MCP customer_lookup, read-only) abgleichen bzw. ergänzen → im Thread
zusammenfassen und per Button fragen, ob das CCS-Ticket angelegt werden soll. Angelegt wird
erst nach Klick eines CS-Admins. Alle Daten stecken im Button-Wert, es gibt keinen Zustand.

Werte werden nie geraten: Ein Feld ist nur gesetzt, wenn es aus dem Text oder eindeutig aus dem
Instance Manager kommt. Widersprechen sich Text und Instance Manager, gewinnt nichts, der Bot
zeigt beides und legt nichts an.
"""
import json
import re

from sandbox_handler import csm_customer_lookup
from text_utils import extract_company_name

_URL_RE = re.compile(r'(?:https?://)?([a-z0-9][a-z0-9-]*\.xentral\.(?:biz|com|de|cloud))', re.IGNORECASE)
_SERIAL_RE = re.compile(r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b', re.IGNORECASE)
_SANDBOX_LINE_RE = re.compile(r'sandbox|ziel|target|test', re.IGNORECASE)
_PROD_LINE_RE = re.compile(r'produktiv|prod|haupt|live|quelle|source', re.IGNORECASE)

_MIRROR_RE = re.compile(r'spiegel|mirror', re.IGNORECASE)
_SANDBOX_RE = re.compile(r'sandbox|testumgebung', re.IGNORECASE)
# Hinweise, dass die Sandbox schon existiert (sonst ist es der Neuanlage-Flow)
_EXISTING_RE = re.compile(
    r'vorhanden|bestehend|existier|bereits|schon|jetzt\s+da|eingerichtet\s+(?:ist|wurde)|'
    r'angelegt\s+(?:ist|wurde)|ist\s+(?:jetzt\s+)?(?:da|fertig|live)',
    re.IGNORECASE,
)
_NEW_SANDBOX_RE = re.compile(r'sandbox\s*(?:einrichten|anlegen|erstellen|bestellen)|neue\s+sandbox', re.IGNORECASE)

_NAME_RE = re.compile(
    r'(?i:(?:sandbox|spiegelung|instanz|kunden?|kundin)\s+(?:von|für|fuer|bei)\s+'
    r'(?:dem\s+kunden\s+|der\s+firma\s+)?)([A-ZÄÖÜ0-9][\w&.\-+ ]{1,60}?)'
    r'(?=\s+(?i:jetzt|ist|sind|wurde|haben|hat|vorhanden|bitte|da|in|auf|die|den)\b|[,.:;!?\n]|$)'
)

_NAME_LOOSE_RE = re.compile(
    r'(?i:\b(?:für|fuer|von|bei)\s+(?:den\s+kunden\s+|die\s+firma\s+)?)([A-ZÄÖÜ0-9][\w&.\-+]*(?:\s+[A-ZÄÖÜ0-9&][\w&.\-+]*){0,4})'
)

ACTION_CREATE = 'mirror_request_create'
ACTION_CANCEL = 'mirror_request_cancel'


def detect_mirror_request(text: str) -> bool:
    """Spiegelung + Sandbox + Hinweis auf bestehende Sandbox (Wort oder Sandbox-URL im Text)."""
    t = text or ''
    if not (_MIRROR_RE.search(t) and _SANDBOX_RE.search(t)):
        return False
    if _NEW_SANDBOX_RE.search(t) and not _EXISTING_RE.search(t):
        return False
    parsed = parse_mirror_text(t)
    return bool(_EXISTING_RE.search(t) or parsed.get('sandbox_url'))


def _host(url: str | None) -> str:
    m = _URL_RE.search(url or '')
    return m.group(1).lower() if m else ''


def parse_mirror_text(text: str) -> dict:
    """Liest Prod/Sandbox-URL und -Serial zeilenweise. Eine Zeile mit 'Sandbox' gehört zur Sandbox,
    eine mit 'Produktiv/Prod' zur Hauptinstanz. Ohne Label wird nichts zugeordnet."""
    out: dict = {}
    for line in (text or '').splitlines():
        urls = _URL_RE.findall(line)
        serials = _SERIAL_RE.findall(line)
        if not (urls or serials):
            continue
        is_sb = bool(_SANDBOX_LINE_RE.search(line.split(urls[0])[0] if urls else line))
        is_prod = bool(_PROD_LINE_RE.search(line.split(urls[0])[0] if urls else line))
        if is_sb == is_prod:  # kein oder doppeltes Label: nicht zuordnen
            continue
        key = 'sandbox' if is_sb else 'prod'
        if urls and f'{key}_url' not in out:
            out[f'{key}_url'] = f'https://{urls[0].lower()}'
        if serials and f'{key}_serial' not in out:
            out[f'{key}_serial'] = serials[0].lower()
    m = _NAME_RE.search(text or '')
    if not m:
        m = _NAME_LOOSE_RE.search(text or '')
    out['customer_name'] = (m.group(1).strip() if m else None) or extract_company_name(text or '')
    return out


def _pick_match(parsed: dict) -> tuple[dict | None, str]:
    """Kunden im Instance Manager finden. Mit Prod-URL über die Instanz-ID (eindeutig, liefert die
    Sandboxen gleich mit), sonst über den Namen, eindeutig gemacht und per Kundennummer nachgeladen."""
    prod_host = _host(parsed.get('prod_url'))
    if prod_host:
        hits = [m for m in csm_customer_lookup(prod_host.split('.')[0], limit=2)
                if _host(m.get('instance_url')) == prod_host]
        if len(hits) == 1:
            return hits[0], ''
    matches = csm_customer_lookup(parsed['customer_name'], limit=5) if parsed.get('customer_name') else []
    if prod_host:
        same = [m for m in matches if _host(m.get('instance_url')) == prod_host]
        if same:
            matches = same
    if len(matches) != 1:
        if not matches:
            return None, 'Kunde im Instance Manager nicht gefunden'
        return None, f'{len(matches)} Kunden mit diesem Namen im Instance Manager, bitte Prod-URL angeben'
    m = matches[0]
    if m.get('kundennummer') and 'sandbox_instances' not in m:
        full = csm_customer_lookup(str(m['kundennummer']), limit=2)
        if len(full) == 1:
            m = full[0]
    return m, ''


def resolve_mirror_request(text: str) -> dict:
    """Ergebnis: customer_name, kundennummer, cs_package, felder {key: wert}, quellen {key: 'Nachricht'|
    'Instance Manager'|'beide'}, konflikte [..], hinweise [..], vollstaendig (bool)."""
    parsed = parse_mirror_text(text)
    res = {'customer_name': parsed.get('customer_name') or '', 'kundennummer': '', 'cs_package': '',
           'leistungsbeschreibung': '', 'felder': {}, 'quellen': {}, 'konflikte': [], 'hinweise': []}
    match, why = _pick_match(parsed)
    im: dict = {}
    if match:
        res['customer_name'] = match.get('name') or res['customer_name']
        res['kundennummer'] = str(match.get('kundennummer') or '')
        res['cs_package'] = match.get('cs_package') or ''
        res['leistungsbeschreibung'] = match.get('leistungsbeschreibung') or ''
        if match.get('instance_url'):
            im['prod_url'] = match['instance_url'].rstrip('/')
        if match.get('serial'):
            im['prod_serial'] = match['serial'].lower()
        if match.get('instance_url_is_sandbox_warning'):
            res['hinweise'].append(str(match['instance_url_is_sandbox_warning']))
        sbs = [s for s in (match.get('sandbox_instances') or [])
               if s.get('subscription_status') == 'active' and s.get('in_instance_manager') and s.get('instance_url')]
        want = _host(parsed.get('sandbox_url'))
        chosen = [s for s in sbs if _host(s['instance_url']) == want] if want else sbs
        if len(chosen) == 1:
            im['sandbox_url'] = chosen[0]['instance_url'].rstrip('/')
            if chosen[0].get('serial'):
                im['sandbox_serial'] = chosen[0]['serial'].lower()
        elif len(sbs) > 1 and not want:
            res['hinweise'].append(f'{len(sbs)} aktive Sandboxen im Instance Manager, bitte Sandbox-URL angeben')
        elif want and not chosen:
            res['hinweise'].append('Sandbox-URL aus der Nachricht ist beim Kunden im Instance Manager nicht hinterlegt')
        if match.get('sandbox_note'):
            res['hinweise'].append(str(match['sandbox_note']))
    elif why:
        res['hinweise'].append(why)

    for key in ('prod_url', 'prod_serial', 'sandbox_url', 'sandbox_serial'):
        a, b = parsed.get(key), im.get(key)
        same = (_host(a) == _host(b)) if key.endswith('url') else (a == b)
        if a and b and not same:
            res['konflikte'].append(f'{key}: Nachricht {a} ≠ Instance Manager {b}')
            continue
        val = b or a  # Instance-Manager-Schreibweise bevorzugen, wenn beide gleich sind
        if val:
            res['felder'][key] = val
            res['quellen'][key] = 'beide' if a and b else ('Nachricht' if a else 'Instance Manager')
    res['vollstaendig'] = (len(res['felder']) == 4 and not res['konflikte'] and bool(res['customer_name']))
    return res


_LABELS = {'prod_url': 'Prod URL', 'prod_serial': 'Prod Serial',
           'sandbox_url': 'Sandbox URL', 'sandbox_serial': 'Sandbox Serial'}
_SRC = {'beide': '✓ Nachricht + Instance Manager', 'Nachricht': 'aus der Nachricht', 'Instance Manager': 'aus dem Instance Manager'}


def build_mirror_request_blocks(res: dict, channel: str, thread_ts: str) -> list[dict]:
    lines = [f":arrows_counterclockwise: *Spiegelung auf bestehende Sandbox erkannt* · {res['customer_name'] or 'Kunde unklar'}"
             + (f" (Kd.-Nr. {res['kundennummer']})" if res['kundennummer'] else '')]
    if res['cs_package'] or res['leistungsbeschreibung']:
        lines.append(f"Paket: {res['cs_package'] or '–'} · LB {res['leistungsbeschreibung'] or '–'}")
    lines.append('')
    for key, label in _LABELS.items():
        val = res['felder'].get(key)
        lines.append(f"• {label}: {val} _({_SRC[res['quellen'][key]]})_" if val else f"• {label}: :x: fehlt")
    for k in res['konflikte']:
        lines.append(f":warning: Widerspruch {k}")
    for h in res['hinweise']:
        lines.append(f":information_source: {h}")
    blocks = [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': '\n'.join(lines)}}]
    if res['vollstaendig']:
        payload = json.dumps({'c': channel, 't': thread_ts, 'n': res['customer_name'], **res['felder']},
                             ensure_ascii=False)
        blocks.append({'type': 'section', 'text': {'type': 'mrkdwn',
                       'text': 'Soll ich das CCS-Ticket für die Spiegelung anlegen? _(nur CS Admin)_'}})
        blocks.append({'type': 'actions', 'elements': [
            {'type': 'button', 'action_id': ACTION_CREATE, 'style': 'primary', 'value': payload,
             'text': {'type': 'plain_text', 'text': 'Ja, CCS-Ticket anlegen'}},
            {'type': 'button', 'action_id': ACTION_CANCEL, 'value': payload,
             'text': {'type': 'plain_text', 'text': 'Nein'}},
        ]})
    else:
        blocks.append({'type': 'context', 'elements': [{'type': 'mrkdwn', 'text':
                       'Noch nicht vollständig: Bitte die fehlenden Werte im Thread nachreichen '
                       '(z. B. „Sandbox: <URL> / <Serial>“), dann prüfe ich erneut.'}]})
    return blocks
