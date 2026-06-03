import logging
import os
import re
import time
from datetime import datetime, timezone

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
VA_DONE_EMOJI = os.environ.get('VA_DONE_EMOJI', 'cs-admin-bot')

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
    expired = [
        key for key, state in list(_pending.items())
        if now - state.get('created_at', now) > PENDING_TTL
    ]
    for key in expired:
        _pending.pop(key, None)
        channel, thread_ts = key
        logger.info(f"Pending state expired for {channel}/{thread_ts} — marking done")
        _set_done(client, channel, thread_ts)


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
        for key, value in offer_data.items():
            if value and not parsed.get(key):  # nur fehlende Felder ergänzen
                parsed[key] = value
                logger.info(f"Offer enrichment: {key}={value!r}")
    except Exception as e:
        logger.warning(f"Offer enrichment failed: {e}")
    return parsed


def _cb_lookup(customer_name: str) -> dict | None:
    """Chargebee-Lookup per Kundenname (exakter Company-Match)."""
    if not customer_name or not CHARGEBEE_API_KEY:
        return None
    return lookup_chargebee_subscription(
        customer_name, CHARGEBEE_API_KEY, CHARGEBEE_SITE, planhat_token='',
    )


def _process_vertragsanpassung(say, client, channel: str, thread_ts: str,
                                user_name: str, parsed: dict,
                                subscription: dict | None = None):
    """Alle Felder vollständig — entweder Zusammenfassung oder CS-Admin-Warnung."""
    if subscription is None:
        subscription = _cb_lookup(parsed.get('customer_name', ''))

    # Mehrere Subscriptions → CS Admin fragen, noch keine Zusammenfassung
    if subscription and subscription.get('multiple_links'):
        logger.info("VA: Mehrere Subscriptions — warte auf CS Admin Bestätigung")
        say(
            blocks=build_cs_admin_subscription_blocks(subscription),
            text="Mehrere Subscriptions gefunden — bitte CS Admin bestätigen",
            thread_ts=thread_ts,
        )
        # State bleibt aktiv damit CS Admin antworten kann
        return

    # Eindeutige Subscription (oder keine) → direkt Zusammenfassung
    blocks = build_va_summary_blocks(parsed, subscription, user_name)
    # CS Admin Team in der Zusammenfassung erwähnen
    admin_mentions = ' '.join(f'<@{uid}>' for uid in CS_ADMIN_USER_IDS)
    blocks.append({
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': (
                f":{VA_DONE_EMOJI}: {admin_mentions} — bitte prüfen und Go geben!"
            ),
        },
    })
    say(blocks=blocks, text="📋 Vertragsanpassung — Zusammenfassung", thread_ts=thread_ts)
    # Reaktion auf Root-Nachricht (👀 → custom emoji)
    _remove_reaction(client, channel, thread_ts, 'eyes')
    _add_reaction(client, channel, thread_ts, VA_DONE_EMOJI)
    _pending_vertragsanpassung.pop((channel, thread_ts), None)


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
            subscription = _cb_lookup(parsed.get('customer_name', ''))
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
                _process_vertragsanpassung(say, client, channel, thread_ts, user_name, parsed, subscription)
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
        subscription = _cb_lookup(parsed.get('customer_name', ''))
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
            _process_vertragsanpassung(say, client, channel, ts, user_name, parsed, subscription)
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


@app.action("create_ticket_button")
def handle_create_ticket(ack, body, say):
    ack()


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
