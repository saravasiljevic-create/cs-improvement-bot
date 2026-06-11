"""
CS Admin Bot — Q&A Handler

Beantwortet Fragen zu Chargebee, Planhat und Jira.

Zwei Modi:
1. Mit ANTHROPIC_API_KEY: Claude als Agent mit Tool-Use (natürlichsprachlich)
2. Ohne API-Key: Regelbasiert (erkennt Kundenname + Intent aus der Frage)
"""
import json
import logging
import re

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Intent-Erkennung (regelbasiert)
# ---------------------------------------------------------------------------

_INTENTS = {
    'subscription': re.compile(
        r'\b(plan|subscription|abo|vertrag|tarif|laufzeit|paket|billing|rechnung'
        r'|zahlweise|zahlungsweise|nächste.rechnung|invoice|mrr|preis|kosten)\b',
        re.IGNORECASE,
    ),
    'health': re.compile(
        r'\b(health|score|gesundheit|status|phase|churn|risiko|csm|owner|zuständig'
        r'|betreuer|expansion|onboarding|renewal|verlängerung)\b',
        re.IGNORECASE,
    ),
    'tasks': re.compile(
        r'\b(task|aufgabe|todo|offen|pending|to.do)\b',
        re.IGNORECASE,
    ),
    'invoices': re.compile(
        r'\b(rechnung|invoice|rechnungen|zahlung|bezahlt|unbezahlt|offen|due)\b',
        re.IGNORECASE,
    ),
    'tickets': re.compile(
        r'\b(ticket|jira|cs.ticket|feature|request|bug|issue|improvement)\b',
        re.IGNORECASE,
    ),
    'overview': re.compile(
        r'\b(überblick|übersicht|alles|komplett|info|information|detail|tell me|zeig)\b',
        re.IGNORECASE,
    ),
}

_COMPANY_SKIP = re.compile(
    r'\b(was|wie|wann|hat|haben|ist|sind|gibt|welch\w*|bitte|kannst|zeig|'
    r'aktuell\w*|akt\w*|der|die|das|den|dem|des|für|bei|von|aus|im|in|an|auf|'
    r'plan|subscription|abo|vertrag|health|score|rechnung|ticket|aufgabe|task|'
    r'status|info|detail|überblick|übersicht|kunden|kunde)\b',
    re.IGNORECASE,
)


def _detect_intent(text: str) -> list[str]:
    """Gibt eine geordnete Liste der erkannten Intents zurück."""
    found = []
    for name, pattern in _INTENTS.items():
        if pattern.search(text):
            found.append(name)
    return found or ['subscription']  # Default: Subscription


def _extract_customer_name(text: str) -> str:
    """Extrahiert einen Kundennamen aus einer Frage."""
    # "von X", "für X", "bei X", "zu X" → X als Name
    for prep_pattern in [
        r'(?:von|für|bei|zu|des|kunden?)\s+([A-ZÄÖÜ][^\?\.!,\n]{2,50}?)(?:\?|$|\.|,|!|\s+(?:hat|ist|haben|sind|gibt))',
        r'([A-ZÄÖÜ][A-Za-zäöüÄÖÜ\s&\-\.]{2,50}?(?:GmbH|AG|Ltd\.?|SE|KG|UG|LLC|GbR)(?:\s*&\s*Co\.?\s*KG)?)',
    ]:
        m = re.search(prep_pattern, text, re.IGNORECASE)
        if m:
            candidate = m.group(1).strip().rstrip('.,!?')
            # Zu kurz oder reine Stop-Words → überspringen
            if len(candidate) > 3 and not _COMPANY_SKIP.fullmatch(candidate):
                return candidate

    # Fallback: alle Wörter mit Großbuchstaben die nicht Stop-Words sind
    words = text.split()
    candidates = []
    for w in words:
        if w[0].isupper() and not _COMPANY_SKIP.fullmatch(w.rstrip('?,.')):
            candidates.append(w.rstrip('?.,!'))
    if candidates:
        return ' '.join(candidates[:3])  # Max 3 Wörter

    return ''


