import logging
import os
import re
from text_utils import normalize_slack_text
import json as _json
from status_handler import detect_status_question, extract_invoice_ids, build_status_reply, detect_open_invoice_question, open_invoices_reply
import threading
import time
from datetime import datetime, timezone, timedelta

import requests

from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from config import (
    CHARGEBEE_API_KEY,
    CHARGEBEE_SITE,
    CS_ADMIN_USER_IDS,
    OPOS_CHANNEL_ID,
    OPOS_SERVICE_URL,
    PLANHAT_API_TOKEN,
    PLANHAT_WORKSPACE_URL,
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_CHANNEL_IDS,
    SLACK_SIGNING_SECRET,
    VERTRAGSANPASSUNG_CHANNEL_ID,
)
def _forward_to_opos_service(event_type: str, event: dict) -> None:
    """Leitet ein Slack-Event aus dem OPOS-Channel an den ausgelagerten
    opos-sperrpruefung-bot Service weiter (siehe dessen README.md). Dieser
    Bot bleibt der einzige Slack-Events-Empfänger (eine Slack-App kann nur
    eine Events-URL haben) und verarbeitet den Inhalt selbst nicht mehr."""
    if not OPOS_SERVICE_URL:
        logging.getLogger(__name__).warning("OPOS_SERVICE_URL nicht gesetzt — Event nicht weitergeleitet")
        return
    try:
        requests.post(f'{OPOS_SERVICE_URL.rstrip("/")}/slack-event',
                      json={'type': event_type, 'event': event}, timeout=10)
    except Exception as e:
        logging.getLogger(__name__).warning(f"opos forward failed: {e}")


from jira_handler import add_vote, create_ticket, delete_ticket, remove_vote, search_similar_tickets, search_customer_contract_tickets
from optimizer import optimize_ticket
from slack_utils import format_error, format_ticket_created
from vertragsanpassung_handler import (
    ask_for_va_info_blocks,
    build_cs_admin_subscription_blocks,
    build_va_summary_blocks,
    detect_vertragsanpassung,
    enrich_from_jira_tickets,
    extract_service_package,
    fetch_item_price,
    fetch_item_price_name,
    fetch_offer_data,
    lookup_chargebee_subscription,
    missing_va_fields,
    parse_vertragsanpassung,
    _fetch_subscription_by_id,
)
from jira_handler import JIRA_SERVER_URL, MIRROR_PROPERTY, get_issue_property, open_bot_mirror_tickets, set_issue_property
from mirror_request import (
    ACTION_CANCEL as MIRROR_ACTION_CANCEL,
    ACTION_CREATE as MIRROR_ACTION_CREATE,
    build_mirror_request_blocks,
    detect_mirror_request,
    resolve_mirror_request,
)
from sandbox_handler import (
    LOOM_SANDBOX_ANNUAL_URL,
    LOOM_SANDBOX_FREE_URL,
    LOOM_SANDBOX_MONTHLY_URL,
    LOOM_SANDBOX_STANDARD_L_URL,
    build_customer_email,
    build_existing_sandbox_mirroring_email,
    build_existing_sandbox_result_blocks,
    build_sandbox_admin_billing_blocks,
    build_sandbox_clarification_blocks,
    build_sandbox_come_back_later_blocks,
    build_sandbox_contract_type_question_blocks,
    build_sandbox_instance_info_request_blocks,
    build_sandbox_lookup_result_blocks,
    build_sandbox_mirroring_question_blocks,
    build_sandbox_step2_stub_blocks,
    build_sandbox_video_blocks,
    create_sandbox_mirroring_ticket,
    fetch_instance_info_from_csm,
    find_chargebee_customer_id_via_csm,
    get_planhat_chargebee_customer_id,
    detect_sandbox_request,
    extract_direct_references,
    get_chargebee_contact_email,
    get_chargebee_contact_email_by_id,
    get_planhat_sandbox_fields,
    get_planhat_sandbox_fields_by_id,
    parse_instance_info,
    parse_sandbox_request,
    resolve_paragraph_variants,
    resolve_spiegelung_only_variant,
    sandbox_is_free,
    sandbox_is_standard_l,
    sandbox_scope_needs_billing,
)

# Erkennt Chargebee-Links (Subscription ODER Customer) und Standard-IDs
_CB_URL_RE = re.compile(
    r'https?://[^\s]*chargebee\.com/d/(?P<type>subscriptions|customers)/(?P<id>[^\s/\|><]+)'
    r'|\b(?P<std_id>\d{4}-\d{4}-\d{4}-\d{4})\b',
    re.IGNORECASE,
)

# Bekannte #bot-Befehle, die in JEDEM Thread Vorrang vor einem laufenden Flow-
# State (Sandbox, VA, ...) haben müssen — insbesondere #bot-stop/#bot-remove,
# damit ein Mute-Befehl nicht von einer aktiven Flow-Rückfrage verschluckt wird.
_BOT_COMMAND_RE = re.compile(
    r'#bot-stop|#bot-remove|#vertragsanpassung|#spiegelung|#improvement|#planhat-log|#planhat-upload',
    re.IGNORECASE,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BOT_VERSION = "v2.8"
logger.info(f"Bot starting — version {_BOT_VERSION}")

# Custom-Emoji für die VA-Zusammenfassung (Slack-Name ohne Doppelpunkte)
VA_DONE_EMOJI = os.environ.get('VA_DONE_EMOJI', 'csadmin-bot')

# Feedback-Emoji: Mit diesem Emoji auf Bot-Nachrichten reagieren um einen Fehler zu melden
FEEDBACK_EMOJI = os.environ.get('FEEDBACK_EMOJI', 'sos')
# Channel für Feedback-Berichte (Slack Channel-ID)
FEEDBACK_CHANNEL_ID = os.environ.get('FEEDBACK_CHANNEL_ID', '')

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

# Sandbox-Anfrage-Flow state
# (channel, thread_ts) -> {'step', 'user_id', 'user_name', 'customer_name', 'chargebee_result',
#                           'planhat_result', 'variant', 'email_text', 'correction_text', 'created_at'}
_pending_sandbox: dict[tuple[str, str], dict] = {}

# VA-Zusammenfassungen die auf CS Admin Bestätigung warten (für 48h Reminder)
# (channel, thread_ts) -> {'sent_at': float, 'reminded': bool}
_va_pending_approval: dict[tuple[str, str], dict] = {}

# Threads, in denen der Bot eine Status-Frage beantwortet hat (für #bot-stop-Logging)
_status_answered: dict[tuple[str, str], float] = {}

# Gründe für #bot-stop (Buttons nach dem Stummschalten, geloggt als BOT_STOP_REASON)
_BOT_STOP_REASONS = [
    ('falscher_zeitpunkt', 'Falscher Zeitpunkt / keine Anfrage'),
    ('falsche_daten', 'Falsche Daten'),
    ('falscher_kunde', 'Falscher Kunde'),
    ('unnoetig', 'Unnötig, machen wir selbst'),
    ('anderes', 'Anderes'),
]

# Threads where the bot has been silenced via #bot-stop
_muted_threads: set[tuple[str, str]] = set()

# Spiegelungs-Anfragen auf bestehende Sandbox, bei denen noch Werte fehlen:
# (channel, root_ts) -> {'text': gesammelter Text, 'created_at': ts}. Nachgereichte Werte im
# Thread werden an den Text angehängt und neu abgeglichen.
_pending_mirror: dict[tuple[str, str], dict] = {}

# (channel, thread_ts) -> Jira key of the ticket created for this thread
_created_tickets: dict[tuple[str, str], str] = {}
VA_REMINDER_TTL = 48 * 3600  # 48 Stunden

# Warten auf Planhat-Link nach fehlgeschlagener Company-Suche
# (channel, thread_ts) -> {'action': 'upload'|'log', 'files': list, 'parsed': dict,
#                           'subscription': dict|None, 'user_name': str}
_pending_planhat_link: dict[tuple[str, str], dict] = {}

JIRA_KEY_RE = re.compile(r'\b([A-Z]+-\d+)\b')
PENDING_TTL = 72 * 3600  # 72 hours in seconds

# Plan-ID → lesbarer Name (für Planhat-Notes)
_PLAN_DISPLAY_NAME: dict[str, str] = {
    # Pro 25 | 24M monatlich
    'pro-biennial-contract-monthly-payment-v1': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Standard S',
    'pro-biennial-contract-monthly-payment-v2': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Standard M',
    'pro-biennial-contract-monthly-payment-v3': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Standard L',
    'pro-biennial-contract-monthly-payment-v4': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Growth S',
    'pro-biennial-contract-monthly-payment-v5': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Growth M',
    'pro-biennial-contract-monthly-payment-v6': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Growth L',
    'pro-biennial-contract-monthly-payment-v7': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Premium S',
    'pro-biennial-contract-monthly-payment-v8': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Premium M',
    'pro-biennial-contract-monthly-payment-v9': 'Pro 25 | 24-Monatsvertrag (monatl.) inkl. Premium L',
    # Pro 25 | 24M jährlich
    'pro-biennial-contract-annual-payment-v1': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Standard S',
    'pro-biennial-contract-annual-payment-v2': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Standard M',
    'pro-biennial-contract-annual-payment-v3': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Standard L',
    'pro-biennial-contract-annual-payment-v4': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Growth S',
    'pro-biennial-contract-annual-payment-v5': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Growth M',
    'pro-biennial-contract-annual-payment-v6': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Growth L',
    'pro-biennial-contract-annual-payment-v7': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Premium S',
    'pro-biennial-contract-annual-payment-v8': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Premium M',
    'pro-biennial-contract-annual-payment-v9': 'Pro 25 | 24-Monatsvertrag (jährl.) inkl. Premium L',
    # Pro 25 | 12M monatlich
    'pro-annual-contract-monthly-payment-v2': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Standard M',
    'pro-annual-contract-monthly-payment-v3': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Standard L',
    'pro-annual-contract-monthly-payment-v4': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Growth S',
    'pro-annual-contract-monthly-payment-v5': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Growth M',
    'pro-annual-contract-monthly-payment-v6': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Growth L',
    'pro-annual-contract-monthly-payment-v7': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Premium S',
    'pro-annual-contract-monthly-payment-v8': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Premium M',
    'pro-annual-contract-monthly-payment-v9': 'Pro 25 | 12-Monatsvertrag (monatl.) inkl. Premium L',
    # Legacy
    'pro23-annual-contract-monthly-payment': 'Pro 23 | 12-Monatsvertrag (monatl.)',
    'pro-annual-contract-monthly-payment': 'Pro 25 | 12-Monatsvertrag (monatl.)',
    'pro-monthly-contract-monthly-payment': 'Pro 25 | Monatsvertrag',
}

def _plan_display(plan_id: str) -> str:
    """Gibt lesbaren Plannamen zurück, Fallback auf plan_id."""
    return _PLAN_DISPLAY_NAME.get(plan_id, plan_id)

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
            first = lines[0]
            rest = '\n'.join(lines[1:]).strip() if len(lines) > 1 else ''
            # Langer einzelner Absatz (>80 Zeichen, kein expliziter Titel) →
            # komplett als Use Case behandeln, Titel muss separat angegeben werden.
            # Das vermeidet, dass Bot title == use_case meldet wenn jemand den
            # vollen Kontext in einer Nachricht schreibt.
            if len(first) > 80 and not rest:
                use_case = first
            else:
                title = first
                if rest:
                    use_case = rest

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

    # Sandbox-Anfrage-Flow: gleiche 72h TTL wie der Improvement-Flow
    expired_sandbox = [
        key for key, state in list(_pending_sandbox.items())
        if now - state.get('created_at', now) > PENDING_TTL
    ]
    for key in expired_sandbox:
        _pending_sandbox.pop(key, None)
        channel, thread_ts = key
        logger.info(f"Sandbox pending state expired for {channel}/{thread_ts}")

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
        _created_tickets[(channel, thread_ts)] = ticket['key']
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

def _enrich_from_jira(parsed: dict) -> tuple[dict, list[dict]]:
    """Sucht Jira-Tickets zum Kunden und ergänzt fehlende Felder."""
    customer = parsed.get('customer_name', '')
    if not customer or len(customer) < 3:
        return parsed, []
    missing = missing_va_fields(parsed)
    if not missing:
        return parsed, []  # Nichts zu ergänzen
    try:
        tickets = search_customer_contract_tickets(customer)
        if tickets:
            logger.info(f"Jira: {len(tickets)} Tickets für '{customer}' gefunden")
            parsed, relevant = enrich_from_jira_tickets(parsed, tickets)
            return parsed, relevant
    except Exception as e:
        logger.warning(f"Jira enrichment failed: {e}")
    return parsed, []


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


_CB_INVOICE_URL_RE = re.compile(r'chargebee\.com/d/invoices/(?P<id>[^\s/|><?#]+)', re.IGNORECASE)
_PLANHAT_COMPANY_ID_RE = re.compile(r'planhat\.com/\S*?(?:profile=Company\.|/profile/|/customer/)(?P<id>[a-f0-9]{24})', re.IGNORECASE)


def _cb_subscriptions_for_customer_id(customer_id: str, company: str = '') -> dict | None:
    from vertragsanpassung_handler import _fetch_subscriptions_for_customer
    base = f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2"
    return _fetch_subscriptions_for_customer(customer_id, base, (CHARGEBEE_API_KEY, ''), CHARGEBEE_SITE, company)


def _cb_company_name(customer_id: str) -> str:
    try:
        resp = requests.get(
            f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/customers/{customer_id}",
            auth=(CHARGEBEE_API_KEY, ''), timeout=8,
        )
        if resp.ok:
            c = resp.json().get('customer', {})
            return c.get('company') or f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
    except Exception as e:
        logger.warning(f"CB customer name lookup failed: {e}")
    return ''


def _va_lookup(parsed: dict, raw_text: str = '') -> dict | None:
    """Findet die Chargebee-Subscription für eine Vertragsanpassung — erst über Links, dann
    über den CSM-MCP, zuletzt über den (geratenen) Namen.

    Links im Post (Chargebee-Kunde/-Subscription/-Rechnung, Planhat-Firma) sind eindeutig
    und schlagen den aus dem Satz extrahierten Namen, der live oft falsch war ("Kunde: im
    Winback. Der Vertrag"). Wird der Kunde gefunden, ersetzt der Chargebee-Firmenname den
    geratenen Namen in `parsed`.
    """
    if not CHARGEBEE_API_KEY:
        return None
    text = normalize_slack_text(raw_text or '')
    customer_id = None
    sub = None

    m = _CB_URL_RE.search(text)
    if m and m.group('type'):
        link_id = (m.group('id') or '').strip()
        if m.group('type').lower() == 'customers':
            customer_id = link_id
        else:
            sub = _fetch_subscription_by_id(link_id, CHARGEBEE_API_KEY, CHARGEBEE_SITE)
            customer_id = (sub or {}).get('customer_id')
    if not customer_id:
        m = _CB_INVOICE_URL_RE.search(text)
        if m:
            try:
                resp = requests.get(
                    f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/invoices/{m.group('id')}",
                    auth=(CHARGEBEE_API_KEY, ''), timeout=8,
                )
                if resp.ok:
                    inv = resp.json().get('invoice', {})
                    customer_id = inv.get('customer_id')
                    if inv.get('subscription_id'):
                        sub = _fetch_subscription_by_id(inv['subscription_id'], CHARGEBEE_API_KEY, CHARGEBEE_SITE)
            except Exception as e:
                logger.warning(f"VA lookup via invoice link failed: {e}")
    if not customer_id:
        m = _PLANHAT_COMPANY_ID_RE.search(text)
        if m:
            customer_id = get_planhat_chargebee_customer_id(m.group('id'), PLANHAT_API_TOKEN)

    name = parsed.get('customer_name', '')
    if not customer_id and not sub:
        found = _cb_lookup(name)
        if found:
            return found
        if name and len(name) >= 4:
            # Eindeutiger Präfix-Treffer in Chargebee ("Rhehag" → "Rhehag Warenhandel GmbH")
            try:
                resp = requests.get(
                    f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/customers",
                    params={'company[starts_with]': name, 'limit': 2},
                    auth=(CHARGEBEE_API_KEY, ''), timeout=8,
                )
                hits = resp.json().get('list', []) if resp.ok else []
                if len(hits) == 1:
                    customer_id = hits[0]['customer']['id']
            except Exception as e:
                logger.warning(f"VA prefix lookup failed: {e}")
        if not customer_id and name:
            customer_id = find_chargebee_customer_id_via_csm(name)

    if not customer_id and not sub:
        return None
    company = _cb_company_name(customer_id) if customer_id else ''
    result = sub or _cb_subscriptions_for_customer_id(customer_id, company)
    if result:
        if company:
            result['company'] = company
            parsed['customer_name'] = company
            parsed['customer_name_source'] = 'chargebee'
        logger.info(f"VA lookup via Link/CSM: customer={customer_id} sub={result.get('subscription_id')}")
    return result


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
# Sandbox-Anfrage-Flow
# ---------------------------------------------------------------------------

_SANDBOX_AFFIRMATIVE_RE = re.compile(r'\bja\b', re.IGNORECASE)

# Labels für die Rückfrage bei unvollständiger Instanz-Info-Antwort
_INSTANCE_INFO_LABELS = {
    'prod_url': 'Prod URL',
    'prod_serial': 'Prod Serial',
    'sandbox_url': 'Sandbox URL',
    'sandbox_serial': 'Sandbox Serial',
}


def _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code):
    """Merkt sich im Thread, dass eine Rückfrage offen ist (kein Match/mehrdeutig/
    unauflösbarer Feldwert/kein Kundenname) — ohne diesen Eintrag würde eine spätere
    Korrektur-Antwort des CSMs im selben Thread ins Leere laufen (fällt sonst auf
    unrelated Handling wie die allgemeine Chat-Q&A zurück statt den Flow fortzusetzen)."""
    _pending_sandbox[(channel, ts)] = {
        'step': 'awaiting_clarification',
        'user_id': user_id,
        'user_name': user_name,
        'wants_spiegelung': wants_spiegelung,
        'wants_custom_code': wants_custom_code,
        'created_at': time.time(),
    }


