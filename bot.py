import logging
import os
import re
import time
from datetime import datetime, timezone

import requests

from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from config import (
    CHARGEBEE_API_KEY,
    CHARGEBEE_SITE,
    CS_ADMIN_USER_IDS,
    PLANHAT_API_TOKEN,
    PLANHAT_WORKSPACE_URL,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_SIGNING_SECRET,
    VERTRAGSANPASSUNG_CHANNEL_ID,
)
from jira_handler import add_vote, create_ticket, search_similar_tickets
from optimizer import optimize_ticket
from slack_utils import format_error, format_ticket_created
from vertragsanpassung_handler import (
    ask_for_va_info_blocks,
    build_cs_admin_subscription_blocks,
    build_va_summary_blocks,
    detect_vertragsanpassung,
    extract_service_package,
    fetch_item_price,
    fetch_item_price_name,
    fetch_offer_data,
    lookup_chargebee_subscription,
    missing_va_fields,
    parse_vertragsanpassung,
    _fetch_subscription_by_id,
)

# Erkennt Chargebee-Links (Subscription ODER Customer) und Standard-IDs
_CB_URL_RE = re.compile(
    r'https?://[^\s]*chargebee\.com/d/(?P<type>subscriptions|customers)/(?P<id>[^\s/\|><]+)'
    r'|\b(?P<std_id>\d{4}-\d{4}-\d{4}-\d{4})\b',
    re.IGNORECASE,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BOT_VERSION = "v2.4"
logger.info(f"Bot starting — version {_BOT_VERSION}")

# Custom-Emoji für die VA-Zusammenfassung (Slack-Name ohne Doppelpunkte)
# Sobald das Custom-Emoji erstellt ist, diesen Wert anpassen:
VA_DONE_EMOJI = os.environ.get('VA_DONE_EMOJI', 'csadmin-bot')

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# (channel, thread_ts) -> {'user_id', 'user_name', 'request_date', 'title', 'use_case',
#                          'slack_link', 'images', 'created_at'}
_pending: dict[tuple[str, str], dict] = {}
# (channel, thread_ts) -> context stored when similar tickets were shown (for rejection flow)
_ticket_data: dict[tuple[str, str], dict] = {}
_similar_shown: dict[tuple[str, str], dict] = {}

# Vertragsanpassungs-Flow state
# (channel, thread_ts) -> {'parsed': dict, 'user_id', 'user_name', 'created_at'}
_pending_vertragsanpassung: dict[tuple[str, str], dict] = {}

# VA-Zusammenfassungen die auf CS Admin Bestätigung warten (für 48h Reminder)
# (channel, thread_ts) -> {'sent_at': float, 'reminded': bool}
_va_pending_approval: dict[tuple[str, str], dict] = {}
VA_REMINDER_TTL = 48 * 3600  # 48 Stunden

# Warten auf Planhat-Link nach fehlgeschlagener Company-Suche
# (channel, thread_ts) -> {'action': 'upload'|'log', 'files': list, 'parsed': dict,
#                           'subscription': dict|None, 'user_name': str}
_pending_planhat_link: dict[tuple[str, str], dict] = {}

JIRA_KEY_RE = re.compile(r'\b([A-Z]+-\d+)\b')
PENDING_TTL = 72 * 3600  # 72 hours in seconds

REJECTION_RE = re.compile(
    r'passen?\s+nicht|passt?\s+nicht|nicht\s+passend|'
    r'trifft?\s+nicht\s+zu|stimmt?\s+nicht|'
    r'kein[e]?\s*(?:match|treffer|übereinstimmung)|'
    r'nicht\s+(?:relevant|zutreffend|richtig|das\s+gleiche|was\s+ich\s+meine|gemeint)|'
    r'anders|falsch|wrong|'
    r'not\s+(?:relevant|matching|applicable|right)|'
    r"doesn'?t\s+match|don'?t\s+match|no\s+match",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_user_name(client, user_id: str) -> str:
    try:
        info = client.users_info(user=user_id)
        profile = info['user']['profile']
        return profile.get('display_name') or profile.get('real_name') or user_id
    except Exception:
        return user_id


def ts_to_date(ts: str) -> str:
    try:
        dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        return dt.strftime('%d.%m.%Y')
    except Exception:
        return ts


def slack_message_link(channel: str, ts: str) -> str:
    return f"https://slack.com/archives/{channel}/p{ts.replace('.', '')}"


def extract_images(files) -> list[dict]:
    return [
        f for f in (files or [])
        if f.get('mimetype', '').startswith('image/')
    ]


def extract_files(files) -> list[dict]:
    """Alle Dateien aus einem Slack-Event — inkl. PDFs, Word-Docs, etc."""
    return [f for f in (files or []) if f.get('id')]


def parse_request(text: str) -> tuple[str | None, str | None]:
    """Extract title and use case from a message."""
    clean = re.sub(r'#improvement', '', text, flags=re.IGNORECASE).strip()
    clean = re.sub(r'<@[A-Z0-9]+>', '', clean).strip()

    title: str | None = None
    use_case: str | None = None

    title_match = re.search(
        r'(?:titel|title)\s*[:\-]\s*(.+?)(?:\n|$)', clean, re.IGNORECASE
    )
    uc_match = re.search(
        r'(?:use\s*case|beschreibung|problem|warum|grund|why|description)\s*[:\-]\s*(.+)',
        clean, re.IGNORECASE | re.DOTALL
    )

    if title_match:
        title = title_match.group(1).strip()
    if uc_match:
        use_case = uc_match.group(1).strip()

    if not title and not use_case:
        lines = [l.strip() for l in clean.split('\n') if l.strip()]
        if lines:
            title = lines[0]
        if len(lines) > 1:
            use_case = '\n'.join(lines[1:]).strip()

    return title or None, use_case or None


def missing_info(title, use_case) -> list[str]:
    missing = []
    if not title:
        missing.append('*Titel* (was soll sich verbessern?)')
    if not use_case:
        missing.append('*Use Case / Beschreibung* (warum ist das wichtig und wie wird es genutzt?)')
    return missing


def validate_use_case(title: str | None, use_case: str | None) -> str | None:
    if not use_case:
        return None

    uc = use_case.strip()
    if len(uc.split()) < 4 or len(uc) < 20:
        return (
            ":pencil2: Der *Use Case* ist sehr kurz. "
            "Bitte erkläre etwas ausführlicher: *warum* ist das wichtig, "
            "*wie* wird es genutzt und welchen *Mehrwert* bringt die Verbesserung?"
        )

    if title and uc.lower() == title.lower():
        return (
            ":pencil2: Der *Use Case* entspricht dem Titel. "
            "Bitte beschreibe den Hintergrund und den Mehrwert etwas genauer."
        )

    return None


def ask_for_info_blocks(user_id: str, missing: list[str]) -> list[dict]:
    items = '\n'.join(f'• {m}' for m in missing)
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"Hey <@{user_id}> :wave: Danke für dein Improvement-Request!\n\n"
                    f"Mir fehlen noch folgende Angaben:\n{items}\n\n"
                    "Bitte ergänze diese Informationen hier im Thread."
                ),
            },
        }
    ]


def found_ticket_blocks(tickets: list[dict], channel: str, thread_ts: str) -> list[dict]:
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ":mag: Es gibt bereits ähnliche Ticket(s) im CS Admin Board:",
            },
        }
    ]
    for t in tickets:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{t['url']}|{t['key']}>*: {t['summary']}\n"
                    f"Status: `{t['status']}` | Assignee: {t['assignee']}"
                ),
            },
        })
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": (
                ":point_up: Wenn dein Request durch eines dieser Tickets abgedeckt ist, "
                "schreibe die *Ticket-Nummer* (z.B. `CS-123`) hier in den Thread — "
                "ich erledige das Upvoting automatisch! :thumbsup:"
            ),
        },
    })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "➕ Kein Ticket passt — neues anlegen"},
                "style": "primary",
                "action_id": "reject_similar_create_ticket",
                "value": f"{channel}|||{thread_ts}",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "❌ Kein Ticket nötig"},
                "action_id": "cancel_create_ticket",
                "value": f"{channel}|||{thread_ts}",
            },
        ],
    })
    return blocks


# ---------------------------------------------------------------------------
# Reaction helpers
# ---------------------------------------------------------------------------

def _add_reaction(client, channel: str, ts: str, emoji: str):
    try:
        client.reactions_add(channel=channel, name=emoji, timestamp=ts)
    except Exception as e:
        err = str(e)
        if 'already_reacted' not in err:
            logger.warning(f"reactions_add :{emoji}: failed on {channel}/{ts}: {err}")


