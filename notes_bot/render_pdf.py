from __future__ import annotations

import html
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .notes import Element, NoteDocument
from .themes import Theme, tint

TEMPLATE_DIR = Path(__file__).parent / "templates"
TOC_MIN_SECTIONS = 3
_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


# Headings take these colours in turn, so a long note is easy to scan.
SECTION_TONES = ("blue", "purple", "green", "orange")
# What a callout looks like: colour role and icon.
CALLOUT_STYLE = {
    "info": ("blue", "💬"),
    "important": ("orange", "📌"),
    "warning": ("yellow", "⚠️"),
    "tip": ("green", "💡"),
    "quote": ("gray", ""),
}


def _html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _INLINE.sub(
        lambda match: f"<strong>{match.group(1)}</strong>" if match.group(1) is not None else f"<code>{match.group(2)}</code>",
        escaped,
    )


def _view(element: Element) -> dict:
    """An element as the template wants it: text already escaped."""
    view: dict = {"kind": element.type}
    if element.type in ("paragraph", "subheading"):
        view["text"] = _html(element.text)
    elif element.type in ("bullets", "numbered", "checklist"):
        view["points"] = [_html(item) for item in element.items]
    elif element.type == "table":
        headers, rows = element.aligned()
        view["headers"] = [html.escape(header) for header in headers] if any(headers) else []
        view["rows"] = [[_html(cell) for cell in row] for row in rows]
    elif element.type == "callout":
        tone, icon = CALLOUT_STYLE[element.kind]
        view.update(text=_html(element.text), tone=tone, icon=icon, callout_kind=element.kind)
    elif element.type == "toggle":
        view.update(title=_html(element.title), text=_html(element.text))
    return view


def _sections(doc: NoteDocument) -> list[dict]:
    sections = []
    for number, section in enumerate(doc.sections, start=1):
        views = []
        subs = []
        for element in section.elements:
            view = _view(element)
            if element.type == "subheading":
                view["id"] = f"s{number}-{len(subs) + 1}"
                subs.append({"id": view["id"], "title": view["text"]})
            views.append(view)
        sections.append({
            "id": f"s{number}",
            "number": number,
            "title": section.heading,
            "icon": section.icon,
            "tone": SECTION_TONES[(number - 1) % len(SECTION_TONES)],
            "elements": views,
            "subs": subs,
        })
    return sections


def theme_css(theme: Theme) -> str:
    rules: list[str] = []
    for role, color in theme.colors.items():
        rules.append(
            f"""
.tone-{role} h2 {{ color: {color}; }}
.tone-{role} th {{ background: {tint(color, 0.86)}; color: {color}; }}
.tone-{role}.callout {{ background: {tint(color, 0.9)}; border-left-color: {color}; }}
.tone-{role} .toggle {{ border-left-color: {color}; background: {tint(color, 0.93)}; }}
.tone-{role} .bullets > li::before {{ color: {color}; }}
.tone-{role} .numbered > li::before {{ color: {color}; }}
.tone-{role} .checklist > li::before {{ border-color: {color}; }}"""
        )
    return "\n".join(rules)


def build_html(doc: NoteDocument, theme: Theme) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    template = environment.get_template("note.html.j2")
    sections = _sections(doc)
    return template.render(
        title=doc.title,
        icon=doc.icon,
        main_title="Главная идея лекции",
        main_icon="🧠",
        main_idea=_html(doc.main_idea),
        sections=sections,
        show_toc=len(sections) >= TOC_MIN_SECTIONS,
        base_css=(TEMPLATE_DIR / "note.css").read_text(encoding="utf-8"),
        theme_css=theme_css(theme),
    )


def render_pdf(doc: NoteDocument, theme: Theme) -> bytes:
    # Imported here: WeasyPrint is slow to load and needs Pango at runtime.
    from weasyprint import HTML

    return HTML(string=build_html(doc, theme), base_url=str(TEMPLATE_DIR)).write_pdf()