def _run_sandbox_lookup(say, client, channel, ts, user_id, user_name, customer_name,
                         wants_spiegelung, wants_custom_code):
    """Chargebee/Planhat-Lookup per Namenssuche -> _finish_sandbox_lookup.

    Wird sowohl vom initialen Intent-Handler als auch von der Namens-Korrektur-
    Rückfrage (Schritt 'awaiting_clarification') mit demselben Kundennamen-Slot
    aufgerufen. Bricht bei fehlendem/mehrdeutigem Match mit einer Rückfrage ab
    (und merkt sich diese im Thread) — rät niemals.
    """
    chargebee_result = get_chargebee_contact_email(customer_name, CHARGEBEE_API_KEY, CHARGEBEE_SITE)
    if chargebee_result is None:
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht in Chargebee finden — "
                "ist der Firmenname korrekt geschrieben?"
            ),
            text="Kein Chargebee-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return
    if chargebee_result.get('ambiguous'):
        candidates = chargebee_result.get('candidates', [])
        candidate_lines = '\n'.join(
            f"• {c.get('company')} — Chargebee-ID `{c.get('customer_id')}`"
            + (f", {c['email']}" if c.get('email') else '')
            for c in candidates
        )
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht eindeutig in Chargebee finden:\n{candidate_lines}\n\n"
                "Schick mir den Chargebee-Kunden-Link zum richtigen Datensatz, dann übernehme ich den direkt."
            ),
            text="Mehrdeutiger Chargebee-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return

    planhat_result = get_planhat_sandbox_fields(
        customer_name, PLANHAT_API_TOKEN, debit_number=chargebee_result.get('debit_number')
    )
    if planhat_result is None:
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht in Planhat finden — "
                "ist der Firmenname korrekt geschrieben?"
            ),
            text="Kein Planhat-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return
    if planhat_result.get('ambiguous'):
        candidates = ', '.join(planhat_result.get('candidates', []))
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht eindeutig in Planhat finden — "
                f"meintest du: {candidates}? Schick mir gerne auch den Planhat-Link zum richtigen Datensatz."
            ),
            text="Mehrdeutiger Planhat-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return

    _finish_sandbox_lookup(say, channel, ts, user_id, user_name, customer_name, wants_spiegelung,
                            wants_custom_code, chargebee_result, planhat_result)


def _run_sandbox_lookup_by_reference(say, client, channel, ts, user_id, user_name, refs,
                                      wants_spiegelung, wants_custom_code):
    """Wie _run_sandbox_lookup, aber ausgehend von einem direkt gepasteten Chargebee-
    Kunden- und/oder Planhat-Firmen-Link (siehe extract_direct_references) statt
    einem Namen — keine Mehrdeutigkeit möglich, da über IDs statt Namen aufgelöst wird.
    Planhats custom-Feld 'CB Customer ID' überbrückt den Fall, dass nur der
    Planhat-Link gepastet wurde (kein separater Chargebee-Link nötig)."""
    chargebee_result = None
    planhat_result = None

    if refs.get('chargebee_customer_id'):
        chargebee_result = get_chargebee_contact_email_by_id(
            refs['chargebee_customer_id'], CHARGEBEE_API_KEY, CHARGEBEE_SITE
        )

    if refs.get('planhat_id'):
        planhat_result = get_planhat_sandbox_fields_by_id(refs['planhat_id'], PLANHAT_API_TOKEN)
        if chargebee_result is None and planhat_result and planhat_result.get('cb_customer_id'):
            chargebee_result = get_chargebee_contact_email_by_id(
                planhat_result['cb_customer_id'], CHARGEBEE_API_KEY, CHARGEBEE_SITE
            )

    if chargebee_result is None:
        say(
            blocks=build_sandbox_clarification_blocks(
                "Über den Link konnte ich keinen Chargebee-Kontakt finden — bitte den "
                "Chargebee-Kunden-Link schicken oder den Firmennamen erneut nennen."
            ),
            text="Link nicht auflösbar",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return

    customer_name = chargebee_result.get('company') or 'Kunde'

    if planhat_result is None:
        planhat_result = get_planhat_sandbox_fields(
            customer_name, PLANHAT_API_TOKEN, debit_number=chargebee_result.get('debit_number')
        )
    if planhat_result is None:
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht in Planhat finden — ist der Firmenname korrekt geschrieben?"
            ),
            text="Kein Planhat-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return
    if planhat_result.get('ambiguous'):
        candidates = ', '.join(planhat_result.get('candidates', []))
        say(
            blocks=build_sandbox_clarification_blocks(
                f"Ich konnte '{customer_name}' nicht eindeutig in Planhat finden — "
                f"meintest du: {candidates}? Schick mir gerne auch den Planhat-Link zum richtigen Datensatz."
            ),
            text="Mehrdeutiger Planhat-Treffer",
            thread_ts=ts,
        )
        _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
        return

    _finish_sandbox_lookup(say, channel, ts, user_id, user_name, customer_name, wants_spiegelung,
                            wants_custom_code, chargebee_result, planhat_result)


def _finish_sandbox_lookup(say, channel, ts, user_id, user_name, customer_name, wants_spiegelung,
                            wants_custom_code, chargebee_result, planhat_result):
    """Preis-Mapping -> E-Mail-Entwurf -> Slack-Antwort -> _pending_sandbox-Eintrag,
    sobald chargebee_result/planhat_result eindeutig aufgelöst sind — egal ob über
    Namenssuche (_run_sandbox_lookup) oder einen direkt gepasteten Link
    (_run_sandbox_lookup_by_reference)."""
    # Hat der Kunde laut Planhat bereits eine Sandbox (aktives 'sandbox-*'-Produkt)?
    # Dann passt Template 1 (Neu-Sandbox) inhaltlich nicht — stattdessen Template 2
    # (Spiegelung auf bestehender Sandbox) vorschlagen.
    if planhat_result.get('has_existing_sandbox'):
        variant = resolve_spiegelung_only_variant(
            planhat_result.get('spiegelung_raw'),
            planhat_result.get('cs_package'),
        )
        if variant.get('mode') == 'unresolved':
            field = variant.get('unresolved_field')
            raw_value = variant.get('raw_value')
            say(
                blocks=build_sandbox_clarification_blocks(
                    f"Ich habe im Planhat-Feld `{field}` einen mir unbekannten Wert "
                    f"'{raw_value}' gefunden — welcher Preis-Absatz passt hier?"
                ),
                text="Unbekannter Preiswert",
                thread_ts=ts,
            )
            _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
            return

        email_text = build_existing_sandbox_mirroring_email(
            chargebee_result.get('first_name'), chargebee_result.get('email'), variant['spiegelung_key']
        )
        say(
            blocks=build_existing_sandbox_result_blocks(
                customer_name, chargebee_result, planhat_result, variant, email_text
            ),
            text="Sandbox-Anfrage — E-Mail-Entwurf (bestehende Sandbox)",
            thread_ts=ts,
        )
    else:
        variant = resolve_paragraph_variants(
            planhat_result.get('sandbox_raw'),
            planhat_result.get('spiegelung_raw'),
            planhat_result.get('cs_package'),
        )
        if variant.get('mode') == 'unresolved':
            field = variant.get('unresolved_field')
            raw_value = variant.get('raw_value')
            say(
                blocks=build_sandbox_clarification_blocks(
                    f"Ich habe im Planhat-Feld `{field}` einen mir unbekannten Wert "
                    f"'{raw_value}' gefunden — welcher Preis-Absatz passt hier?"
                ),
                text="Unbekannter Preiswert",
                thread_ts=ts,
            )
            _await_sandbox_clarification(channel, ts, user_id, user_name, wants_spiegelung, wants_custom_code)
            return

        email_text = build_customer_email(
            chargebee_result.get('first_name'), chargebee_result.get('email'), variant
        )
        say(
            blocks=build_sandbox_lookup_result_blocks(
                customer_name, chargebee_result, planhat_result, variant, email_text
            ),
            text="Sandbox-Anfrage — E-Mail-Entwurf",
            thread_ts=ts,
        )
    _pending_sandbox[(channel, ts)] = {
        'step': 'awaiting_fit_confirmation',
        'channel': channel,
        'user_id': user_id,
        'user_name': user_name,
        'customer_name': customer_name,
        'wants_custom_code': wants_custom_code,
        'chargebee_result': chargebee_result,
        'planhat_result': planhat_result,
        'has_existing_sandbox': bool(planhat_result.get('has_existing_sandbox')),
        'variant': variant,
        'email_text': email_text,
        'created_at': time.time(),
    }


def _handle_sandbox_intent(say, client, channel, ts, user_id, user_name, text):
    """Parst die Sandbox-Anfrage und startet den Lookup — siehe _run_sandbox_lookup."""
    parsed = parse_sandbox_request(text)
    customer_name = parsed.get('customer_name')
    if not customer_name:
        say(
            blocks=build_sandbox_clarification_blocks(
                "Ich konnte keinen Kundennamen aus deiner Nachricht erkennen — "
                "für welchen Kunden soll die Sandbox eingerichtet werden?"
            ),
            text="Kundenname unklar",
            thread_ts=ts,
        )
        _await_sandbox_clarification(
            channel, ts, user_id, user_name, parsed.get('wants_spiegelung'), parsed.get('custom_code')
        )
        return

    _run_sandbox_lookup(
        say, client, channel, ts, user_id, user_name,
        customer_name, parsed.get('wants_spiegelung'), parsed.get('custom_code'),
    )


def _confirm_sandbox_fit(say, thread_ts, state):
    """E-Mail-Entwurf wurde bestätigt (Text 'ja' oder Button) — bittet den CSM,
    nach Kundenantwort zurückzukommen."""
    say(
        blocks=build_sandbox_come_back_later_blocks(),
        text="Alles klar — bis zur Kundenantwort!",
        thread_ts=thread_ts,
    )
    state['step'] = 'awaiting_customer_reply'


def _request_sandbox_correction(say, thread_ts, state, correction_text=None):
    """E-Mail-Entwurf passt nicht (Korrektur-Text oder Button) — fragt nach, was
    zu ändern ist. Bleibt in awaiting_fit_confirmation, ein Button allein kann
    die eigentliche Korrektur nicht abbilden, die muss weiterhin als Text folgen."""
    if correction_text is not None:
        state['correction_text'] = correction_text
    say(
        text="Danke für den Hinweis — was genau sollte ich anders formulieren?",
        thread_ts=thread_ts,
    )


