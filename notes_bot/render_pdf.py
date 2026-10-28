from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from .blocks import BY_ID
from .notes import NoteDocument
from .themes import Theme, tint

TEMPLATE_DIR = Path(__file__).parent / "templates"
TOC_MIN_SECTIONS = 6
_INLINE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`")


@dataclass
class Section:
    id: str
    title: str
    icon: str
    tone: str
    kind: str
    items: list = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    intro: str = ""


def _html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return _INLINE.sub(
        lambda match: f"<strong>{match.group(1)}</strong>" if match.group(1) is not None else f"<code>{match.group(2)}</code>",
        escaped,
    )


def _sections(doc: NoteDocument) -> list[Section]:
    sections: list[Section] = []

    def add(block_id: str, kind: str, **data) -> None:
        block = BY_ID[block_id]
        sections.append(Section(block_id, block.title, block.icon, block.tone, kind, **data))

    if doc.has_block("context"):
        add("context", "paragraphs", items=[_html(p) for p in doc.context])
    if doc.has_block("concepts"):
        add(
            "concepts",
            "table",
            headers=["Понятие", "Определение", "Зачем важно"],
            rows=[[f"<strong>{html.escape(c.term)}</strong>", _html(c.definition), _html(c.why)] for c in doc.concepts],
        )
    if doc.has_block("process") and doc.process is not None:
        add(
            "process",
            "steps",
            intro=_html(doc.process.intro),
            items=[_html(step) for step in doc.process.steps],
        )
    if doc.has_block("differences") and doc.differences is not None:
        headers, rows = doc.differences.aligned()
        add(
            "differences",
            "table",
            headers=[html.escape(h) for h in headers],
            rows=[[_html(cell) for cell in row] for row in rows],
        )
    if doc.has_block("examples"):
        add(
            "examples",
            "examples",
            items=[{"title": html.escape(e.title), "text": _html(e.text)} for e in doc.examples],
        )
    if doc.has_block("warnings"):
        add("warnings", "callouts", items=[_html(w) for w in doc.warnings])
    if doc.has_block("practice"):
        add("practice", "bullets", items=[_html(p) for p in doc.practice])
    if doc.has_block("takeaways"):
        add("takeaways", "bullets", items=[_html(t) for t in doc.takeaways])
    if doc.has_block("questions"):
        add("questions", "numbered", items=[_html(q) for q in doc.questions])
    return sections


def theme_css(theme: Theme) -> str:
    rules: list[str] = []
    for role, color in theme.colors.items():
        rules.append(
            f"""
.tone-{role} h2 {{ color: {color}; }}
.tone-{role} th {{ background: {tint(color, 0.86)}; color: {color}; }}
.tone-{role}.callout {{ background: {tint(color, 0.9)}; border-left-color: {color}; }}
.tone-{role} .example {{ border-left-color: {color}; }}
.tone-{role} .bullets > li::before {{ color: {color}; }}
.tone-{role} .steps > li::before, .tone-{role} .numbered > li::before {{ color: {color}; }}"""
        )
    return "\n".join(rules)


def build_html(doc: NoteDocument, theme: Theme) -> str:
    environment = Environment(loader=FileSystemLoader(TEMPLATE_DIR), autoescape=True)
    template = environment.get_template("note.html.j2")
    sections = _sections(doc)
    return template.render(
        title=doc.title,
        icon=doc.icon,
        main_title=BY_ID["main_idea"].title,
        main_icon=BY_ID["main_idea"].icon,
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
