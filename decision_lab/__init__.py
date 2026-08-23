"""A fine-tuning instrument for the panel's decision logic.

Standalone by contract: this package imports `tradebot` and `tradebot` knows nothing about it
(spec §2). It scores decisions and compares configurations; it never trades, never writes to a
bot database, and has no code path from a `Decision` to an `OrderIntent`.

Failure semantics: every refusal here is a `ConfigError` naming the command that fixes it. An
unverified dataset, a missing pinned day set and a compacted corpus all fail closed, because
every number downstream is derived from them.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
