"""
Rule-based formatter for Jira ticket titles and descriptions.
No external API or AI key required.
"""
import re

__all__ = ['optimize_ticket']

_PLACEHOLDER = '_nicht angegeben_'

# Matches lines that look like user-supplied section headers
_SECTION_RE = re.compile(
    r'(?m)^\s*\*?'
    r'(?P<label>'
    r'problem|issue|fehler|the\s+problem|das\s+problem'
    r'|auswirkung|impact|effekt|effect|why|warum|grund'
    r'|erwartetes?\s*verhalten|expected\s*(?:behavior|behaviour)'
    r'|l[öo]sung|solution'
    r')\*?'
    r'\s*[:\-]\s*',
    re.IGNORECASE,
)

_PROBLEM_PREFIXES = ('problem', 'issue', 'fehler', 'the problem', 'das problem')
_IMPACT_PREFIXES = ('auswirkung', 'impact', 'effekt', 'effect', 'why', 'warum', 'grund')
_EXPECTED_PREFIXES = ('erwartetes', 'expected', 'lösung', 'losung', 'solution')


def optimize_ticket(title: str, use_case: str) -> tuple[str, str]:
    """Format title and structure description for a Jira ticket.

    Returns (formatted_title, structured_description).
    Falls back to originals silently on any error.
    """
    try:
        opt_title = _format_title(title)
        opt_desc = _structure_description(use_case)
        return opt_title, opt_desc
    except Exception:
        return title, use_case


def _format_title(title: str) -> str:
    """Clean and shorten the title to at most 8 words."""
    t = re.sub(r'#improvement\S*', '', title, flags=re.IGNORECASE).strip()
    # Strip leading noise characters
    t = re.sub(r'^[\s\-*•:]+', '', t).strip()
    # Strip trailing punctuation
    t = re.sub(r'[.!?]+$', '', t).strip()
    # Capitalise first letter, leave the rest as-is
    if t:
        t = t[0].upper() + t[1:]
    # Limit to 8 words
    words = t.split()
    if len(words) > 8:
        t = ' '.join(words[:8])
    return t or title.strip()


def _structure_description(text: str) -> str:
    """Wrap text in *Problem:* / *Auswirkung:* / *Erwartetes Verhalten:* sections."""
    stripped = text.strip()
    if not stripped:
        return text

    matches = list(_SECTION_RE.finditer(stripped))
    if matches:
        return _remap_sections(stripped, matches)

    # Plain text — put everything in Problem, leave others as placeholder
    return (
        f"*Problem:*\n{stripped}\n\n"
        f"*Auswirkung:*\n{_PLACEHOLDER}\n\n"
        f"*Erwartetes Verhalten:*\n{_PLACEHOLDER}"
    )


def _remap_sections(text: str, matches: list) -> str:
    """Map detected section headers to canonical Jira labels."""
    pairs: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip() or _PLACEHOLDER
        label_raw = m.group('label').lower().strip()

        if any(label_raw.startswith(p) for p in _PROBLEM_PREFIXES):
            canonical = '*Problem:*'
        elif any(label_raw.startswith(p) for p in _IMPACT_PREFIXES):
            canonical = '*Auswirkung:*'
        elif any(label_raw.startswith(p) for p in _EXPECTED_PREFIXES):
            canonical = '*Erwartetes Verhalten:*'
        else:
            canonical = f'*{label_raw.capitalize()}:*'

        pairs.append((canonical, content))

    # Ensure all three required sections exist
    present = {s[0] for s in pairs}
    for label in ('*Problem:*', '*Auswirkung:*', '*Erwartetes Verhalten:*'):
        if label not in present:
            pairs.append((label, _PLACEHOLDER))

    # Fixed order: Problem → Auswirkung → Erwartetes Verhalten → anything else
    order = ['*Problem:*', '*Auswirkung:*', '*Erwartetes Verhalten:*']
    ordered = [(label, c) for label in order for (sl, c) in pairs if sl == label]
    rest = [s for s in pairs if s[0] not in order]

    return '\n\n'.join(f"{label}\n{content}" for label, content in ordered + rest)
