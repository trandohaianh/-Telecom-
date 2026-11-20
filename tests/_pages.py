"""Page source for static tests: the HTML plus the local scripts it loads.

The page scripts were moved out of the HTML into .js files (CSP script-src 'self', 20/11/2026). Tests that
check page behaviour read this combined text, so they keep checking the same code in its new place.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _script_path(page: Path, src: str) -> Path | None:
    if src.startswith(("http://", "https://", "//")):
        return None
    if src.startswith("/admin/assets/"):
        return STATIC / "admin" / "assets" / src[len("/admin/assets/") :]
    if src.startswith("/app/"):
        return STATIC / "app" / src[len("/app/") :]
    return page.parent / src


def page_source(rel: str) -> str:
    page = ROOT / rel
    html = page.read_text(encoding="utf-8")
    if page.suffix != ".html":
        return html
    parts = [html]
    for src in re.findall(r'<script src="([^"]+)"></script>', html):
        path = _script_path(page, src)
        if path is not None:
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)
