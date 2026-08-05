"""Vendored client assets are pinned by hash, like every other dependency (ADR 0014, ADR 0024).

Nothing is fetched from a CDN, so the files on disk *are* the supply chain. This re-derives each
one's hash and fails if it has changed: an edited or swapped asset is a failing build rather than
a silent one, and the same string appears in the ADR, the template's `integrity` attribute and
here.

The template assertion is conditional on purpose. An asset lands in the pass that vendors it and
is referenced by the pass that uses it, so between those gates a hash-pinned file may legitimately
have no template pointing at it — but a template that *does* point at one may never serve a
different hash than the one recorded.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from tradebot.dashboard.views import PACKAGE

STATIC = PACKAGE / "static"
TEMPLATES = PACKAGE / "templates"

#: Every vendored asset and the hash it is pinned to.
#:
#: * htmx 2.0.7 — https://unpkg.com/htmx.org@2.0.7/dist/htmx.min.js
#: * lightweight-charts 5.2.0 (Apache-2.0, TradingView) — the standalone IIFE production build,
#:   https://unpkg.com/lightweight-charts@5.2.0/dist/lightweight-charts.standalone.production.js
EXPECTED_SRI = {
    "htmx.min.js": "sha384-ZBXiYtYQ6hJ2Y0ZNoYuI+Nq5MqWBr+chMrS/RkXpNzQCApHEhOt2aY8EJgqwHLkJ",
    "lightweight-charts.standalone.production.js": (
        "sha384-q1KYLSKHgBnW5tWYGGR8+6YV4/iPy31dILoF2I1OD7XiVUvHEp/TaxIQVmB0j3R2"
    ),
}

CHART_BUNDLE = "lightweight-charts.standalone.production.js"

#: The chart API Phase 10 depends on — every name `static/workspace.js` actually calls. A minified
#: bundle that no longer carries these is a different library wearing the same filename, and the
#: failure would otherwise surface as a blank pane in front of an operator rather than as a red
#: build.
CHART_EXPORTS = (
    "createChart",
    "CandlestickSeries",
    "createSeriesMarkers",
    "addSeries",
    "setMarkers",
    "setData",
    "autoSize",
)


def sri_of(path: Path) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()


def served_hashes() -> dict[str, str]:
    """Every `src`/`integrity` pair the templates publish, keyed by asset filename."""
    pattern = re.compile(r'src="[^"]*/(?P<asset>[^"/]+)"\s+integrity="(?P<sri>[^"]+)"')
    return {
        match["asset"]: match["sri"]
        for template in TEMPLATES.rglob("*.html")
        for match in pattern.finditer(template.read_text(encoding="utf-8"))
    }


def test_every_vendored_asset_matches_its_recorded_hash() -> None:
    for asset, expected in EXPECTED_SRI.items():
        assert sri_of(STATIC / asset) == expected, asset


def test_every_served_asset_is_one_we_pinned() -> None:
    """A script tag naming an asset with no recorded hash is an unpinned dependency."""
    assert served_hashes().keys() <= EXPECTED_SRI.keys()


def test_templates_serve_the_hashes_they_recorded() -> None:
    """One asset, one hash, in every place it is written down."""
    for asset, sri in served_hashes().items():
        assert sri == EXPECTED_SRI[asset], asset


def test_the_chart_bundle_exports_what_the_workspace_calls() -> None:
    bundle = (STATIC / CHART_BUNDLE).read_text(encoding="utf-8", errors="ignore")
    for export in CHART_EXPORTS:
        assert export in bundle, export


def test_the_chart_bundle_keeps_its_licence_notice() -> None:
    """Apache-2.0 requires the notice to travel with the copy; vendoring must not strip it."""
    header = (STATIC / CHART_BUNDLE).read_text(encoding="utf-8", errors="ignore")[:500]
    assert "Apache License 2.0" in header
    assert "TradingView" in header


def test_no_page_reaches_a_cdn() -> None:
    """The repo hash-pins everything and must work offline; a remote asset breaks both."""
    for template in TEMPLATES.rglob("*.html"):
        body = template.read_text(encoding="utf-8")
        assert "//cdn." not in body, template
        assert "https://" not in body, template
