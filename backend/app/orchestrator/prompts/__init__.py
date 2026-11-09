from pathlib import Path
from string import Template

_DIR = Path(__file__).parent


def render(name: str, **values: str) -> str:
    """Render ``<name>.md`` with ``$placeholders``; a missing value raises ``KeyError``."""
    return Template((_DIR / f"{name}.md").read_text(encoding="utf-8")).substitute(values)
