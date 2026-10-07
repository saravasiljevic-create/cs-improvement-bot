"""
Status-Fragen in #ask-cs-admin direkt aus Chargebee beantworten (read-only).

Typische Fragen aus dem Channel: „Könnt ihr mir den Stand zur Rechnung 68392 mitteilen?",
„Hat Nordica heute das Upgrade auf Business bekommen?", „Warum sind die zwei Rechnungen noch
posted?". Der Bot liefert die Fakten aus Chargebee in den Thread. Er entscheidet nichts und
ändert nichts — die Bewertung bleibt beim CS Admin Team.
"""
import logging
import os
import re
from datetime import datetime, timezone

import requests

from text_utils import normalize_slack_text, strip_urls

logger = logging.getLogger(__name__)

CHARGEBEE_SITE = os.environ.get('CHARGEBEE_SITE', 'xentral-dach')

# ---------------------------------------------------------------------------
# Erkennung
# ---------------------------------------------------------------------------

_INVOICE_LINK_RE = re.compile(r'chargebee\.com/d/invoices/(?P<id>[A-Za-z0-9_\-]+)', re.IGNORECASE)
# „Rechnung 68392", „Rechnungen 76999 und 77958", „RE 74766", „invoice #76897", „Rg. 75270"
_INVOICE_NUMBER_RE = re.compile(
    r'\b(?:rechnung(?:en|snummer|snr\.?)?|re\.?|rg\.?|invoices?|beleg)\s*(?:nr\.?|nummer)?\s*'
    r'[#:]?\s*(\d{4,6}(?:\s*(?:,|und|&|/)\s*#?\d{4,6})*)',
    re.IGNORECASE,
)
_STATUS_WORDS_RE = re.compile(
    r'\b(?:stand|status|offen\w*|bezahlt|beglichen|eingegangen|eingezogen|f[äa]llig|posted|'
    r'gemahnt|mahnung|storniert|gutschrift|bekommen|umgestellt|aktiv|hinterlegt|'
    r'upgrade|downgrade|verl[äa]ngert|renewal|laufzeit)\b',
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r'\?|\b(?:k[öo]nnt\s+ihr\s+(?:mir\s+)?(?:bitte\s+)?(?:den\s+)?(?:aktuellen\s+)?(?:stand|status)|'
    r'mitteilen|nachschauen|schauen\s+ob|pr[üu]fen\s+ob|wisst\s+ihr|wei[ßs]\s+jemand)\b',
    re.IGNORECASE,
)
# Aufträge statt Fragen: dann ist es eine Vertragsanpassung o. Ä., kein Status
_ORDER_RE = re.compile(
    r'\bbitte\b[^.?!\n]{0,80}\b(?:umstellen|anlegen|einrichten|hinterlegen|vornehmen|einstellen|'
    r'freischalten|stornieren|gutschreiben|erstellen|[äa]ndern|anpassen|k[üu]ndigen)\b',
    re.IGNORECASE,
)


def extract_invoice_ids(text: str) -> list[str]:
    """Rechnungsnummern aus Links und aus „Rechnung 12345"-Formulierungen, in Reihenfolge, ohne Duplikate."""
    text = normalize_slack_text(text or '')
    ids: list[str] = []
    for m in _INVOICE_LINK_RE.finditer(text):
        ids.append(m.group('id'))
    for m in _INVOICE_NUMBER_RE.finditer(strip_urls(text)):
        ids.extend(re.findall(r'\d{4,6}', m.group(1)))
    seen, out = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out[:3]


_OPEN_INVOICE_RE = re.compile(
    r'\b(?:offen\w*|unbezahlt\w*|ausstehend\w*|[üu]berf[äa]llig\w*|nicht\s+bezahlt\w*|r[üu]ckst[äa]nd\w*|'
    r'zahlungsr[üu]ckstand\w*|schuld\w*|f[äa]llig\w*)\b[^.?!\n]{0,40}\b(?:rechnung\w*|posten|betr[äa]g\w*|zahlung\w*|invoices?)\b'
    r'|\b(?:rechnung\w*|posten|invoices?)\b[^.?!\n]{0,40}\b(?:offen\w*|unbezahlt\w*|ausstehend\w*|[üu]berf[äa]llig\w*|nicht\s+bezahlt)\b'
    r'|\bopen\s+invoices?\b|\boffene\s+posten\b',
    re.IGNORECASE,
)


