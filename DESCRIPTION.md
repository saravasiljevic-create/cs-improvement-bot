# CS Admin Bot — Produktbeschreibung & Bedienungsanleitung

> **Für wen ist dieses Dokument?**
> Für alle Mitglieder des CS-Teams und CS-Leads bei Xentral — ohne technisches Vorwissen.
> Es erklärt, was der Bot kann, wie man ihn benutzt und wo er seine Grenzen hat.

---

## Was ist der CS Admin Bot?

Der **CS Admin Bot** ist ein Slack-Bot, der im Xentral-Workspace läuft und das CS Admin Team bei zwei konkreten Aufgaben unterstützt:

1. **Feature-Anfragen (Improvement Requests)** strukturiert und konsistent in Jira erfassen
2. **Vertragsanpassungen** vorbereiten — mit automatisch aufgesammelten Kundendaten aus Chargebee und Planhat

Der Bot ist kein Chatbot im allgemeinen Sinne. Er reagiert auf bestimmte Schlüsselwörter oder Signale im CS Admin Channel und führt dann einen strukturierten Ablauf (Flow) durch. Alles, was er tut, ist transparent im Thread sichtbar.

**Entwicklung & Betrieb:** Der Bot wurde intern entwickelt und wird im Hintergrund automatisch betrieben.

---

## Wo läuft der Bot?

| Umgebung | Slack-Channel |
|---|---|
| Test (Sandbox) | `#cs-admin-test` |
| Produktion | `#cs-admin` (der echte CS Admin Channel) |

> Im Test-Channel kann man ungefährdet ausprobieren — nichts davon landet im echten Jira oder wird an Kunden weitergegeben.

---

## Feature 1: Improvement Request Flow

### Wofür ist das?

Wenn jemand im CS-Team eine **Idee für eine Produktverbesserung** oder ein **Feature-Request** hat, kann er das mit dem Hashtag `#improvement` direkt im Channel posten. Der Bot sorgt dafür, dass daraus ein sauberes Jira-Ticket wird — oder prüft, ob der Wunsch schon als Ticket existiert, damit man Duplikate vermeidet und stattdessen bestehende Tickets hochvoten kann.

### Schritt-für-Schritt: Was passiert?

**Schritt 1 — Nachricht schreiben**

Jemand schreibt im CS Admin Channel eine Nachricht, die `#improvement` enthält, zum Beispiel:

```
#improvement Beim Erstellen von Angeboten sollte das Service-Paket vorausgefüllt sein,
wenn der Kunde bereits eines hat.
```

Titel und Beschreibung können frei formuliert werden — der Bot extrahiert die relevanten Informationen selbst.

---

**Schritt 2 — Bot bestätigt die Verarbeitung**

Der Bot setzt das 👀-Emoji auf die Nachricht. Das bedeutet: "Ich habe es gesehen und arbeite daran."

---

**Schritt 3 — Ähnliche Tickets suchen**

Der Bot durchsucht das CS-Jira-Board nach ähnlichen bestehenden Tickets.

**Fall A: Ähnliche Tickets gefunden**

Der Bot antwortet im Thread und zeigt die gefundenen Tickets mit Links:

> **Ich habe ähnliche Tickets gefunden:**
>
> • CS-42 — Angebotserstellung: Service-Paket vorausfüllen *(Link)*
> • CS-117 — Prefill von Kundendaten im Angebot *(Link)*
>
> Passt eines davon zu deiner Anfrage? Schreib einfach die Ticket-Nummer (z.B. `CS-42`),
> oder klicke auf einen der Buttons unten.
>
> [Kein Ticket passt] [❌ Kein Ticket nötig]

**Fall B: Keine ähnlichen Tickets gefunden**

Der Bot erstellt direkt ein neues Jira-Ticket und meldet sich kurz im Thread.

---

**Schritt 4 — Reaktion der Person**

Es gibt drei Möglichkeiten:

| Aktion | Was passiert |
|---|---|
| Ticket-Nummer schreiben (z.B. `CS-42`) | Bot upvotet dieses Ticket automatisch (fügt einen Kommentar + Vote hinzu) |
| Klick auf "Kein Ticket passt" | Bot erstellt ein neues Jira-Ticket |
| Klick auf "❌ Kein Ticket nötig" | Vorgang wird abgebrochen, kein Ticket wird erstellt |

