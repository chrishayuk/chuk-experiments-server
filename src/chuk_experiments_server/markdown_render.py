"""Markdown -> sanitized HTML for rendering write-up bodies in the dashboard.

Sanitized because write-up bodies aren't necessarily human-authored — MCP
agents can call append_writeup too — so this is a real trust boundary, not
just formatting. bleach.clean() strips anything outside the allowlist
(scripts, event handler attributes, etc.) regardless of what markdown
produced.
"""

import bleach
import markdown as _markdown

from .constants import MARKDOWN_ALLOWED_ATTRIBUTES, MARKDOWN_ALLOWED_TAGS

_MD = _markdown.Markdown(extensions=["extra", "sane_lists"])


def render(body_md: str) -> str:
    html = _MD.convert(body_md)
    _MD.reset()
    return bleach.clean(html, tags=MARKDOWN_ALLOWED_TAGS, attributes=MARKDOWN_ALLOWED_ATTRIBUTES, strip=True)
