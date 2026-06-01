import os
import sys

SLACK_BOT_TOKEN = os.environ.get('SLACK_BOT_TOKEN')
SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
SLACK_CHANNEL_ID = os.environ.get('SLACK_CHANNEL_ID')
SLACK_CHANNEL_NAME = os.environ.get('SLACK_CHANNEL_NAME')
JIRA_SERVER_URL = os.environ.get('JIRA_SERVER_URL', '')
if JIRA_SERVER_URL and not JIRA_SERVER_URL.startswith('http'):
    JIRA_SERVER_URL = 'https://' + JIRA_SERVER_URL
JIRA_USER_EMAIL = os.environ.get('JIRA_USER_EMAIL')
JIRA_API_TOKEN = os.environ.get('JIRA_API_TOKEN')

# Vertragsanpassungs-Flow (optional — Flow ist deaktiviert wenn VERTRAGSANPASSUNG_CHANNEL_ID leer)
CHARGEBEE_API_KEY = os.environ.get('CHARGEBEE_API_KEY', '')
CHARGEBEE_SITE = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')
VERTRAGSANPASSUNG_CHANNEL_ID = os.environ.get('VERTRAGSANPASSUNG_CHANNEL_ID', '')

# CS Admin User IDs (dürfen #vertragsanpassung im Thread triggern)
# Mirjam Köberlein, Linda Litzkow, Sara Vasiljevic
_DEFAULT_ADMIN_IDS = 'U07G83YH6RW,U092RN6D339,U07TRKK8BH9'
CS_ADMIN_USER_IDS: set[str] = set(
    os.environ.get('CS_ADMIN_USER_IDS', _DEFAULT_ADMIN_IDS).split(',')
)

required_credentials = {
    'SLACK_BOT_TOKEN': SLACK_BOT_TOKEN,
    'SLACK_SIGNING_SECRET': SLACK_SIGNING_SECRET,
    'SLACK_CHANNEL_ID': SLACK_CHANNEL_ID,
    'SLACK_CHANNEL_NAME': SLACK_CHANNEL_NAME,
    'JIRA_SERVER_URL': JIRA_SERVER_URL,
    'JIRA_USER_EMAIL': JIRA_USER_EMAIL,
    'JIRA_API_TOKEN': JIRA_API_TOKEN,
}

missing_credentials = [key for key, value in required_credentials.items() if not value]
if missing_credentials:
    print(f"Error: Missing required credentials: {', '.join(missing_credentials)}")
    sys.exit(1)
