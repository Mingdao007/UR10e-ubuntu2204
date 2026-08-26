#!/usr/bin/env python3
"""Apply the local R013 portable-report viewport compatibility patch.

The canonical portable renderer intentionally uses a full-bleed ``100vw``
header.  Chromium reserves a classic scrollbar gutter on this host, which
otherwise exposes a page-level horizontal scrollbar even though the document
content fits.  Keep the fix local to this report and leave inner table
scrolling untouched.
"""

from __future__ import annotations

from pathlib import Path


REPORT_HTML = Path(__file__).with_name("r013_floor_discovery_offline_report.html")
MARKER = "<!-- r013-offline-report-viewport-compat-v1 -->"
STYLE = (
    f"{MARKER}\n"
    '<style data-r013-viewport-compat="v1">'
    "html,body{overflow-x:clip!important}"
    "</style>"
)


def main() -> None:
    html = REPORT_HTML.read_text(encoding="utf-8")
    if MARKER in html:
        return
    closing_head = "</head>"
    if closing_head not in html:
        raise RuntimeError("portable report has no closing </head>")
    REPORT_HTML.write_text(
        html.replace(closing_head, f"{STYLE}\n{closing_head}", 1),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
