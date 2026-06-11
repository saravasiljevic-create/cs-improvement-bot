"""
CS Admin Bot Skill Registry
===========================

Jede Datei in diesem Verzeichnis ist ein Skill.
Skills stellen Claude Werkzeuge (Tools) bereit, mit denen er Daten aus
externen Systemen abfragen oder Aktionen ausführen kann.

Einen neuen Skill anlegen — 3 Schritte:
1. Neue Datei anlegen: skills/mein_skill.py
2. TOOLS-Liste definieren (Anthropic Tool-Format)
3. execute(tool_name, params, context) implementieren

Beispiel → skills/example_skill.py

Alle Skills hier werden automatisch geladen. Kein weiterer Code nötig.
"""
import importlib
import logging
import os

logger = logging.getLogger(__name__)

_registry: dict = {}  # tool_name → (skill_module, tool_definition)


def load_all() -> list[dict]:
    """Lädt alle Skills aus diesem Verzeichnis und gibt alle Tool-Definitionen zurück."""
    global _registry
    _registry = {}
    tools = []

    skills_dir = os.path.dirname(__file__)
    for filename in sorted(os.listdir(skills_dir)):
        if filename.startswith('_') or not filename.endswith('.py'):
            continue
        module_name = filename[:-3]
        try:
            mod = importlib.import_module(f'skills.{module_name}')
            skill_tools = getattr(mod, 'TOOLS', [])
            for tool_def in skill_tools:
                name = tool_def.get('name') or tool_def.get('function', {}).get('name', '')
                if name:
                    _registry[name] = (mod, tool_def)
                    tools.append(tool_def)
            logger.info(f"Skill geladen: {module_name} ({len(skill_tools)} Tools)")
        except Exception as e:
            logger.warning(f"Skill '{module_name}' konnte nicht geladen werden: {e}")

    logger.info(f"Skills geladen: {len(tools)} Tools aus {len(set(m.__name__ for m, _ in _registry.values()))} Dateien")
    return tools


def execute(tool_name: str, params: dict, context: dict) -> str:
    """Führt ein Tool aus und gibt das Ergebnis als String zurück."""
    if tool_name not in _registry:
        return f"Unbekanntes Tool: {tool_name}"
    mod, _ = _registry[tool_name]
    try:
        return mod.execute(tool_name, params, context)
    except Exception as e:
        logger.warning(f"Tool '{tool_name}' Fehler: {e}")
        return f"Fehler bei Tool '{tool_name}': {e}"
