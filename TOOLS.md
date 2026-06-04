# CS Admin Bot — Funktionen & Tools

> Zuletzt automatisch aktualisiert: 03.06.2026 13:48


---

## `bot.py`

_Haupt-Bot: Event-Handler, Flow-Steuerung, Slack-Aktionen_


| Funktion | Parameter | Beschreibung |
|---|---|---|
| `get_user_name` | `client, user_id` | – |
| `ts_to_date` | `ts` | – |
| `slack_message_link` | `channel, ts` | – |
| `extract_images` | `files` | – |
| `parse_request` | `text` | Extract title and use case from a message. |
| `missing_info` | `title, use_case` | – |
| `validate_use_case` | `title, use_case` | – |
| `ask_for_info_blocks` | `user_id, missing` | – |
| `found_ticket_blocks` | `tickets, channel, thread_ts` | – |
| `_add_reaction` | `client, channel, ts, emoji` | – |
| `_remove_reaction` | `client, channel, ts, emoji` | – |
| `_set_eyes` | `client, channel, ts` | – |
| `_set_done` | `client, channel, ts` | – |
| `_set_cancelled` | `client, channel, ts` | – |
| `_cleanup_expired_pending` | `client` | – |
| `_do_create_ticket` | `say, client, channel, thread_ts, ctx` | Optimize and create a Jira ticket, then post the result in Slack. |
| `_process_request` | `say, client, channel, thread_ts, user_id, user_name, request_date, title, use_case, images` | Search Jira CS board — show similar tickets or create one directly. |
| `_enrich_from_offer` | `parsed` | Lädt Vertragsdaten aus der Angebots-URL und ergänzt fehlende Felder. |
| `_cb_lookup` | `customer_name` | Chargebee-Lookup per Kundenname (exakter Company-Match). |
| `_process_vertragsanpassung` | `say, client, channel, thread_ts, user_name, parsed, subscription` | Alle Felder vollständig — entweder Zusammenfassung oder CS-Admin-Warnung. |
| `_handle_message_core` | `event, say, client` | Core message processing logic, shared by the generic and file_share handlers. |
| `handle_message` | `event, say, client` | – |
| `handle_file_share_message` | `event, say, client` | Explicit handler for messages that include file/image uploads. |
| `handle_reject_similar` | `ack, body, say, client` | User clicked '➕ Kein Ticket passt — neues anlegen' button. |
| `handle_cancel` | `ack, body, say, client` | User clicked '❌ Kein Ticket nötig' — remove 👀 and add ❌ on root message. |
| `handle_va_take_over` | `ack, body, say, client` | CS Admin übernimmt die Umsetzung — nur für CS Admin Team. |
| `handle_va_approved` | `ack, body, say, client` | CS Admin hat geprüft und gibt das Go — nur für CS Admin Team. |
| `handle_create_ticket` | `ack, body, say` | – |
| `slack_events` | `–` | – |
| `health` | `–` | – |

---

## `vertragsanpassung_handler.py`

_Vertragsanpassungs-Flow: Erkennung, Parsing, Chargebee, Zusammenfassung_