# ---------------------------------------------------------------------------
# Antwort-Formatter
# ---------------------------------------------------------------------------

def _fmt_subscription(data: dict) -> str:
    if 'error' in data:
        return f"⚠️ {data['error']}"
    subs = data.get('subscriptions', [])
    if not subs:
        return f"Kein aktiver Vertrag für *{data.get('company', '?')}* gefunden."

    lines = [f"📋 *{data.get('company', '?')}*"]
    lines.append(f"Chargebee: {data.get('chargebee_url', '')}")
    for s in subs[:3]:
        lines.append(f"\n*Subscription:* {s.get('url', s.get('id', ''))}")
        if s.get('plan_id'):
            lines.append(f"• Plan: `{s['plan_id']}` · {s.get('billing_period', '')}")
        if s.get('next_billing_at'):
            lines.append(f"• Nächste Rechnung: {s['next_billing_at']}")
        if s.get('current_term_end'):
            lines.append(f"• Vertragsende: {s['current_term_end']}")
        if s.get('addons'):
            lines.append(f"• Add-Ons: {', '.join(s['addons'])}")
        if s.get('active_coupons'):
            lines.append(f"• Aktiver Rabatt: {', '.join(s['active_coupons'])}")
        if s.get('mrr'):
            lines.append(f"• MRR: {s['mrr']}")
        lines.append(f"• Status: `{s.get('status', '–')}`")
    return '\n'.join(lines)


def _fmt_health(data: dict) -> str:
    if 'error' in data:
        return f"⚠️ {data['error']}"
    lines = [f"💚 *{data.get('name', '?')}* (Planhat)"]
    if data.get('health_score') is not None:
        lines.append(f"• Health Score: *{data['health_score']}*")
    if data.get('phase'):
        lines.append(f"• Phase: {data['phase']}")
    if data.get('csm_owner'):
        lines.append(f"• CSM: {data['csm_owner']}")
    if data.get('mrr'):
        lines.append(f"• MRR: {data['mrr']}")
    if data.get('churn_score') is not None:
        lines.append(f"• Churn-Score: {data['churn_score']}")
    if data.get('last_activity'):
        lines.append(f"• Letzte Aktivität: {data['last_activity']}")
    if data.get('planhat_url'):
        lines.append(f"• Planhat: {data['planhat_url']}")
    return '\n'.join(lines)


def _fmt_tasks(data) -> str:
    if isinstance(data, str):
        return f"📝 {data}"
    tasks = data if isinstance(data, list) else []
    if not tasks:
        return "Keine offenen Tasks."
    lines = ["📝 *Offene Tasks:*"]
    for t in tasks[:5]:
        due = f" (fällig: {t['due_date'][:10]})" if t.get('due_date') else ''
        owner = f" — {t['owner']}" if t.get('owner') else ''
        lines.append(f"• {t.get('title', '?')}{due}{owner}")
    return '\n'.join(lines)


def _fmt_invoices(data) -> str:
    if isinstance(data, str):
        return f"🧾 {data}"
    invoices = data if isinstance(data, list) else []
    if not invoices:
        return "Keine Rechnungen gefunden."
    lines = ["🧾 *Letzte Rechnungen:*"]
    for inv in invoices[:5]:
        status_icon = "✅" if inv.get('status') == 'paid' else "⏳"
        lines.append(f"• {status_icon} {inv.get('date', '?')} — {inv.get('total', '?')} ({inv.get('status', '?')}) {inv.get('url', '')}")
    return '\n'.join(lines)


def _fmt_tickets(data) -> str:
    if isinstance(data, str):
        return f"🎫 {data}"
    tickets = data if isinstance(data, list) else []
    if not tickets:
        return "Keine CS-Tickets gefunden."
    lines = ["🎫 *CS-Tickets:*"]
    for t in tickets[:5]:
        lines.append(f"• [{t['key']}]({t.get('url', '')}) {t['summary']} — `{t['status']}`")
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Haupt-Handler
# ---------------------------------------------------------------------------