def _advance_sandbox_thread(say, client, channel, thread_ts, user_id, user_name, text) -> bool:
    """Treibt den Sandbox-Flow im Thread einen Schritt weiter, falls einer aktiv ist.

    Gibt True zurück wenn eine Nachricht behandelt wurde (Aufrufer soll dann returnen),
    sonst False (kein aktiver Sandbox-Flow in diesem Thread).
    """
    state = _pending_sandbox.get((channel, thread_ts))
    if not state:
        return False

    # Bot-Befehle (#bot-stop, #bot-remove, ...) müssen IMMER Vorrang haben, auch
    # wenn hier ein Sandbox-Flow aktiv ist — sonst verschluckt z.B.
    # awaiting_clarification den Mute-Befehl als (erfolglosen) Korrekturversuch
    # und der Thread lässt sich nie stummschalten (live beobachteter Bug: #bot-stop
    # wurde als Firmenname interpretiert, der Thread blieb aktiv).
    if _BOT_COMMAND_RE.search(text or ''):
        return False

    step = state.get('step')
    if step == 'awaiting_clarification':
        corrected_text = normalize_slack_text(text or '').strip()
        # Nur die anfragende Person beantwortet die Rückfrage, und nur mit einem Namen
        # oder Link — sonst hat der Bot live jede Thread-Unterhaltung (auch zwischen
        # CS-Admins) als Firmennamen in Chargebee gesucht.
        if user_id != state.get('user_id'):
            return True
        if not extract_direct_references(corrected_text) and len(corrected_text.split()) > 8:
            return True
        if not corrected_text:
            say(text="Für welchen Kunden soll die Sandbox eingerichtet werden?", thread_ts=thread_ts)
            return True
        # Direkt gepasteter Chargebee-/Planhat-Link? Dann per ID statt per Namen
        # auflösen — sonst würde der Link selbst als (garantiert erfolgloser)
        # Namens-Suchbegriff behandelt.
        refs = extract_direct_references(corrected_text)
        if refs:
            _run_sandbox_lookup_by_reference(
                say, client, channel, thread_ts, state['user_id'], state['user_name'],
                refs, state.get('wants_spiegelung'), state.get('wants_custom_code'),
            )
            return True
        _run_sandbox_lookup(
            say, client, channel, thread_ts, state['user_id'], state['user_name'],
            corrected_text, state.get('wants_spiegelung'), state.get('wants_custom_code'),
        )
        return True

    if step == 'awaiting_fit_confirmation':
        if _SANDBOX_AFFIRMATIVE_RE.search(text or ''):
            _confirm_sandbox_fit(say, thread_ts, state)
        else:
            logger.info(f"Sandbox-Korrektur in {channel}/{thread_ts}: {text!r}")
            _request_sandbox_correction(say, thread_ts, state, correction_text=text)
        return True

    if step == 'awaiting_customer_reply':
        say(
            blocks=build_sandbox_mirroring_question_blocks(state.get('has_existing_sandbox', False)),
            text="Möchte der Kunde eine Spiegelung?",
            thread_ts=thread_ts,
        )
        state['step'] = 'awaiting_scope_choice'
        return True

    if step == 'awaiting_scope_choice':
        say(text="Bitte nutze einen der Buttons oben 👆", thread_ts=thread_ts)
        return True

    if step == 'awaiting_contract_type':
        say(text="Bitte nutze einen der Buttons oben 👆", thread_ts=thread_ts)
        return True

    if step == 'awaiting_instance_info':
        # Werte aus dem Instance Manager (falls vorhanden) + was der CSM ergänzt; Eingaben
        # des CSM überschreiben die vorbefüllten Werte.
        info = {**(state.get('instance_info_prefill') or {}), **parse_instance_info(text or '')}
        required = ('prod_url', 'prod_serial', 'sandbox_url', 'sandbox_serial')
        missing = [k for k in required if k not in info]
        if missing:
            missing_labels = ', '.join(_INSTANCE_INFO_LABELS[k] for k in missing)
            say(
                text=f"Mir fehlen noch: {missing_labels} — bitte ergänzen.",
                thread_ts=thread_ts,
            )
            return True

        _create_mirroring_ticket_and_report(say, thread_ts, state, info)
        return True

    return False


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

_VA_ORDER_RE = re.compile(
    r'\b(?:bitte|k[öo]nnt\s+ihr|k[öo]nnten\s+wir|w[üu]rdet\s+ihr)\b[^\n]{0,160}?\b(?:umstellen|umzustellen|anlegen|einrichten|'
    r'hinterlegen|vornehmen|einstellen|upgraden|downgraden|wechseln|verl[äa]ngern|[äa]ndern|anpassen|eintragen|buchen)\b'
    r'|\bfolgende\w*\s+vertrags\w*\b|\bunterschrieben\w*\s+angebot|\bangebot\b[^\n]{0,60}\bunterschrieben',
    re.IGNORECASE,
)
_FYI_RE = re.compile(r'\b(?:fyi|zur\s+info|zur\s+kenntnis|nur\s+zur\s+info|info\s+an\s+euch)\b', re.IGNORECASE)


def _looks_like_va_order(text: str) -> bool:
    """Auto-Erkennung nur für Aufträge, nicht für Fragen, Meldungen oder FYIs.

    Live-Fehltreffer, die das verhindert: Kilians Winback-Frage (Scaff24), Finance-FYI zur
    MANAX-Gutschrift, Reklamationen wie „hat eine Jahresrechnung erhalten, sollte aber …".
    """
    if _FYI_RE.search(text):
        return False
    if not _VA_ORDER_RE.search(text):
        return False
    # Eine Frage ohne klaren Auftrag am Satzanfang ("Bitte … umstellen") ist keiner
    questions = text.count('?')
    return questions == 0 or bool(re.search(r'(?:^|\n|[.!:])\s*(?:hi\w*\s+\w+,?\s*)?bitte\b', text, re.IGNORECASE))


def _start_va_flow(say, client, channel, ts, user_id, user_name, text, files=None):
    """Vertragsanpassung aus einem Text starten (Auto-Erkennung oder Menü-Button)."""
    _set_eyes(client, channel, ts)
    parsed = _enrich_from_offer(parse_vertragsanpassung(text))
    # Nicht lesbarer Angebots-Link: kein eigener Warn-Post mehr (führte zu Doppel-
    # antworten), der Hinweis steht in der Infozeile zum Angebots-Link.
    subscription = _va_lookup(parsed, text)
    parsed = _inherit_from_subscription(parsed, subscription)
    # Jira-Tickets zum Kunden nach fehlenden Infos durchsuchen
    parsed, jira_tickets = _enrich_from_jira(parsed)
    if jira_tickets:
        parsed['_jira_sources'] = jira_tickets
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
        _process_vertragsanpassung(say, client, channel, ts, user_name, parsed, subscription, files=files)