---

**Schritt 5 — Abschluss**

Sobald alles erledigt ist, setzt der Bot ein ✅-Emoji auf die ursprüngliche Nachricht. Das bedeutet: "Erledigt."

---

### Funktioniert das auch in Thread-Antworten?

**Ja.** Wenn jemand `#improvement` in eine **Thread-Antwort** schreibt, liest der Bot auch die ursprüngliche Root-Nachricht des Threads für zusätzlichen Kontext.

---

### Wie sieht ein fertiges Jira-Ticket aus?

Der Bot formatiert das Ticket so, dass es einheitlich und professionell wirkt:

- **Typ:** Task
- **Titel:** Aus dem Text extrahiert oder wie angegeben
- **Beschreibung:** Aufgeräumt und strukturiert
- **Board:** CS Jira-Board
- **Quelle:** Slack-Link zur Originalnachricht

---

## Feature 2: Vertragsanpassungs-Flow

### Wofür ist das?

Wenn ein Kunde seinen Vertrag ändern möchte — neuer Plan, andere Laufzeit, anderes Service-Paket — schreibt das CS-Team typischerweise eine Nachricht im CS Admin Channel. Der Bot erkennt diese Anfragen automatisch und hilft dabei, alle relevanten Informationen (IST-Zustand aus Chargebee, SOLL-Zustand aus der Anfrage) sauber aufzubereiten.

Am Ende steht eine vollständige Zusammenfassung, die ein CS Admin direkt als Grundlage für die Änderung in Chargebee verwenden kann — **ohne selbst in Chargebee suchen oder rechnen zu müssen.**

---

### Wie wird der Flow ausgelöst?

**Automatisch (empfohlen):**
Der Bot erkennt Nachrichten, die typische Signale einer Vertragsanpassung enthalten, zum Beispiel:
- "Vertragsanpassung"
- "unterzeichnetes Angebot"
- "24-Monatsvertrag"
- "Pro 25 | ..."
- "Vertrag anlegen"
- "Jahresrechnung"

Er wertet mehrere solcher Begriffe zusammen aus und entscheidet, ob es sich wirklich um eine Vertragsanpassungs-Anfrage handelt.

**Manuell (wenn der Bot nicht automatisch reagiert):**
Ein CS Admin kann `#vertragsanpassung` in den Thread schreiben — dann startet der Bot den Flow manuell.

> Hinweis: Der manuelle Trigger steht nur CS Admin-Mitgliedern zur Verfügung (Mirjam, Linda, Sara).

---

### Schritt-für-Schritt: Was passiert?

#### Schritt 1 — Erkennung

Bot erkennt die Nachricht als Vertragsanpassungs-Anfrage und setzt 👀 auf die Nachricht.

---

#### Schritt 2 — Informationen extrahieren

Der Bot liest die Nachricht und — falls ein Link zu einem Xentral-Angebot vorhanden ist — auch den Inhalt der verlinkten Angebotsseite. Dabei sucht er automatisch nach:

| Information | Beispiel |
|---|---|
| Kundenname | "Mustermann GmbH" |
| Neuer Plan | "Pro 25" |
| Vertragslaufzeit | 12 Monate / 24 Monate |
| Zahlungsweise | monatlich / jährlich |
| Vertragsbeginn | "01.07.2026" oder "ASAP" |
| Service-Paket | "Premium L", "Standard S" |
| Angebots-Link | Link zur Xentral-Angebotsseite |

---

#### Schritt 3 — Fehlende Informationen erfragen

Falls etwas Wichtiges fehlt, zeigt der Bot im Thread, was er bereits erkannt hat, und fragt gezielt nach dem Fehlenden:

> **Bereits erkannt:**
> - Kundenname: Mustermann GmbH
> - Neuer Plan: Pro 25
> - Laufzeit: 24 Monate
>
> **Noch offen:**
> - Vertragsbeginn: Ab wann soll der neue Vertrag gelten? (oder "ASAP" für nächste Rechnungsstellung)