def _rule_based_answer(question: str) -> str:
    """Beantwortet eine Frage regelbasiert ohne LLM."""
    import skills as skill_registry

    customer = _extract_customer_name(question)
    if not customer:
        return (
            "Ich konnte keinen Kundennamen in deiner Frage erkennen.\n"
            "Beispiel: _Welchen Plan hat Heavn Lights GmbH?_"
        )

    intents = _detect_intent(question)
    logger.info(f"Chat rule-based: customer={customer!r} intents={intents}")

    results = []

    for intent in intents:
        if intent in ('subscription', 'overview', 'invoices') and 'subscription' in intents or intent == 'subscription':
            raw = skill_registry.execute('chargebee_customer_lookup', {'company_name': customer}, {})
            try:
                data = json.loads(raw)
            except Exception:
                data = {'error': raw}
            results.append(_fmt_subscription(data))
            # Rechnungen nur wenn explizit gefragt
            if 'invoices' in intents:
                cid = data.get('customer_id', '') if isinstance(data, dict) else ''
                if cid:
                    inv_raw = skill_registry.execute('chargebee_invoice_history', {'customer_id': cid, 'limit': 5}, {})
                    try:
                        results.append(_fmt_invoices(json.loads(inv_raw)))
                    except Exception:
                        pass
            break

    for intent in intents:
        if intent == 'health':
            raw = skill_registry.execute('planhat_customer_info', {'company_name': customer}, {})
            try:
                results.append(_fmt_health(json.loads(raw)))
            except Exception:
                results.append(_fmt_health({'error': raw}))
        elif intent == 'tasks':
            raw = skill_registry.execute('planhat_open_tasks', {'company_name': customer}, {})
            try:
                results.append(_fmt_tasks(json.loads(raw)))
            except Exception:
                results.append(_fmt_tasks(raw))
        elif intent == 'tickets':
            raw = skill_registry.execute('jira_search_customer_tickets', {'company_name': customer}, {})
            try:
                results.append(_fmt_tickets(json.loads(raw)))
            except Exception:
                results.append(_fmt_tickets(raw))

    # Wenn kein spezifischer Intent → nur Subscription
    if not results:
        raw = skill_registry.execute('chargebee_customer_lookup', {'company_name': customer}, {})
        try:
            results.append(_fmt_subscription(json.loads(raw)))
        except Exception:
            results.append(raw)

    return '\n\n'.join(results)


def answer(question: str, user_name: str = '') -> str:
    """Beantwortet eine Frage — mit Claude wenn API-Key vorhanden, sonst regelbasiert."""
    import os
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')

    if api_key:
        # Mit Claude (natürlichsprachlich, multi-turn Tool-Use)
        return _llm_answer(question, user_name, api_key)
    else:
        # Regelbasiert (kein API-Key nötig)
        return _rule_based_answer(question)


def _llm_answer(question: str, user_name: str, api_key: str) -> str:
    """Claude-gestützte Antwort mit Tool-Use."""
    SYSTEM_PROMPT = """Du bist der CS Admin Bot von Xentral — interner Assistent für das CS Admin Team.
Du hilfst bei Fragen zu Subscriptions (Chargebee), Kundenstatus (Planhat), CS-Tickets (Jira) und CS-Prozessen.
Antworte auf Deutsch, freundlich und präzise. Nutze die Tools um Daten zu holen."""

    try:
        from anthropic import Anthropic
        import skills as skill_registry

        client = Anthropic(api_key=api_key)
        tools = skill_registry.load_all()
        prefix = f"{user_name}: " if user_name else ""
        messages = [{"role": "user", "content": f"{prefix}{question}"}]

        for _ in range(5):
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )
            if response.stop_reason == "end_turn":
                texts = [b.text for b in response.content if hasattr(b, 'text')]
                return '\n'.join(texts).strip() or "Keine Antwort."
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        result = skill_registry.execute(block.name, block.input, {})
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
            else:
                break

        texts = [b.text for b in response.content if hasattr(b, 'text')]
        return '\n'.join(texts).strip() or "Keine Antwort."
    except Exception as e:
        logger.warning(f"LLM answer failed, falling back to rule-based: {e}")
        return _rule_based_answer(question)