def _handle_message_core(event, say, client):
    """Core message processing logic, shared by the generic and file_share handlers."""
    subtype = event.get('subtype')
    logger.info(
        f"Incoming message: subtype={subtype!r}, channel={event.get('channel')!r}, "
        f"has_files={bool(event.get('files'))}, thread={event.get('thread_ts')!r}"
    )

    if event.get('bot_id'):
        return

    # --- OPOS-Sperrprüfung: an ausgelagerten Service weiterleiten ---
    # Muss VOR dem Channel-Filter unten laufen, da #opos-instance-blocking nicht in
    # SLACK_CHANNEL_IDS/VERTRAGSANPASSUNG_CHANNEL_ID enthalten ist. Die eigentliche
    # OPOS-Logik lebt seit 2026-08-18 im separaten Repo/Service opos-sperrpruefung-bot
    # (siehe dessen README.md) — hier wird nur weitergeleitet, nicht verarbeitet.
    if event.get('channel') == OPOS_CHANNEL_ID:
        _forward_to_opos_service('message', event)
        return

    # --- DMs direkt an den Bot → Chat ---
    if event.get('channel_type') == 'im':
        user_id = event.get('user', '')
        if not user_id:
            return
        text = event.get('text', '') or ''
        channel = event.get('channel', '')
        dm_ts = event.get('ts')
        dm_thread_ts = event.get('thread_ts')
        user_name = get_user_name(client, user_id)

        # --- Sandbox-Anfrage-Flow: für ALLE CSMs, unabhängig von CS_ADMIN_USER_IDS ---
        # Muss vor dem Admin-only-Gate laufen, damit auch Nicht-Admin-CSMs den Flow per DM
        # nutzen können. Alle anderen DMs fallen unverändert durch die bestehende Admin-Logik.
        if dm_thread_ts and _advance_sandbox_thread(say, client, channel, dm_thread_ts, user_id, user_name, text):
            return
        if detect_sandbox_request(text):
            _handle_sandbox_intent(say, client, channel, dm_ts, user_id, user_name, text)
            return

        if user_id not in CS_ADMIN_USER_IDS:
            say(text="Die Chat-Funktion ist aktuell nur für das CS Admin Team verfügbar.")
            return
        # In DMs kein thread_ts — direkt in den DM-Channel antworten
        import re as _re
        clean = _re.sub(r'<@[A-Z0-9]+>', '', text).strip()
        if not clean:
            say(text="Was möchtest du wissen? :thinking_face:")
            return
        try:
            client.chat_postMessage(channel=channel, text=":thinking_face: Ich schaue nach...")
            from chat_handler import answer
            response = answer(clean, user_name=user_name)
            client.chat_postMessage(channel=channel, text=response)
        except Exception as e:
            logger.warning(f"DM chat error: {e}")
            client.chat_postMessage(channel=channel, text=f":warning: Fehler: {e}")
        return

    # Allow messages from the improvement channel OR the vertragsanpassung channel
    _in_improvement = (event.get('channel') in SLACK_CHANNEL_IDS)
    _in_va = (VERTRAGSANPASSUNG_CHANNEL_ID and event.get('channel') == VERTRAGSANPASSUNG_CHANNEL_ID)
    if not _in_improvement and not _in_va:
        return
    # Skip system subtypes (edits, deletes, joins, …) but allow file_share through
    if subtype and subtype != 'file_share':
        return

    _cleanup_expired_pending(client)

    # Slack-Markup (<url|Label>, <@U…>, <!here>) einmal zentral in Klartext umwandeln —
    # alle Parser (Links, Kundennamen, Daten) arbeiten danach auf sauberem Text.
    text = normalize_slack_text(event.get('text', '') or '')
    channel = event.get('channel')
    ts = event.get('ts')
    thread_ts = event.get('thread_ts')
    user_id = event.get('user')
    user_name = get_user_name(client, user_id)
    request_date = ts_to_date(thread_ts or ts)

    logger.info(f"Processing: channel={channel}, thread={thread_ts}, user={user_id} ({user_name})")

    # Thread stummgeschaltet via #bot-stop → komplett ignorieren
    if thread_ts and (channel, thread_ts) in _muted_threads:
        return

    # -----------------------------------------------------------------------
    # THREAD REPLY
    # -----------------------------------------------------------------------
    if thread_ts:
        # --- Sandbox-Anfrage: Fortsetzung eines aktiven Flows in diesem Thread ---
        if _advance_sandbox_thread(say, client, channel, thread_ts, user_id, user_name, text):
            return

        # --- Spiegelungs-Anfrage: nachgereichte URL/Serial im Thread ---
        _pm = _pending_mirror.get((channel, thread_ts))
        if _pm and time.time() - _pm['created_at'] < 3 * 86400 and re.search(r'xentral\.|[0-9a-f]{8}-[0-9a-f]{4}-', text, re.IGNORECASE):
            _pm['text'] += '\n' + text
            _post_mirror_request(say, client, channel, thread_ts, _pm['text'])
            return

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
                        "• Für eine Spiegelung auf bestehende Sandbox: `#spiegelung` hier posten (nur CS Admin)\n"
                        "• Für Feature-Anfragen: `#improvement` in einer neuen Nachricht schreiben"
                    ),
                    thread_ts=thread_ts,
                )
            return

        # --- Admin-Befehle zuerst prüfen (vor allen State-Checks) ---
        if '#bot-stop' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(text=":no_entry: `#bot-stop` kann nur vom CS Admin Team genutzt werden.", thread_ts=thread_ts)
                return

            jira_key = _created_tickets.get((channel, thread_ts))
            if not jira_key:
                try:
                    replies = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                    for msg in replies.get('messages', []):
                        m = JIRA_KEY_RE.search((msg.get('text') or '').upper())
                        if m:
                            jira_key = m.group(1)
                            break
                except Exception as e:
                    logger.warning(f"#bot-stop thread scan failed: {e}")

            deleted_ticket = None
            if jira_key:
                try:
                    result = delete_ticket(jira_key)
                    deleted_ticket = result.get('summary', jira_key)
                except Exception as e:
                    logger.warning(f"#bot-stop: ticket delete {jira_key} failed: {e}")

            for emoji in ('eyes', 'white_check_mark', 'x', VA_DONE_EMOJI):
                try:
                    client.reactions_remove(channel=channel, name=emoji, timestamp=thread_ts)
                except Exception:
                    pass

            # Messbarkeit: welcher Flow war aktiv, als gestoppt wurde (Cloud-Logging: "BOT_STOP")
            _key = (channel, thread_ts)
            _active_flows = [name for name, store in (
                ('improvement', _pending), ('vertragsanpassung', _pending_vertragsanpassung),
                ('va_freigabe', _va_pending_approval), ('sandbox', _pending_sandbox),
                ('aehnliche_tickets', _similar_shown), ('status', _status_answered),
            ) if _key in store]
            logger.info("BOT_STOP " + _json.dumps({
                'channel': channel, 'thread_ts': thread_ts, 'user': user_name,
                'flows': _active_flows or ['unbekannt'], 'jira_key': jira_key or '',
            }, ensure_ascii=False))

            _pending.pop((channel, thread_ts), None)
            _ticket_data.pop((channel, thread_ts), None)
            _similar_shown.pop((channel, thread_ts), None)
            _pending_vertragsanpassung.pop((channel, thread_ts), None)
            _va_pending_approval.pop((channel, thread_ts), None)
            _created_tickets.pop((channel, thread_ts), None)
            _muted_threads.add((channel, thread_ts))

            parts = [":mute: Thread stummgeschaltet — der Bot antwortet hier nicht mehr."]
            if deleted_ticket:
                parts.append(f":wastebasket: Ticket *{jira_key}* ({deleted_ticket}) gelöscht.")
            say(
                text='\n'.join(parts),
                blocks=[
                    {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '\n'.join(parts) + "\n_Was war los? (1 Klick, hilft beim Verbessern)_"}},
                    {'type': 'actions', 'elements': [
                        {'type': 'button', 'text': {'type': 'plain_text', 'text': label},
                         'action_id': f'botstop_reason_{code}', 'value': f'{channel}|{thread_ts}'}
                        for code, label in _BOT_STOP_REASONS
                    ]},
                ],
                thread_ts=thread_ts,
            )
            return

        if '#bot-remove' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(text=":no_entry: `#bot-remove` kann nur vom CS Admin Team genutzt werden.", thread_ts=thread_ts)
                return

            jira_match = JIRA_KEY_RE.search(text.upper())
            is_delete = bool(re.search(r'\bdelete\b', text, re.IGNORECASE))

            if jira_match:
                issue_key = jira_match.group(1)
                if is_delete:
                    try:
                        result = delete_ticket(issue_key)
                        say(
                            text=f":wastebasket: Ticket *{issue_key}* (__{result.get('summary', '')}__) wurde gelöscht.",
                            thread_ts=thread_ts,
                        )
                    except Exception as e:
                        say(text=f":x: Ticket `{issue_key}` konnte nicht gelöscht werden: {str(e)}", thread_ts=thread_ts)
                else:
                    try:
                        result = remove_vote(issue_key)
                        parts = []
                        if result.get('vote_removed'):
                            parts.append("Vote entfernt")
                        if result.get('comment_removed'):
                            parts.append("Upvote-Kommentar gelöscht")
                        if parts:
                            summary = result.get('summary', issue_key)
                            say(text=f":broom: *{issue_key}* ({summary}): {', '.join(parts)}.", thread_ts=thread_ts)
                        else:
                            say(text=f":broom: Kein Bot-Upvote auf `{issue_key}` gefunden.", thread_ts=thread_ts)
                    except Exception as e:
                        say(text=f":x: Fehler bei `{issue_key}`: {str(e)}", thread_ts=thread_ts)
            else:
                removed = []
                for emoji in ('eyes', 'white_check_mark', 'x', VA_DONE_EMOJI):
                    try:
                        client.reactions_remove(channel=channel, name=emoji, timestamp=thread_ts)
                        removed.append(f":{emoji}:")
                    except Exception:
                        pass
                if removed:
                    say(text=f":broom: Reaktionen entfernt: {' '.join(removed)}", thread_ts=thread_ts)
                else:
                    say(text=":broom: Keine eigenen Reaktionen auf der Nachricht gefunden.", thread_ts=thread_ts)
            return

        # --- Planhat-Link als Antwort auf _ask_for_planhat_link ---
        # Auch ohne aktiven State reagieren (Bot-Neustart löscht State)
        ph_pending = _pending_planhat_link.get((channel, thread_ts))
        ph_url_match_direct = re.search(r'https?://(?:app|ws)\.planhat\.com/\S+', text)
        # Früher: Hinweis "Planhat-Link erkannt" auf jede Antwort mit Planhat-Link — hat live
        # nie geholfen (kam meist nach einer bereits erledigten Antwort) und wurde entfernt.
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
                    f"Plan: {_plan_display(old_plan)} → {_plan_display(new_plan)}",
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
        if va_state and (va_state.get('user_id') == user_id or user_id in CS_ADMIN_USER_IDS):
            # Only parse text — no URL fetch here (avoids duplicate error messages from thread replies)
            new_parsed = parse_vertragsanpassung(text)
            # Merge: only fill empty fields from the follow-up reply
            changed = False
            for k, v in new_parsed.items():
                if v and not va_state['parsed'].get(k):
                    va_state['parsed'][k] = v
                    changed = True
            # Try Chargebee lookup now if customer_name just became available
            if not va_state.get('subscription') and va_state['parsed'].get('customer_name'):
                va_state['subscription'] = _va_lookup(va_state['parsed'], text)
                if va_state['subscription']:
                    changed = True
            if not changed:
                # Nothing new found — stay silent, don't repeat the dropdown
                return
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
                f"Plan: {_plan_display(old_plan)} → {_plan_display(new_plan)}",
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

        # --- Spiegelung auf bestehende Sandbox: manueller Thread-Trigger (CS Admin only) ---
        # Für Anfragen, die die Auto-Erkennung verpasst hat oder die vor dem Deploy kamen.
        if '#spiegelung' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(text=":no_entry: `#spiegelung` kann nur vom CS Admin Team genutzt werden.",
                    thread_ts=thread_ts)
                return
            texts = []
            try:
                msgs = client.conversations_replies(channel=channel, ts=thread_ts, limit=50).get('messages', [])
                texts = [normalize_slack_text(m.get('text', '')) for m in msgs
                         if not m.get('bot_id') and m.get('text') and '#spiegelung' not in m.get('text', '').lower()]
            except Exception as e:
                logger.warning(f"conversations_replies failed in #spiegelung trigger: {e}")
            _post_mirror_request(say, client, channel, thread_ts, '\n'.join(texts) or text)
            return

        # --- Vertragsanpassung: manual thread trigger (CS Admin only) ---
        if '#vertragsanpassung' in text.lower():
            if user_id not in CS_ADMIN_USER_IDS:
                say(
                    text=":no_entry: `#vertragsanpassung` kann nur vom CS Admin Team genutzt werden.",
                    thread_ts=thread_ts,
                )
                return
            # Alle Thread-Nachrichten lesen (Root + Replies) für maximalen Kontext
            root_text = ''
            root_author = user_id
            thread_texts = []
            try:
                result = client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
                messages = result.get('messages', [])
                if messages:
                    root_text = normalize_slack_text(messages[0].get('text', ''))
                    root_author = messages[0].get('user') or user_id
                    # Alle nicht-Bot-Replies sammeln (außer der aktuellen #vertragsanpassung)
                    for msg in messages[1:]:
                        if not msg.get('bot_id') and msg.get('text') and '#vertragsanpassung' not in msg.get('text', '').lower():
                            thread_texts.append(normalize_slack_text(msg.get('text', '')))
                logger.info(f"VA manual trigger: root + {len(thread_texts)} thread replies")
            except Exception as e:
                logger.warning(f"conversations_replies failed in VA trigger: {e}")
            _set_eyes(client, channel, thread_ts)

            # Root-Nachricht parsen
            parsed = _enrich_from_offer(parse_vertragsanpassung(root_text or text))
            # Fehlende Felder aus Thread-Replies ergänzen
            for reply_text in thread_texts:
                reply_parsed = parse_vertragsanpassung(reply_text)
                for k, v in reply_parsed.items():
                    if v and not parsed.get(k):
                        parsed[k] = v
                        logger.info(f"VA trigger: '{k}' aus Reply ergänzt: {v!r}")
            # Nicht lesbarer Angebots-Link: kein eigener Warn-Post mehr (führte zu Doppel-
            # antworten), der Hinweis steht in der Infozeile zum Angebots-Link.
            subscription = _va_lookup(parsed, '\n'.join([root_text or text] + thread_texts))
            parsed = _inherit_from_subscription(parsed, subscription)
            parsed, jira_tickets = _enrich_from_jira(parsed)
            if jira_tickets:
                parsed['_jira_sources'] = jira_tickets
            missing = missing_va_fields(parsed)
            if missing:
                _pending_vertragsanpassung[(channel, thread_ts)] = {
                    'parsed': parsed,
                    'user_id': user_id,
                    'user_name': user_name,
                    'subscription': subscription,
                    'created_at': time.time(),
                }
                # Ansprechen: wer die Anfrage gestellt hat (Root-Post), nicht die CS-Admin,
                # die den Flow per #vertragsanpassung gestartet hat
                say(
                    blocks=ask_for_va_info_blocks(root_author, missing, parsed, subscription),
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
                # Nur uc_parsed verwenden — kein text.strip() Fallback.
                # Wenn jemand "Titel: X" schreibt, soll das NICHT als Use Case landen.
                # Use Case muss explizit angegeben oder aus parse_request extrahiert werden.
                if uc_parsed:
                    state['use_case'] = uc_parsed
                elif not has_explicit_title:
                    # Freier Text ohne Label → als Use Case werten (wenn >20 Zeichen)
                    stripped = text.strip()
                    if len(stripped) > 20:
                        state['use_case'] = stripped

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
        # NUR wenn der Bot vorher für diesen Thread ähnliche Tickets gezeigt hat
        # (verhindert false positives wie Chargebee-Nummern "CN-3418" o.ä.)
        _in_improvement_flow = (
            (channel, thread_ts) in _similar_shown
            or (channel, thread_ts) in _ticket_data
        )
        jira_match = JIRA_KEY_RE.search(text.upper()) if _in_improvement_flow else None
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

    # --- Sandbox-Anfrage: auto-detection (Improvement- oder VA-Channel, z.B. #ask-cs-admin) ---
    # Vor dem Vertragsanpassungs-Check platziert, damit es nicht von überlappenden Keywords
    # geschluckt wird — detect_sandbox_request schließt VA-Treffer bereits selbst aus.
    # Eigene Posts des CS-Admin-Teams (Notizen, Reminder, Ankündigungen) sind keine
    # Anfragen — live hat der Bot z.B. auf Mirjams POC-Notiz einen VA-Flow gestartet.
    # Das Team startet Flows bei Bedarf bewusst per #vertragsanpassung im Thread.
    _auto_detect = user_id not in CS_ADMIN_USER_IDS

    # --- Status-Frage: direkt aus Chargebee beantworten (nur lesend) ---
    if _auto_detect and _in_va and (detect_status_question(text) or detect_open_invoice_question(text)):
        try:
            reply = (open_invoices_reply(text, _va_lookup) if detect_open_invoice_question(text)
                     else build_status_reply(text, _va_lookup))
        except Exception as e:
            logger.warning(f"Status answer failed: {e}")
            reply = None
        if reply:
            say(text=reply, thread_ts=ts)
            _status_answered[(channel, ts)] = time.time()
            logger.info("STATUS_ANSWER " + _json.dumps({'channel': channel, 'ts': ts, 'user': user_name,
                                                        'invoices': extract_invoice_ids(text)}))
            return

    # --- Spiegelung auf bestehende Sandbox: Werte abgleichen, CCS-Ticket per Button ---
    # Vor dem Sandbox-Neuanlage-Flow, weil beide auf "Sandbox" + "Spiegelung" reagieren.
    if _auto_detect and (_in_improvement or _in_va) and detect_mirror_request(text):
        _post_mirror_request(say, client, channel, ts, text)
        return

    if _auto_detect and (_in_improvement or _in_va) and detect_sandbox_request(text):
        _handle_sandbox_intent(say, client, channel, ts, user_id, user_name, text)
        return

    # --- Vertragsanpassung: auto-detection (only in VA channel) ---
    if _auto_detect and _in_va and detect_vertragsanpassung(text) and _looks_like_va_order(text):
        _start_va_flow(say, client, channel, ts, user_id, user_name, text,
                       files=extract_files(event.get('files')) or None)
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

    state = _va_pending_approval.get((channel, thread_ts), {})
    parsed = state.get('parsed', {})
    subscription = state.get('subscription', {})
    customer_name = parsed.get('customer_name', '')
    old_plan = subscription.get('plan_id', '–') if subscription else '–'
    new_plan_id = parsed.get('chargebee_plan_id', '–') or '–'
    effective_date = parsed.get('effective_date', '–')
    sub_id = subscription.get('subscription_id', '') if subscription else ''
    slack_thread_link = slack_message_link(channel, thread_ts)

    say(
        text=f":csadmin-bot: *{user_name}* übernimmt die Umsetzung — bitte im Thread als ✅ done markieren wenn erledigt.",
        thread_ts=thread_ts,
    )

    if customer_name and PLANHAT_API_TOKEN:
        ph_company = _planhat_search_company(customer_name, _debit_number_from_subscription(subscription))
        if ph_company and ph_company.get('id'):
            cb_sub_link = f"https://{CHARGEBEE_SITE}.chargebee.com/d/subscriptions/{sub_id}" if sub_id else ''
            note_text = (
                f"Vertragsanpassung manuell übernommen von {user_name}\n\n"
                f"Plan: {_plan_display(old_plan)} → {_plan_display(new_plan_id)}\n"
                f"Effective: {effective_date}\n"
                + (f"Chargebee: {cb_sub_link}\n" if cb_sub_link else '')
                + f'Slack-Thread: <a href="{slack_thread_link}">Thread öffnen</a>'
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
                logger.info(f"Planhat note created (manual take-over) for {customer_name} by {user_name}")
            except Exception as e:
                logger.warning(f"Planhat note failed (take-over): {e}")

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

            customer_name = parsed.get('customer_name', '')
            old_plan = subscription.get('plan_id', '–') if subscription else '–'

            # Chargebee Customer Note — Attribution: wer hat freigegeben
            cb_customer_id = subscription.get('customer_id', '') if subscription else ''
            if cb_customer_id and CHARGEBEE_API_KEY:
                cb_note = (
                    f"Ramp freigegeben von {user_name}\n"
                    f"Plan: {_plan_display(old_plan)} → {_plan_display(new_plan_id)}\n"
                    f"Effective: {effective_date}\n"
                    f"Ramp-ID: {ramp_id}\n"
                    f"Slack: {slack_thread_link}"
                )
                try:
                    requests.post(
                        f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/customer_notes",
                        auth=(CHARGEBEE_API_KEY, ''),
                        data={
                            'customer_id': cb_customer_id,
                            'entity_type': 'subscription',
                            'entity_id': sub_id,
                            'note': cb_note,
                        },
                        timeout=10,
                    )
                    logger.info(f"Chargebee note created for customer {cb_customer_id} by {user_name} (ramp {ramp_id})")
                except Exception as e:
                    logger.warning(f"Chargebee customer note failed: {e}")

            # Planhat Note erstellen
            if customer_name and PLANHAT_API_TOKEN:
                ph_company = _planhat_search_company(customer_name, _debit_number_from_subscription(subscription))
                if ph_company and ph_company.get('id'):
                    note_text = (
                        f"Vertragsanpassung vorgenommen von {user_name}\n\n"
                        f"Plan: {_plan_display(old_plan)} → {_plan_display(new_plan_id)}\n"
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
                        logger.info(f"Planhat note created for {customer_name} by {user_name}")
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


@app.action(re.compile(r"^botstop_reason_"))
def handle_botstop_reason(ack, body, client):
    ack()
    action = (body.get('actions') or [{}])[0]
    code = action.get('action_id', '').replace('botstop_reason_', '')
    channel, _, thread_ts = (action.get('value') or '').partition('|')
    user = (body.get('user') or {}).get('name') or (body.get('user') or {}).get('id', '')
    logger.info("BOT_STOP_REASON " + _json.dumps({'channel': channel, 'thread_ts': thread_ts,
                                                  'reason': code, 'user': user}, ensure_ascii=False))
    label = dict(_BOT_STOP_REASONS).get(code, code)
    try:
        msg = body.get('message') or {}
        client.chat_update(
            channel=(body.get('channel') or {}).get('id', channel), ts=msg.get('ts'),
            text=f":mute: Thread stummgeschaltet. Grund notiert: {label}. Danke!",
            blocks=[{'type': 'section', 'text': {'type': 'mrkdwn',
                     'text': f":mute: Thread stummgeschaltet. Grund notiert: *{label}*. Danke!"}}],
        )
    except Exception as e:
        logger.warning(f"botstop reason update failed: {e}")


def _post_mirror_request(say, client, channel: str, root_ts: str, text: str):
    """Gleicht die Spiegelungs-Anfrage mit dem Instance Manager ab und postet Zusammenfassung + Buttons."""
    try:
        res = resolve_mirror_request(text)
    except Exception as e:
        logger.warning(f"resolve_mirror_request failed: {e}")
        return
    say(blocks=build_mirror_request_blocks(res, channel, root_ts),
        text="Spiegelung auf bestehende Sandbox erkannt", thread_ts=root_ts)
    # Status-Reaktionen auf der Anfrage: :cs-admin-bot: = Bot kann das CCS-Ticket anlegen (wartet auf
    # Entscheidung), :status_in_progress: = Ticket angelegt, ✅ = Spiegelung abgeschlossen.
    _add_reaction(client, channel, root_ts, 'cs-admin-bot')
    if res['vollstaendig']:
        _pending_mirror.pop((channel, root_ts), None)
    else:
        _pending_mirror[(channel, root_ts)] = {'text': text, 'created_at': time.time()}
    logger.info("MIRROR_REQUEST " + _json.dumps({'channel': channel, 'ts': root_ts,
                'customer': res['customer_name'], 'complete': res['vollstaendig'],
                'conflicts': len(res['konflikte'])}, ensure_ascii=False))


def _mirror_button_payload(body) -> tuple[dict, str, str]:
    action = (body.get('actions') or [{}])[0]
    try:
        data = _json.loads(action.get('value') or '{}')
    except ValueError:
        data = {}
    channel = (body.get('channel') or {}).get('id') or data.get('c', '')
    msg_ts = (body.get('message') or {}).get('ts', '')
    return data, channel, msg_ts


def _mirror_finish_message(client, channel, msg_ts, text):
    try:
        client.chat_update(channel=channel, ts=msg_ts, text=text,
                           blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}])
    except Exception as e:
        logger.warning(f"mirror message update failed: {e}")


@app.action(MIRROR_ACTION_CREATE)
def handle_mirror_request_create(ack, body, client):
    ack()
    user_id = (body.get('user') or {}).get('id', '')
    data, channel, msg_ts = _mirror_button_payload(body)
    if user_id not in CS_ADMIN_USER_IDS:
        try:
            client.chat_postEphemeral(channel=channel, user=user_id, thread_ts=data.get('t'),
                                      text="Das CCS-Ticket legt das CS-Admin-Team an, wir sind informiert. :slightly_smiling_face:")
        except Exception:
            pass
        return
    if not all(data.get(k) for k in ('n', 'prod_url', 'prod_serial', 'sandbox_url', 'sandbox_serial')):
        _mirror_finish_message(client, channel, msg_ts, ":warning: Daten unvollständig, bitte neu anstoßen.")
        return
    ticket = create_sandbox_mirroring_ticket(data['n'], data['prod_url'], data['prod_serial'],
                                             data['sandbox_url'], data['sandbox_serial'],
                                             slack_link=_slack_permalink(client, channel, data.get('t')))
    who = f"<@{user_id}>"
    if ticket:
        _mirror_finish_message(client, channel, msg_ts,
            f":status_in_progress: CCS-Ticket <{ticket['url']}|{ticket['key']}> angelegt (von {who}) · {data['n']}\n"
            f"Ich prüfe regelmäßig, ob die Spiegelung abgeschlossen ist, und melde mich dann hier.\n"
            f"Prod: {data['prod_url']} · {data['prod_serial']}\n"
            f"Sandbox: {data['sandbox_url']} · {data['sandbox_serial']}")
        if data.get('t'):
            _remove_reaction(client, channel, data['t'], 'cs-admin-bot')
            _add_reaction(client, channel, data['t'], 'status_in_progress')
            set_issue_property(ticket['key'], MIRROR_PROPERTY,
                               {'channel': channel, 'ts': data['t'], 'notify': user_id,
                                'status_ts': msg_ts, 'customer': data['n'], 'done': False})
        logger.info("MIRROR_TICKET " + _json.dumps({'channel': channel, 'ts': data.get('t'),
                    'key': ticket['key'], 'by': user_id}))
    else:
        _mirror_finish_message(client, channel, msg_ts,
            ":warning: Das CCS-Ticket konnte nicht angelegt werden. Bitte manuell im CCS-Projekt anlegen.")


@app.action(MIRROR_ACTION_CANCEL)
def handle_mirror_request_cancel(ack, body, client):
    ack()
    user_id = (body.get('user') or {}).get('id', '')
    data, channel, msg_ts = _mirror_button_payload(body)
    if user_id not in CS_ADMIN_USER_IDS:
        return
    _mirror_finish_message(client, channel, msg_ts, f":no_entry_sign: Kein CCS-Ticket angelegt (<@{user_id}>).")
    if data.get('t'):
        _remove_reaction(client, channel, data['t'], 'cs-admin-bot')


# ---------------------------------------------------------------------------
# Menü: @CS Admin Bot ohne Text → Buttons, jeder öffnet ein kurzes Formular (Modal)
# ---------------------------------------------------------------------------

_MENU_ITEMS = [
    ('va', ':page_facing_up: Vertragsanpassung'),
    ('lookup', ':mag: Customer Lookup'),
    ('invoices', ':receipt: Offene Rechnungen'),
    ('sandbox', ':test_tube: Sandbox / Spiegelung'),
    ('improvement', ':bulb: Improvement-Idee'),
    ('help', ':grey_question: Was kannst du?'),
]


def _menu_blocks(user_id: str, channel: str, thread_ts: str) -> list[dict]:
    value = _json.dumps({'c': channel, 't': thread_ts})
    return [
        {'type': 'section', 'text': {'type': 'mrkdwn',
         'text': f"Hey <@{user_id}>! Wobei kann ich helfen?"}},
        {'type': 'actions', 'block_id': 'bot_menu', 'elements': [
            {'type': 'button', 'action_id': f'menu_{k}', 'value': value,
             'text': {'type': 'plain_text', 'text': label, 'emoji': True}}
            for k, label in _MENU_ITEMS]},
    ]


def _menu_input(block_id, label, placeholder, multiline=True, optional=False):
    return {'type': 'input', 'block_id': block_id, 'optional': optional,
            'label': {'type': 'plain_text', 'text': label},
            'element': {'type': 'plain_text_input', 'action_id': 'v', 'multiline': multiline,
                        'placeholder': {'type': 'plain_text', 'text': placeholder}}}


_MENU_MODALS = {
    'va': ('Vertragsanpassung', [
        _menu_input('text', 'Was soll angepasst werden?',
                    'Kunde, neuer Plan/Laufzeit/Servicepaket, ab wann, Link zum Angebot oder zu Chargebee')]),
    'lookup': ('Customer Lookup', [
        _menu_input('text', 'Kunde', 'Name, Kundennummer oder Chargebee-/Planhat-Link', multiline=False)]),
    'invoices': ('Offene Rechnungen', [
        _menu_input('text', 'Kunde', 'Name, Kundennummer oder Chargebee-Link', multiline=False)]),
    'sandbox': ('Sandbox / Spiegelung', [
        _menu_input('text', 'Kunde und Wunsch',
                    'z. B. Sandbox inkl. Spiegelung für Muster GmbH')]),
    'improvement': ('Improvement-Idee', [
        _menu_input('title', 'Titel', 'Kurz: was soll besser werden?', multiline=False),
        _menu_input('text', 'Use Case', 'Wer braucht es, wofür, was ist heute das Problem?')]),
}


@app.action(re.compile(r"^menu_(va|lookup|invoices|sandbox|improvement|help)$"))
def handle_menu_button(ack, body, client, say):
    ack()
    action = (body.get('actions') or [{}])[0]
    kind = action.get('action_id', '').replace('menu_', '')
    try:
        ctx = _json.loads(action.get('value') or '{}')
    except ValueError:
        ctx = {}
    if kind == 'help':
        say(text="CS Admin Bot Übersicht", thread_ts=ctx.get('t'),
            blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text':
                     "*Das kann ich:*\n"
                     "• *Vertragsanpassung*: Kunde, Plan und Angebot auslesen, Umstellung vorbereiten\n"
                     "• *Customer Lookup*: Subscription, Verlängerung, Instanz, offene Rechnungen\n"
                     "• *Offene Rechnungen*: alle offenen Rechnungen eines Kunden mit Summe\n"
                     "• *Sandbox / Spiegelung*: Preis, E-Mail-Entwurf, Anleitung, CCS-Ticket\n"
                     "• *Improvement-Idee*: ähnliche Jira-Tickets suchen oder neues anlegen\n"
                     "Status-Fragen wie „Ist Rechnung 77324 bezahlt?“ kannst du auch direkt stellen."}}])
        return
    title, inputs = _MENU_MODALS[kind]
    ctx['u'] = (body.get('user') or {}).get('id', '')
    try:
        client.views_open(trigger_id=body['trigger_id'], view={
            'type': 'modal', 'callback_id': f'menu_submit_{kind}', 'private_metadata': _json.dumps(ctx),
            'title': {'type': 'plain_text', 'text': title[:24]},
            'submit': {'type': 'plain_text', 'text': 'Los'},
            'close': {'type': 'plain_text', 'text': 'Abbrechen'},
            'blocks': inputs,
        })
    except Exception as e:
        logger.warning(f"menu modal open failed: {e}")


