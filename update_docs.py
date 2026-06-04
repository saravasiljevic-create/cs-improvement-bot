#!/usr/bin/env python3
"""
Auto-generates README.md and TOOLS.md from the current codebase.
Triggered by the PostToolUse Claude Code hook when .py files change.
"""
import ast
import os
import re
import subprocess
from datetime import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))

def git_log(n=5):
    try:
        result = subprocess.run(
            ['git', 'log', f'-{n}', '--oneline', '--no-decorate'],
            cwd=ROOT, capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception:
        return ''

def extract_functions(filepath):
    """Extract all top-level functions from a Python file with their docstrings."""
    try:
        with open(filepath) as f:
            source = f.read()
        tree = ast.parse(source)
    except Exception:
        return []

    funcs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.col_offset == 0:  # only top-level
                doc = ast.get_docstring(node) or ''
                args = [a.arg for a in node.args.args if a.arg != 'self']
                funcs.append({
                    'name': node.name,
                    'args': args,
                    'doc': doc.split('\n')[0] if doc else '',
                    'lineno': node.lineno,
                })
    return funcs

def extract_action_ids(filepath):
    """Find all Slack action IDs registered in bot.py."""
    ids = []
    try:
        with open(filepath) as f:
            for line in f:
                m = re.search(r'@app\.action\(["\']([^"\']+)["\']', line)
                if m:
                    ids.append(m.group(1))
    except Exception:
        pass
    return ids

def extract_secrets(filepath):
    """Extract configured secrets from config.py."""
    secrets = []
    try:
        with open(filepath) as f:
            for line in f:
                m = re.match(r"^(\w+)\s*=\s*os\.environ\.get\(['\"](\w+)['\"]", line)
                if m:
                    secrets.append(m.group(1))
    except Exception:
        pass
    return secrets

def count_lines(filepath):
    try:
        with open(filepath) as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

def generate_readme():
    now = datetime.now().strftime('%d.%m.%Y %H:%M')
    log = git_log(5)
    bot_lines = count_lines(os.path.join(ROOT, 'bot.py'))
    va_lines = count_lines(os.path.join(ROOT, 'vertragsanpassung_handler.py'))

    content = f"""# CS Admin Bot

> Zuletzt automatisch aktualisiert: {now}

Slack-Bot für das CS Admin Team bei Xentral. Verbindet Slack mit Jira (Improvement Requests) und Chargebee (Vertragsanpassungen).

**Repository:** [saravasiljevic-create/cs-improvement-bot](https://github.com/saravasiljevic-create/cs-improvement-bot)
**Deployment:** Google Cloud Run (`cs-admin-bot-497509`, Region: `europe-west1`)

---

## Flows

### 1. Improvement-Request-Flow

**Trigger:** Nachricht mit `#improvement` im konfigurierten Slack-Channel

**Ablauf:**
1. Bot erkennt `#improvement` → setzt 👀 auf die Nachricht
2. Extrahiert Titel und Use Case aus dem Text
3. Fragt nach fehlenden Informationen falls nötig
4. Sucht nach ähnlichen Jira-Tickets im CS-Board (JQL-Suche)
5. **Ähnliche Tickets gefunden:** Zeigt sie + Buttons „Kein Ticket passt" / „❌ Kein Ticket nötig"
6. **Keine ähnlichen Tickets:** Erstellt direkt ein Jira-Ticket
7. Jemand schreibt eine Ticket-Nummer (z.B. `CS-123`) → Bot upvotet automatisch (Kommentar + Vote)
8. Abschluss: ✅ auf Root-Nachricht

**Datenquellen:** Jira (CS-Board), Slack

---

### 2. Vertragsanpassungs-Flow

**Trigger (automatisch):** Nachricht mit Vertragsanpassungs-Signalen (Score ≥ 3):
- Stark (3 Punkte): „Vertrag anlegen", „24-Monatsvertrag", „unterschriebenes Angebot", „unterzeichnetes Angebot"
- Mittel (1 Punkt): „Jahresrechnung", „Zahlungsplan", „upgraden", „rückgängig machen", etc.

**Trigger (manuell):** CS Admin schreibt `#vertragsanpassung` in einen Thread (nur CS Admin Team)

**Ablauf:**
1. Erkennung → Bot setzt 👀
2. Extraktion aus Nachricht: Kundenname, Plan, Zahlweise, Datum, Angebots-URL
3. Wenn Angebots-URL vorhanden: Seite laden und Infos ergänzen (Plan, Zahlung, „inkl. X")
4. Chargebee-Lookup: Subscription des Kunden (exakter Company-Name-Match)
5. Fehlende Pflichtfelder nachfragen: Plan, Zahlweise, Vertragsbeginn (ASAP = nächste Rechnung)
6. Wenn alle Infos da und mehrere Subscriptions: CS Admin Warnung mit Links
7. CS Admin schreibt Chargebee-URL → Bot lädt die richtige Subscription
8. Zusammenfassung: IST-Zustand, SOLL-Änderungen, Chargebee `item_price_id`, Hinweise
9. Buttons: „🙋 Mache ich — ich übernehme" / „✅ Geprüft — bitte ausführen"
10. Abschluss: `:csadmin-bot:` Emoji auf Root-Nachricht

**Datenquellen:** Chargebee (read-only), Planhat (optional, für Company-ID), Angebots-HTML

---

## Architektur

| Komponente | Technologie |
|---|---|
| Bot-Framework | Slack Bolt for Python 1.18 |
| Web-Server | Flask 3.0 + SlackRequestHandler |
| Hosting | Google Cloud Run (europe-west1, min 1 Instanz, kein CPU-Throttling) |
| CI/CD | Google Cloud Build (push auf `main` → automatisches Deployment) |
| Secrets | GCP Secret Manager |
| In-Memory-State | Python-Dicts (`_pending`, `_ticket_data`, `_pending_vertragsanpassung`) |
| Codezeilen | bot.py: {bot_lines} | vertragsanpassung_handler.py: {va_lines} |

**Wichtiger Hinweis:** Der In-Memory-State überlebt keine Neustarts. Bei einem Deployment werden alle offenen States (👀-Nachrichten) zurückgesetzt.

---

## Konfiguration / Secrets (GCP Secret Manager)

| Secret | Pflicht | Beschreibung |
|---|---|---|
| `SLACK_BOT_TOKEN` | ✅ | Bot OAuth Token (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | ✅ | Webhook-Signaturprüfung |
| `SLACK_APP_TOKEN` | ✅ | Socket Mode Token |
| `SLACK_CHANNEL_ID` | ✅ | Slack Channel-ID für Improvement + VA Flow |
| `SLACK_CHANNEL_NAME` | ✅ | Channel-Name (nur für Logs) |
| `JIRA_SERVER_URL` | ✅ | z.B. `https://xentral.atlassian.net` |
| `JIRA_USER_EMAIL` | ✅ | Jira-Serviceaccount E-Mail |
| `JIRA_API_TOKEN` | ✅ | Jira API Token |
| `CHARGEBEE_API_KEY` | ✅ | Chargebee API Key (xentral-dach, read-only) |
| `PLANHAT_API_TOKEN` | optional | Planhat Service Account Token (für Company-ID-Lookup) |
| `VA_DONE_EMOJI` | optional | Custom-Emoji nach VA-Abschluss (default: `csadmin-bot`) |

### CS Admin Team (Slack User IDs)

| Name | Slack ID |
|---|---|
| Mirjam Köberlein | `U07G83YH6RW` |
| Linda Litzkow | `U092RN6D339` |
| Sara Vasiljevic | `U07TRKK8BH9` |

---

## Deployment

```bash
# Automatisch via Cloud Build bei Push auf main
git push origin main

# Status prüfen
# https://console.cloud.google.com/cloud-build/builds?project=cs-admin-bot-497509
```

Cloud Build führt aus:
1. `docker build` → Image in `gcr.io/cs-admin-bot-497509/cs-improvement-bot:latest`
2. `docker push`
3. `gcloud run deploy` mit Secrets aus Secret Manager

---

## Externe Integrationen

### Slack API
Benötigte OAuth-Scopes: `channels:history`, `groups:history`, `chat:write`, `reactions:read`, `reactions:write`, `users:read`, `files:read`

### Jira
- Projekt: `CS` (Customer Success Admin Board)
- Operationen: Ticket erstellen (Task), ähnliche Tickets suchen (JQL), Upvote (Vote + Kommentar)
- Auth: Basic Auth (E-Mail + API Token)

### Chargebee
- Site: `xentral-dach` → `https://xentral-dach.chargebee.com`
- **Nur Lesezugriff** (kein Schreiben)
- Operationen: Kunden suchen, Subscriptions laden, item_price auflösen

### Planhat
- Nur für Vertragsanpassungs-Flow (optional)
- Operationen: Company-Suche → `externalId` (Chargebee Customer-ID)

---

## Letzte Commits

```
{log}
```

---

*Diese Datei wird automatisch aktualisiert wenn Python-Dateien im Projekt geändert werden.*
"""
    return content

def generate_tools():
    now = datetime.now().strftime('%d.%m.%Y %H:%M')

    files = [
        ('bot.py', 'Haupt-Bot: Event-Handler, Flow-Steuerung, Slack-Aktionen'),
        ('vertragsanpassung_handler.py', 'Vertragsanpassungs-Flow: Erkennung, Parsing, Chargebee, Zusammenfassung'),
        ('jira_handler.py', 'Jira-Integration: Suche, Ticket-Erstellung, Upvoting'),
        ('optimizer.py', 'Rule-based Formatter für Jira-Ticket-Titel und -Beschreibungen'),
        ('config.py', 'Konfiguration aus Umgebungsvariablen'),
    ]

    sections = [f"# CS Admin Bot — Funktionen & Tools\n\n> Zuletzt automatisch aktualisiert: {now}\n"]

    for filename, description in files:
        filepath = os.path.join(ROOT, filename)
        if not os.path.exists(filepath):
            continue

        funcs = extract_functions(filepath)
        sections.append(f"\n---\n\n## `{filename}`\n\n_{description}_\n")

        if funcs:
            sections.append("\n| Funktion | Parameter | Beschreibung |")
            sections.append("|---|---|---|")
            for f in funcs:
                args_str = ', '.join(f['args']) if f['args'] else '–'
                doc = f['doc'].replace('|', '\\|') if f['doc'] else '–'
                sections.append(f"| `{f['name']}` | `{args_str}` | {doc} |")
        else:
            sections.append("\n_(Keine Top-Level-Funktionen)_")

    # Slack Actions
    action_ids = extract_action_ids(os.path.join(ROOT, 'bot.py'))
    if action_ids:
        sections.append("\n---\n\n## Slack Action IDs\n")
        sections.append("| Action ID | Beschreibung |")
        sections.append("|---|---|")
        ACTION_DOCS = {
            'reject_similar_create_ticket': 'Nutzer klickt „Kein Ticket passt" bei ähnlichen Tickets',
            'cancel_create_ticket': 'Nutzer klickt „❌ Kein Ticket nötig"',
            'va_take_over': 'CS Admin übernimmt VA manuell',
            'va_approved': 'CS Admin gibt Go für VA-Ausführung',
            'confirm_create_ticket': 'Ticket-Erstellung bestätigen (legacy)',
            'create_ticket_button': 'Ticket-Button (legacy)',
        }
        for aid in action_ids:
            doc = ACTION_DOCS.get(aid, '–')
            sections.append(f"| `{aid}` | {doc} |")

    # Chargebee Plan-ID Mapping
    sections.append("""
---

## Chargebee Plan-ID Mapping

Die Funktion `resolve_chargebee_plan_id(plan_name, contract_months, payment_type)` löst aus
den geparsten Feldern die exakte Chargebee `item_price_id` auf.

| Plan-Name | Monate | Zahlung | item_price_id |
|---|---|---|---|
| Pro 25 | 12 | monatlich | `pro-annual-contract-monthly-payment` |
| Pro 25 | 12 | jährlich | `pro-annual-contract-annual-payment` |
| Pro 25 | 24 | monatlich | `pro-biennial-contract-monthly-payment` |
| Pro 25 | 24 | jährlich | `pro-biennial-contract-annual-payment` |
| Business 25 | 12 | monatlich | `business-annual-contract-monthly-payment` |
| Business 25 | 24 | jährlich | `business-biennial-contract-annual-payment` |
| Scale | 12 | monatlich | `scale-annual-contract-monthly-payment` |
| Scale | 24 | jährlich | `scale-biennial-contract-annual-payment` |

**Schema:** `{plan-slug}-{duration}-contract-{payment}-payment`
- Slugs: `pro`, `pro-25` (2025), `business`, `business-25`, `scale`, `starter`, `launch`
- Duration: `monthly` (1M), `annual` (12M), `biennial` (24M)
- Payment: `monthly`, `annual`

---

## Vertragsanpassungs-Erkennungs-Score

Minimum-Score für automatische Erkennung: **3 Punkte**

| Signal | Punkte | Beispiele |
|---|---|---|
| `vertrags*anpassung/änderung/verlängerung` | 3 | „Vertragsanpassung" |
| `vertragswechsel` | 3 | „Vertragswechsel" |
| `vertrag anlegen` | 3 | „Vertrag anlegen" |
| `\\d+-Monatsvertrag/Jahresvertrag` | 3 | „24-Monatsvertrag" |
| `unterschriebenes/unterzeichnetes Angebot` | 3 | „unterzeichnete Angebot:" |
| `signed offer/contract` | 3 | |
| Plan+Wechsel | 1 | „Plan upgraden" |
| Add-On-Änderung | 1 | „Add-On hinzufügen" |
| Jahresrechnung/Zahlungsplan | 1 | „Jahresrechnung" |
| Upgrade/Upgraden | 1 | „upgraden", „gradet ... up" |
| Verlängerungs-Signale | 1 | „Verlängerung seines Vertrags" |
| Rückgängig machen | 1 | |

---

*Diese Datei wird automatisch aktualisiert wenn Python-Dateien im Projekt geändert werden.*
""")

    return '\n'.join(sections)

def main():
    readme = generate_readme()
    tools = generate_tools()

    readme_path = os.path.join(ROOT, 'README.md')
    tools_path = os.path.join(ROOT, 'TOOLS.md')

    with open(readme_path, 'w') as f:
        f.write(readme)
    with open(tools_path, 'w') as f:
        f.write(tools)

    print(f"✅ README.md ({len(readme)} chars) und TOOLS.md ({len(tools)} chars) aktualisiert.")

if __name__ == '__main__':
    main()