| Funktion | Parameter | Beschreibung |
|---|---|---|
| `fetch_item_price_name` | `item_price_id, api_key, site` | Lädt den offiziellen Chargebee-Namen einer item_price_id. |
| `resolve_chargebee_plan_id` | `plan_name, contract_months, payment_type` | Leitet die Chargebee item_price_id ab. |
| `detect_vertragsanpassung` | `text` | Gibt True zurück wenn der Text mit hoher Konfidenz eine Vertragsanpassungs-Anfrage ist. |
| `parse_vertragsanpassung` | `text` | Extrahiert strukturierte Felder aus einer Vertragsanpassungs-Anfrage im Freitext. |
| `missing_va_fields` | `parsed` | – |
| `fetch_offer_data` | `url` | Lädt eine Xentral-Angebots-URL und extrahiert Vertragsinformationen. |
| `_ts_to_date` | `ts` | – |
| `_chargebee_customer_search` | `base, auth, customer_name` | Versucht mehrere Suchstrategien um einen Chargebee-Kunden zu finden. |
| `_planhat_company_search` | `customer_name, api_token` | Sucht einen Kunden in Planhat und gibt Subscription-ID + Planhat-Link zurück. |
| `_fetch_subscription_by_id` | `subscription_id, api_key, site` | Lädt eine Chargebee-Subscription direkt per Subscription-ID. |
| `_build_subscription_result` | `sub, site` | Wandelt ein Chargebee-Subscription-Objekt in unser einheitliches Format um. |
| `_search_by_debit_number` | `debit_number, base, auth, site, company_name` | Sucht Chargebee-Kunden eindeutig über cf_debit_number (Debitorennummer). |
| `_fetch_subscriptions_for_customer` | `customer_id, base, auth, site, company_name` | Lädt Subscriptions für eine bekannte Chargebee Customer-ID. |
| `lookup_chargebee_subscription` | `customer_name, api_key, site, planhat_token` | Sucht Chargebee-Subscription. |
| `_format_found_fields` | `parsed, subscription` | – |
| `ask_for_va_info_blocks` | `user_id, missing, parsed, subscription` | – |
| `build_cs_admin_subscription_blocks` | `subscription` | Postet die CS-Admin-Warnung. Bei ≤3 Subscriptions alle zeigen, sonst nur erste 3 + Hinweis. |
| `_try_parse_date` | `date_str` | – |
| `_build_suggestions` | `parsed, subscription` | Generiert kontextuelle Hinweise basierend auf IST-Zustand vs. gewünschten Änderungen. |
| `build_va_summary_blocks` | `parsed, subscription, requester` | Erstellt Slack Block Kit Blocks für die Vertragsanpassungs-Zusammenfassung. |

---

## `jira_handler.py`

_Jira-Integration: Suche, Ticket-Erstellung, Upvoting_


| Funktion | Parameter | Beschreibung |
|---|---|---|
| `_get_client` | `–` | – |
| `search_similar_tickets` | `title, use_case` | Search for similar unresolved Jira tickets in the CS project. |
| `add_vote` | `issue_key, user_name` | Record a CS-team upvote on a Jira issue. |
| `_attach_images` | `issue_key, images, slack_token` | Download images from Slack and attach them to the Jira issue. |
| `create_ticket` | `summary, use_case, user_name, request_date, slack_link, images, slack_token` | Create a new Jira task ticket from a Slack improvement request. |

---

## `optimizer.py`

_Rule-based Formatter für Jira-Ticket-Titel und -Beschreibungen_


| Funktion | Parameter | Beschreibung |
|---|---|---|
| `optimize_ticket` | `title, use_case` | Format title and structure description for a Jira ticket. |
| `_format_title` | `title` | Clean and shorten the title to at most 8 words. |
| `_structure_description` | `text` | Wrap text in *Problem:* / *Auswirkung:* / *Erwartetes Verhalten:* sections. |
| `_remap_sections` | `text, matches` | Map detected section headers to canonical Jira labels. |

---

## `config.py`

_Konfiguration aus Umgebungsvariablen_


_(Keine Top-Level-Funktionen)_

---

## Slack Action IDs

| Action ID | Beschreibung |
|---|---|
| `reject_similar_create_ticket` | Nutzer klickt „Kein Ticket passt" bei ähnlichen Tickets |
| `cancel_create_ticket` | Nutzer klickt „❌ Kein Ticket nötig" |
| `va_take_over` | CS Admin übernimmt VA manuell |
| `va_approved` | CS Admin gibt Go für VA-Ausführung |
| `create_ticket_button` | Ticket-Button (legacy) |

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
| `\d+-Monatsvertrag/Jahresvertrag` | 3 | „24-Monatsvertrag" |
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