@app.view(re.compile(r"^menu_submit_(va|lookup|invoices|sandbox|improvement)$"))
def handle_menu_submit(ack, body, client, view):
    ack()
    kind = view.get('callback_id', '').replace('menu_submit_', '')
    try:
        ctx = _json.loads(view.get('private_metadata') or '{}')
    except ValueError:
        ctx = {}
    vals = view.get('state', {}).get('values', {})
    get = lambda b: ((vals.get(b) or {}).get('v') or {}).get('value') or ''
    channel, thread_ts = ctx.get('c'), ctx.get('t')
    user_id = (body.get('user') or {}).get('id', '') or ctx.get('u', '')
    if not (channel and thread_ts):
        return
    user_name = get_user_name(client, user_id)

    def say(**kw):
        kw.setdefault('channel', channel)
        return client.chat_postMessage(**kw)

    text = get('text').strip()
    title = get('title').strip()
    label = dict(_MENU_ITEMS)[kind].split(' ', 1)[1]
    shown = (f"*{title}*\n{text}" if title else text)
    say(thread_ts=thread_ts, text=f"<@{user_id}> · {label}:\n>{shown.replace(chr(10), chr(10) + '>')}")
    logger.info("MENU " + _json.dumps({'kind': kind, 'channel': channel, 'user': user_id}))
    try:
        if kind == 'va':
            _start_va_flow(say, client, channel, thread_ts, user_id, user_name, text)
        elif kind == 'lookup':
            reply = build_status_reply(f"Stand für {text}?", _va_lookup)
            say(thread_ts=thread_ts, text=reply or f":thinking_face: Ich konnte „{text}“ keinem Kunden eindeutig "
                "zuordnen. Probier es mit Kundennummer oder Chargebee-Link.")
        elif kind == 'invoices':
            reply = open_invoices_reply(f"Offene Rechnungen für {text}?", _va_lookup)
            say(thread_ts=thread_ts, text=reply or f":thinking_face: Ich konnte „{text}“ keinem Kunden eindeutig "
                "zuordnen. Probier es mit Kundennummer oder Chargebee-Link.")
        elif kind == 'sandbox':
            if detect_mirror_request(text):
                _post_mirror_request(say, client, channel, thread_ts, text)
            else:
                _handle_sandbox_intent(say, client, channel, thread_ts, user_id, user_name, text)
        elif kind == 'improvement':
            _process_request(say, client, channel, thread_ts, user_id, user_name,
                             ts_to_date(thread_ts), title, text)
    except Exception as e:
        logger.exception(f"menu {kind} failed")
        say(thread_ts=thread_ts, text=f":warning: Das hat nicht geklappt: {e}")


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


@app.action("va_select_service_package")
def handle_va_select_service_package(ack, body, say, client):
    """Dropdown-Auswahl: Service-Paket für Vertragsanpassung."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    selected = body.get('actions', [{}])[0].get('selected_option', {}).get('value', '')
    msg_ts = body.get('message', {}).get('ts', '')
    thread_ts = body.get('message', {}).get('thread_ts') or msg_ts
    channel = body.get('channel', {}).get('id', '')
    if not selected or not thread_ts:
        return
    state = _pending_vertragsanpassung.get((channel, thread_ts))
    if not state:
        say(text=f":wave: Service-Paket *{selected}* notiert — kein aktiver VA-Flow mehr. Bitte `#vertragsanpassung` neu triggern.", thread_ts=thread_ts)
        return
    state['parsed']['service_package'] = selected
    # Plan-ID neu auflösen mit Service-Paket
    from vertragsanpassung_handler import resolve_chargebee_plan_id
    if state['parsed'].get('new_plan') and state['parsed'].get('contract_months') and state['parsed'].get('payment_type'):
        pid = resolve_chargebee_plan_id(
            state['parsed']['new_plan'], state['parsed']['contract_months'],
            state['parsed']['payment_type'], service_package=selected,
        )
        if pid:
            state['parsed']['chargebee_plan_id'] = pid
    missing = missing_va_fields(state['parsed'])
    if missing:
        new_blocks = ask_for_va_info_blocks(user_id, missing, state['parsed'], state.get('subscription'))
        try:
            client.chat_update(channel=channel, ts=msg_ts, blocks=new_blocks, text="Weitere Infos benötigt")
        except Exception:
            say(blocks=new_blocks, text="Weitere Infos benötigt", thread_ts=thread_ts)
    else:
        _process_vertragsanpassung(say, client, channel, thread_ts,
                                    state['user_name'], state['parsed'], state.get('subscription'))


@app.action("va_select_plan")
def handle_va_select_plan(ack, body, say, client):
    """Dropdown-Auswahl: Plan + Laufzeit für Vertragsanpassung."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    selected = body.get('actions', [{}])[0].get('selected_option', {}).get('value', '')
    msg_ts = body.get('message', {}).get('ts', '')
    thread_ts = body.get('message', {}).get('thread_ts') or msg_ts
    channel = body.get('channel', {}).get('id', '')
    if not selected or not thread_ts:
        return
    state = _pending_vertragsanpassung.get((channel, thread_ts))
    if not state:
        say(text=":wave: Kein aktiver VA-Flow — bitte `#vertragsanpassung` neu triggern.", thread_ts=thread_ts)
        return
    # Format: "Plan|Monate|Zahlung" z.B. "Pro 25|24|monatlich"
    parts = selected.split('|')
    if len(parts) == 3:
        plan_name, months_str, payment = parts
        state['parsed']['new_plan'] = plan_name.strip()
        state['parsed']['contract_months'] = int(months_str)
        state['parsed']['payment_type'] = payment.strip()
    missing = missing_va_fields(state['parsed'])
    if missing:
        new_blocks = ask_for_va_info_blocks(user_id, missing, state['parsed'], state.get('subscription'))
        try:
            client.chat_update(channel=channel, ts=msg_ts, blocks=new_blocks, text="Weitere Infos benötigt")
        except Exception:
            say(blocks=new_blocks, text="Weitere Infos benötigt", thread_ts=thread_ts)
    else:
        _process_vertragsanpassung(say, client, channel, thread_ts,
                                    state['user_name'], state['parsed'], state.get('subscription'))


def _slack_permalink(client, channel: str | None, ts: str | None) -> str | None:
    if not (channel and ts):
        return None
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=ts)
        return resp.get('permalink') if resp.get('ok') else None
    except Exception as e:
        logger.warning(f"chat_getPermalink failed for {channel}/{ts}: {e}")
        return None


