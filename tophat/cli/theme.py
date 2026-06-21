"""Rich color theme for TopHat CLI output."""

from rich.style import Style
from rich.theme import Theme

TOPHAT_THEME = Theme({
    "banner": "bold cyan",
    "title": "bold white",
    "muted": "dim",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "money": "green",
    "phase.eval": "yellow",
    "phase.funded": "cyan",
    "phase.retired": "dim",
    "nuke": "bold magenta",
    "flip": "blue",
    "long": "green",
    "short": "red",
    "flat": "dim",
    "id": "bold",
})

HEADER_STYLE = Style(color="cyan", bold=True)
PANEL_BORDER = "bright_blue"
