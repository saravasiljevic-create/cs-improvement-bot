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


def search_similar_tickets(query_text):
    """Search for similar unresolved Jira tickets matching query_text."""
    try:
        safe_query = query_text.replace('"', '\\"')
        jql = f'text ~ "{safe_query}" AND resolution = Unresolved ORDER BY created DESC'
        issues = _get_client().search_issues(jql, maxResults=5)

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
    except Exception as e:
        print(f"Error searching tickets: {str(e)}")
        return []


def create_ticket(slack_user_id, original_text, summary=None):
    """Create a new Jira task ticket from a Slack message."""
    try:
        ticket_summary = summary or f"Slack request from user {slack_user_id}"
        description = (
            f"This ticket was created automatically via the CS Improvement Bot.\n\n"
            f"*Slack User ID:* {slack_user_id}\n\n"
            f"*Original Message:*\n{original_text}"
        )
        issue_dict = {
            'project': {'key': 'CS'},
            'summary': ticket_summary,
            'description': description,
            'issuetype': {'name': 'Task'},
        }

        new_issue = _get_client().create_issue(fields=issue_dict)

        return {
            'key': new_issue.key,
            'url': f'{JIRA_SERVER_URL}/browse/{new_issue.key}',
            'summary': ticket_summary,
        }
    except Exception as e:
        print(f"Error creating ticket: {str(e)}")
        raise