def _remove_reaction(client, channel: str, ts: str, emoji: str):
    try:
        client.reactions_remove(channel=channel, name=emoji, timestamp=ts)
    except Exception as e:
        err = str(e)
        if 'no_reaction' not in err:
            logger.warning(f"reactions_remove :{emoji}: failed on {channel}/{ts}: {err}")


def _set_eyes(client, channel: str, ts: str):
    _add_reaction(client, channel, ts, 'eyes')


def _set_done(client, channel: str, ts: str):
    _remove_reaction(client, channel, ts, 'eyes')
    _add_reaction(client, channel, ts, 'white_check_mark')


def _set_cancelled(client, channel: str, ts: str):
    _remove_reaction(client, channel, ts, 'eyes')
    _add_reaction(client, channel, ts, 'x')


def _cleanup_expired_pending(client):
    now = time.time()
    # Improvement-Flow: 72h TTL
    expired = [
        key for key, state in list(_pending.items())
        if now - state.get('created_at', now) > PENDING_TTL
    ]
    for key in expired:
        _pending.pop(key, None)
        channel, thread_ts = key
        logger.info(f"Pending state expired for {channel}/{thread_ts} — marking done")
        _set_done(client, channel, thread_ts)

    # VA-Zusammenfassung: 48h Reminder wenn noch keine Bestätigung
    admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)
    for key, state in list(_va_pending_approval.items()):
        if not state.get('reminded') and now - state.get('sent_at', now) > VA_REMINDER_TTL:
            channel, thread_ts = key
            logger.info(f"VA 48h reminder for {channel}/{thread_ts}")
            try:
                from slack_bolt import App as _App
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=(
                        f":reminder_ribbon: {admin_mentions} — "
                        "Erinnerung: Diese Vertragsanpassung wartet noch auf Bestätigung oder Ausführung."
                    ),
                )
            except Exception as e:
                logger.warning(f"VA reminder failed: {e}")
            state['reminded'] = True


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def _do_create_ticket(say, client, channel: str, thread_ts: str, ctx: dict):
    """Optimize and create a Jira ticket, then post the result in Slack."""
    opt_title, opt_description = optimize_ticket(ctx['title'], ctx['use_case'])
    try:
        ticket = create_ticket(
            summary=opt_title,
            use_case=opt_description,
            user_name=ctx['user_name'],
            request_date=ctx['request_date'],
            slack_link=ctx.get('slack_link', ''),
            images=ctx.get('images', []),
            slack_token=SLACK_BOT_TOKEN,
        )
        say(
            blocks=format_ticket_created(ticket),
            text=f"Ticket {ticket['key']} erstellt",
            thread_ts=thread_ts,
        )
        _set_done(client, channel, thread_ts)
    except Exception as e:
        logger.exception("Ticket creation failed")
        say(
            blocks=format_error(f"Fehler beim Erstellen des Tickets: {str(e)}"),
            text="Fehler",
            thread_ts=thread_ts,
        )


def _process_request(say, client, channel, thread_ts, user_id, user_name, request_date,
                     title, use_case, images=None):
    """Search Jira CS board — show similar tickets or create one directly."""
    images = images or []
    slack_link = slack_message_link(channel, thread_ts)

    try:
        similar = search_similar_tickets(title=title, use_case=use_case)
    except Exception as e:
        logger.exception("Jira search failed")
        say(
            blocks=format_error(f"Fehler bei der Jira-Suche: {str(e)}"),
            text="Jira-Fehler",
            thread_ts=thread_ts,
        )
        return

    ctx_data = {
        'user_id': user_id,
        'user_name': user_name,
        'request_date': request_date,
        'title': title,
        'use_case': use_case,
        'slack_link': slack_link,
        'images': images,
    }

    if similar:
        # Store context so the rejection button handler can find it
        _similar_shown[(channel, thread_ts)] = ctx_data
        _ticket_data[(channel, thread_ts)] = ctx_data
        say(
            blocks=found_ticket_blocks(similar, channel, thread_ts),
            text="Ähnliche Tickets gefunden",
            thread_ts=thread_ts,
        )
        return

    # No similar tickets — create directly
    _do_create_ticket(say, client, channel, thread_ts, ctx_data)


# ---------------------------------------------------------------------------
# Vertragsanpassungs-Flow helpers
# ---------------------------------------------------------------------------

def _enrich_from_offer(parsed: dict) -> dict:
    """Lädt Vertragsdaten aus der Angebots-URL und ergänzt fehlende Felder."""
    url = parsed.get('offer_link')
    if not url:
        return parsed
    try:
        offer_data = fetch_offer_data(url)
        if offer_data:
            for key, value in offer_data.items():
                if value and not parsed.get(key):  # nur fehlende Felder ergänzen
                    parsed[key] = value
                    logger.info(f"Offer enrichment: {key}={value!r}")
        else:
            # Leere Antwort = URL nicht lesbar
            parsed['offer_fetch_failed'] = True
    except Exception as e:
        logger.warning(f"Offer enrichment failed: {e}")
        parsed['offer_fetch_failed'] = True
    return parsed


def _inherit_from_subscription(parsed: dict, subscription: dict | None) -> dict:
    """Übernimmt Zahlweise aus der IST-Subscription wenn nicht explizit angegeben."""
    if not subscription:
        return parsed
    unit = subscription.get('billing_period_unit', '')
    if not parsed.get('payment_type') and unit:
        parsed['payment_type'] = 'jährlich' if unit == 'year' else 'monatlich'
        parsed['payment_type_inherited'] = True
        logger.info(f"Zahlweise aus IST übernommen: {parsed['payment_type']}")
    return parsed


def _debit_number_from_subscription(subscription: dict | None) -> str:
    """Liest cf_debit_number aus dem Subscription-Dict oder dem _raw_sub.

    cf_debit_number liegt in Chargebee auf dem Customer-Objekt, nicht auf der
    Subscription. Es kann an verschiedenen Stellen auftauchen je nach API-Aufruf.
    """
    if not subscription:
        return ''

    def _extract(d: dict) -> str:
        val = str(d.get('cf_debit_number', '')).strip()
        return val if val.isdigit() else ''

    # 1. Direkt im aufgelösten Dict
    v = _extract(subscription)
    if v:
        return v

    # 2. Im _raw_sub (Subscription-Objekt direkt von Chargebee)
    raw = subscription.get('_raw_sub') or {}
    v = _extract(raw)
    if v:
        return v

    # 3. Im eingebetteten Customer-Objekt (Chargebee bettet customer manchmal ein)
    customer = raw.get('customer') or subscription.get('customer') or {}
    v = _extract(customer)
    if v:
        return v

    # 4. Über customer_id direkt aus Chargebee nachladen
    customer_id = subscription.get('customer_id') or raw.get('customer_id', '')
    if customer_id and CHARGEBEE_API_KEY:
        try:
            resp = requests.get(
                f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/customers/{customer_id}",
                auth=(CHARGEBEE_API_KEY, ''),
                timeout=8,
            )
            if resp.ok:
                v = _extract(resp.json().get('customer', {}))
                if v:
                    return v
        except Exception as e:
            logger.warning(f"Customer lookup for debit_number failed: {e}")

    return ''


def _planhat_search_company(customer_name: str, debit_number: str = '') -> dict | None:
    """Sucht Planhat-Company.

    Planhat ignoriert URL-Filter-Parameter — die API gibt immer dieselben ersten
    Companies zurück unabhängig von name/externalId. Deshalb: Ergebnisse auf
    exakten Match prüfen. Kein Match → None (löst _ask_for_planhat_link aus).

    Strategie:
    1. Fetch-and-match per externalId (Debitorennummer)
    2. Fetch-and-match per Name als Fallback
    """
    if not PLANHAT_API_TOKEN:
        return None
    headers = {'Authorization': f'Bearer {PLANHAT_API_TOKEN}'}

    def _fetch_page(params: dict) -> list:
        try:
            resp = requests.get('https://api.planhat.com/companies',
                                headers=headers, params=params, timeout=10)
            if resp.ok and isinstance(resp.json(), list):
                return resp.json()
        except Exception as e:
            logger.warning(f"Planhat fetch failed: {e}")
        return []

    # 1. Seiten durchsuchen bis externalId-Match gefunden (max 5 Seiten à 100)
    if debit_number:
        for offset in range(0, 500, 100):
            page = _fetch_page({'limit': 100, 'offset': offset})
            if not page:
                break
            match = next((c for c in page if str(c.get('externalId', '')) == debit_number), None)
            if match:
                logger.info(f"Planhat externalId match: {debit_number} → {match.get('name')}")
                return {'id': match.get('_id') or match.get('id'), 'name': match.get('name', '')}
            if len(page) < 100:
                break  # letzte Seite

    # 2. Namens-Match (exakter Vergleich, case-insensitive)
    if customer_name:
        for offset in range(0, 500, 100):
            page = _fetch_page({'limit': 100, 'offset': offset})
            if not page:
                break
            match = next(
                (c for c in page if c.get('name', '').lower() == customer_name.lower()), None
            )
            if match:
                logger.info(f"Planhat name match: {customer_name} → {match.get('_id')}")
                return {'id': match.get('_id') or match.get('id'), 'name': match.get('name', '')}
            if len(page) < 100:
                break

    return None