def detect_open_invoice_question(text: str) -> bool:
    """„Haben wir offene Rechnungen bei X?“, „Ist bei X noch etwas unbezahlt?“ (ohne Rechnungsnummer)."""
    t = normalize_slack_text(text or '')
    return bool(_OPEN_INVOICE_RE.search(t)) and not extract_invoice_ids(t)


def open_invoices_reply(text: str, lookup) -> str | None:
    """Alle offenen Rechnungen des Kunden (gestellt, fällig oder nicht bezahlt) mit Summe.
    None, wenn kein eindeutiger Kunde gefunden wurde."""
    from text_utils import extract_company_name
    parsed = {'customer_name': question_customer_name(text)}
    sub = lookup(parsed, text)
    if not sub or sub.get('multiple_links'):
        return None
    raw = (_cb(f"subscriptions/{sub.get('subscription_id')}") or {}).get('subscription') or {}
    cid = raw.get('customer_id') or sub.get('customer_id')
    if not cid:
        return None
    cust = (_cb(f"customers/{cid}") or {}).get('customer', {})
    name = cust.get('company') or sub.get('company') or parsed['customer_name']
    curl = f"https://{CHARGEBEE_SITE}.chargebee.com/d/customers/{cid}"
    data = _cb('invoices', {'customer_id[is]': cid, 'status[in]': '["payment_due","not_paid","posted"]',
                            'limit': 20, 'sort_by[asc]': 'date'}) or {}
    invs = [x['invoice'] for x in data.get('list', [])]
    head = f":receipt: *Offene Rechnungen · <{curl}|{name}>* (Deb.-Nr. {cust.get('cf_debit_number', '–')})"
    if not invs:
        body = "Aktuell sind *keine offenen Rechnungen* in Chargebee hinterlegt."
    else:
        lines = []
        for i in invs:
            url = f"https://{CHARGEBEE_SITE}.chargebee.com/d/invoices/{i['id']}"
            line = (f"• <{url}|{i['id']}> vom {_d(i.get('date'))}: offen *{_eur(i.get('amount_due'))}*"
                    f" von {_eur(i.get('total'))} · {_INVOICE_STATUS_DE.get(i.get('status'), i.get('status'))}")
            if i.get('due_date'):
                line += f" · fällig {_d(i.get('due_date'))}"
            if i.get('dunning_status'):
                line += f" · Mahnlauf `{i['dunning_status']}`"
            lines.append(line)
        total = sum(i.get('amount_due') or 0 for i in invs)
        body = '\n'.join(lines) + f"\n*Summe offen: {_eur(total)}* ({len(invs)} Rechnung{'en' if len(invs) != 1 else ''})"
    return (f"{head}\n{body}\n\n"
            "_Automatisch aus Chargebee, nur lesend. Falsche Antwort? `#bot-stop` in den Thread._")


def detect_status_question(text: str) -> bool:
    """True, wenn der Post eine Status-Frage ist (Frage + Status-Wort), kein Auftrag."""
    t = normalize_slack_text(text or '')
    if not _QUESTION_RE.search(t) or not _STATUS_WORDS_RE.search(t):
        return False
    if _ORDER_RE.search(t) and '?' not in t:
        return False
    return True


# ---------------------------------------------------------------------------
# Chargebee (read-only)
# ---------------------------------------------------------------------------

def _cb(path: str, params: dict | None = None) -> dict | None:
    key = os.environ.get('CHARGEBEE_API_KEY', '')
    if not key:
        return None
    try:
        resp = requests.get(
            f"https://{CHARGEBEE_SITE}.chargebee.com/api/v2/{path}",
            params=params or {}, auth=(key, ''), timeout=10,
        )
        if resp.ok:
            return resp.json()
        logger.info(f"Status CB {path}: {resp.status_code}")
    except Exception as e:
        logger.warning(f"Status CB {path} failed: {e}")
    return None


def _d(ts) -> str:
    if not ts:
        return '–'
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone().strftime('%d.%m.%Y')


def _de_date(iso: str | None) -> str:
    """'2026-11-05' → '05.11.2026'"""
    m = re.match(r'(\d{4})-(\d{2})-(\d{2})', iso or '')
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else ''


def _eur(cents) -> str:
    return f"{(cents or 0) / 100:,.2f} €".replace(',', 'X').replace('.', ',').replace('X', '.')