Wenn "ASAP" angegeben wird, schlägt der Bot automatisch das nächste Rechnungsdatum aus Chargebee vor.

---

#### Schritt 4 — Chargebee-Lookup

Der Bot sucht den Kunden in Chargebee (über den Firmennamen) und lädt die aktuellen Vertragsdaten:

- Aktueller Plan
- Billing-Zyklus (monatlich / jährlich)
- Aktuelles Service-Paket
- Nächstes Rechnungsdatum

---

#### Schritt 5 — CS Admin wird einbezogen (wenn nötig)

**Situation A: Mehrere Subscriptions gefunden**

Falls der Kunde mehrere aktive Subscriptions in Chargebee hat, kann der Bot nicht automatisch entscheiden, welche gemeint ist. Er meldet sich im Thread:

> "@Mirjam @Linda @Sara — Für Mustermann GmbH gibt es mehrere Subscriptions:
>
> • sub_abc123 — Pro 10, monatlich *(Chargebee-Link)*
> • sub_xyz789 — Starter, jährlich *(Chargebee-Link)*
>
> Bitte schreibt den richtigen Chargebee-Link hier in den Thread."

Ein CS Admin schreibt dann einfach den Link in den Thread, und der Bot lädt die richtige Subscription.

**Situation B: Kein Kunde gefunden**

Falls der Kundenname in Chargebee nicht gefunden wird (z.B. wegen abweichender Schreibweise), meldet der Bot das und bittet ebenfalls um den Chargebee-Link.

---

#### Schritt 6 — Vollständige Zusammenfassung

Sobald alle Informationen vorliegen, erstellt der Bot eine strukturierte Zusammenfassung im Thread:

---

> **Vertragsanpassung: Mustermann GmbH**
>
> **IST-Zustand** *(aus Chargebee)*
> - Subscription: [Link zur Subscription]
> - Aktueller Plan: Pro 10 (monatlich)
> - Service-Paket: Standard S
> - Nächste Rechnung: 01.07.2026
>
> **SOLL-Änderungen**
> - Neuer Plan: Pro 25 — 24 Monate — jährlich
> - Chargebee item_price_id: `pro-25-EUR-Yearly`
> - Service-Paket: Standard S → Premium L
> - Zahlungsweise: monatlich → jährlich
> - Vertragsbeginn: 01.07.2026
>
> **Hinweise**
> ⚠️ Vertragsbeginn liegt in der Zukunft → Ramp-Konfiguration prüfen
> 📎 Add-ons wurden nicht erwähnt — bitte prüfen, ob bestehende Add-ons übernommen werden sollen
>
> [🙋 Mache ich — ich übernehme] [✅ Geprüft — bitte ausführen]

---

#### Schritt 7 — Genehmigung & Übernahme

Die zwei Buttons am Ende der Zusammenfassung sind für das CS Admin Team:

| Button | Bedeutung |
|---|---|
| **🙋 Mache ich — ich übernehme** | Ein CS Admin bestätigt, dass er die Änderung manuell in Chargebee durchführt |
| **✅ Geprüft — bitte ausführen** | Die Zusammenfassung wurde geprüft (für zukünftige Automatisierung vorgesehen) |

Beide Buttons taggen @Mirjam, @Linda und @Sara, damit das Team informiert ist, wer die Aufgabe übernimmt.

---

#### Schritt 8 — Abschluss

Nach Klick auf einen der Buttons setzt der Bot das :cs-admin-bot:-Emoji auf die ursprüngliche Nachricht. Das signalisiert: "Flow abgeschlossen."

---

## Was liest der Bot? — Datenquellen im Überblick

| Datenquelle | Was der Bot liest |
|---|---|
| **Slack** | Nachrichten und Thread-Antworten im CS Admin Channel |
| **Chargebee** | Subscriptions, Pläne, Billing-Zyklus, Rechnungsdaten (nur Lesezugriff) |
| **Planhat** | Firmendaten zur Identifikation der Chargebee Customer-ID |
| **Xentral-Angebotsseiten** | Planname, Service-Paket, Zahlungsweise aus verlinkten Angebotsdokumenten |
| **Jira CS-Board** | Bestehende Tickets für die Ähnlichkeitssuche |