def _upload_offer_to_planhat(files: list, customer_name: str, planhat_company_id: str,
                              thread_ts: str, say) -> list[str]:
    """Hinterlegt Angebots-Dateien in Planhat als Note mit Link.

    Statt Binärdatei-Upload (Planhat /assets erfordert S3-Presigned-URL-Flow)
    wird eine Note mit Dateiname + Link erstellt — zuverlässiger und ausreichend
    für den Audit-Trail.
    """
    if not files or not planhat_company_id or not PLANHAT_API_TOKEN:
        return []

    logged = []
    note_lines = [f"Angebots-Dokument(e) aus Slack — hinterlegt von CS Admin Bot:"]

    for file_info in files:
        url = file_info.get('url_private_download') or file_info.get('url_private') or file_info.get('_url_download', '')
        raw_name = file_info.get('name', '')
        # URL-Slugs und generische Namen durch lesbaren Label ersetzen
        filename = raw_name if (raw_name and '.' in raw_name) else 'Angebotslink'
        if not url:
            continue

        note_lines.append(f'• <a href="{url}">{filename}</a>')
        logged.append(filename)

    if not logged:
        return []

    note_text = '\n'.join(note_lines)
    try:
        resp = requests.post(
            'https://api.planhat.com/conversations',
            headers={'Authorization': f'Bearer {PLANHAT_API_TOKEN}'},
            json={'subject': 'Vertragsanpassung', 'description': note_text, 'companyId': planhat_company_id, 'type': 'note'},
            timeout=15,
        )
        if resp.ok:
            logger.info(f"Planhat note with file links created for {planhat_company_id}")
            return logged
        else:
            say(
                text=(
                    f":warning: Planhat-Note fehlgeschlagen:\n"
                    f"HTTP {resp.status_code} · `{resp.text[:300]}`\n"
                    f"company_id: `{planhat_company_id}`"
                ),
                thread_ts=thread_ts,
            )
            return []
    except Exception as e:
        say(text=f":warning: Planhat-Note Fehler: `{e}`", thread_ts=thread_ts)
        return []


def _ask_for_planhat_link(say, channel: str, thread_ts: str, customer_name: str,
                           action: str, context: dict) -> None:
    """Postet Nachfrage nach Planhat-Link mit Überspringen-Button und speichert Pending-State."""
    thread_ref = f"{channel}|||{thread_ts}"
    action_label = 'Angebots-Upload' if action == 'upload' else 'Log-Eintrag'
    say(
        blocks=[
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (
                        f":mag: Kein Planhat-Eintrag für *{customer_name}* gefunden.\n"
                        f"Bitte den Planhat-Link des Kunden hier in den Thread posten "
                        f"um den {action_label} fortzuführen."
                    ),
                },
            },
            {
                'type': 'actions',
                'elements': [
                    {
                        'type': 'button',
                        'text': {'type': 'plain_text', 'text': '❌ Überspringen'},
                        'style': 'danger',
                        'action_id': 'planhat_link_skip',
                        'value': thread_ref,
                    },
                ],
            },
        ],
        text=f"Planhat-Link für {customer_name} benötigt",
        thread_ts=thread_ts,
    )
    _pending_planhat_link[(channel, thread_ts)] = {**context, 'action': action}


def _cb_lookup(customer_name: str) -> dict | None:
    """Chargebee-Lookup per Kundenname (exakter Company-Match)."""
    if not customer_name or not CHARGEBEE_API_KEY:
        return None
    return lookup_chargebee_subscription(
        customer_name, CHARGEBEE_API_KEY, CHARGEBEE_SITE, planhat_token='',
    )


def _process_vertragsanpassung(say, client, channel: str, thread_ts: str,
                                user_name: str, parsed: dict,
                                subscription: dict | None = None,
                                files: list | None = None):
    """Alle Felder vollständig — entweder Zusammenfassung oder CS-Admin-Warnung."""
    if subscription is None:
        subscription = _cb_lookup(parsed.get('customer_name', ''))

    # Zahlweise aus IST übernehmen (falls nicht explizit angegeben)
    parsed = _inherit_from_subscription(parsed, subscription)

    # Service-Paket aus IST-Subscription lesen (via item_price Name)
    if subscription and not subscription.get('service_package') and subscription.get('plan_id') and CHARGEBEE_API_KEY:
        ist_price_name = fetch_item_price_name(subscription['plan_id'], CHARGEBEE_API_KEY, CHARGEBEE_SITE)
        if ist_price_name:
            subscription['item_price_name'] = ist_price_name
            pkg = extract_service_package(ist_price_name)
            if pkg:
                subscription['service_package'] = pkg
                logger.info(f"Service-Paket IST: {pkg!r} (aus {ist_price_name!r})")

    # Mehrere Subscriptions → CS Admin fragen, noch keine Zusammenfassung
    admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)
    customer = parsed.get('customer_name', 'unbekannt')

    # Keine Subscription gefunden → CS Admin nach Link fragen
    if not subscription:
        logger.info("VA: Keine Subscription gefunden — warte auf CS Admin Link")
        say(
            blocks=[{
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (
                        f":{VA_DONE_EMOJI}: {admin_mentions}\n"
                        f"Keine Chargebee-Subscription für *{customer}* gefunden.\n"
                        "Bitte den richtigen Chargebee-Link in den Thread schreiben — "
                        "ich erstelle danach die vollständige Zusammenfassung."
                    ),
                },
            }],
            text="Chargebee-Link fehlt — bitte CS Admin",
            thread_ts=thread_ts,
        )
        return  # State bleibt aktiv bis CS Admin den Link schreibt

    # Mehrere Subscriptions → CS Admin nach korrektem Link fragen
    if subscription.get('multiple_links'):
        logger.info("VA: Mehrere Subscriptions — warte auf CS Admin Bestätigung")
        say(
            blocks=build_cs_admin_subscription_blocks(subscription),
            text="Mehrere Subscriptions gefunden — bitte CS Admin bestätigen",
            thread_ts=thread_ts,
        )
        return  # State bleibt aktiv damit CS Admin antworten kann

    # item_price Name + Listenpreis aus Chargebee laden (für Zusammenfassung + Preischeck)
    if parsed.get('chargebee_plan_id') and CHARGEBEE_API_KEY and not parsed.get('chargebee_plan_name'):
        ip_data = fetch_item_price(parsed['chargebee_plan_id'], CHARGEBEE_API_KEY, CHARGEBEE_SITE)
        if ip_data.get('name'):
            parsed['chargebee_plan_name'] = ip_data['name']
        if ip_data.get('price') is not None:
            parsed['chargebee_plan_list_price'] = ip_data['price']
        logger.info(f"item_price: {ip_data!r}")

    # Vollständige Zusammenfassung mit allen Infos
    blocks = build_va_summary_blocks(parsed, subscription, user_name)
    # CS Admin Buttons
    thread_ref = f"{channel}|||{thread_ts}"
    blocks.append({'type': 'divider'})
    blocks.append({
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': f":{VA_DONE_EMOJI}: {admin_mentions}",
        },
    })
    blocks.append({
        'type': 'actions',
        'elements': [
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': '🙋 Mache ich — ich übernehme'},
                'action_id': 'va_take_over',
                'value': thread_ref,
            },
            {
                'type': 'button',
                'text': {'type': 'plain_text', 'text': '✅ Geprüft — bitte ausführen'},
                'style': 'primary',
                'action_id': 'va_approved',
                'value': thread_ref,
            },
        ],
    })
    say(blocks=blocks, text="📋 Vertragsanpassung — Zusammenfassung", thread_ts=thread_ts)
    # Reaktion auf Root-Nachricht (👀 → custom emoji)
    _remove_reaction(client, channel, thread_ts, 'eyes')
    _add_reaction(client, channel, thread_ts, VA_DONE_EMOJI)
    _pending_vertragsanpassung.pop((channel, thread_ts), None)
    # 48h Reminder + Ramp-Kontext speichern
    _va_pending_approval[(channel, thread_ts)] = {
        'sent_at': time.time(),
        'reminded': False,
        'parsed': parsed,
        'subscription': subscription,
    }

    # Angebots-Dateien zu Planhat hochladen (wenn vorhanden)
    if files and PLANHAT_API_TOKEN:
        customer_name = parsed.get('customer_name', '')
        ph_company = _planhat_search_company(customer_name, _debit_number_from_subscription(subscription))
        if ph_company and ph_company.get('id'):
            uploaded = _upload_offer_to_planhat(
                files, customer_name, ph_company['id'], thread_ts, say,
            )
            if uploaded:
                say(
                    text=(
                        f":paperclip: *{len(uploaded)} Datei(en) in Planhat hinterlegt* "
                        f"unter _{ph_company['name']}_:\n"
                        + "\n".join(f"• `{f}`" for f in uploaded)
                    ),
                    thread_ts=thread_ts,
                )
            else:
                say(
                    text=(
                        ":warning: Dateien konnten nicht zu Planhat hochgeladen werden. "
                        "Bitte manuell unter dem Kunden hinterlegen."
                    ),
                    thread_ts=thread_ts,
                )
        elif customer_name:
            say(
                text=(
                    f":warning: Kein Planhat-Eintrag für *{customer_name}* gefunden — "
                    "Dateien bitte manuell beim Kunden in Planhat hinterlegen."
                ),
                thread_ts=thread_ts,
            )


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