_INVOICE_STATUS_DE = {
    'paid': 'bezahlt', 'posted': 'gestellt, noch nicht fällig', 'payment_due': 'fällig, offen',
    'not_paid': 'nicht bezahlt', 'voided': 'storniert', 'pending': 'Entwurf (noch nicht gestellt)',
}
_TXN_STATUS_DE = {
    'success': 'erfolgreich', 'failure': 'fehlgeschlagen', 'in_progress': 'läuft',
    'late_failure': 'nachträglich fehlgeschlagen', 'voided': 'storniert', 'needs_attention': 'prüfen',
}


def invoice_status_lines(invoice_id: str, customers: list | None = None) -> list[str]:
    data = _cb(f"invoices/{invoice_id}")
    if not data or 'invoice' not in data:
        return [f"*Rechnung {invoice_id}:* in Chargebee nicht gefunden."]
    inv = data['invoice']
    url = f"https://{CHARGEBEE_SITE}.chargebee.com/d/invoices/{inv['id']}"
    status = _INVOICE_STATUS_DE.get(inv.get('status'), inv.get('status'))
    lines = [
        f"*Rechnung <{url}|{inv['id']}>* vom {_d(inv.get('date'))}: *{status}*",
        f"• Betrag {_eur(inv.get('total'))} brutto · bezahlt {_eur(inv.get('amount_paid'))}"
        f" · gutgeschrieben {_eur((inv.get('credits_applied') or 0) + (inv.get('amount_adjusted') or 0))}"
        f" · offen *{_eur(inv.get('amount_due'))}*",
    ]
    if inv.get('due_date') and inv.get('status') in ('payment_due', 'not_paid', 'posted'):
        lines.append(f"• Fällig am {_d(inv.get('due_date'))}")
    if inv.get('dunning_status'):
        lines.append(f"• Mahnlauf: `{inv['dunning_status']}`")
    for p in (inv.get('linked_payments') or [])[:4]:
        lines.append(
            f"• Zahlung {_d(p.get('txn_date'))}: {_eur(p.get('applied_amount'))} "
            f"({_TXN_STATUS_DE.get(p.get('txn_status'), p.get('txn_status'))})"
        )
    for cn in (inv.get('issued_credit_notes') or [])[:3]:
        lines.append(f"• Gutschrift {cn.get('cn_id')}: {_eur(cn.get('cn_total'))} ({cn.get('cn_status')})")
    sched = _cb(f"invoices/{inv['id']}/payment_schedules") or {}
    entries = [e for s in (sched.get('payment_schedules') or []) for e in s.get('schedule_entries', [])]
    if entries:
        open_e = [e for e in entries if e.get('status') != 'paid']
        nxt = open_e[0] if open_e else None
        lines.append(
            f"• Ratenplan: {len(entries)} Raten, davon {len(entries) - len(open_e)} bezahlt"
            + (f" · nächste Rate {_eur(nxt.get('amount'))} am {_d(nxt.get('date'))}" if nxt else '')
        )
    if inv.get('customer_id'):
        if customers is not None:
            customers.append(inv['customer_id'])
        cust = (_cb(f"customers/{inv['customer_id']}") or {}).get('customer', {})
        if cust.get('company'):
            lines.append(f"• Kunde: {cust['company']} (Deb.-Nr. {cust.get('cf_debit_number', '–')})")
    return lines


