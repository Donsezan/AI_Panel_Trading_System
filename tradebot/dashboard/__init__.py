"""The operator's dashboard: Configure, Monitor, Control (DESIGN §6.10).

Server-rendered FastAPI + Jinja2 + HTMX with no build step, and htmx vendored into the repo so
the whole client surface is hash-pinned and works offline (ADR 0014).

The dashboard **takes a wired `Application` and never builds one** — `app.py` remains the only
module that names a concrete adapter. Everything here is a view over calls pass 1 already
exposes, with one exception: closing a position by hand, which goes through the ordinary
`OrderIntent` → Tier-1 → Tier-2 → `ExecutionService` path like any other order. There are no
side doors.

Failure semantics: authentication is mandatory and the server refuses to start without a token;
reads fail closed to an empty view rather than a partial one; every write records its actor in
the event log.
"""

from __future__ import annotations