def _handle_message_core(event, say, client):
    """Core message processing logic, shared by the generic and file_share handlers."""
    subtype = event.get('subtype')
    logger.info(
        f"Incoming message: subtype={subtype!r}, channel={event.get('channel')!r}, "
        f"has_files={bool(event.get('files'))}, thread={event.get('thread_ts')!r}"
    )

    if event.get('bot_id'):
        return
    # Allow messages from the improvement channel OR the vertragsanpassung channel
    _in_improvement = (event.get('channel') == SLACK_CHANNEL_ID)
    _in_va = (VERTRAGSANPASSUNG_CHANNEL_ID and event.get('channel') == VERTRAGSANPASSUNG_CHANNEL_ID)
    if not _in_improvement and not _in_va:
        return
    # Skip system subtypes (edits, deletes, joins, …) but allow file_share through
    if subtype and subtype != 'file_share':
        return

    _cleanup_expired_pending(client)

    text = event.get('text', '') or ''
    channel = event.get('channel')
    ts = event.get('ts')
    thread_ts = event.get('thread_ts')
    user_id = event.get('user')
    user_name = get_user_name(client, user_id)
    request_date = ts_to_date(thread_ts or ts)

    logger.info(f"Processing: channel={channel}, thread={thread_ts}, user={user_id} ({user_name})")

    # -----------------------------------------------------------------------
    # THREAD REPLY
    # -----------------------------------------------------------------------
    if thread_ts:
        # --- ? Hilfe-Trigger: ein oder mehrere Fragezeichen → Bot erklärt was zu tun ist ---
        if re.fullmatch(r'\?+', text.strip()):
            va_state_check = _pending_vertragsanpassung.get((channel, thread_ts))
            imp_state_check = _pending.get((channel, thread_ts))
            va_approval_check = _va_pending_approval.get((channel, thread_ts))
            similar_check = _similar_shown.get((channel, thread_ts))

            if va_state_check or va_approval_check:
                # Vertragsanpassungs-Flow aktiv
                if va_state_check:
                    missing = missing_va_fields(va_state_check.get('parsed', {}))
                    if missing:
                        say(
                            text=(
                                ":wave: Der Bot wartet noch auf fehlende Informationen zur Vertragsanpassung:\n"
                                + "\n".join(f"• {m}" for m in missing)
                                + "\nBitte ergänze die fehlenden Angaben direkt hier im Thread."
                            ),
                            thread_ts=thread_ts,
                        )
                    else:
                        say(
                            text=(
                                ":wave: Die Zusammenfassung wurde erstellt. "
                                "Bitte prüfe sie und klicke dann:\n"
                                "• *✅ Geprüft — bitte ausführen* → Ramp wird automatisch angelegt\n"
                                "• *🙋 Mache ich — ich übernehme* → du erledigst es manuell in Chargebee"
                            ),
                            thread_ts=thread_ts,
                        )
                elif va_approval_check:
                    say(
                        text=(
                            ":wave: Die Vertragsanpassungs-Zusammenfassung wurde gepostet. "
                            "Bitte klicke auf einen der Buttons:\n"
                            "• *✅ Geprüft — bitte ausführen* → Ramp wird automatisch angelegt\n"
                            "• *🙋 Mache ich — ich übernehme* → du erledigst es manuell in Chargebee"
                        ),
                        thread_ts=thread_ts,
                    )
            elif imp_state_check:
                # Improvement-Flow aktiv
                missing_imp = missing_info(imp_state_check.get('title'), imp_state_check.get('use_case'))
                if missing_imp:
                    say(
                        text=(
                            ":wave: Der Bot wartet auf fehlende Infos für den Improvement Request:\n"
                            + "\n".join(f"• {m}" for m in missing_imp)
                            + "\nEinfach hier im Thread antworten und die Infos ergänzen."
                        ),
                        thread_ts=thread_ts,
                    )
                else:
                    say(
                        text=(
                            ":wave: Klicke auf einen der Buttons:\n"
                            "• Ticket-Nummer schreiben (z.B. `CS-42`) → Bot upvotet automatisch\n"
                            "• *Kein Ticket passt* → neues Jira-Ticket wird erstellt\n"
                            "• *❌ Kein Ticket nötig* → Flow wird abgebrochen"
                        ),
                        thread_ts=thread_ts,
                    )
            elif similar_check:
                say(
                    text=(
                        ":wave: Ähnliche Tickets wurden gefunden. Bitte:\n"
                        "• Schreibe die Ticket-Nummer (z.B. `CS-42`) um es upzuvoten\n"
                        "• Klicke *Kein Ticket passt* um ein neues Ticket zu erstellen\n"
                        "• Klicke *❌ Kein Ticket nötig* um den Vorgang abzubrechen"
                    ),
                    thread_ts=thread_ts,
                )
            else:
                say(
                    text=(
                        ":wave: Kein aktiver Bot-Flow in diesem Thread.\n"
                        "• Für Vertragsanpassungen: `#vertragsanpassung` hier posten (nur CS Admin)\n"
                        "• Für Feature-Anfragen: `#improvement` in einer neuen Nachricht schreiben"
                    ),
                    thread_ts=thread_ts,
                )
            return

        # --- Planhat-Link als Antwort auf _ask_for_planhat_link ---
        # Auch ohne aktiven State reagieren (Bot-Neustart löscht State)
        ph_pending = _pending_planhat_link.get((channel, thread_ts))
        ph_url_match_direct = re.search(r'https?://(?:app|ws)\.planhat\.com/\S+', text)
        if ph_url_match_direct and user_id in CS_ADMIN_USER_IDS and not ph_pending:
            # Kein aktiver State — kurze Hinweis-Nachricht dass #planhat-upload neu getriggert werden soll
            say(
                text=(
                    ":wave: Planhat-Link erkannt. Falls der Bot neugestartet wurde, "
                    "bitte nochmal `#planhat-upload` schreiben — der Link wird dann direkt verwendet."
                ),
                thread_ts=thread_ts,
            )
        if ph_pending and user_id in CS_ADMIN_USER_IDS:
            ph_url_match = re.search(r'https?://(?:app|ws)\.planhat\.com/\S+', text)
            if ph_url_match:
                ph_url = ph_url_match.group(0).rstrip('>')
                # Company-ID aus URL extrahieren (letztes Segment)
                # Company-ID aus verschiedenen Planhat-URL-Formaten extrahieren:
                # app.planhat.com/customer/62f68edfd9448f71ee7757c3
                # ws.planhat.com/.../task?profile=Company.62f68edfd9448f71ee7757c3
                _ph_profile = re.search(r'profile=Company\.([a-f0-9]+)', ph_url)
                _ph_customer = re.search(r'/customer/([a-f0-9]+)', ph_url)
                _ph_segment = re.search(r'/([a-f0-9]{24})(?:[/?]|$)', ph_url)
                ph_id = (
                    (_ph_profile and _ph_profile.group(1))
                    or (_ph_customer and _ph_customer.group(1))
                    or (_ph_segment and _ph_segment.group(1))
                    or ph_url.rstrip('/').split('/')[-1]
                )
                ph_name = ph_pending.get('customer_name', '')
                parsed_ctx = ph_pending.get('parsed', {})
                sub_ctx = ph_pending.get('subscription')
                all_files_pending = ph_pending.get('files', [])

                old_plan = sub_ctx.get('plan_id', '–') if sub_ctx else '–'
                new_plan = parsed_ctx.get('chargebee_plan_id') or parsed_ctx.get('new_plan') or '–'
                effective = parsed_ctx.get('effective_date') or '–'
                slack_link = slack_message_link(channel, thread_ts)

                note_lines = [
                    f"Vertragsanpassung — {ph_pending.get('user_name', user_name)}",
                    f"Plan: {old_plan} → {new_plan}",
                    f"Effective: {effective}",
                    f'Slack-Thread: <a href="{slack_link}">Thread öffnen</a>',
                ]
                if all_files_pending:
                    note_lines.append('\nAngebots-Dokument(e):')
                    for f in all_files_pending:
                        raw = f.get('name', '')
                        fname = raw if (raw and '.' in raw) else 'Angebotslink'
                        furl = f.get('url_private_download') or f.get('_url_download', '')
                        note_lines.append(f'• <a href="{furl}">{fname}</a>' if furl else f"• {fname}")

                try:
                    ph_resp = requests.post(
                        'https://api.planhat.com/conversations',
                        headers={'Authorization': f'Bearer {PLANHAT_API_TOKEN}'},
                        json={'subject': 'Vertragsanpassung', 'description': '\n'.join(note_lines), 'companyId': ph_id, 'type': 'note'},
                        timeout=10,
                    )
                    if ph_resp.ok:
                        file_info_str = f" + {len(all_files_pending)} Datei-Link(s)" if all_files_pending else ""
                        say(
                            text=(
                                f":memo: *Planhat-Note erstellt* für _{ph_name}_"
                                f"{file_info_str}\n"
                                f"• Plan: `{old_plan}` → `{new_plan}`\n"
                                f"• Effective: {effective}"
                            ),
                            thread_ts=thread_ts,
                        )
                    else:
                        say(text=f":warning: Note fehlgeschlagen: `{ph_resp.status_code} {ph_resp.text[:200]}`",
                            thread_ts=thread_ts)
                except Exception as e:
                    say(text=f":warning: Note fehlgeschlagen: `{e}`", thread_ts=thread_ts)

                _pending_planhat_link.pop((channel, thread_ts), None)
                return

        # --- Vertragsanpassung: CS Admin bestätigt Chargebee-Link ---
        # Akzeptiert jeden Chargebee-Link (Subscription ODER Customer) — egal ob in der Liste
        va_state = _pending_vertragsanpassung.get((channel, thread_ts))
        if va_state and user_id in CS_ADMIN_USER_IDS:
            cb_match = _CB_URL_RE.search(text)
            if cb_match:
                link_type = cb_match.group('type') or ''
                link_id = (cb_match.group('id') or cb_match.group('std_id') or '').strip()
                if link_id:
                    logger.info(f"VA: CS Admin Link: type={link_type!r} id={link_id!r}")
                    confirmed_sub = None

                    if link_type.lower() == 'customers':
                        # Customer-URL → Subscriptions für diesen Kunden laden
                        from vertragsanpassung_handler import _fetch_subscriptions_for_customer
                        base = f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2"
                        auth = (CHARGEBEE_API_KEY, '')
                        confirmed_sub = _fetch_subscriptions_for_customer(
                            link_id, base, auth, CHARGEBEE_SITE,
                            va_state['parsed'].get('customer_name', ''),
                        )
                        # Wenn mehrere → nimm die beste (Standard-Format bevorzugt)
                        if confirmed_sub and confirmed_sub.get('multiple_subs'):
                            # Wähle das XXXX-XXXX-XXXX-XXXX Format wenn vorhanden
                            confirmed_sub.pop('multiple_subs', None)
                            confirmed_sub.pop('multiple_links', None)
                    else:
                        # Subscription-URL oder Standard-ID direkt laden
                        confirmed_sub = _fetch_subscription_by_id(link_id, CHARGEBEE_API_KEY, CHARGEBEE_SITE)
                        if confirmed_sub:
                            confirmed_sub['company'] = va_state['parsed'].get('customer_name', '')

                    if confirmed_sub:
                        _process_vertragsanpassung(
                            say, client, channel, thread_ts,
                            va_state['user_name'], va_state['parsed'], confirmed_sub,
                        )
                        return
                    else:
                        say(text=f":x: Link `{link_id}` konnte nicht aufgelöst werden.", thread_ts=thread_ts)
                        return

        # --- Vertragsanpassung: follow-up to pending state ---
        if va_state and va_state.get('user_id') == user_id:
            new_parsed = _enrich_from_offer(parse_vertragsanpassung(text))
            # Merge: only fill empty fields from the follow-up reply
            for k, v in new_parsed.items():
                if v and not va_state['parsed'].get(k):
                    va_state['parsed'][k] = v
            # Try Chargebee lookup now if customer_name just became available
            if not va_state.get('subscription') and va_state['parsed'].get('customer_name'):
                va_state['subscription'] = _cb_lookup(va_state['parsed']['customer_name'])
            missing = missing_va_fields(va_state['parsed'])
            if missing:
                say(
                    blocks=ask_for_va_info_blocks(
                        user_id, missing, va_state['parsed'], va_state.get('subscription')
                    ),
                    text="Fehlende Informationen",
                    thread_ts=thread_ts,
                )
            else:
                _process_vertragsanpassung(
                    say, client, channel, thread_ts,
                    va_state['user_name'], va_state['parsed'], va_state.get('subscription'),
                )
            return

        # --- Planhat Log + Upload kombiniert (#planhat-log) ---
        # Erstellt eine Note in Planhat mit VA-Kontext + optionalen Datei-Links.
        # Ersetzt #planhat-upload und #planhat-log. Auch #planhat-upload wird noch erkannt.
        if '#planhat-log' in text.lower() or '#planhat-upload' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(
                    text=":no_entry: Dieser Befehl kann nur vom CS Admin Team genutzt werden.",
                    thread_ts=thread_ts,
                )
                return

            # Thread-Nachrichten lesen
            all_msgs = []
            try:
                root_result = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                all_msgs = root_result.get('messages', [])
            except Exception as e:
                logger.warning(f"Thread read failed for planhat-log: {e}")

            root_text = all_msgs[0].get('text', '') if all_msgs else ''

            # VA-Kontext aus State oder Thread
            va_state_pl = _pending_vertragsanpassung.get((channel, thread_ts)) or {}
            va_approval_pl = _va_pending_approval.get((channel, thread_ts)) or {}
            parsed_ctx = va_state_pl.get('parsed') or va_approval_pl.get('parsed') or {}
            sub_ctx = va_state_pl.get('subscription') or va_approval_pl.get('subscription')
            if not parsed_ctx and root_text:
                parsed_ctx = parse_vertragsanpassung(root_text)

            # Kundenname ermitteln
            customer_name = parsed_ctx.get('customer_name', '')
            if not customer_name:
                name_inline = re.sub(r'#planhat-(?:log|upload)\s*', '', text, flags=re.IGNORECASE).strip()
                if name_inline:
                    customer_name = name_inline

            if not customer_name:
                say(
                    text=(
                        ":thinking_face: Kein Kundenname erkannt. "
                        "Schreibe `#planhat-log Kundenname GmbH` um den Kunden anzugeben."
                    ),
                    thread_ts=thread_ts,
                )
                return

            # CB-Lookup für Debitnummer falls kein State
            if not sub_ctx and CHARGEBEE_API_KEY:
                sub_ctx = _cb_lookup(customer_name)

            ph_company = _planhat_search_company(customer_name, _debit_number_from_subscription(sub_ctx))
            if not ph_company or not ph_company.get('id'):
                _ask_for_planhat_link(say, channel, thread_ts, customer_name, 'log', {
                    'customer_name': customer_name, 'user_name': user_name,
                    'parsed': parsed_ctx, 'subscription': sub_ctx,
                })
                return

            # Dateien aus Thread einsammeln (Slack-Anhänge + URLs)
            current_files = extract_files(event.get('files')) or []
            thread_files = []
            for msg in all_msgs:
                thread_files.extend(extract_files(msg.get('files')) or [])
            seen_ids: set = set()
            all_files = []
            for f in (current_files + thread_files):
                fid = f.get('id')
                if fid and fid not in seen_ids:
                    seen_ids.add(fid)
                    all_files.append(f)
            # Angebots-URLs aus Thread-Text sammeln (immer, nicht nur als Fallback)
            # Ignoriert: Planhat, Slack-interne Links, Chargebee, Bot-eigene Links
            _ignore = ('planhat.com', 'slack.com/archives', 'chargebee.com',
                       'xentral-dach', 'slack.com/files')
            url_re_pl = re.compile(r'https?://[^\s<>]+', re.IGNORECASE)
            seen_urls: set = set()
            for msg in all_msgs:
                for url_match in url_re_pl.finditer(msg.get('text', '')):
                    url_raw = url_match.group(0).rstrip('>).,"\'')
                    if url_raw in seen_urls:
                        continue
                    if any(ign in url_raw for ign in _ignore):
                        continue
                    seen_urls.add(url_raw)
                    all_files.append({
                        '_url_download': url_raw,
                        'url_private_download': url_raw,
                        'name': 'Angebotslink',
                        'mimetype': 'application/octet-stream',
                        'id': url_raw,
                        '_no_slack_auth': True,
                    })

            # Note aufbauen: VA-Kontext + optionale Datei-Links
            old_plan = sub_ctx.get('plan_id', '–') if sub_ctx else '–'
            new_plan = parsed_ctx.get('chargebee_plan_id') or parsed_ctx.get('new_plan') or '–'
            effective = parsed_ctx.get('effective_date') or '–'
            slack_link = slack_message_link(channel, thread_ts)

            note_lines = [
                f"Vertragsanpassung — {user_name}",
                f"Plan: {old_plan} → {new_plan}",
                f"Effective: {effective}",
                f'Slack-Thread: <a href="{slack_link}">Thread öffnen</a>',
            ]

            # Angebotslink: zuerst aus parsed_ctx, dann aus Datei-Liste
            offer_link = parsed_ctx.get('offer_link', '')
            if offer_link:
                note_lines.append(f'\nAngebots-Dokument(e):')
                note_lines.append(f'• <a href="{offer_link}">Angebotslink</a>')
            elif all_files:
                note_lines.append('\nAngebots-Dokument(e):')
                for f in all_files:
                    raw = f.get('name', '')
                    fname = raw if (raw and '.' in raw) else 'Angebotslink'
                    furl = f.get('url_private_download') or f.get('_url_download', '')
                    note_lines.append(f'• <a href="{furl}">{fname}</a>' if furl else f"• {fname}")

            note_text = '\n'.join(note_lines)
            try:
                ph_resp = requests.post(
                    'https://api.planhat.com/conversations',
                    headers={'Authorization': f'Bearer {PLANHAT_API_TOKEN}'},
                    json={'subject': 'Vertragsanpassung', 'description': note_text, 'companyId': ph_company['id'], 'type': 'note'},
                    timeout=10,
                )
                if ph_resp.ok:
                    file_info_str = f" + {len(all_files)} Datei-Link(s)" if all_files else ""
                    say(
                        text=(
                            f":memo: *Planhat-Note erstellt* für _{ph_company['name']}_"
                            f"{file_info_str}\n"
                            f"• Plan: `{old_plan}` → `{new_plan}`\n"
                            f"• Effective: {effective}"
                        ),
                        thread_ts=thread_ts,
                    )
                else:
                    say(
                        text=f":warning: Planhat-Note fehlgeschlagen: `{ph_resp.status_code} {ph_resp.text[:200]}`",
                        thread_ts=thread_ts,
                    )
            except Exception as e:
                say(text=f":warning: Planhat-Note fehlgeschlagen: `{e}`", thread_ts=thread_ts)
            return

        # --- Vertragsanpassung: manual thread trigger (CS Admin only) ---
        if '#vertragsanpassung' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(
                    text=":no_entry: `#vertragsanpassung` kann nur vom CS Admin Team genutzt werden.",
                    thread_ts=thread_ts,
                )
                return
            # Read root message of the thread for context
            try:
                result = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)
                root_text = result.get('messages', [{}])[0].get('text', '')
            except Exception as e:
                logger.warning(f"conversations_replies failed in VA trigger: {e}")
                root_text = ''
            _set_eyes(client, channel, thread_ts)
            parsed = _enrich_from_offer(parse_vertragsanpassung(root_text or text))
            if parsed.get('offer_fetch_failed'):
                say(
                    text=(
                        ":warning: Der verlinkte Angebots-Link konnte nicht geöffnet werden "
                        f"(`{parsed.get('offer_link', '?')}`).\n"
                        "Bitte entweder:\n"
                        "• Einen neuen/öffentlich zugänglichen Link hier posten, *oder*\n"
                        "• Die Infos manuell ergänzen: *Plan*, *Laufzeit*, *Zahlweise*, *Vertragsbeginn*, *Service-Paket*"
                    ),
                    thread_ts=thread_ts,
                )
            subscription = _cb_lookup(parsed.get('customer_name', ''))
            parsed = _inherit_from_subscription(parsed, subscription)
            missing = missing_va_fields(parsed)
            if missing:
                _pending_vertragsanpassung[(channel, thread_ts)] = {
                    'parsed': parsed,
                    'user_id': user_id,
                    'user_name': user_name,
                    'subscription': subscription,
                    'created_at': time.time(),
                }
                say(
                    blocks=ask_for_va_info_blocks(user_id, missing, parsed, subscription),
                    text="Vertragsanpassung — fehlende Informationen",
                    thread_ts=thread_ts,
                )
            else:
                # Dateien aus Root-Nachricht mitgeben
                try:
                    root_result = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)
                    root_files = extract_files(root_result.get('messages', [{}])[0].get('files')) or []
                except Exception:
                    root_files = []
                _process_vertragsanpassung(say, client, channel, thread_ts, user_name, parsed, subscription,
                                           files=root_files or None)
            return

        state = _pending.get((channel, thread_ts))

        # --- Follow-up reply to our info request ---
        if state and state.get('user_id') == user_id:
            title_parsed, uc_parsed = parse_request(text)

            # Only replace the stored title when the user explicitly wrote "Titel: …"
            # A plain reply without that label must NOT overwrite the original title —
            # parse_request() would return the first line as a fallback title, making
            # title == use_case and triggering the "entspricht dem Titel" error.
            has_explicit_title = bool(
                re.search(r'(?:titel|title)\s*[:\-]', text, re.IGNORECASE)
            )
            if has_explicit_title and title_parsed:
                state['title'] = title_parsed
            elif not state.get('title'):
                state['title'] = title_parsed or text.split('\n')[0].strip()

            if not state.get('use_case'):
                state['use_case'] = uc_parsed or text.strip()

            still_missing = missing_info(state.get('title'), state.get('use_case'))
            if still_missing:
                say(
                    blocks=ask_for_info_blocks(user_id, still_missing),
                    text="Bitte ergänze die fehlenden Infos",
                    thread_ts=thread_ts,
                )
                return

            uc_error = validate_use_case(state.get('title'), state.get('use_case'))
            if uc_error:
                state['use_case'] = None
                say(text=uc_error, thread_ts=thread_ts)
                return

            _pending.pop((channel, thread_ts), None)
            _process_request(
                say=say,
                client=client,
                channel=channel,
                thread_ts=thread_ts,
                user_id=state['user_id'],
                user_name=state.get('user_name', user_name),
                request_date=state.get('request_date', request_date),
                title=state['title'],
                use_case=state['use_case'],
                images=state.get('images', []),
            )
            return

        # --- Jira ticket number posted → auto-upvote ---
        jira_match = JIRA_KEY_RE.search(text.upper())
        if jira_match:
            issue_key = jira_match.group(1)
            try:
                issue = add_vote(issue_key, user_name=user_name)
                say(
                    text=(
                        f":thumbsup: Ich habe dein Upvote auf "
                        f"*<{issue['url']}|{issue_key}>* eingetragen!\n"
                        f"_{issue['summary']}_"
                    ),
                    thread_ts=thread_ts,
                )
                _similar_shown.pop((channel, thread_ts), None)
                _set_done(client, channel, thread_ts)
            except Exception as e:
                logger.exception("Jira vote failed")
                say(
                    text=f":x: Upvote für `{issue_key}` fehlgeschlagen: {str(e)}",
                    thread_ts=thread_ts,
                )
            return

        # --- User rejects similar tickets via text → create new ticket directly ---
        rejection_match = REJECTION_RE.search(text)
        logger.info(
            f"Thread reply: rejection_match={bool(rejection_match)}, "
            f"similar_shown={(channel, thread_ts) in _similar_shown}, "
            f"ticket_data={(channel, thread_ts) in _ticket_data}, "
            f"text={text!r}"
        )
        if rejection_match:
            ctx = _similar_shown.pop((channel, thread_ts), None)
            if not ctx:
                ctx = _ticket_data.get((channel, thread_ts))

            if not ctx:
                logger.warning(f"Rejection in {channel}/{thread_ts} — no context, fetching thread root")
                try:
                    result = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)
                    messages = result.get('messages', [])
                    root_msg = messages[0] if messages else {}
                    root_text = root_msg.get('text', '')
                    root_user = root_msg.get('user', user_id)
                    root_images = extract_images(root_msg.get('files'))
                    logger.info(f"Thread root: user={root_user}, text={root_text[:60]!r}")
                except Exception as e:
                    logger.warning(f"conversations_replies failed: {e}")
                    root_text = ''
                    root_user = user_id
                    root_images = []

                if '#improvement' not in root_text.lower():
                    return

                title, use_case = parse_request(root_text)
                if not title or not use_case:
                    say(
                        text=(
                            ":thinking_face: Kein Problem! Mein Kurzzeitgedächtnis wurde durch "
                            "ein Server-Update gelöscht.\n"
                            "Schreib bitte `#improvement` erneut in diesen Thread mit Titel "
                            "und Use Case — dann lege ich das Ticket direkt an."
                        ),
                        thread_ts=thread_ts,
                    )
                    return

                ctx = {
                    'user_id': root_user,
                    'user_name': get_user_name(client, root_user),
                    'request_date': ts_to_date(thread_ts),
                    'title': title,
                    'use_case': use_case,
                    'slack_link': slack_message_link(channel, thread_ts),
                    'images': root_images,
                }

            _do_create_ticket(say, client, channel, thread_ts, ctx)
            return

        # --- #improvement trigger inside a thread → read from original message ---
        if '#improvement' in text.lower():
            try:
                result = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)
                messages = result.get('messages', [])
                root_msg = messages[0] if messages else {}
                original_text = root_msg.get('text', '')
                original_images = extract_images(root_msg.get('files'))
            except Exception:
                logger.exception("Could not fetch original thread message")
                original_text = ''
                original_images = []

            original_request_date = ts_to_date(thread_ts)
            title, use_case = parse_request(original_text or text)
            still_missing = missing_info(title, use_case)

            _set_eyes(client, channel, thread_ts)

            if still_missing:
                _pending[(channel, thread_ts)] = {
                    'user_id': user_id,
                    'user_name': user_name,
                    'request_date': original_request_date,
                    'title': title,
                    'use_case': use_case,
                    'images': original_images,
                    'created_at': time.time(),
                }
                say(
                    blocks=ask_for_info_blocks(user_id, still_missing),
                    text="Fehlende Informationen",
                    thread_ts=thread_ts,
                )
                return

            uc_error = validate_use_case(title, use_case)
            if uc_error:
                _pending[(channel, thread_ts)] = {
                    'user_id': user_id,
                    'user_name': user_name,
                    'request_date': original_request_date,
                    'title': title,
                    'use_case': None,
                    'images': original_images,
                    'created_at': time.time(),
                }
                say(text=uc_error, thread_ts=thread_ts)
                return

            _process_request(
                say=say,
                client=client,
                channel=channel,
                thread_ts=thread_ts,
                user_id=user_id,
                user_name=user_name,
                request_date=original_request_date,
                title=title,
                use_case=use_case,
                images=original_images,
            )
        return

    # -----------------------------------------------------------------------
    # NEW MESSAGE
    # -----------------------------------------------------------------------

    # --- Vertragsanpassung: auto-detection (only in VA channel) ---
    if _in_va and detect_vertragsanpassung(text):
        _set_eyes(client, channel, ts)
        parsed = _enrich_from_offer(parse_vertragsanpassung(text))
        if parsed.get('offer_fetch_failed'):
            say(
                text=(
                    ":warning: Der verlinkte Angebots-Link konnte nicht geöffnet werden "
                    f"(`{parsed.get('offer_link', '?')}`).\n"
                    "Bitte entweder:\n"
                    "• Einen neuen/öffentlich zugänglichen Link hier posten, *oder*\n"
                    "• Die Infos manuell ergänzen: *Plan*, *Laufzeit*, *Zahlweise*, *Vertragsbeginn*, *Service-Paket*"
                ),
                thread_ts=ts,
            )
        subscription = _cb_lookup(parsed.get('customer_name', ''))
        parsed = _inherit_from_subscription(parsed, subscription)
        missing = missing_va_fields(parsed)
        if missing:
            _pending_vertragsanpassung[(channel, ts)] = {
                'parsed': parsed,
                'user_id': user_id,
                'user_name': user_name,
                'subscription': subscription,
                'created_at': time.time(),
            }
            say(
                blocks=ask_for_va_info_blocks(user_id, missing, parsed, subscription),
                text="Vertragsanpassung erkannt — fehlende Informationen",
                thread_ts=ts,
            )
        else:
            event_files = extract_files(event.get('files')) or None
            _process_vertragsanpassung(say, client, channel, ts, user_name, parsed, subscription,
                                       files=event_files)
        return

    # --- Improvement: only react to #improvement tag ---
    if '#improvement' not in text.lower():
        return

    images = extract_images(event.get('files'))
    title, use_case = parse_request(text)
    still_missing = missing_info(title, use_case)

    _set_eyes(client, channel, ts)

    if still_missing:
        _pending[(channel, ts)] = {
            'user_id': user_id,
            'user_name': user_name,
            'request_date': request_date,
            'title': title,
            'use_case': use_case,
            'images': images,
            'created_at': time.time(),
        }
        say(
            blocks=ask_for_info_blocks(user_id, still_missing),
            text="Fehlende Informationen",
            thread_ts=ts,
        )
        return

    uc_error = validate_use_case(title, use_case)
    if uc_error:
        _pending[(channel, ts)] = {
            'user_id': user_id,
            'user_name': user_name,
            'request_date': request_date,
            'title': title,
            'use_case': None,
            'images': images,
            'created_at': time.time(),
        }
        say(text=uc_error, thread_ts=ts)
        return

    _process_request(
        say=say,
        client=client,
        channel=channel,
        thread_ts=ts,
        user_id=user_id,
        user_name=user_name,
        request_date=request_date,
        title=title,
        use_case=use_case,
        images=images,
    )


