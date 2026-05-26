def extract_user_info(message):
    """Extract relevant fields from a raw Slack message event."""
    return {
        'user_id': message.get('user'),
        'text': message.get('text', ''),
        'channel': message.get('channel'),
        'ts': message.get('ts'),
    }


def format_search_results(tickets, user_info):
    """Build Slack Block Kit payload for Jira search results."""
    if not tickets:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":mag: No similar tickets found.\n"
                        "Would you like to create a new one?"
                    ),
                },
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Create Ticket"},
                        "style": "primary",
                        "action_id": "create_ticket_button",
                        "value": user_info.get('user_id', ''),
                    }
                ],
            },
        ]

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":mag: Found *{len(tickets)}* similar ticket(s):",
            },
        }
    ]

    for ticket in tickets:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{ticket['url']}|{ticket['key']}>*: {ticket['summary']}\n"
                    f"Status: `{ticket['status']}` | Assignee: {ticket['assignee']}"
                ),
            },
        })

    blocks.append({"type": "divider"})
    blocks.append({
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": "None of these match? You can open a new ticket:",
        },
    })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Create New Ticket"},
                "style": "primary",
                "action_id": "create_ticket_button",
                "value": user_info.get('user_id', ''),
            }
        ],
    })

    return blocks


def format_ticket_created(ticket):
    """Build Slack Block Kit payload confirming ticket creation."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f":white_check_mark: Ticket created successfully!\n"
                    f"*<{ticket['url']}|{ticket['key']}>*: {ticket['summary']}"
                ),
            },
        }
    ]


def format_error(message):
    """Build a simple error block."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":x: {message}",
            },
        }
    ]
