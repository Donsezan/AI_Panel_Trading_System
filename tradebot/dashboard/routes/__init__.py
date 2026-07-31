"""The dashboard's three jobs, one router each (DESIGN §6.10).

`monitor` reads projections; `configure` publishes versioned configuration; `control` acts on
persisted risk state and, for a manual close, on the ordinary execution path. Nothing here
reaches a venue directly — every route is a view over a call the control plane already exposes.
"""

from __future__ import annotations
