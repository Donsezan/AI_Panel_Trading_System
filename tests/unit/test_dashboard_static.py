"""The vendored client asset is pinned by hash, like every other dependency (ADR 0014).

htmx is served from the repo rather than a CDN, so the file on disk *is* the supply chain. This
re-derives its hash and fails if it has changed: an edited or swapped asset is a failing build
rather than a silent one, and the same string appears in the ADR, the template's `integrity`
attribute and here.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from tradebot.dashboard.views import PACKAGE

HTMX = PACKAGE / "static" / "htmx.min.js"
BASE_TEMPLATE = PACKAGE / "templates" / "base.html"

#: htmx 2.0.7, from https://unpkg.com/htmx.org@2.0.7/dist/htmx.min.js
EXPECTED_SRI = "sha384-ZBXiYtYQ6hJ2Y0ZNoYuI+Nq5MqWBr+chMrS/RkXpNzQCApHEhOt2aY8EJgqwHLkJ"


def sri_of(path: Path) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()


def test_vendored_htmx_matches_its_recorded_hash() -> None:
    assert sri_of(HTMX) == EXPECTED_SRI


def test_the_template_serves_the_hash_it_recorded() -> None:
    """One asset, one hash, in both places it is written down."""
    served = re.search(r'integrity="([^"]+)"', BASE_TEMPLATE.read_text(encoding="utf-8"))
    assert served is not None
    assert served.group(1) == EXPECTED_SRI


def test_no_page_reaches_a_cdn() -> None:
    """The repo hash-pins everything and must work offline; a remote asset breaks both."""
    for template in (PACKAGE / "templates").rglob("*.html"):
        body = template.read_text(encoding="utf-8")
        assert "//cdn." not in body, template
        assert "https://" not in body, template
