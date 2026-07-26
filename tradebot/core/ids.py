"""Identifier generation, above all the `client_order_id` that makes submits idempotent.

    {prefix}-{base32(blake2s(basket_id|cycle_id|instrument|seq))}    # 20 chars

The id is **deterministic and recomputable from durable data**, which is what lets recovery
query the venue for an order whose response we never saw (PLAN §2.2). We store it anyway.

The `prefix` is per-mode (`sim`/`pap`/`liv`) so a paper id can never collide with, or be
mistaken for, a live one — and so the reconciler can adopt "our" orders by prefix (DESIGN §8.2).

Length and charset are venue-constrained: Binance spot `newClientOrderId` is
`^[\\.A-Z\\:/a-z0-9_-]{1,36}$`. Base32's `A-Z2-7` alphabet and the `-` separator sit inside
that set, and inside Alpaca's limit too; `assert_venue_safe` enforces it at generation time
rather than discovering it in a venue rejection.

Failure semantics: pure functions over their inputs; identical inputs always produce an
identical id. That is the whole point — a retry must not be able to mint a second id.
"""

from __future__ import annotations

import base64
import re
import uuid
from hashlib import blake2s
from typing import Final

from tradebot.core.enums import Mode
from tradebot.core.errors import ConfigError

#: Binance spot `newClientOrderId` charset and length cap — the tightest of our venues.
VENUE_ID_PATTERN: Final = re.compile(r"^[\.A-Z\:/a-z0-9_-]{1,36}$")

_DIGEST_BYTES: Final = 10  # 80 bits → exactly 16 base32 chars, no padding


def assert_venue_safe(client_order_id: str) -> str:
    """Raise unless the id is accepted by every venue we support."""
    if not VENUE_ID_PATTERN.match(client_order_id):
        raise ConfigError(f"client_order_id is not venue-safe: {client_order_id!r}")
    return client_order_id


def client_order_id(*, mode: Mode, basket_id: str, cycle_id: str, instrument: str, seq: int) -> str:
    """Derive the idempotency key for one order.

    `seq` distinguishes multiple orders for the same instrument within one cycle (an entry and
    its protective legs, or a retry of a *different* order). Reusing the same tuple deliberately
    reproduces the same id — that is how a resumed submit reaches the venue's existing order
    instead of creating a second one.
    """
    payload = "|".join((basket_id, cycle_id, instrument, str(seq))).encode()
    digest = blake2s(payload, digest_size=_DIGEST_BYTES).digest()
    suffix = base64.b32encode(digest).decode().rstrip("=")
    return assert_venue_safe(f"{mode.id_prefix}-{suffix}")


def owns_client_order_id(client_order_id_: str, mode: Mode) -> bool:
    """Whether an id seen at the venue was minted by this bot in this mode.

    Used by the reconciler to adopt our own orders and leave a human's manual orders alone.
    """
    return client_order_id_.startswith(f"{mode.id_prefix}-")


def new_uuid() -> str:
    """Opaque identifier for cycles, snapshots and events (not sent to any venue)."""
    return str(uuid.uuid4())