def subscription_status_lines(subscription: dict, customers: list | None = None) -> list[str]:
    """Kurzer Stand einer Subscription: Plan, Preis, Status, Laufzeit, geplante Änderungen, offene Posten."""
    sub_id = subscription.get('subscription_id')
    raw = (_cb(f"subscriptions/{sub_id}") or {}).get('subscription') if sub_id else None
    if not raw:
        return []
    url = f"https://{CHARGEBEE_SITE}.chargebee.com/d/subscriptions/{sub_id}"
    plan = next((i for i in raw.get('subscription_items', []) if i.get('item_type') == 'plan'), {})
    lines = [
        f"*Subscription <{url}|{sub_id}>*"
        + (f" · {subscription.get('company')}" if subscription.get('company') else '')
        + f": `{raw.get('status')}`",
        f"• Plan `{plan.get('item_price_id', '–')}` zu {_eur(plan.get('unit_price'))} netto"
        f" · Abrechnung alle {raw.get('billing_period', 1)} {'Jahr(e)' if raw.get('billing_period_unit') == 'year' else 'Monat(e)'}",
        f"• Verlängerungsdatum: {_de_date(raw.get('cf_renewal_date')) or _d(raw.get('current_term_end'))}"
        f" · nächste Rechnung {_d(raw.get('next_billing_at'))}",
    ]
    if raw.get('cancelled_at'):
        lines.append(f"• Gekündigt zum {_d(raw.get('cancelled_at'))}")
    if raw.get('has_scheduled_changes'):
        sch = (_cb(f"subscriptions/{sub_id}/retrieve_with_scheduled_changes") or {}).get('subscription', {})
        nplan = next((i for i in sch.get('subscription_items', []) if i.get('item_type') == 'plan'), {})
        lines.append(
            f"• Geplante Änderung: `{nplan.get('item_price_id', '?')}` zu {_eur(nplan.get('unit_price'))}"
        )
    ramps = (_cb('ramps', {'subscription_id[is]': sub_id, 'status[is]': 'scheduled'}) or {}).get('list', [])
    for r in ramps[:2]:
        rp = r.get('ramp', {})
        lines.append(f"• Ramp geplant zum {_d(rp.get('effective_from'))}")
    if raw.get('customer_id') and customers is not None:
        customers.append(raw['customer_id'])
    if raw.get('customer_id'):
        inv = _cb('invoices', {'customer_id[is]': raw['customer_id'],
                              'status[in]': '["payment_due","not_paid"]', 'limit': 5}) or {}
        open_inv = [x['invoice'] for x in inv.get('list', [])]
        if open_inv:
            lines.append(
                "• Offene Rechnungen: " + ', '.join(
                    f"{i['id']} ({_eur(i.get('amount_due'))}, {_INVOICE_STATUS_DE.get(i.get('status'), i.get('status'))})"
                    for i in open_inv)
            )
        else:
            lines.append("• Keine offenen Rechnungen")
    return lines


_NAME_AFTER_RE = re.compile(
    r'\b(?:ob|f[üu]r|bei|beim|vom|von|der|des|zum|zur|zu|kunde[n]?|kundin)\s+(?=\S)', re.IGNORECASE)
_NAME_STOP = re.compile(
    r'^(?:noch|aktuell|gerade|schon|bitte|eigentlich|mal|denn|jetzt|heute|ist|sind|hat|haben|gibt|'
    r'wurde|werden|wird|kann|k[öo]nnt|offen\w*|bezahlt|f[äa]llig\w*|eingegangen|aktiv|gek[üu]ndigt|'
    r'im|in|an|auf|mit|seit|bis|und|oder)$', re.IGNORECASE)
_NAME_SKIP_FIRST = re.compile(r'^(?:kunde[n]?|kundin|firma|dem|den|die|das|der|des|einem|einer)$', re.IGNORECASE)


def _guess_name(text: str) -> str | None:
    """Kundenname aus einer Frage: Teil nach der letzten Präposition („… der otom Group?“,
    „… beim Kunden Nordica Coffee GmbH?“), bis zum ersten Füll-/Statuswort, höchstens 6 Wörter.
    Kleinschreibung am Anfang ist erlaubt (otom, wev …)."""
    t = strip_urls(normalize_slack_text(text or ''))
    t = re.sub(r'<@[A-Z0-9]+>|@\S+\s+(?:Admin\s+)?Bot\b', ' ', t)
    cands = []
    for m in _NAME_AFTER_RE.finditer(t):
        rest = re.split(r'[?!.,;:\n]', t[m.end():], maxsplit=1)[0]
        words = []
        for w in rest.split():
            if not words and _NAME_SKIP_FIRST.match(w):
                continue
            if _NAME_STOP.match(w) or (words and _STATUS_WORDS_RE.fullmatch(w)) or len(words) >= 6:
                break
            words.append(w)
        if words and not _STATUS_WORDS_RE.fullmatch(words[0]):
            cands.append(' '.join(words))
    if not cands:
        return None
    # Mit Rechtsform: der längste Kandidat, der auf GmbH/AG/... endet („ob Feines von Hemmen GmbH“
    # statt „Hemmen GmbH“). Ohne Rechtsform: der letzte („Renewal date der otom Group“ → „otom Group“).
    legal = [c for c in cands if re.search(r'\b(?:GmbH|AG|KG|UG|SE|Ltd\.?|LLC|Inc\.?|GbR|e\.\s?K\.?|OHG)$', c)]
    legal = [c for c in legal if not re.search(r'\s(?:bei|beim|f[üu]r|zum|zur|der|des|ob|kunde[n]?)\s', c, re.IGNORECASE)] or legal
    return max(legal, key=len) if legal else cands[-1]


