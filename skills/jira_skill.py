"""
Jira Skill — CS-Tickets suchen und lesen
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "jira_search_customer_tickets",
        "description": (
            "Sucht im CS Jira-Board nach Tickets, die mit einem Kunden zusammenhängen. "
            "Gibt offene und kürzlich geschlossene Tickets zurück."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company_name": {
                    "type": "string",
                    "description": "Firmenname oder Suchbegriff",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximale Anzahl Ergebnisse (default 10)",
                    "default": 10,
                },
            },
            "required": ["company_name"],
        },
    },
]


def execute(tool_name: str, params: dict, context: dict) -> str:
    if tool_name == "jira_search_customer_tickets":
        return _search_tickets(params.get('company_name', ''), params.get('max_results', 10))
    return f"Unbekanntes Tool: {tool_name}"


def _search_tickets(company_name: str, max_results: int) -> str:
    if not company_name:
        return "Kein Suchbegriff angegeben."
    try:
        from jira_handler import _get_client, JIRA_SERVER_URL
        client = _get_client()
        jira_url = os.environ.get('JIRA_SERVER_URL', '')

        import re
        words = re.findall(r'\b\w{3,}\b', company_name.lower())
        stopwords = {'und', 'der', 'die', 'das', 'the', 'and', 'for', 'gmbh', 'ag'}
        keywords = [w for w in words if w not in stopwords][:3]

        if not keywords:
            return "Keine verwertbaren Suchbegriffe."

        conditions = ' OR '.join(f'summary ~ "{kw}" OR description ~ "{kw}"' for kw in keywords)
        jql = f'project = CS AND ({conditions}) ORDER BY created DESC'

        issues = client.search_issues(jql, maxResults=min(max_results, 20))
        result = []
        for issue in issues:
            result.append({
                "key": issue.key,
                "summary": issue.fields.summary,
                "status": issue.fields.status.name,
                "assignee": issue.fields.assignee.displayName if issue.fields.assignee else "Unassigned",
                "created": issue.fields.created[:10] if issue.fields.created else '',
                "url": f"{jira_url}/browse/{issue.key}",
            })
        if not result:
            return f"Keine CS-Tickets für '{company_name}' gefunden."
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Jira search failed: {e}")
        return f"Jira-Suche fehlgeschlagen: {e}"