@app.event("message")
def handle_message(event, say, client):
    _handle_message_core(event, say, client)


@app.event({"type": "message", "subtype": "file_share"})
def handle_file_share_message(event, say, client):
    """Explicit handler for messages that include file/image uploads."""
    _handle_message_core(event, say, client)


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

@app.action("reject_similar_create_ticket")
def handle_reject_similar(ack, body, say, client):
    """User clicked '➕ Kein Ticket passt — neues anlegen' button."""
    ack()
    value = body['actions'][0]['value']
    try:
        channel, thread_ts = value.split('|||')
    except ValueError:
        say(text="Fehler beim Verarbeiten der Anfrage.", thread_ts=body.get('message', {}).get('thread_ts'))
        return

    ctx = _similar_shown.pop((channel, thread_ts), None) or _ticket_data.get((channel, thread_ts))

    if not ctx:
        logger.info(f"reject_similar button: no in-memory context for {channel}/{thread_ts} — fetching thread root")
        try:
            result = client.conversations_replies(channel=channel, ts=thread_ts, limit=1)
            messages = result.get('messages', [])
            root_msg = messages[0] if messages else {}
            root_text = root_msg.get('text', '')
            root_user = root_msg.get('user', '')
            root_images = extract_images(root_msg.get('files'))
        except Exception as e:
            logger.warning(f"conversations_replies failed in reject_similar: {e}")
            root_text = ''
            root_user = ''
            root_images = []

        title, use_case = parse_request(root_text) if root_text else (None, None)

        if not title or not use_case:
            say(
                text=(
                    ":thinking_face: Ich konnte deine ursprüngliche Anfrage nicht mehr laden.\n"
                    "Bitte schreib `#improvement` mit Titel und Use Case nochmal in den Thread — "
                    "ich lege das Ticket dann direkt an."
                ),
                thread_ts=thread_ts,
            )
            return

        ctx = {
            'user_id': root_user,
            'user_name': get_user_name(client, root_user) if root_user else 'Unbekannt',
            'request_date': ts_to_date(thread_ts),
            'title': title,
            'use_case': use_case,
            'slack_link': slack_message_link(channel, thread_ts),
            'images': root_images,
        }

    _do_create_ticket(say, client, channel, thread_ts, ctx)