def _create_mirroring_ticket_and_report(say, thread_ts, state, info: dict, from_instance_manager: bool = False):
    """Legt das CCS-Spiegelungs-Ticket an und meldet das Ergebnis im Thread."""
    # Link für das Jira-Ticket: DM-Threads sind für andere nicht sichtbar, daher bevorzugt
    # der Rechnungs-Post in #ask-cs-admin (verlinkt selbst auf den Ursprung), sonst der Ursprungs-Thread.
    ref = state.get('billing_ref') or {}
    slack_link = (_slack_permalink(app.client, ref.get('channel'), ref.get('ts'))
                  or _slack_permalink(app.client, state.get('channel'), thread_ts))
    ticket = create_sandbox_mirroring_ticket(
        state.get('customer_name', ''),
        info['prod_url'],
        info['prod_serial'],
        info['sandbox_url'],
        info['sandbox_serial'],
        slack_link=slack_link,
    )
    if ticket:
        # Abschluss-Check (siehe _check_mirror_tickets): bei kostenpflichtigen Fällen im Thread des
        # Rechnungs-Posts in #ask-cs-admin (Button „Rechnung gestellt“), zusätzlich immer im
        # Ursprungs-Thread (DM oder Channel) mit Tag an die anfragende Person.
        origin = {'channel': state.get('channel'), 'ts': thread_ts, 'user': state.get('user_id')}
        ref = state.get('billing_ref')
        if ref:
            prop = {'mode': 'billing', 'channel': ref['channel'], 'ts': ref['ts'], 'origin': origin}
        elif origin['channel']:
            prop = {'mode': 'sandbox', 'channel': origin['channel'], 'ts': thread_ts,
                    'notify': origin['user']}
        else:
            prop = None
        if prop:
            prop.update({'customer': state.get('customer_name', ''), 'done': False})
            set_issue_property(ticket['key'], MIRROR_PROPERTY, prop)
    if ticket:
        source = ""
        if from_instance_manager:
            source = (
                "\n_Instanz-Infos automatisch aus dem Instance Manager:_\n"
                f"Prod: {info['prod_url']} · {info['prod_serial']}\n"
                f"Sandbox: {info['sandbox_url']} · {info['sandbox_serial']}"
            )
        say(
            text=f":white_check_mark: Jira-Ticket erstellt: <{ticket['url']}|{ticket['key']}>{source}",
            thread_ts=thread_ts,
        )
    else:
        say(
            text=(
                ":warning: Das Jira-Ticket konnte nicht angelegt werden — bitte manuell "
                "im CCS-Projekt in Jira anlegen oder nochmal hier antworten, um es erneut zu versuchen."
            ),
            thread_ts=thread_ts,
        )
    state['step'] = 'done'


def _request_instance_info_or_create_ticket(say, thread_ts, state):
    """Versucht zuerst, alle vier Instanz-Werte aus dem Instance Manager zu holen (CSM-MCP).
    Sind alle vier eindeutig da, wird das CCS-Ticket direkt angelegt. Sonst werden die
    bekannten Werte angezeigt und nur die fehlenden beim CSM erfragt."""
    chargebee_result = state.get('chargebee_result') or {}
    query = chargebee_result.get('debit_number') or state.get('customer_name', '')
    info = fetch_instance_info_from_csm(query)
    required = ('prod_url', 'prod_serial', 'sandbox_url', 'sandbox_serial')
    if all(info.get(k) for k in required):
        _create_mirroring_ticket_and_report(say, thread_ts, state, info, from_instance_manager=True)
        return
    state['instance_info_prefill'] = info
    say(
        blocks=build_sandbox_instance_info_request_blocks(info),
        text="Instanz-Infos für Jira-Ticket benötigt",
        thread_ts=thread_ts,
    )
    state['step'] = 'awaiting_instance_info'


def _continue_sandbox_after_video(say, channel, thread_ts, state, scope: str):
    """Nach dem Zeigen des passenden Loom-Videos (oder direkt, falls kein Video
    nötig war) geht es weiter wie bisher: bei new_plus_mirror Instanz-Infos für
    das Jira-Ticket abfragen, sonst ist der Flow für diesen Scope abgeschlossen."""
    if scope == 'new_plus_mirror':
        _request_instance_info_or_create_ticket(say, thread_ts, state)
    else:
        state['step'] = 'done'


def _notify_cs_admin_billing(say, client, channel, thread_ts, state, scope: str):
    """Benachrichtigt #ask-cs-admin, dass für diese Sandbox/Spiegelung eine Rechnung
    nötig ist — als Thread-Antwort wenn der Flow ohnehin schon dort läuft, sonst als
    neue Cross-Post-Nachricht mit Permalink zurück zum Ursprungs-Thread (DM oder
    anderer Channel), damit CS Admin den vollen Kontext hat."""
    customer_name = state.get('customer_name', '')
    requester_name = state.get('user_name', '')

    if channel == _ASK_CS_ADMIN_CHANNEL:
        resp = say(
            blocks=build_sandbox_admin_billing_blocks(customer_name, scope, requester_name),
            text="Sandbox-Rechnungsstellung nötig",
            thread_ts=thread_ts,
        )
        if resp and resp.get('ts'):
            state['billing_ref'] = {'channel': channel, 'ts': resp['ts']}
        return

    origin_permalink = None
    try:
        resp = client.chat_getPermalink(channel=channel, message_ts=thread_ts)
        if resp.get('ok'):
            origin_permalink = resp.get('permalink')
    except Exception as e:
        logger.warning(f"chat_getPermalink failed for {channel}/{thread_ts}: {e}")

    try:
        resp = client.chat_postMessage(
            channel=_ASK_CS_ADMIN_CHANNEL,
            blocks=build_sandbox_admin_billing_blocks(customer_name, scope, requester_name, origin_permalink),
            text="Sandbox-Rechnungsstellung nötig",
        )
        if resp and resp.get('ts'):
            state['billing_ref'] = {'channel': _ASK_CS_ADMIN_CHANNEL, 'ts': resp['ts']}
    except Exception as e:
        logger.warning(f"Cross-post to #ask-cs-admin failed: {e}")


def _apply_sandbox_scope(say, client, channel, thread_ts, state, scope: str):
    """Postet die Schritt-2-Anleitung für den aufgelösten Scope und leitet je nach
    Fall zum Video/zur Vertragsart-Frage oder direkt zur Instanz-Info-Abfrage weiter.

    `scope` ist bereits aufgelöst (siehe `_handle_sandbox_mirroring_choice`) —
    diese Funktion selbst trifft keine Ja/Nein-Entscheidung mehr, sie war früher
    der Körper der 3-Wege-Scope-Buttons, ist jetzt aber die gemeinsame Anwendungs-
    logik für alle Wege dorthin.
    """
    say(blocks=build_sandbox_step2_stub_blocks(scope), text="Nächste Schritte", thread_ts=thread_ts)

    if sandbox_scope_needs_billing(scope, state.get('variant') or {}):
        _notify_cs_admin_billing(say, client, channel, thread_ts, state, scope)

    if scope in ('new_only', 'new_plus_mirror'):
        # Loom-Anleitung zur Sandbox-Erstellung: kostenlos direkt aus der schon in
        # Schritt 1 berechneten Preis-Variante ableitbar, sonst muss der CSM sagen,
        # ob der Kunde Monats- oder Jahresvertrag gewählt hat (weiß nur er/sie).
        variant = state.get('variant') or {}
        if sandbox_is_free(variant):
            say(
                blocks=build_sandbox_video_blocks(LOOM_SANDBOX_FREE_URL),
                text="Anleitung: Sandbox erstellen (kostenlos)",
                thread_ts=thread_ts,
            )
            _continue_sandbox_after_video(say, channel, thread_ts, state, scope)
        elif sandbox_is_standard_l(variant):
            say(
                blocks=build_sandbox_video_blocks(LOOM_SANDBOX_STANDARD_L_URL),
                text="Anleitung: Sandbox erstellen (Standard L)",
                thread_ts=thread_ts,
            )
            _continue_sandbox_after_video(say, channel, thread_ts, state, scope)
        else:
            say(
                blocks=build_sandbox_contract_type_question_blocks(),
                text="Monats- oder Jahresvertrag?",
                thread_ts=thread_ts,
            )
            state['pending_video_scope'] = scope
            state['step'] = 'awaiting_contract_type'
    elif scope == 'mirror_existing':
        # Instanz-Werte zuerst aus dem Instance Manager (CSM-MCP); nur was dort fehlt,
        # wird beim CSM erfragt (awaiting_instance_info, siehe _advance_sandbox_thread).
        _request_instance_info_or_create_ticket(say, thread_ts, state)


def _handle_sandbox_contract_type(ack, body, say, client, contract_type: str):
    """Gemeinsame Logik für die Monats-/Jahresvertrag-Buttons (bestimmt das Loom-Video)."""
    ack()
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')
    if not thread_ts:
        return
    state = _pending_sandbox.get((channel, thread_ts))
    if not state or state.get('step') != 'awaiting_contract_type':
        say(
            text=":wave: Kein aktiver Sandbox-Flow in diesem Thread mehr — bitte die Anfrage neu stellen.",
            thread_ts=thread_ts,
        )
        return

    loom_url = LOOM_SANDBOX_MONTHLY_URL if contract_type == 'monthly' else LOOM_SANDBOX_ANNUAL_URL
    say(
        blocks=build_sandbox_video_blocks(loom_url),
        text=f"Anleitung: Sandbox erstellen ({contract_type})",
        thread_ts=thread_ts,
    )
    scope = state.pop('pending_video_scope', 'new_only')
    _continue_sandbox_after_video(say, channel, thread_ts, state, scope)


@app.action("sandbox_contract_monthly")
def handle_sandbox_contract_monthly(ack, body, say, client):
    """Button: Kunde hat Monatsvertrag für die Sandbox gewählt."""
    _handle_sandbox_contract_type(ack, body, say, client, 'monthly')


@app.action("sandbox_contract_annual")
def handle_sandbox_contract_annual(ack, body, say, client):
    """Button: Kunde hat Jahresvertrag für die Sandbox gewählt."""
    _handle_sandbox_contract_type(ack, body, say, client, 'annual')


def _handle_sandbox_fit_button(ack, body, say, client, fits: bool):
    """Gemeinsame Logik für die '✅ Passt'/'✏️ Korrektur nötig'-Buttons — Alternative
    zum Freitext-Weg ('ja'/Korrekturtext), beide funktionieren nebeneinander."""
    ack()
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')
    if not thread_ts:
        return
    state = _pending_sandbox.get((channel, thread_ts))
    if not state or state.get('step') != 'awaiting_fit_confirmation':
        say(
            text=":wave: Kein aktiver Sandbox-Flow in diesem Thread mehr — bitte die Anfrage neu stellen.",
            thread_ts=thread_ts,
        )
        return
    if fits:
        _confirm_sandbox_fit(say, thread_ts, state)
    else:
        _request_sandbox_correction(say, thread_ts, state)


@app.action("sandbox_fit_confirmed")
def handle_sandbox_fit_confirmed(ack, body, say, client):
    """Button: '✅ Passt' — E-Mail-Entwurf ist in Ordnung."""
    _handle_sandbox_fit_button(ack, body, say, client, True)


@app.action("sandbox_fit_correction")
def handle_sandbox_fit_correction(ack, body, say, client):
    """Button: '✏️ Korrektur nötig' — CSM muss die Korrektur danach als Text schicken."""
    _handle_sandbox_fit_button(ack, body, say, client, False)


_BILLING_TAKEN = 'sandbox_billing_taken'
_BILLING_READY = 'sandbox_billing_ready'
_BILLING_DONE = 'sandbox_billing_done'


def _billing_done_button(channel: str, post_ts: str) -> dict:
    return {'type': 'actions', 'elements': [{
        'type': 'button', 'style': 'primary', 'action_id': 'sandbox_billing_done',
        'text': {'type': 'plain_text', 'text': '🧾 Rechnung gestellt'},
        'value': _json.dumps({'c': channel, 't': post_ts})}]}


@app.action("sandbox_admin_take_billing")
def handle_sandbox_admin_take_billing(ack, body, say, client):
    """Button: CS Admin übernimmt die Rechnungsstellung für eine Sandbox/Spiegelung.
    Keine automatische Chargebee-Aktion. Statuskette auf dem Rechnungs-Post:
    :status_in_progress: (übernommen) → ✅ erst nach Klick auf „Rechnung gestellt“.
    Mit Spiegelung erscheint der Button erst, wenn das CCS-Ticket abgeschlossen ist
    (siehe _check_mirror_tickets); ohne Spiegelung (nur neue Sandbox) sofort."""
    ack()
    user_id = body.get('user', {}).get('id', '')
    if user_id not in CS_ADMIN_USER_IDS:
        return
    channel = body.get('channel', {}).get('id', '')
    msg = body.get('message', {}) or {}
    own_ts = msg.get('ts')
    thread_ts = msg.get('thread_ts') or own_ts
    raw = ((body.get('actions') or [{}])[0].get('value') or '')
    try:
        scope = (_json.loads(raw) or {}).get('scope', '')
    except ValueError:
        scope = ''  # alte Posts: Wert war nur der Kundenname
    if own_ts:
        _add_reaction(client, channel, own_ts, 'status_in_progress')
        # Button entfernen, damit nicht doppelt übernommen wird
        try:
            client.chat_update(channel=channel, ts=own_ts, text=msg.get('text', ''),
                               blocks=[b for b in (msg.get('blocks') or []) if b.get('type') != 'actions'])
        except Exception as e:
            logger.warning(f"billing post update failed: {e}")
    meta = {'event_type': _BILLING_TAKEN, 'event_payload': {'user': user_id}}
    ready_now = (scope == 'new_only' or not scope
                 or bool(_billing_thread_state(client, channel, thread_ts)['ready_ts']))
    try:
        if ready_now:
            line = (f":status_in_progress: Rechnungsstellung von <@{user_id}> übernommen. "
                    f"Bitte nach dem Stellen der Rechnung bestätigen:")
            client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=line, metadata=meta,
                                    blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': line}},
                                            _billing_done_button(channel, own_ts or thread_ts)])
        else:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, metadata=meta,
                text=(f":status_in_progress: Rechnungsstellung von <@{user_id}> übernommen. Sobald die "
                      f"Spiegelung im CCS-Ticket abgeschlossen ist, melde ich mich hier mit dem Button "
                      f"„Rechnung gestellt“."))
    except Exception as e:
        logger.warning(f"billing take-over reply failed: {e}")
    logger.info("BILLING_TAKEN " + _json.dumps({'channel': channel, 'ts': own_ts, 'user': user_id, 'scope': scope}))


