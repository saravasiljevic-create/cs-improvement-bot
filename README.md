# CS Admin Bot

> Zuletzt automatisch aktualisiert: 03.06.2026 13:48

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
10. Abschluss: `:cs-admin-bot:` Emoji auf Root-Nachricht

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
| Codezeilen | bot.py: 1059 | vertragsanpassung_handler.py: 1072 |

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
| `VA_DONE_EMOJI` | optional | Custom-Emoji nach VA-Abschluss (default: `cs-admin-bot`) |

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
9a6d04a VA: fix plan_id, CS-Admin-only buttons, 48h reminder
46ef536 Fix current plan + approval buttons + remove item_price name
088c94e Fix IST plan + add va_take_over handler + remove item_price name
659c862 Add Chargebee item_price name to VA summary
37a9e54 Use :cs-admin-bot: emoji for VA summary completion
```

---

*Diese Datei wird automatisch aktualisiert wenn Python-Dateien im Projekt geändert werden.*