@app.action("cancel_create_ticket")
def handle_cancel(ack, body, say, client):
    """User clicked '❌ Kein Ticket nötig' — remove 👀 and add ❌ on root message."""
    ack()
    value = body['actions'][0].get('value', '')
    try:
        channel, thread_ts = value.split('|||')
    except ValueError:
        channel = body.get('channel', {}).get('id', '')
        thread_ts = (
            body.get('message', {}).get('thread_ts')
            or body.get('message', {}).get('ts')
        )
    _similar_shown.pop((channel, thread_ts), None)
    _ticket_data.pop((channel, thread_ts), None)
    _set_cancelled(client, channel, thread_ts)
    say(text=":x: OK — kein Ticket wird erstellt.", thread_ts=thread_ts)


@app.action("va_take_over")
def handle_va_take_over(ack, body, say, client):
    """CS Admin übernimmt die Umsetzung — nur für CS Admin Team."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    if user_id not in CS_ADMIN_USER_IDS:
        return
    user_name = get_user_name(client, user_id)
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')
    say(
        text=f":csadmin-bot: *{user_name}* übernimmt die Umsetzung — bitte im Thread als ✅ done markieren wenn erledigt.",
        thread_ts=thread_ts,
    )
    _va_pending_approval.pop((channel, thread_ts), None)


@app.action("va_approved")
def handle_va_approved(ack, body, say, client):
    """CS Admin hat geprüft und gibt das Go — Ramp in Chargebee anlegen."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    if user_id not in CS_ADMIN_USER_IDS:
        return
    user_name = get_user_name(client, user_id)
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')

    state = _va_pending_approval.get((channel, thread_ts), {})
    parsed = state.get('parsed', {})
    subscription = state.get('subscription', {})

    sub_id = subscription.get('subscription_id') if subscription else None
    new_plan_id = parsed.get('chargebee_plan_id')
    old_plan_id = subscription.get('plan_id') if subscription else None
    effective_date = parsed.get('effective_date')  # datetime oder None

    # Effective Date = heute → manuell nötig
    today = datetime.now(timezone.utc).date()
    if effective_date and getattr(effective_date, 'date', lambda: effective_date)() <= today:
        say(
            text=(
                f":csadmin-bot: *{user_name}* hat geprüft — "
                "⛔ Effective Date ist heute oder in der Vergangenheit. "
                "Bitte die Ramp manuell in Chargebee anlegen (Tab 'Ramps' > 'Add Ramp')."
            ),
            thread_ts=thread_ts,
        )
        _va_pending_approval.pop((channel, thread_ts), None)
        return

    # Prüfen ob alle nötigen Infos vorhanden
    if not sub_id or not new_plan_id or not old_plan_id or not effective_date or not CHARGEBEE_API_KEY:
        say(
            text=(
                f":csadmin-bot: *{user_name}* hat geprüft — "
                "⚠️ Ramp kann nicht automatisch angelegt werden: fehlende Daten "
                f"(sub_id={sub_id!r}, new_plan={new_plan_id!r}, effective_date={effective_date!r}). "
                "Bitte manuell in Chargebee anlegen."
            ),
            thread_ts=thread_ts,
        )
        _va_pending_approval.pop((channel, thread_ts), None)
        return

    # Unix-Timestamp für effective_from berechnen
    if hasattr(effective_date, 'timestamp'):
        effective_from = int(effective_date.timestamp())
    else:
        from datetime import datetime as _dt
        effective_from = int(_dt(effective_date.year, effective_date.month, effective_date.day,
                                 tzinfo=timezone.utc).timestamp())

    # Ramp-Call aufbauen
    url = f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/subscriptions/{sub_id}/create_ramp"
    data = {
        'effective_from': effective_from,
        'items_to_add[item_price_id][0]': new_plan_id,
        'items_to_add[quantity][0]': '1',
        'items_to_remove[0]': old_plan_id,
    }
    # Service-Paket: altes entfernen wenn vorhanden und neu (inkl. im Plan)
    old_service = subscription.get('service_package_addon_id', '')
    if old_service:
        data['items_to_remove[1]'] = old_service

    try:
        import requests as _req
        resp = requests.post(url, auth=(CHARGEBEE_API_KEY, ''), data=data, timeout=15)
        if resp.ok:
            ramp = resp.json().get('ramp', {})
            ramp_id = ramp.get('id', '–')
            cb_sub_link = f"https://{CHARGEBEE_SITE}.chargebee.com/d/subscriptions/{sub_id}"
            cb_ramp_link = f"https://{CHARGEBEE_SITE}.chargebee.com/d/subscriptions/{sub_id}#ramps"
            slack_thread_link = slack_message_link(channel, thread_ts)
            say(
                text=(
                    f":white_check_mark: *Ramp angelegt* — freigegeben von *{user_name}*\n"
                    f"• Ramp-ID: `{ramp_id}`\n"
                    f"• Effective: `{effective_date}`\n"
                    f"• Neuer Plan: `{new_plan_id}`\n\n"
                    f"<{cb_ramp_link}|Ramp in Chargebee öffnen> · <{cb_sub_link}|Subscription öffnen>"
                ),
                thread_ts=thread_ts,
            )
            _add_reaction(client, channel, thread_ts, 'white_check_mark')

            # Planhat Note erstellen
            customer_name = parsed.get('customer_name', '')
            if customer_name and PLANHAT_API_TOKEN:
                ph_company = _planhat_search_company(customer_name, _debit_number_from_subscription(subscription))
                if ph_company and ph_company.get('id'):
                    old_plan = subscription.get('plan_id', '–') if subscription else '–'
                    note_text = (
                        f"Vertragsanpassung vorgenommen von {user_name}\n\n"
                        f"Plan: {old_plan} → {new_plan_id}\n"
                        f"Effective: {effective_date}\n"
                        f"Ramp-ID: {ramp_id}\n"
                        f"Chargebee: {cb_ramp_link}\n"
                        f'Slack-Thread: <a href="{slack_thread_link}">Thread öffnen</a>'
                    )
                    try:
                        requests.post(
                            'https://api.planhat.com/conversations',
                            headers={'Authorization': f'Bearer {PLANHAT_API_TOKEN}'},
                            json={
                                'subject': 'Vertragsanpassung',
                                'description': note_text,
                                'companyId': ph_company['id'],
                                'type': 'note',
                            },
                            timeout=10,
                        )
                        logger.info(f"Planhat note created for {customer_name}")
                    except Exception as e:
                        logger.warning(f"Planhat note failed: {e}")
        else:
            say(
                text=(
                    f":warning: *Ramp-Anlage fehlgeschlagen* (freigegeben von *{user_name}*)\n"
                    f"HTTP {resp.status_code}: `{resp.text[:500]}`\n"
                    "Bitte Ramp manuell in Chargebee anlegen."
                ),
                thread_ts=thread_ts,
            )
    except Exception as e:
        say(
            text=f":warning: *Ramp-Anlage fehlgeschlagen* — Exception: `{e}`\nBitte manuell anlegen.",
            thread_ts=thread_ts,
        )

    _va_pending_approval.pop((channel, thread_ts), None)


