# CS Admin Bot — Eigenen Skill hinzufügen

Der Bot lädt automatisch alle `.py`-Dateien aus dem `skills/`-Verzeichnis.
Jede Datei kann einen oder mehrere **Tools** definieren, die Claude nutzen kann.

---

## Minimales Beispiel

```python
# skills/mein_skill.py

TOOLS = [
    {
        "name": "mein_tool",
        "description": "Was dieses Tool macht — Claude liest das und entscheidet wann es nützlich ist.",
        "input_schema": {
            "type": "object",
            "properties": {
                "kunde": {"type": "string", "description": "Firmenname"},
            },
            "required": ["kunde"],
        },
    },
]

def execute(tool_name: str, params: dict, context: dict) -> str:
    """Wird aufgerufen wenn Claude das Tool nutzen will. Gibt immer einen String zurück."""
    if tool_name == "mein_tool":
        kunde = params.get("kunde", "")
        # ... Daten holen, berechnen, etc. ...
        return f"Ergebnis für {kunde}: ..."
    return f"Unbekanntes Tool: {tool_name}"
```

---

## Interface

| Feld | Typ | Beschreibung |
|---|---|---|
| `TOOLS` | `list[dict]` | Liste von Tool-Definitionen im Anthropic-Format |
| `execute(tool_name, params, context)` | `str` | Führt das Tool aus, gibt Ergebnis als String zurück |

### Tool-Definition (Anthropic-Format)
```python
{
    "name": "eindeutiger_tool_name",      # Nur a-z, 0-9, Unterstriche
    "description": "Was es macht",        # Claude liest das — je präziser desto besser
    "input_schema": {                      # JSON Schema der Parameter
        "type": "object",
        "properties": { ... },
        "required": [ ... ],
    },
}
```

---

## Vorhandene Skills als Referenz

| Datei | Tools | Beschreibung |
|---|---|---|
| `chargebee_skill.py` | `chargebee_customer_lookup`, `chargebee_invoice_history` | Chargebee Subscription-Daten |
| `planhat_skill.py` | `planhat_customer_info`, `planhat_open_tasks` | Planhat Health & Tasks |
| `jira_skill.py` | `jira_search_customer_tickets` | CS Jira-Board Suche |

---

## Tipps

- **Tool-Namen** müssen im gesamten Bot eindeutig sein → Präfix nutzen, z.B. `zendesk_tickets`
- **Description** ist entscheidend — Claude entscheidet anhand der Description ob es das Tool nutzt
- **Fehlerbehandlung**: Bei Problemen Fehlermeldung als String zurückgeben (kein Exception-Raise)
- **Logging**: `import logging; logger = logging.getLogger(__name__)`
- Der Skill hat Zugriff auf alle Umgebungsvariablen (API-Keys via `os.environ.get(...)`)

---

## Neuen Skill deployen

1. Datei in `skills/` anlegen
2. `git add skills/mein_skill.py && git commit -m "Add Mein Skill" && git push`
3. Cloud Build deployt automatisch — nach ~5 Min ist der Skill im Bot aktiv

Fragen? → Sara Vasiljevic
