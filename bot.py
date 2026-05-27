import logging
import os
import re

from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from config import (
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_SIGNING_SECRET,
)
from jira_handler import create_ticket, search_similar_tickets
from slack_utils import format_error, format_ticket_created

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# (channel, thread_ts) -> {'user_id': str, 'title': str|None, 'use_case': str|None}
_pending: dict[tuple[str, str], dict] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_request(text: str) -> tuple[str | None, str | None]:
    """Extract title and use case from a message.

    Supports explicit labels (Titel:, Use Case:, Beschreibung:, Problem:)
    or falls back to first-line = title, rest = use case.
    """
    clean = re.sub(r'#improvement-request', '', text, flags=re.IGNORECASE).strip()
    # Remove Slack user mentions for parsing
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

    # Fallback: first non-empty line = title, rest = use case
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


def found_ticket_blocks(tickets: list[dict]) -> list[dict]:
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
                ":point_up: Bitte schau dir die Tickets an.\n"
                "Wenn dein Request bereits abgedeckt ist, kannst du das Ticket "
                "*upvoten* — füge einfach einen :thumbsup: Reaktion hinzu "
                "oder hinterlasse einen Kommentar mit deinem Use Case."
            ),
        },
    })
    return blocks


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------

@app.event("message")
def handle_message(event, say, client):
    if event.get('bot_id'):
        return
    if event.get('channel') != SLACK_CHANNEL_ID:
        return
    if event.get('subtype'):
        return

    text = event.get('text', '') or ''
    channel = event.get('channel')
    ts = event.get('ts')
    thread_ts = event.get('thread_ts')
    user_id = event.get('user')

    logger.info(f"Message: channel={channel}, thread={thread_ts}, user={user_id}")

    # -----------------------------------------------------------------------
    # THREAD REPLY — bot is waiting for more info from this user
    # -----------------------------------------------------------------------
    if thread_ts:
        state = _pending.get((channel, thread_ts))
        if not state or state.get('user_id') != user_id:
            return  # not our thread or wrong user

        # Try to parse explicit labels first
        title_parsed, uc_parsed = parse_request(text)

        # Fill in what's still missing — treat full reply as the missing field
        if not state.get('title'):
            state['title'] = title_parsed or text.split('\n')[0].strip()
        if not state.get('use_case'):
            # If title was just set from first line and there's more text, use rest as use_case
            state['use_case'] = uc_parsed or text.strip()

        still_missing = missing_info(state.get('title'), state.get('use_case'))

        if still_missing:
            say(
                blocks=ask_for_info_blocks(user_id, still_missing),
                text="Bitte ergänze die fehlenden Infos",
                thread_ts=thread_ts,
            )
            return

        # All info available — search Jira
        _pending.pop((channel, thread_ts), None)
        _process_request(
            say=say,
            channel=channel,
            thread_ts=thread_ts,
            user_id=user_id,
            title=state['title'],
            use_case=state['use_case'],
        )
        return

    # -----------------------------------------------------------------------
    # NEW MESSAGE — only react to #improvement-request
    # -----------------------------------------------------------------------
    if '#improvement-request' not in text.lower():
        return

    title, use_case = parse_request(text)
    still_missing = missing_info(title, use_case)

    if still_missing:
        # Ask for missing info and store pending state
        _pending[(channel, ts)] = {
            'user_id': user_id,
            'title': title,
            'use_case': use_case,
        }
        say(
            blocks=ask_for_info_blocks(user_id, still_missing),
            text="Fehlende Informationen",
            thread_ts=ts,
        )
        return

    # All info present immediately
    _process_request(
        say=say,
        channel=channel,
        thread_ts=ts,
        user_id=user_id,
        title=title,
        use_case=use_case,
    )


def _process_request(say, channel, thread_ts, user_id, title, use_case):
    """Search Jira CS board and respond appropriately."""
    try:
        similar = search_similar_tickets(f"{title} {use_case}")

        if similar:
            say(
                blocks=found_ticket_blocks(similar),
                text="Ähnliche Tickets gefunden",
                thread_ts=thread_ts,
            )
        else:
            # Create new ticket
            ticket = create_ticket(
                slack_user_id=user_id,
                original_text=use_case,
                summary=title,
            )
            say(
                blocks=format_ticket_created(ticket),
                text=f"Ticket {ticket['key']} erstellt",
                thread_ts=thread_ts,
            )
    except Exception as e:
        logger.exception("Error processing request")
        say(
            blocks=format_error(f"Fehler: {str(e)}"),
            text="Fehler",
            thread_ts=thread_ts,
        )


@app.action("create_ticket_button")
def handle_create_ticket(ack, body, say):
    ack()
    # kept for backwards compatibility — no longer used in new flow
    say(
        text="Bitte nutze den neuen Flow: Schreib #improvement-request mit Titel und Use Case.",
        thread_ts=body.get('message', {}).get('ts'),
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