def question_customer_name(text: str) -> str:
    """Bester Kundenname für Status-/Rechnungsfragen: erst die Fragen-Logik, dann die Rechtsform-Suche."""
    from text_utils import extract_company_name
    return _guess_name(text) or extract_company_name(text) or ''


def instance_status_lines(customer_id: str) -> list[str]:
    """Instanz-, Lizenz- und Vertragsinfos aus dem CSM-MCP (`customer_lookup`, Instance Manager +
    Planhat), gesucht über die Debitorennummer. Leer ohne Token oder ohne eindeutigen Treffer."""
    from sandbox_handler import csm_customer_lookup
    cust = (_cb(f"customers/{customer_id}") or {}).get('customer', {})
    query = str(cust.get('cf_debit_number') or '') or cust.get('company') or ''
    matches = csm_customer_lookup(query, limit=2) if query else []
    if len(matches) != 1:
        return []
    m = matches[0]
    lines = ["*Instanz & Vertrag* (Instance Manager / Planhat):"]
    if m.get('instance_url'):
        status = ' · '.join(x for x in (
            f"Version {m['xentral_version']}" if m.get('xentral_version') else '',
            f"Lizenz `{m['license_status']}`" if m.get('license_status') else '',
            f"Instanz `{m['instance_status']}`" if m.get('instance_status') else '',
        ) if x)
        lines.append(f"• {m['instance_url']}" + (f" · {status}" if status else ''))
    if m.get('serial'):
        lines.append(f"• Serial: {m['serial']}")
    lb = ' · '.join(x for x in (
        f"Leistungsbeschreibung {m['leistungsbeschreibung']}" if m.get('leistungsbeschreibung') else '',
        f"Servicepaket {m['cs_package']}" if m.get('cs_package') else '',
    ) if x)
    if lb:
        lines.append(f"• {lb}")
    sandboxes = m.get('sandbox_instances') or []
    if sandboxes:
        for sb in sandboxes[:2]:
            lines.append(
                f"• Sandbox: {sb.get('instance_url') or 'noch nicht im Instance Manager'}"
                f" · Abo `{sb.get('subscription_status', '?')}`"
            )
    elif m.get('sandbox_note'):
        lines.append(f"• Sandbox: {m['sandbox_note']}")
    else:
        lines.append("• Sandbox: keine Sandbox-Lizenz hinterlegt")
    if m.get('owner_nickname'):
        lines.append(f"• CSM/Owner: {m['owner_nickname']}")
    return lines if len(lines) > 1 else []


def build_status_reply(text: str, lookup) -> str | None:
    """Antworttext für eine Status-Frage oder None, wenn nichts Eindeutiges gefunden wurde.

    `lookup(parsed, text)` ist die Kundensuche des Bots (Links → CSM-MCP → Name) und liefert
    eine Subscription; sie wird nur genutzt, wenn keine Rechnungsnummer im Post steht.
    """
    sections: list[list[str]] = []
    customers: list[str] = []
    for inv_id in extract_invoice_ids(text):
        sections.append(invoice_status_lines(inv_id, customers))
    if not sections:
        from text_utils import extract_company_name
        parsed = {'customer_name': question_customer_name(text)}
        sub = lookup(parsed, text)
        if not sub or sub.get('multiple_links'):
            return None
        lines = subscription_status_lines(sub, customers)
        if lines:
            sections.append(lines)
    if not sections:
        return None
    # Instanz-/Lizenzstand aus dem CSM-MCP, einmal je Kunde (max. 2)
    source = 'Chargebee'
    for cid in list(dict.fromkeys(customers))[:2]:
        try:
            inst = instance_status_lines(cid)
        except Exception as e:
            logger.warning(f"instance_status_lines({cid}) failed: {e}")
            inst = []
        if inst:
            sections.append(inst)
            source = 'Chargebee und Instance Manager'
    body = '\n\n'.join('\n'.join(s) for s in sections)
    return (
        f":mag: *Stand aus {source}* (automatisch, nur lesend):\n\n"
        f"{body}\n\n"
        "_Das CS Admin Team schaut bei Bedarf noch drauf. Falsche Antwort? `#bot-stop` in den Thread._"
    )