---

## Was der Bot NICHT macht

Dieser Abschnitt ist besonders wichtig für das Vertrauen in den Bot.

### Der Bot verändert NICHTS in Chargebee

Der Bot hat **nur Lesezugriff** auf Chargebee. Er schaut nach, was dort steht — aber er ändert keine Subscriptions, bucht keine Rechnungen um und macht keine Plan-Wechsel. Alle Änderungen in Chargebee werden weiterhin manuell von einem CS Admin durchgeführt.

### Der Bot trifft keine Entscheidungen

Der Bot bereitet Informationen auf — er entscheidet nicht, ob eine Vertragsanpassung genehmigt wird oder ob ein Feature-Request umgesetzt werden soll. Das bleibt immer beim Menschen.

### Der Bot schreibt keine Kunden-E-Mails

Der Bot kommuniziert ausschließlich intern im Slack-Thread. Er schickt keine E-Mails, kontaktiert keine Kunden und kommuniziert nicht außerhalb von Slack.

### Der Bot ist kein allgemeiner Assistent

Er reagiert nur auf die spezifischen Trigger (`#improvement`, `#vertragsanpassung`, oder automatisch erkannte Vertragsanpassungs-Signale). Allgemeine Fragen oder andere Themen werden nicht beantwortet.

### Der Bot merkt sich nichts zwischen Neustarts

Der Bot arbeitet rein im Moment. Wenn er neu gestartet wird (z.B. nach einem Update), vergisst er alle laufenden offenen Abläufe. In diesem Fall muss der Flow ggf. manuell mit `#vertragsanpassung` neu gestartet werden.

### Jira: Der Bot erstellt und upvotet nur

Im Improvement-Flow erstellt der Bot neue Tickets und upvotet bestehende. Er löscht keine Tickets und ändert keine bestehenden Ticketinhalte.

---

## Häufige Fragen (FAQ)

### Muss ich etwas Besonderes schreiben, damit der Bot reagiert?

**Für Improvement Requests:** Ja — `#improvement` muss in der Nachricht vorkommen. Der Rest ist freitext.

**Für Vertragsanpassungen:** Nicht unbedingt. Der Bot versucht, Vertragsanpassungen automatisch zu erkennen. Falls er nicht reagiert, einfach `#vertragsanpassung` in den Thread schreiben.

---

### Was passiert, wenn der Bot die falsche Subscription in Chargebee findet?

Der Bot informiert das CS Admin Team sofort, wenn mehrere Subscriptions gefunden werden oder wenn er keine findet. In beiden Fällen bittet er explizit um den korrekten Chargebee-Link — dann lädt er die richtige Subscription.

---

### Kann ich den Bot im normalen CS-Channel nutzen oder nur im cs-admin Channel?

Der Bot läuft nur in seinem konfigurierten Channel (Produktion: `#cs-admin`, Test: `#cs-admin-test`). In anderen Channels reagiert er nicht.

---

### Was passiert, wenn ich `CS-123` in den Thread schreibe, aber gar kein Improvement-Flow aktiv ist?

Der Bot ist kontextbewusst — er reagiert auf Ticket-Nummern nur, wenn vorher ein Improvement-Request-Flow im selben Thread gestartet wurde. Zufällige Erwähnungen von Ticket-Nummern in anderen Threads werden ignoriert.

---

### Was passiert, wenn der Kundenname im Angebot anders geschrieben ist als in Chargebee?

Der Bot versucht eine Suche nach dem Namen, wie er in der Nachricht oder im Angebot steht. Wenn er keinen Treffer findet, meldet er das transparent und bittet das CS Admin Team, den Chargebee-Link manuell bereitzustellen.

---

### Kann jeder im Team `#vertragsanpassung` verwenden?

Nein. Der manuelle Trigger `#vertragsanpassung` ist auf das CS Admin Team beschränkt: Mirjam Köberlein, Linda Litzkow und Sara Vasiljevic. Andere Personen können im Channel schreiben und damit die automatische Erkennung auslösen, aber den manuellen Trigger nur CS Admins nutzen.

