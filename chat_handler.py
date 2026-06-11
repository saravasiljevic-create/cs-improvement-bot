"""
CS Admin Bot — Conversational AI Handler

Verwendet Claude mit Tool-Use um natürlichsprachliche Fragen zu
Chargebee, Planhat, Jira und CS-Prozessen zu beantworten.
"""
import json
import logging
import os

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Du bist der CS Admin Bot von Xentral — ein interner Assistent für das CS Admin Team.

Du hilfst bei Fragen zu:
- Kundensubscriptions und Verträgen (Chargebee)
- Kundenstatus, Health Scores und Tasks (Planhat)
- CS-Tickets und Feature Requests (Jira)
- CS Admin Prozessen (Vertragsanpassungen, Improvement Requests, Workflows)

Dein Ton: freundlich, direkt, professionell. Antworte auf Deutsch.
Wenn du Daten aus externen Systemen brauchst, nutze die verfügbaren Tools.
Wenn du etwas nicht weißt oder nicht findest, sag es klar.

CS Admin Team: Mirjam Köberlein (Lead), Linda Litzkow, Sara Vasiljevic.

Wichtig: Du machst KEINE Änderungen in Chargebee oder anderen Systemen.
Du liest Daten und beantwortest Fragen — Änderungen führt das CS Admin Team selbst durch."""

MAX_TOOL_ROUNDS = 5  # Max. Runden Tool-Calls um Endlosschleifen zu vermeiden


def answer(question: str, user_name: str = '') -> str:
    """Beantwortet eine Frage mit Claude + Tool-Use.

    Gibt die Antwort als String zurück.
    Fällt bei Problemen auf eine einfache Fehlermeldung zurück.
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return ":warning: ANTHROPIC_API_KEY nicht konfiguriert."

    try:
        from anthropic import Anthropic
        import skills as skill_registry

        client = Anthropic(api_key=api_key)
        tools = skill_registry.load_all()

        context_prefix = f"{user_name}: " if user_name else ""
        messages = [{"role": "user", "content": f"{context_prefix}{question}"}]

        for round_num in range(MAX_TOOL_ROUNDS):
            response = client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
            )

            # Keine Tool-Calls → fertig
            if response.stop_reason == "end_turn":
                return _extract_text(response)

            # Tool-Calls ausführen
            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        logger.info(f"Chat: Tool '{block.name}' mit params={block.input}")
                        result = skill_registry.execute(block.name, block.input, {})
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                # Antwort + Tool-Ergebnisse in Konversation hinzufügen
                messages.append({"role": "assistant", "content": response.content})
                messages.append({"role": "user", "content": tool_results})
                continue

            # Anderer Stop-Grund → abbrechen
            break

        return _extract_text(response)

    except Exception as e:
        logger.warning(f"Chat handler error: {e}")
        return f":warning: Fehler beim Verarbeiten der Anfrage: {e}"


def _extract_text(response) -> str:
    """Extrahiert den Textinhalt aus einer Claude-Antwort."""
    texts = []
    for block in response.content:
        if hasattr(block, 'text'):
            texts.append(block.text)
    return '\n'.join(texts).strip() or "Keine Antwort erhalten."