@app.action("planhat_link_skip")
def handle_planhat_link_skip(ack, body, say):
    """CS Admin überspringt Planhat-Upload oder Log."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    if user_id not in CS_ADMIN_USER_IDS:
        return
    thread_ref = body.get('actions', [{}])[0].get('value', '')
    if '|||' in thread_ref:
        ch, ts = thread_ref.split('|||', 1)
        _pending_planhat_link.pop((ch, ts), None)
    say(
        text=":white_check_mark: Übersprungen — kein Planhat-Eintrag erstellt.",
        thread_ts=body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts'),
    )


@app.action("create_ticket_button")
def handle_create_ticket(ack, body, say):
    ack()


@app.event("reaction_added")
def handle_reaction_added(event, say, client):
    """Wenn jemand außerhalb des CS Admin Teams mit 👀 oder ✅ auf eine Nachricht reagiert,
    informiert der Bot das CS Admin Team im Thread."""
    reaction = event.get('reaction', '')
    # Nur 👀 und ✅ überwachen (csadmin-bot ist ausschließlich für Bot-interne Nutzung)
    if reaction not in ('eyes', 'white_check_mark'):
        return

    user_id = event.get('user', '')
    if not user_id:
        return

    # CS Admin Team + Bot-User ignorieren
    if user_id in CS_ADMIN_USER_IDS:
        return
    try:
        info = client.users_info(user=user_id)
        if info['user'].get('is_bot') or info['user'].get('is_app_user'):
            return  # Bot-Reaktionen (auch unsere eigenen) ignorieren
    except Exception:
        pass

    item = event.get('item', {})
    if item.get('type') != 'message':
        return

    channel = item.get('channel', '')
    if channel != SLACK_CHANNEL_ID:
        return

    ts = item.get('ts', '')
    if not ts:
        return

    user_name = get_user_name(client, user_id)
    admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)
    emoji = '👀' if reaction == 'eyes' else '✅'

    logger.info(f"Reaction {reaction!r} by {user_name} ({user_id}) on {channel}/{ts}")

    say(
        text=(
            f"{admin_mentions} — <@{user_id}> hat mit {emoji} auf diese Nachricht reagiert.\n"
            "Bitte prüfen und ggf. übernehmen."
        ),
        thread_ts=ts,
    )


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting CS Improvement Bot on port {port}...")
    flask_app.run(host="0.0.0.0", port=port)
