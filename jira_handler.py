from jira import JIRA
from config import JIRA_SERVER_URL, JIRA_USER_EMAIL, JIRA_API_TOKEN

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

    # Extract meaningful keywords (min 3 chars, skip stopwords)
    stopwords = {'und', 'der', 'die', 'das', 'ist', 'in', 'an', 'auf', 'zu',
                 'mit', 'für', 'von', 'den', 'dem', 'ein', 'eine', 'the',
                 'and', 'for', 'with', 'from', 'that', 'this', 'are', 'not'}
    words = re.findall(r'\b\w{3,}\b', f"{title} {use_case}".lower())
    keywords = [w for w in words if w not in stopwords][:5]  # top 5 keywords

    if not keywords:
        return []

    # Build OR conditions for each keyword
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


def create_ticket(summary: str, use_case: str, user_name: str, request_date: str):
    """Create a new Jira task ticket from a Slack improvement request."""
    try:
        description = (
            f"*Beschreibung:*\n{use_case}\n\n"
            f"*Anfrage von:* {user_name}\n"
            f"*Datum der Anfrage:* {request_date}\n\n"
            f"This ticket was created automatically via the CS Improvement Bot.\n"
            f"*Autor:* {user_name}"
        )
        issue_dict = {
            'project': {'key': 'CS'},
            'summary': summary,
            'description': description,
            'issuetype': {'name': 'Task'},
        }

        new_issue = _get_client().create_issue(fields=issue_dict)

        return {
            'key': new_issue.key,
            'url': f'{JIRA_SERVER_URL}/browse/{new_issue.key}',
            'summary': summary,
        }
    except Exception as e:
        print(f"Error creating ticket: {str(e)}")
        raise
