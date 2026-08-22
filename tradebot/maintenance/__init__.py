"""Housekeeping that outlives a cycle: backups now, retention and compaction next.

Deliberately not part of `ops/`, which reads and never writes anything but its own cursor. This
package writes: it copies the database and, in a later piece, rewrites payloads in the event log.
"""

from __future__ import annotations