@app.action("sandbox_billing_done")
def handle_sandbox_billing_done(ack, body, client):
    ack()
    user_id = body.get('user', {}).get('id', '')
    if user_id not in CS_ADMIN_USER_IDS:
        return
    try:
        data = _json.loads(((body.get('actions') or [{}])[0].get('value') or '{}'))
    except ValueError:
        data = {}
    channel = data.get('c') or body.get('channel', {}).get('id', '')
    post_ts = data.get('t')
    msg_ts = (body.get('message') or {}).get('ts')
    text = f":white_check_mark: Rechnung gestellt (bestätigt von <@{user_id}>)."
    try:
        client.chat_update(channel=channel, ts=msg_ts, text=text,
                           blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text}}],
                           metadata={'event_type': _BILLING_DONE, 'event_payload': {'user': user_id}})
    except Exception as e:
        logger.warning(f"billing done update failed: {e}")
    if post_ts:
        _remove_reaction(client, channel, post_ts, 'status_in_progress')
        _add_reaction(client, channel, post_ts, 'white_check_mark')
    logger.info("BILLING_DONE " + _json.dumps({'channel': channel, 'ts': post_ts, 'user': user_id}))


def _billing_thread_state(slack_client, channel: str, post_ts: str) -> dict:
    """Liest aus dem Thread des Rechnungs-Posts (Message-Metadata): wer übernommen hat,
    ob der Button „Rechnung gestellt“ schon erschienen ist und ob er geklickt wurde."""
    st = {'taken_by': None, 'ready_ts': None, 'done': False}
    try:
        msgs = slack_client.conversations_replies(channel=channel, ts=post_ts, limit=100,
                                                  include_all_metadata=True).get('messages', [])
    except Exception as e:
        logger.warning(f"billing thread read failed {post_ts}: {e}")
        return st
    for m in msgs:
        md = m.get('metadata') or {}
        et = md.get('event_type')
        if et == _BILLING_TAKEN:
            st['taken_by'] = (md.get('event_payload') or {}).get('user')
        elif et == _BILLING_READY:
            st['ready_ts'] = m.get('ts')
        elif et == _BILLING_DONE:
            st['done'] = True
    return st


@app.action("sandbox_customer_replied")
def handle_sandbox_customer_replied(ack, body, say, client):
    """Button: 'Kunde hat sich zurückgemeldet' — Alternative zur Text-Antwort im
    Thread, springt direkt zur Spiegelungs-Frage (siehe auch der
    'awaiting_customer_reply'-Zweig in _advance_sandbox_thread, den dieser
    Button-Pfad dupliziert)."""
    ack()
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')
    if not thread_ts:
        return
    state = _pending_sandbox.get((channel, thread_ts))
    if not state or state.get('step') != 'awaiting_customer_reply':
        say(
            text=":wave: Kein aktiver Sandbox-Flow in diesem Thread mehr — bitte die Anfrage neu stellen.",
            thread_ts=thread_ts,
        )
        return
    say(
        blocks=build_sandbox_mirroring_question_blocks(state.get('has_existing_sandbox', False)),
        text="Möchte der Kunde eine Spiegelung?",
        thread_ts=thread_ts,
    )
    state['step'] = 'awaiting_scope_choice'


def _handle_sandbox_mirroring_choice(ack, body, say, client, wants_mirroring: bool):
    """Gemeinsame Logik für die Ja/Nein-Spiegelungs-Buttons (Schritt 2).

    Leitet aus dem bereits in Schritt 1 bekannten `has_existing_sandbox` + der
    Ja/Nein-Antwort den finalen Scope ab, statt ihn (wie früher) über 3 separate
    Buttons direkt abzufragen — der CSM muss nicht mehr angeben, ob es um eine
    neue oder bestehende Sandbox geht, das weiß der Bot schon.
    """
    ack()
    thread_ts = body.get('message', {}).get('thread_ts') or body.get('message', {}).get('ts')
    channel = body.get('channel', {}).get('id', '')
    if not thread_ts:
        return
    state = _pending_sandbox.get((channel, thread_ts))
    if not state or state.get('step') != 'awaiting_scope_choice':
        say(
            text=":wave: Kein aktiver Sandbox-Flow in diesem Thread mehr — bitte die Anfrage neu stellen.",
            thread_ts=thread_ts,
        )
        return

    has_existing = state.get('has_existing_sandbox', False)
    if has_existing and wants_mirroring:
        scope = 'mirror_existing'
    elif not has_existing and wants_mirroring:
        scope = 'new_plus_mirror'
    elif not has_existing and not wants_mirroring:
        scope = 'new_only'
    else:
        # has_existing und keine Spiegelung gewünscht -> nichts umzusetzen, nichts zu berechnen.
        say(
            text=(
                ":white_check_mark: Alles klar — der Kunde hat bereits eine Sandbox und möchte "
                "keine Spiegelung. Keine weitere Aktion nötig."
            ),
            thread_ts=thread_ts,
        )
        state['step'] = 'done'
        return

    _apply_sandbox_scope(say, client, channel, thread_ts, state, scope)


@app.action("sandbox_wants_mirroring_yes")
def handle_sandbox_wants_mirroring_yes(ack, body, say, client):
    """Button: Kunde möchte eine Spiegelung (neu oder auf bestehender Sandbox)."""
    _handle_sandbox_mirroring_choice(ack, body, say, client, True)


@app.action("sandbox_wants_mirroring_no")
def handle_sandbox_wants_mirroring_no(ack, body, say, client):
    """Button: Kunde möchte keine Spiegelung."""
    _handle_sandbox_mirroring_choice(ack, body, say, client, False)


@app.action("create_ticket_button")
def handle_create_ticket(ack, body, say):
    ack()


def _handle_chat(text: str, user_id: str, user_name: str, say, thread_ts: str,
                  is_dm: bool = False):
    """Leitet eine Frage an den Chat-Handler weiter und postet die Antwort."""
    # Nur CS Admin Team (im Channel) — in DMs erstmal offen lassen
    if not is_dm and user_id not in CS_ADMIN_USER_IDS:
        say(
            text=(
                f"Hey <@{user_id}> :wave: Die Chat-Funktion ist aktuell nur für das CS Admin Team verfügbar.\n"
                "Für Feature-Requests: `#improvement` | Für Vertragsanpassungen: einfach beschreiben."
            ),
            thread_ts=thread_ts,
        )
        return

    # Frage bereinigen (Bot-Mention entfernen)
    import re
    clean_text = re.sub(r'<@[A-Z0-9]+>', '', text).strip()
    if not clean_text:
        say(text="Was möchtest du wissen? :thinking_face:", thread_ts=thread_ts)
        return

    say(text=":thinking_face: Ich schaue nach...", thread_ts=thread_ts)
    try:
        from chat_handler import answer
        response = answer(clean_text, user_name=user_name)
        say(text=response, thread_ts=thread_ts)
    except Exception as e:
        logger.warning(f"Chat failed: {e}")
        say(text=f":warning: Fehler: {e}", thread_ts=thread_ts)


@app.event("app_mention")
def handle_app_mention(event, say, client):
    """@CS Admin Bot Erwähnung — Chat oder Hilfe-Menü."""
    raw_text = (event.get('text') or '')
    text_lower = raw_text.lower()
    thread_ts = event.get('thread_ts') or event.get('ts')
    user_id = event.get('user', '')

    if event.get('bot_id'):
        return
    # Stummgeschaltete Threads und Bot-Befehle (#bot-stop, …) gehören dem Message-Handler —
    # live wurde ein #bot-stop mit @-Erwähnung sonst als Chat-Frage beantwortet.
    if (event.get('channel'), thread_ts) in _muted_threads or _BOT_COMMAND_RE.search(raw_text):
        return
    # Improvement/VA → Message-Handler übernimmt
    if any(kw in text_lower for kw in ('#improvement', '#vertragsanpassung')):
        return

    # Status-Frage per @Bot (auch in Threads): Fakten aus Chargebee
    _status_text = normalize_slack_text(raw_text)
    if detect_open_invoice_question(_status_text):
        try:
            reply = open_invoices_reply(_status_text, _va_lookup)
        except Exception as e:
            logger.warning(f"Open invoices answer (mention) failed: {e}")
            reply = None
        if reply:
            say(text=reply, thread_ts=thread_ts)
            _status_answered[(event.get('channel'), thread_ts)] = time.time()
            logger.info("STATUS_ANSWER " + _json.dumps({'channel': event.get('channel'), 'ts': thread_ts,
                                                        'via': 'mention', 'kind': 'open_invoices'}))
            return
    if detect_status_question(_status_text) or extract_invoice_ids(_status_text):
        try:
            reply = build_status_reply(_status_text, _va_lookup)
        except Exception as e:
            logger.warning(f"Status answer (mention) failed: {e}")
            reply = None
        if reply:
            say(text=reply, thread_ts=thread_ts)
            _status_answered[(event.get('channel'), thread_ts)] = time.time()
            logger.info("STATUS_ANSWER " + _json.dumps({'channel': event.get('channel'), 'ts': thread_ts,
                                                        'via': 'mention', 'invoices': extract_invoice_ids(_status_text)}))
            return

    user_name = get_user_name(client, user_id)

    # Bot-Mention aus Text entfernen um den Kern zu prüfen
    import re as _re
    clean_mention = _re.sub(r'<@[A-Z0-9]+>', '', raw_text).strip()

    clean_lower = clean_mention.lower()

    _SIMPLE_GREETINGS = {'hallo', 'hi', 'hey', 'hello', 'servus', 'moin',
                         'guten tag', 'guten morgen', 'guten abend', '?', ''}
    _CAPABILITY_KEYWORDS = ('was kannst', 'was kann', 'funktionen', 'befehle',
                            'features', 'hilfe', 'help', 'wie funktioniert',
                            'was gibt', 'übersicht', 'commands', 'was machst',
                            'was bist', 'erkläre dich')

    _is_simple_greeting = clean_lower in _SIMPLE_GREETINGS or len(clean_mention) <= 3
    _is_capability_question = any(kw in clean_lower for kw in _CAPABILITY_KEYWORDS)

    def _build_help_blocks(full: bool):
        blocks = [
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': f":robot_face: Hey <@{user_id}>! Ich bin der *CS Admin Bot* — hier ist was ich kann:",
                },
            },
            {'type': 'divider'},
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (
                        '*:ticket: Feature-Requests*\n'
                        'Schreib `#improvement` + Titel und Beschreibung im Channel.\n'
                        'Ich suche ähnliche Jira-Tickets und erstelle bei Bedarf automatisch ein neues.\n'
                        'Andere können dann eine Ticket-Nummer (z.B. `CS-123`) in den Thread schreiben — ich vote automatisch.'
                    ),
                },
            },
            {
                'type': 'section',
                'text': {
                    'type': 'mrkdwn',
                    'text': (
                        '*:page_facing_up: Vertragsanpassungen*\n'
                        "Beschreib die Änderung direkt im Channel (z.B. _'Bitte für Firma GmbH auf Business 25 Jahresvertrag upgraden'_).\n"
                        'Ich erkenne das automatisch, schlage die Chargebee-Subscription auf und lege die Ramp direkt an — nach eurer Freigabe.'
                    ),
                },
            },
        ]
        if full:
            blocks += [
                {
                    'type': 'section',
                    'text': {
                        'type': 'mrkdwn',
                        'text': (
                            '*:mag: Chargebee-Fragen* _(CS Admin)_\n'
                            '`@CS Admin Bot Welchen Plan hat Firma GmbH?`\n'
                            '`@CS Admin Bot Zeig mir die Subscription von Firma GmbH`'
                        ),
                    },
                },
                {'type': 'divider'},
                {
                    'type': 'section',
                    'text': {
                        'type': 'mrkdwn',
                        'text': (
                            '*:wrench: Admin-Befehle* _(nur CS Admin Team)_\n'
                            '`#bot-remove` — Bot-Reaktionen vom Root-Post entfernen\n'
                            '`#bot-remove CS-123` — Bot-Upvote von Ticket entfernen\n'
                            '`#bot-remove delete CS-123` — Ticket löschen\n'
                            '`#bot-stop` — Thread stummschalten + Ticket löschen\n'
                            '`#vertragsanpassung` — VA-Flow manuell starten (im Thread)'
                        ),
                    },
                },
            ]
        return blocks

    # Einfacher Gruß oder bloße Erwähnung → Menü mit Buttons
    if _is_simple_greeting:
        say(
            blocks=_menu_blocks(user_id, event.get('channel', ''), thread_ts),
            text=f"Hey {user_name}! Wobei kann ich helfen?",
            thread_ts=thread_ts,
        )
        return

    # Capability-Frage → vollständige Übersicht
    if _is_capability_question:
        say(
            blocks=_build_help_blocks(full=True),
            text="CS Admin Bot Übersicht",
            thread_ts=thread_ts,
        )
        return

    # CS Admin mit echter Frage: Chat nutzen
    if user_id in CS_ADMIN_USER_IDS:
        _handle_chat(raw_text, user_id, user_name, say, thread_ts, is_dm=False)
        return

    # Alle anderen ohne erkennbares Keyword: kurze Hilfe
    say(
        blocks=_build_help_blocks(full=True),
        text="CS Admin Bot Übersicht",
        thread_ts=thread_ts,
    )



# DM-Handler ist jetzt in _handle_message_core integriert (channel_type == 'im' Check)