---

### Was bedeuten die Emojis auf den Nachrichten?

| Emoji | Bedeutung |
|---|---|
| 👀 | Bot hat die Nachricht erkannt und verarbeitet sie gerade |
| ✅ | Improvement-Flow erfolgreich abgeschlossen |
| :cs-admin-bot: | Vertragsanpassungs-Flow abgeschlossen |

---

### Was ist der Unterschied zwischen IST-Zustand und SOLL-Zustand in der Zusammenfassung?

- **IST-Zustand:** Der aktuelle Vertrag des Kunden, so wie er heute in Chargebee hinterlegt ist
- **SOLL-Zustand:** Die gewünschten Änderungen, wie sie aus der Anfrage und dem Angebot entnommen wurden

Diese Gegenüberstellung hilft dem CS Admin, auf einen Blick zu sehen, was sich ändert — und was gleich bleibt.

---

### Was bedeutet "Ramp-Warnung" in der Zusammenfassung?

Wenn der Vertragsbeginn in der Zukunft liegt (z.B. zum nächsten Monatsersten), weist der Bot darauf hin, dass in Chargebee ggf. eine Ramp-Konfiguration nötig ist. Das ist nur ein Hinweis — die Prüfung und Entscheidung liegt beim CS Admin.

---

### Kann der Bot Angebote öffnen, die passwortgeschützt sind?

Nein. Der Bot kann nur öffentlich zugängliche Xentral-Angebotsseiten lesen. Falls ein Angebot nicht zugänglich ist, arbeitet er mit den Informationen aus der Slack-Nachricht allein.

---

### Was passiert, wenn der Bot mitten im Ablauf neu gestartet wird?

Falls der Bot während eines laufenden Flows neu gestartet wird (z.B. wegen eines Updates), geht der aktuelle Flow-Status verloren. Der Bot setzt kein Abschluss-Emoji und antwortet nicht mehr in dem Thread. In diesem Fall kann das CS Admin Team einfach `#vertragsanpassung` im betreffenden Thread schreiben, um den Flow neu zu starten.

---

### Ich sehe das 👀-Emoji, aber der Bot antwortet nicht — was tun?

Das passiert gelegentlich, wenn der Bot neu gestartet wurde, während er die Nachricht verarbeitete. Einfach `#vertragsanpassung` im Thread schreiben, um den Flow manuell neu zu starten. Falls das Problem wiederholt auftritt, Sara Vasiljevic Bescheid geben.

---

## CS Admin Team

Der Bot taggt das folgende Team bei Bedarf:

| Name | Rolle |
|---|---|
| Mirjam Köberlein | Team Lead CS Admin |
| Linda Litzkow | CS Admin |
| Sara Vasiljevic | CS Admin |

---

## Kurzübersicht: Trigger & Aktionen

| Trigger | Wer kann ihn verwenden | Was passiert |
|---|---|---|
| `#improvement` in Nachricht | Alle im Channel | Improvement-Request-Flow startet |
| Ticket-Nummer (z.B. `CS-42`) im Thread | Alle im Channel | Bot upvotet das Ticket |
| Button "Kein Ticket passt" | Alle im Channel | Bot erstellt neues Jira-Ticket |
| Button "❌ Kein Ticket nötig" | Alle im Channel | Flow wird abgebrochen |
| Vertragsanpassungs-Signale in Nachricht | Alle im Channel (automatisch erkannt) | Vertragsanpassungs-Flow startet |
| `#vertragsanpassung` im Thread | Nur CS Admin Team | Vertragsanpassungs-Flow startet manuell |
| Chargebee-Link im Thread | CS Admin Team | Bot lädt die verlinkte Subscription |
| Button "🙋 Mache ich — ich übernehme" | CS Admin Team | Bestätigung der manuellen Übernahme |
| Button "✅ Geprüft — bitte ausführen" | CS Admin Team | Bestätigung der Prüfung |

---

*Letzte Aktualisierung: Juni 2026 — Bei Fragen oder Feedback: Sara Vasiljevic*
