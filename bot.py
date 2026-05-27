import logging
import os

from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler

from config import (
    SLACK_BOT_TOKEN,
    SLACK_CHANNEL_ID,
    SLACK_SIGNING_SECRET,
)
from jira_handler import create_ticket, search_similar_tickets
from slack_utils import (
    extract_user_info,
    format_error,
    format_search_results,
    format_ticket_created,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = App(token=SLACK_BOT_TOKEN, signing_secret=SLACK_SIGNING_SECRET)
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

_message_store: dict[tuple[str, str], str] = {}


@app.event("message")
def handle_message(event, say):
    logger.info(f"Message received: channel={event.get('channel')}, subtype={event.get('subtype')}")

    if event.get('bot_id'):
        return
    if event.get('channel') != SLACK_CHANNEL_ID:
        logger.info(f"Ignoring: channel {event.get('channel')} != {SLACK_CHANNEL_ID}")
        return
    if event.get('subtype'):
        return

    user_info = extract_user_info(event)
    text = user_info['text']
    if not text:
        return

    key = (user_info['channel'], user_info['ts'])
    _message_store[key] = text

    try:
        similar_tickets = search_similar_tickets(text)
        blocks = format_search_results(similar_tickets, user_info)
        say(blocks=blocks, text="Jira ticket search results", thread_ts=user_info['ts'])
    except Exception as e:
        logger.exception("Error handling message")
        say(blocks=format_error(f"An error occurred: {str(e)}"),
            text="Error", thread_ts=user_info['ts'])


@app.action("create_ticket_button")
def handle_create_ticket(ack, body, say):
    ack()
    slack_user_id = body['user']['id']
    message = body.get('message', {})
    channel = body.get('channel', {}).get('id', '')
    thread_ts = message.get('thread_ts') or message.get('ts', '')
    original_text = _message_store.get((channel, thread_ts), '')

    try:
        ticket = create_ticket(slack_user_id=slack_user_id, original_text=original_text)
        say(blocks=format_ticket_created(ticket), text=f"Ticket {ticket['key']} created",
            thread_ts=thread_ts)
        _message_store.pop((channel, thread_ts), None)
    except Exception as e:
        logger.exception("Error creating ticket")
        say(blocks=format_error(f"Failed to create ticket: {str(e)}"),
            text="Error creating ticket", thread_ts=thread_ts)


@flask_app.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)


@flask_app.route("/health", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Starting CS Improvement Bot (HTTP mode) on port {port}...")
    flask_app.run(host="0.0.0.0", port=port)