@app.event("reaction_added")
def handle_reaction_added(event, say, client):
    """Verarbeitet Reaktionen auf Nachrichten im CS Admin Channel.

    - 👀 / ✅ von Nicht-Admins → benachrichtigt CS Admin Team im Thread
    - 🆘 (sos) auf Bot-Nachrichten → Fehler-Feedback sammeln und loggen
    """
    reaction = event.get('reaction', '')
    user_id = event.get('user', '')
    if not user_id:
        return

    # --- OPOS-Sperrprüfung: an ausgelagerten Service weiterleiten (siehe Kommentar
    # bei _handle_message_core) ---
    item = event.get('item', {})
    if item.get('type') == 'message' and item.get('channel') == OPOS_CHANNEL_ID:
        _forward_to_opos_service('reaction_added', event)
        return

    # Bot-eigene Reaktionen ignorieren
    try:
        info = client.users_info(user=user_id)
        if info['user'].get('is_bot') or info['user'].get('is_app_user'):
            return
    except Exception:
        pass

    item = event.get('item', {})
    if item.get('type') != 'message':
        return

    channel = item.get('channel', '')
    ts = item.get('ts', '')
    if not ts:
        return

    # --- Feedback-Emoji: 🆘 auf Bot-Nachrichten → Fehler melden ---
    if reaction == FEEDBACK_EMOJI:
        user_name = get_user_name(client, user_id)
        logger.info(f"FEEDBACK: :{FEEDBACK_EMOJI}: by {user_name} ({user_id}) on {channel}/{ts}")

        # Originaltext der betroffenen Nachricht laden
        msg_text = ''
        msg_user = ''
        try:
            hist = client.conversations_history(channel=channel, latest=ts, limit=1, inclusive=True)
            msgs = hist.get('messages', [])
            if msgs:
                msg_text = msgs[0].get('text', '')[:300]
                msg_user = msgs[0].get('user', '')
        except Exception:
            pass

        # Link zur Nachricht
        msg_link = slack_message_link(channel, ts)
        admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)

        # Feedback im Thread bestätigen
        say(
            text=f":sos: Danke für das Feedback! Wir schauen uns das an, <@{user_id}>.",
            thread_ts=ts,
        )

        # Strukturierten Bericht in Feedback-Channel posten (falls konfiguriert)
        report = (
            f":sos: *Bot-Fehler gemeldet* von <@{user_id}> ({user_name})\n"
            f"• *Nachricht:* <{msg_link}|Link>\n"
            f"• *Bot-Antwort:* {msg_text[:200] if msg_text else '(nicht geladen)'}\n"
            f"• *Zeitpunkt:* <!date^{int(float(ts))}^{{date_short}} {{time}}|{ts}>\n"
            f"{admin_mentions}"
        )
        if FEEDBACK_CHANNEL_ID:
            try:
                client.chat_postMessage(channel=FEEDBACK_CHANNEL_ID, text=report)
                logger.info(f"Feedback report posted to {FEEDBACK_CHANNEL_ID}")
            except Exception as e:
                logger.warning(f"Could not post to feedback channel: {e}")
        else:
            # Kein Channel konfiguriert → nur im selben Thread loggen
            logger.warning(f"FEEDBACK_CHANNEL_ID not set — feedback only in log. Report: {report}")
        return

    # --- 👀 / ✅ von Nicht-Admins → CS Admin Team benachrichtigen ---
    if reaction not in ('eyes', 'white_check_mark'):
        return
    if user_id in CS_ADMIN_USER_IDS:
        return
    if channel not in SLACK_CHANNEL_IDS:
        return

    user_name = get_user_name(client, user_id)
    admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)
    emoji = '👀' if reaction == 'eyes' else '✅'

    logger.info(f"Reaction {reaction!r} by {user_name} ({user_id}) on {channel}/{ts}")

    # Prüfen ob bereits ein CS Admin mit 👀 reagiert hat
    admin_already_watching = False
    try:
        rx = client.reactions_get(channel=channel, timestamp=ts, full=True)
        msg_reactions = rx.get('message', {}).get('reactions', [])
        for r in msg_reactions:
            if r.get('name') == 'eyes':
                if any(u in CS_ADMIN_USER_IDS for u in r.get('users', [])):
                    admin_already_watching = True
                    break
    except Exception:
        pass

    if admin_already_watching:
        # CS Admin hat bereits 👀 gesetzt — kein Alert nötig
        return

    # 🚨 auf die Nachricht setzen damit CS Admin es direkt sieht
    try:
        client.reactions_add(channel=channel, name='rotating_light', timestamp=ts)
    except Exception:
        pass

    # Explizit mit channel= posten (say() kennt den Channel in reaction_added nicht immer)
    try:
        client.chat_postMessage(
            channel=channel,
            thread_ts=ts,
            text=(
                f":rotating_light: {admin_mentions} — <@{user_id}> hat mit {emoji} reagiert, "
                f"aber noch niemand aus dem CS Admin Team hat mit 👀 bestätigt.\n"
                "Bitte prüfen und ggf. übernehmen."
            ),
        )
    except Exception as e:
        logger.warning(f"reaction_added notify failed: {e}")


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Daily reminder: unanswered questions in #ask-cs-admin
# ---------------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _BERLIN_TZ = _ZoneInfo('Europe/Berlin')
except ImportError:
    _BERLIN_TZ = timezone(timedelta(hours=2))

_ASK_CS_ADMIN_CHANNEL = 'C06MK1QGMTR'
_REMINDER_TAG_IDS = {'U07TRKK8BH9', 'U07G83YH6RW'}  # Sara, Mirjam
_REMINDER_MARKER = 'noch-offen-reminder'


def _check_unanswered_questions(slack_client) -> None:
    now = datetime.now(tz=_BERLIN_TZ)
    # On Mondays look back 72h to catch Friday messages
    lookback_hours = 72 if now.weekday() == 0 else 48
    oldest = (now - timedelta(hours=lookback_hours)).timestamp()

    try:
        history = slack_client.conversations_history(
            channel=_ASK_CS_ADMIN_CHANNEL,
            oldest=str(oldest),
            limit=50,
        )
    except Exception as e:
        logger.warning(f"ask-cs-admin history fetch failed: {e}")
        return

    for msg in history.get('messages', []):
        if msg.get('bot_id') or msg.get('subtype'):
            continue
        ts = msg.get('ts', '')
        if not ts or not msg.get('user'):
            continue
        # Keine Reminder auf eigene CS-Admin-Posts (Notizen, geplante Reminder) und auf
        # Ankündigungen an alle (@here/@channel) — beides sind keine offenen Anfragen.
        if msg.get('user') in CS_ADMIN_USER_IDS:
            continue
        if re.search(r'<!(?:here|channel|everyone)', msg.get('text') or ''):
            continue

        # Skip if already marked done via ✅ reaction
        reactions = {r.get('name') for r in msg.get('reactions', [])}
        if reactions & {'white_check_mark', 'heavy_check_mark', 'status_in_progress'}:
            continue

        # Check thread for CS admin reply or existing reminder from this bot
        already_reminded = False
        has_admin_reply = False
        if msg.get('reply_count', 0) > 0:
            try:
                replies = slack_client.conversations_replies(
                    channel=_ASK_CS_ADMIN_CHANNEL, ts=ts, limit=30, include_all_metadata=True,
                )
                for reply in replies.get('messages', [])[1:]:
                    if reply.get('user') in CS_ADMIN_USER_IDS:
                        has_admin_reply = True
                        break
                    if (_REMINDER_MARKER in (reply.get('text') or '')
                            or (reply.get('metadata') or {}).get('event_type') == _REMINDER_MARKER.replace('-', '_')):
                        already_reminded = True
                        break
            except Exception as e:
                logger.warning(f"Thread fetch for {ts} failed: {e}")
                continue

        if has_admin_reply or already_reminded:
            continue

        mentions = ' '.join(f'<@{uid}>' for uid in sorted(_REMINDER_TAG_IDS))
        try:
            slack_client.chat_postMessage(
                channel=_ASK_CS_ADMIN_CHANNEL,
                thread_ts=ts,
                text=(
                    f":wave: {mentions} — diese Anfrage ist noch offen und wurde bisher nicht beantwortet."
                ),
                # Unsichtbare Markierung (früher als <!-- … --> im Text, das zeigte Slack sichtbar an)
                metadata={'event_type': _REMINDER_MARKER.replace('-', '_'), 'event_payload': {'ts': ts}},
            )
            logger.info(f"Unanswered reminder posted for {ts}")
        except Exception as e:
            logger.warning(f"Reminder post failed for {ts}: {e}")


_MIRROR_DONE_RE = re.compile(
    r'mirror\w*\s+(?:is\s+|has\s+been\s+|was\s+)?complet|'
    r'(?:data\s+)?sync\w*\s+(?:has\s+been\s+|is\s+)?complet|spiegelung\s+(?:ist\s+)?abgeschlossen',
    re.IGNORECASE,
)


def _check_mirror_tickets(slack_client) -> None:
    """Meldet abgeschlossene Spiegelungen im Slack-Thread: Kommentar "Mirroring complete" (o. ä.)
    oder Status-Kategorie "done". Dann ✅ statt :status_in_progress: auf der Anfrage und die Person
    taggen, die das Ticket per Button angelegt hat. Merkt sich die Meldung in der Issue-Property."""
    for t in open_bot_mirror_tickets():
        prop = get_issue_property(t['key'], MIRROR_PROPERTY)
        if not prop or prop.get('done') or not prop.get('channel') or not prop.get('ts'):
            continue
        hit = next((c for c in t['comments'] if _MIRROR_DONE_RE.search(c['body'] or '')), None)
        if not hit and t['status_category'] != 'done':
            continue
        channel, ts = prop['channel'], prop['ts']
        url = f"{JIRA_SERVER_URL.rstrip('/')}/browse/{t['key']}"
        detail = (f"„{hit['body'].strip().splitlines()[0][:120]}“ ({hit['author']})" if hit
                  else f"Status: {t['status']}")
        if prop.get('mode') == 'billing':
            st = _billing_thread_state(slack_client, channel, ts)
            if st['done']:
                prop['done'] = True
                set_issue_property(t['key'], MIRROR_PROPERTY, prop)
                continue
            who = f"<@{st['taken_by']}>" if st['taken_by'] else ' '.join(
                f'<@{u}>' for u in sorted(_REMINDER_TAG_IDS))
            line = (f":white_check_mark: {who} die Spiegelung für {prop.get('customer') or 'den Kunden'} "
                    f"ist abgeschlossen: <{url}|{t['key']}> {detail}. Bitte jetzt die Rechnung stellen "
                    f"und hier bestätigen:")
            try:
                slack_client.chat_postMessage(
                    channel=channel, thread_ts=ts, text=line,
                    blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': line}},
                            _billing_done_button(channel, ts)],
                    metadata={'event_type': _BILLING_READY, 'event_payload': {'key': t['key']}})
            except Exception as e:
                logger.warning(f"billing ready post failed for {t['key']}: {e}")
                continue
            org = prop.get('origin') or {}
            if org.get('channel') and org.get('ts') and (org['channel'], org['ts']) != (channel, ts):
                try:
                    slack_client.chat_postMessage(
                        channel=org['channel'], thread_ts=org['ts'],
                        text=(f":white_check_mark: {'<@' + org['user'] + '> ' if org.get('user') else ''}"
                              f"die Spiegelung für {prop.get('customer') or 'den Kunden'} ist abgeschlossen: "
                              f"<{url}|{t['key']}>. Du kannst den Kunden jetzt informieren."))
                except Exception as e:
                    logger.warning(f"origin notify failed for {t['key']}: {e}")
            prop['done'] = True
            set_issue_property(t['key'], MIRROR_PROPERTY, prop)
            logger.info("BILLING_READY " + _json.dumps({'key': t['key'], 'channel': channel, 'ts': ts}))
            continue
        who = f"<@{prop['notify']}> " if prop.get('notify') else ''
        try:
            slack_client.chat_postMessage(
                channel=channel, thread_ts=ts,
                text=(f":white_check_mark: {who}die Spiegelung für {prop.get('customer') or 'den Kunden'} "
                      f"ist abgeschlossen: <{url}|{t['key']}> {detail}. "
                      + ("Du kannst den Kunden jetzt informieren." if prop.get('mode') == 'sandbox'
                         else "Der Kunde kann jetzt informiert werden.")),
            )
        except Exception as e:
            logger.warning(f"mirror done post failed for {t['key']}: {e}")
            continue
        _remove_reaction(slack_client, channel, ts, 'status_in_progress')
        _add_reaction(slack_client, channel, ts, 'white_check_mark')
        prop['done'] = True
        set_issue_property(t['key'], MIRROR_PROPERTY, prop)
        logger.info("MIRROR_DONE " + _json.dumps({'key': t['key'], 'channel': channel, 'ts': ts}))


def _remind_open_billing(slack_client) -> None:
    """Werktags 8 Uhr: Rechnungs-Posts der letzten 30 Tage, die übernommen (:status_in_progress:)
    und abrechenbar sind (ohne Spiegelung sofort, mit Spiegelung nach „abgeschlossen“), aber noch
    ohne „Rechnung gestellt“ — die übernehmende Person einmal täglich im Thread erinnern."""
    oldest = (datetime.now(tz=_BERLIN_TZ) - timedelta(days=30)).timestamp()
    try:
        hist = slack_client.conversations_history(channel=_ASK_CS_ADMIN_CHANNEL, oldest=str(oldest), limit=200)
    except Exception as e:
        logger.warning(f"billing reminder history failed: {e}")
        return
    for m in hist.get('messages', []):
        if 'Sandbox-Rechnungsstellung nötig' not in (m.get('text') or ''):
            continue
        reactions = {r.get('name') for r in m.get('reactions', [])}
        if 'status_in_progress' not in reactions or reactions & {'white_check_mark', 'heavy_check_mark'}:
            continue
        st = _billing_thread_state(slack_client, _ASK_CS_ADMIN_CHANNEL, m['ts'])
        if st['done'] or not st['taken_by']:
            continue
        no_mirror = 'Nur neue Sandbox anlegen' in (m.get('text') or '')
        if not (no_mirror or st['ready_ts']):
            continue  # Spiegelung läuft noch, Rechnung erst danach
        try:
            slack_client.chat_postMessage(
                channel=_ASK_CS_ADMIN_CHANNEL, thread_ts=m['ts'],
                text=(f":receipt: <@{st['taken_by']}> Erinnerung: Für diese Sandbox ist die Rechnung noch "
                      f"nicht als gestellt bestätigt. Bitte stellen und oben auf „Rechnung gestellt“ klicken."))
        except Exception as e:
            logger.warning(f"billing reminder post failed: {e}")


def _mirror_check_loop() -> None:
    """Alle 30 Minuten werktags 7–19 Uhr die offenen Spiegelungs-Tickets prüfen."""
    while True:
        try:
            now = datetime.now(tz=_BERLIN_TZ)
            if now.weekday() < 5 and 7 <= now.hour < 19:
                _check_mirror_tickets(app.client)
        except Exception as e:
            logger.warning(f"Mirror check loop error: {e}")
        time.sleep(1800)


def _daily_reminder_loop() -> None:
    last_run_date = None
    while True:
        try:
            now = datetime.now(tz=_BERLIN_TZ)
            is_weekday = now.weekday() < 5
            is_8am = now.hour == 8 and now.minute < 5
            today = now.date()
            if is_weekday and is_8am and last_run_date != today:
                last_run_date = today
                _check_unanswered_questions(app.client)
                _remind_open_billing(app.client)
        except Exception as e:
            logger.warning(f"Daily reminder loop error: {e}")
        time.sleep(60)


# Flask routes
# ---------------------------------------------------------------------------

@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@flask_app.route("/productive-sync", methods=["POST"])
def productive_sync_endpoint():
    from productive_sync import run_productive_sync
    import json
    try:
        summary = run_productive_sync()
        return json.dumps(summary), 200, {'Content-Type': 'application/json'}
    except Exception as e:
        logger.exception("Productive sync failed")
        return json.dumps({'error': str(e)}), 500, {'Content-Type': 'application/json'}


# OPOS-Sperrprüfung läuft seit 2026-08-18 als eigener Service/Repo
# (opos-sperrpruefung-bot) — dieser Bot leitet nur noch die relevanten
# Slack-Events dorthin weiter, siehe _forward_to_opos_service oben.


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting CS Improvement Bot on port {port}...")
    threading.Thread(target=_daily_reminder_loop, daemon=True, name="daily-reminder").start()
    threading.Thread(target=_mirror_check_loop, daemon=True, name="mirror-check").start()
    flask_app.run(host="0.0.0.0", port=port)
