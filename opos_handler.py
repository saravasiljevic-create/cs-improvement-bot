"""
OPOS-Sperrprüfung: wöchentlicher Job, Zyklus Freitag → Donnerstag.

Fragt offene Chargebee-Rechnungen direkt über die API ab (kein manueller
Wochen-Export mehr nötig), gruppiert pro Kunde, und wendet die (vorläufige)
Sperr-Regel an: ein Kunde wird gesperrt, wenn seine älteste offene Rechnung
vor >= 30 Tagen gestellt wurde UND das daraus berechnete Datum in die
aktuelle Woche fällt.

Pro Fall wird eine eigene Slack-Nachricht in #opos-instance-blocking gepostet
(Enrichment aus Planhat + best-effort Zendesk, CSM-Tag falls individuelle
Person). Jeden Freitag wird zuerst die VORWOCHE abgeschlossen (❌-Reaktionen
auswerten, Kurzbericht) bevor die neue Woche eröffnet wird.

State liegt in GCS (OPOS_STATE_BUCKET), nicht lokal — Cloud Run ist
zustandslos, ein lokales State-File würde bei jedem Redeploy/Neustart
verloren gehen.

Getriggert via POST /opos-sperrpruefung (Cloud Scheduler, freitags 8 Uhr).

Bekannte Einschränkungen (bewusst, nicht "vergessen"):
- Die 30-Tage-Sperr-Regel ist vorläufig (von Sara am 2026-08-07 bestätigt).
- Zendesk-Enrichment ist best-effort (Namenssuche über die Organisation,
  kein etablierter Chargebee<->Zendesk-Org-Mapping-Mechanismus in diesem
  Bot) — liefert das nichts, wird das Signal ausgelassen statt geraten.
- Planhat-Owner→Slack-Mapping geht über die E-Mail-Konvention
  vorname.nachname@xentral.com (wie im Rest des Bots) — bei generischen
  Konten (z.B. "Xentral Customer Success") kein Tag, nur Klartext-Name.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests
from google.cloud import storage
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from config import (
    CHARGEBEE_API_KEY,
    CHARGEBEE_SITE,
    OPOS_CHANNEL_ID,
    OPOS_STATE_BUCKET,
    PLANHAT_API_TOKEN,
    SLACK_BOT_TOKEN,
    ZENDESK_API_TOKEN,
    ZENDESK_EMAIL,
    ZENDESK_SUBDOMAIN,
)

logger = logging.getLogger(__name__)

CHARGEBEE_BASE_URL = f'https://{CHARGEBEE_SITE}.chargebee.com/api/v2'
PLANHAT_BASE_URL = 'https://api.planhat.com'
ZENDESK_BASE_URL = f'https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2'
LOCK_DAYS_AFTER_INVOICE = 30
OBJECTION_EMOJI_NAMES = {'x', 'negative_squared_cross_mark'}
STATE_OBJECT_NAME = 'opos_sperrpruefung_state.json'
TEST_MODE = True  # Solange True: Abschluss-Bericht geht nur in den Channel, nicht separat ans Accounting.

# Ein ❌ allein zählt nicht als gültiger Einspruch — es muss eine geschriebene
# Begründung im Thread folgen, die mindestens diese Schwellen erreicht.
# Reine Heuristik (Wort-/Zeichenzahl), keine inhaltliche Prüfung — bewusst
# einfach gehalten, da kein LLM-Zugriff im Bot verfügbar ist (ANTHROPIC_API_KEY
# wurde aus dem Deployment entfernt, siehe cloudbuild.yaml).
MIN_JUSTIFICATION_WORDS = 8
MIN_JUSTIFICATION_CHARS = 40

slack_client = WebClient(token=SLACK_BOT_TOKEN)


# ---------------------------------------------------------------------------
# State (GCS) — Ersatz für die lokale JSON-Datei, da Cloud Run zustandslos ist
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    client = storage.Client()
    blob = client.bucket(OPOS_STATE_BUCKET).blob(STATE_OBJECT_NAME)
    if not blob.exists():
        return {'last_friday_run': None, 'weeks': {}}
    return json.loads(blob.download_as_text())


def _save_state(state: dict) -> None:
    client = storage.Client()
    blob = client.bucket(OPOS_STATE_BUCKET).blob(STATE_OBJECT_NAME)
    blob.upload_from_string(
        json.dumps(state, indent=2, ensure_ascii=False, default=str),
        content_type='application/json',
    )


# ---------------------------------------------------------------------------
# Chargebee: offene Rechnungen direkt über die API (ersetzt den manuellen
# Wochen-Export aus der "[Chargebee] ... OPOS weekly report"-Mail)
# ---------------------------------------------------------------------------

def _fetch_open_invoices() -> list[dict]:
    invoices = []
    offset = None
    while True:
        params = {
            'status[in]': '["payment_due","not_paid"]',
            'limit': 100,
            'sort_by[asc]': 'date',
        }
        if offset:
            params['offset'] = offset
        resp = requests.get(
            f'{CHARGEBEE_BASE_URL}/invoices',
            auth=(CHARGEBEE_API_KEY, ''),
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        invoices.extend(item['invoice'] for item in body.get('list', []))
        offset = body.get('next_offset')
        if not offset:
            break
    return invoices


def _fetch_customer(customer_id: str) -> dict:
    resp = requests.get(
        f'{CHARGEBEE_BASE_URL}/customers/{customer_id}',
        auth=(CHARGEBEE_API_KEY, ''),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get('customer', {})


# ---------------------------------------------------------------------------
# Planhat: Company-Snapshot (gleiche Match-Strategie wie
# bot.py::_planhat_search_company, aber volles Objekt statt nur id/name)
# ---------------------------------------------------------------------------

def _planhat_headers() -> dict:
    return {'Authorization': f'Bearer {PLANHAT_API_TOKEN}'}


def _planhat_fetch_page(params: dict) -> list:
    try:
        resp = requests.get(f'{PLANHAT_BASE_URL}/companies', headers=_planhat_headers(),
                             params=params, timeout=10)
        if resp.ok and isinstance(resp.json(), list):
            return resp.json()
    except Exception as e:
        logger.warning(f"Planhat fetch failed: {e}")
    return []


def _planhat_find_company(customer_name: str, debit_number: str = '') -> dict | None:
    if not PLANHAT_API_TOKEN:
        return None

    if debit_number:
        for offset in range(0, 1000, 100):
            page = _planhat_fetch_page({'limit': 100, 'offset': offset})
            if not page:
                break
            match = next((c for c in page if str(c.get('externalId', '')) == str(debit_number)), None)
            if match:
                return match
            if len(page) < 100:
                break

    if customer_name:
        for offset in range(0, 1000, 100):
            page = _planhat_fetch_page({'limit': 100, 'offset': offset})
            if not page:
                break
            match = next((c for c in page if c.get('name', '').lower() == customer_name.lower()), None)
            if match:
                return match
            if len(page) < 100:
                break

    return None


def _planhat_user_name(user_id: str) -> str:
    if not user_id:
        return ''
    try:
        resp = requests.get(f'{PLANHAT_BASE_URL}/users/{user_id}', headers=_planhat_headers(), timeout=10)
        if resp.ok:
            return resp.json().get('name', '')
    except Exception as e:
        logger.warning(f"Planhat user fetch failed: {e}")
    return ''


def _enrich_from_planhat(customer_name: str, debit_number: str) -> dict:
    """Best-effort Planhat-Snapshot. Fehlende Felder bleiben defensiv leer."""
    company = _planhat_find_company(customer_name, debit_number)
    if not company:
        return {'found': False}

    owner_id = company.get('owner')
    owner_name = _planhat_user_name(owner_id) if owner_id else ''
    owner_email = ''
    if owner_name and 'xentral customer success' not in owner_name.lower():
        parts = owner_name.strip().lower().split()
        if len(parts) >= 2:
            owner_email = f"{parts[0]}.{parts[-1]}@xentral.com"

    custom = company.get('custom', {}) or {}
    return {
        'found': True,
        'planhat_id': company.get('_id'),
        'health': company.get('health'),
        'phase': company.get('phase') or custom.get('Phase'),
        'tier': custom.get('Tier') or custom.get('tier'),
        'owner_name': owner_name,
        'owner_email': owner_email,
    }


# ---------------------------------------------------------------------------
# Zendesk: offene Tickets — best effort, siehe Modul-Docstring
# ---------------------------------------------------------------------------

def _enrich_from_zendesk(company_name: str) -> dict:
    if not ZENDESK_API_TOKEN or not company_name:
        return {'open_tickets': None}
    try:
        resp = requests.get(
            f'{ZENDESK_BASE_URL}/search.json',
            auth=(f'{ZENDESK_EMAIL}/token', ZENDESK_API_TOKEN),
            params={'query': f'type:ticket status<solved organization:"{company_name}"'},
            timeout=15,
        )
        if resp.ok:
            body = resp.json()
            return {'open_tickets': body.get('count', len(body.get('results', [])))}
    except Exception as e:
        logger.warning(f"Zendesk search failed for '{company_name}': {e}")
    return {'open_tickets': None}


# ---------------------------------------------------------------------------
# Slack Helpers
# ---------------------------------------------------------------------------

def _slack_user_id_for_email(email: str) -> str | None:
    if not email:
        return None
    try:
        resp = slack_client.users_lookupByEmail(email=email)
        if resp.get('ok'):
            return resp['user']['id']
    except SlackApiError as e:
        logger.info(f"Slack lookup for {email} failed: {e}")
    return None


def _format_case_message(case: dict) -> str:
    lines = [f"*{case['customer_name']}*"]
    tier = case.get('tier')
    if tier:
        tier_note = " (wird bereits im Daily besprochen)" if str(tier) in ('1', '2') else ""
        lines.append(f"Tier {tier}{tier_note}")

    if case.get('csm_slack_user_id'):
        lines.append(f"Zuständiger CSM: <@{case['csm_slack_user_id']}>")
    elif case.get('owner_name'):
        lines.append(f"Zuständiger CSM: {case['owner_name']} (Slack-User nicht eindeutig gefunden)")
    else:
        lines.append("Zuständiger CSM: kein individueller CSM zugeordnet")

    lines.append("")
    for inv in case['invoices']:
        lines.append(f"Offene Rechnung: {inv['invoice_id']} — €{inv['amount_due']:,.2f} "
                      f"(Rechnungsdatum {inv['invoice_date']})")
    lines.append(f"Gesamtbetrag: €{case['overdue_amount_eur']:,.2f}")
    lines.append(f"Berechnetes Sperrdatum: {case['lock_date']}")
    lines.append("")

    if case.get('planhat_found'):
        bits = []
        if case.get('health') is not None:
            bits.append(f"Health {case['health']}")
        if case.get('phase'):
            bits.append(f'Phase "{case["phase"]}"')
        if case.get('open_tickets') is not None:
            bits.append(f"{case['open_tickets']} offene Zendesk-Tickets")
        lines.append(f"*Zusammenfassung:* {', '.join(bits) if bits else 'Keine weiteren Signale gefunden.'}")
    else:
        lines.append("*Zusammenfassung:* Kunde nicht in Planhat gefunden — bitte manuell prüfen.")

    lines.append("")
    lines.append("❌ auf diese Nachricht = Einspruch gegen die Sperre — der Bot fragt dich danach "
                 "im Thread nach einer kurzen Begründung.")
    return "\n".join(lines)


def _week_key(d: datetime) -> str:
    return d.strftime('%Y-%m-%d')


def _ensure_bot_in_channel(channel: str) -> None:
    """Bot tritt dem Channel bei, falls er noch nicht Mitglied ist (self-join
    funktioniert nur bei öffentlichen Channels — bei privaten Channels bleibt
    ein manuelles /invite nötig, siehe Warnung im Log)."""
    try:
        slack_client.conversations_join(channel=channel)
    except SlackApiError as e:
        logger.warning(f"could not auto-join channel {channel} — "
                       f"falls privat, bitte Bot manuell per /invite hinzufügen: {e}")


def _find_case_by_message(state: dict, channel: str, message_ts: str):
    """Sucht den OPOS-Fall (über alle Wochen) zu einer Slack-Nachricht."""
    for week_key, week in state.get('weeks', {}).items():
        if week.get('slack_channel_id') != channel:
            continue
        for case in week.get('cases', []):
            if case.get('slack_message_ts') == message_ts:
                return week_key, case
    return None, None


def _is_sufficient_justification(text: str) -> bool:
    text = (text or '').strip()
    return len(text.split()) >= MIN_JUSTIFICATION_WORDS and len(text) >= MIN_JUSTIFICATION_CHARS


# ---------------------------------------------------------------------------
# Echtzeit-Handler: ❌-Reaktion → Begründung anfordern → Begründung validieren
# (aufgerufen aus bot.py's reaction_added / message Event-Handlern)
# ---------------------------------------------------------------------------

def handle_opos_reaction(event: dict, client) -> bool:
    """❌ auf eine OPOS-Fall-Nachricht → fordert eine schriftliche Begründung
    im Thread an. Gibt True zurück, wenn das Event eine OPOS-Nachricht betraf
    (damit bot.py's eigene reaction_added-Logik nicht zusätzlich greift)."""
    if event.get('reaction') not in OBJECTION_EMOJI_NAMES:
        return False
    item = event.get('item', {})
    if item.get('type') != 'message':
        return False
    channel = item.get('channel', '')
    ts = item.get('ts', '')
    user_id = event.get('user', '')
    if not (channel and ts and user_id):
        return False

    state = _load_state()
    _, case = _find_case_by_message(state, channel, ts)
    if not case:
        return False  # keine OPOS-Fall-Nachricht — anderer Handler ist zuständig

    case.setdefault('objection_reasons', {})
    case.setdefault('pending_justification_users', [])

    if user_id in case['objection_reasons']:
        return True  # hat schon eine akzeptierte Begründung geliefert

    if user_id not in case['pending_justification_users']:
        case['pending_justification_users'].append(user_id)
        try:
            client.chat_postMessage(
                channel=channel,
                thread_ts=ts,
                text=(
                    f"<@{user_id}> Danke für deinen Einspruch (❌) gegen die Sperre von "
                    f"*{case['customer_name']}*. Bitte antworte in diesem Thread mit einer "
                    f"kurzen Begründung, warum diese Instanz NICHT gesperrt werden soll — "
                    f"ein, zwei Sätze reichen, nur ein paar Stichworte leider nicht."
                ),
            )
        except SlackApiError as e:
            logger.warning(f"justification prompt failed: {e}")

    _save_state(state)
    return True


def handle_opos_thread_reply(event: dict, client) -> bool:
    """Prüft, ob eine Thread-Antwort eine angeforderte Einspruchs-Begründung
    ist, und validiert deren Länge/Substanz. Gibt True zurück, wenn behandelt."""
    thread_ts = event.get('thread_ts')
    channel = event.get('channel', '')
    user_id = event.get('user', '')
    text = event.get('text', '')
    if not (thread_ts and channel and user_id) or event.get('bot_id'):
        return False

    state = _load_state()
    _, case = _find_case_by_message(state, channel, thread_ts)
    if not case:
        return False

    pending = case.get('pending_justification_users', [])
    if user_id not in pending:
        return False  # dieser User wurde nicht zu einer Begründung aufgefordert

    if _is_sufficient_justification(text):
        case.setdefault('objection_reasons', {})[user_id] = text.strip()
        pending.remove(user_id)
        try:
            client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                     text=f"✅ Danke <@{user_id}>, Begründung übernommen.")
        except SlackApiError as e:
            logger.warning(f"justification confirm failed: {e}")
    else:
        try:
            client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=(f"<@{user_id}> Das ist noch etwas knapp — magst du kurz ausführen, "
                      f"warum dieser Fall nicht gesperrt werden soll? "
                      f"(mind. {MIN_JUSTIFICATION_WORDS} Wörter)"),
            )
        except SlackApiError as e:
            logger.warning(f"justification nudge failed: {e}")

    _save_state(state)
    return True


