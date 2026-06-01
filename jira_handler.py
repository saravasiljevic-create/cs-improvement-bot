import io

import requests as rq
from jira import JIRA

from config import JIRA_API_TOKEN, JIRA_SERVER_URL, JIRA_USER_EMAIL

_jira_client = None


def _get_client():
    global _jira_client
    if _jira_client is None:
        _jira_client = JIRA(
            server=JIRA_SERVER_URL,
            basic_auth=(JIRA_USER_EMAIL, JIRA_API_TOKEN),
        )
    return _jira_client


def search_similar_tickets(title: str, use_case: str = ''):
    """Search for similar unresolved Jira tickets in the CS project.

    Searches by individual keywords from title and use_case to maximize recall.
    """
    import re

    stopwords = {'und', 'der', 'die', 'das', 'ist', 'in', 'an', 'auf', 'zu',
                 'mit', 'für', 'von', 'den', 'dem', 'ein', 'eine', 'the',
                 'and', 'for', 'with', 'from', 'that', 'this', 'are', 'not'}
    words = re.findall(r'\b\w{3,}\b', f"{title} {use_case}".lower())
    keywords = [w for w in words if w not in stopwords][:5]

    if not keywords:
        return []

    conditions = ' OR '.join(
        f'summary ~ "{kw}" OR description ~ "{kw}"'
        for kw in keywords
    )
    jql = (
        f'project = CS AND ({conditions}) '
        f'AND resolution = Unresolved ORDER BY created DESC'
    )

    print(f"Jira JQL: {jql}")
    issues = _get_client().search_issues(jql, maxResults=5)
    print(f"Jira results: {len(issues)} tickets found")

    results = []
    for issue in issues:
        results.append({
            'key': issue.key,
            'summary': issue.fields.summary,
            'status': issue.fields.status.name,
            'assignee': (
                issue.fields.assignee.displayName
                if issue.fields.assignee
                else 'Unassigned'
            ),
            'created': issue.fields.created,
            'url': f'{JIRA_SERVER_URL}/browse/{issue.key}',
        })

    return results


def add_vote(issue_key: str) -> dict:
    """Record a CS-team upvote on a Jira issue.

    Attempts a real Jira vote first (works for tickets the bot didn't create).
    Always adds a comment as a reliable, visible record — the Jira vote API
    silently ignores self-votes (reporter == voter) and may also be disabled
    at the project level.
    """
    from datetime import datetime, timezone as _tz
    client = _get_client()

    # Try the real vote (best-effort — self-vote is silently rejected by Jira)
    try:
        client.vote_issue(issue_key)
        print(f"Voted on {issue_key}")
    except Exception as e:
        print(f"vote_issue({issue_key}) skipped: {e}")

    # Always add a comment so the upvote is visible regardless of vote settings
    date_str = datetime.now(tz=_tz.utc).strftime('%d.%m.%Y')
    try:
        client.add_comment(issue_key, f"👍 *Upvote vom CS Team* ({date_str})")
    except Exception as e:
        print(f"add_comment({issue_key}) failed: {e}")

    issue = client.issue(issue_key, fields='summary')
    return {
        'key': issue_key,
        'summary': issue.fields.summary,
        'url': f'{JIRA_SERVER_URL}/browse/{issue_key}',
    }


def _attach_images(issue_key: str, images: list, slack_token: str):
    """Download images from Slack and attach them to the Jira issue.

    Best-effort: failures are logged but do not raise.
    """
    client = _get_client()
    for f in images:
        name = f.get('name', 'attachment')
        try:
            url = f.get('url_private_download') or f.get('url_private')
            if not url:
                print(f"No download URL for {name}, skipping attachment")
                continue

            resp = rq.get(
                url,
                headers={"Authorization": f"Bearer {slack_token}"},
                timeout=30,
            )
            resp.raise_for_status()

            content = io.BytesIO(resp.content)
            client.add_attachment(issue=issue_key, attachment=content, filename=name)
            print(f"Attached {name} to {issue_key}")
        except Exception as e:
            print(f"Failed to attach {name} to {issue_key}: {e}")


def create_ticket(summary: str, use_case: str, user_name: str, request_date: str,
                  slack_link: str = '', images: list = None, slack_token: str = ''):
    """Create a new Jira task ticket from a Slack improvement request."""
    images = images or []

    # Build optional image section for the description
    if images:
        image_lines = '\n'.join(
            f"- [{f.get('name', 'Bild')}|{f.get('permalink', f.get('url_private', ''))}]"
            for f in images
            if f.get('permalink') or f.get('url_private')
        )
        image_section = f"\n\n*Anhänge (Slack):*\n{image_lines}" if image_lines else ''
    else:
        image_section = ''

    slack_ref = f"\n*Slack-Post:* {slack_link}" if slack_link else ""

    # When the use_case already carries structured sections (Problem / Auswirkung / …),
    # use it directly so we don't nest "*Beschreibung:* → *Problem:*".
    _is_structured = '*Problem:*' in use_case or '*Auswirkung:*' in use_case
    if _is_structured:
        desc_body = use_case
    else:
        desc_body = f"*Beschreibung:*\n{use_case}"

    description = (
        f"{desc_body}\n\n"
        f"*Anfrage von:* {user_name}\n"
        f"*Datum der Anfrage:* {request_date}{slack_ref}"
        f"{image_section}\n\n"
        f"This ticket was created automatically via the CS Improvement Bot.\n"
        f"*Autor:* {user_name}"
    )

    try:
        issue_dict = {
            'project': {'key': 'CS'},
            'summary': summary,
            'description': description,
            'issuetype': {'name': 'Task'},
        }
        new_issue = _get_client().create_issue(fields=issue_dict)
        print(f"Created Jira ticket {new_issue.key}")

        # Attach images as actual Jira attachments (best-effort)
        if images and slack_token:
            _attach_images(new_issue.key, images, slack_token)

        return {
            'key': new_issue.key,
            'url': f'{JIRA_SERVER_URL}/browse/{new_issue.key}',
            'summary': summary,
        }
    except Exception as e:
        print(f"Error creating ticket: {str(e)}")
        raise