# ---------------------------------------------------------------------------
# Hauptlogik
# ---------------------------------------------------------------------------

def run_opos_check() -> dict:
    now = datetime.now(timezone.utc)
    result = {'closeout': None, 'new_week': None}
    state = _load_state()
    _ensure_bot_in_channel(OPOS_CHANNEL_ID)

    # --- SCHRITT A: Vorwoche abschließen -----------------------------------
    previous_week_key = _week_key(now - timedelta(days=7))
    prev = state['weeks'].get(previous_week_key)
    if prev and not prev.get('closeout_delivered_at') and prev.get('cases'):
        gesperrt, einspruch, unklar = [], [], []
        for case in prev['cases']:
            # Ein ❌ allein reicht nicht — nur ein akzeptiertes objection_reasons-Eintrag
            # (siehe handle_opos_reaction/handle_opos_thread_reply) zählt als Einspruch.
            reasons = case.get('objection_reasons', {}) or {}
            pending = case.get('pending_justification_users', []) or []
            case['objected'] = bool(reasons)
            case['objected_by'] = list(reasons.keys())
            case['delivered_to_accounting'] = True
            if reasons:
                einspruch.append(case)
            elif pending:
                # Hat reagiert, aber nie eine ausreichende Begründung nachgeliefert.
                unklar.append(case)
            else:
                gesperrt.append(case)

        prefix = "🧪 *TEST — noch nicht live für Accounting*\n" if TEST_MODE else ""
        lines = [
            f"{prefix}📋 *OPOS-Sperrprüfung Abschluss — Woche {previous_week_key}*",
            "",
            f"{len(prev['cases'])} Fälle insgesamt · *{len(gesperrt)} werden gesperrt* · "
            f"*{len(einspruch)} Einspruch* (nicht sperren)"
            + (f" · *{len(unklar)} unklar* (❌ ohne Begründung)" if unklar else ""),
            "",
        ]
        lines += [
            f"❌ {c['customer_name']} — Begründung von "
            + ", ".join(f"<@{u}>: „{r[:120]}“" for u, r in c.get('objection_reasons', {}).items())
            for c in einspruch
        ]
        lines += [
            f"⚠️ {c['customer_name']} — {', '.join(f'<@{u}>' for u in c.get('pending_justification_users', []))} "
            f"hat reagiert, aber keine ausreichende Begründung geliefert — bitte manuell prüfen."
            for c in unklar
        ]
        try:
            slack_client.chat_postMessage(channel=prev['slack_channel_id'], text="\n".join(lines))
        except SlackApiError as e:
            logger.warning(f"closeout report post failed: {e}")

        prev['closeout_delivered_at'] = now.isoformat()
        result['closeout'] = {'week': previous_week_key, 'gesperrt': len(gesperrt),
                               'einspruch': len(einspruch), 'unklar': len(unklar)}

    # --- SCHRITT B: neue Woche eröffnen -------------------------------------
    this_week_key = _week_key(now)
    week_end = now + timedelta(days=6)

    already_posted = any(
        c.get('slack_message_ts') for c in state['weeks'].get(this_week_key, {}).get('cases', [])
    )
    if already_posted:
        result['new_week'] = {'skipped': True, 'reason': 'already posted this week'}
    else:
        invoices = _fetch_open_invoices()
        by_customer: dict[str, list[dict]] = {}
        for inv in invoices:
            by_customer.setdefault(inv['customer_id'], []).append(inv)

        cases = []
        for customer_id, invs in by_customer.items():
            min_date = min(datetime.fromtimestamp(i['date'], tz=timezone.utc) for i in invs)
            lock_date = min_date + timedelta(days=LOCK_DAYS_AFTER_INVOICE)
            if not (now <= lock_date <= week_end):
                continue

            try:
                customer = _fetch_customer(customer_id)
            except Exception as e:
                logger.warning(f"Chargebee customer fetch failed for {customer_id}: {e}")
                customer = {}
            company_name = customer.get('company') or \
                f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or customer_id
            debit_number = str(customer.get('cf_debit_number', '') or '')

            planhat = _enrich_from_planhat(company_name, debit_number)
            zendesk = _enrich_from_zendesk(company_name)
            csm_slack_id = _slack_user_id_for_email(planhat.get('owner_email', ''))

            total_amount = round(sum(i['amount_due'] for i in invs) / 100, 2)
            cases.append({
                'case_key': customer_id,
                'customer_name': company_name,
                'invoices': [{
                    'invoice_id': i['id'],
                    'amount_due': round(i['amount_due'] / 100, 2),
                    'invoice_date': datetime.fromtimestamp(i['date'], tz=timezone.utc).strftime('%Y-%m-%d'),
                } for i in invs],
                'overdue_amount_eur': total_amount,
                'lock_date': lock_date.strftime('%Y-%m-%d'),
                'tier': planhat.get('tier'),
                'planhat_found': planhat.get('found', False),
                'health': planhat.get('health'),
                'phase': planhat.get('phase'),
                'owner_name': planhat.get('owner_name'),
                'open_tickets': zendesk.get('open_tickets'),
                'csm_slack_user_id': csm_slack_id,
                'slack_message_ts': None,
                'objected': None,
                'objected_by': [],
                'delivered_to_accounting': False,
            })

        cases.sort(key=lambda c: -c['overdue_amount_eur'])

        summary_anchor_ts = None
        for case in cases:
            try:
                resp = slack_client.chat_postMessage(channel=OPOS_CHANNEL_ID, text=_format_case_message(case))
                case['slack_message_ts'] = resp['ts']
                if summary_anchor_ts is None:
                    summary_anchor_ts = resp['ts']
            except SlackApiError as e:
                logger.warning(f"case post failed for {case['customer_name']}: {e}")

        if summary_anchor_ts:
            total = sum(c['overdue_amount_eur'] for c in cases)
            deadline = (now + timedelta(days=6)).strftime('%d.%m.')
            summary_text = (
                f"📋 *Zusammenfassung — OPOS-Sperrprüfung Woche {this_week_key}*\n\n"
                f"{len(cases)} Fälle oben im Channel, Gesamtsumme offener Rechnungen: *€{total:,.2f}*\n\n"
                f"Widerspruchsfrist: bis Donnerstag, {deadline} um 18 Uhr. "
                f"❌-Reaktion auf die jeweilige Fall-Nachricht = Einspruch gegen die Sperre.\n\n"
                f"_Hinweis: Sperr-Regel (30 Tage nach ältester offener Rechnung) ist vorläufig._"
            )
            try:
                slack_client.chat_postMessage(channel=OPOS_CHANNEL_ID, thread_ts=summary_anchor_ts, text=summary_text)
            except SlackApiError as e:
                logger.warning(f"summary post failed: {e}")

        state['weeks'][this_week_key] = {
            'slack_channel_id': OPOS_CHANNEL_ID,
            'summary_anchor_ts': summary_anchor_ts,
            'cases': cases,
            'closeout_delivered_at': None,
        }
        result['new_week'] = {
            'case_count': len(cases),
            'total_amount': sum(c['overdue_amount_eur'] for c in cases),
        }

    state['last_friday_run'] = now.isoformat()
    _save_state(state)
    return result
