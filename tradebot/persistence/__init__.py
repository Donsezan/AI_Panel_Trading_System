"""Append-only event log plus relational projections (DESIGN §6.9)."""

from tradebot.persistence.database import SingleWriter, create_database
from tradebot.persistence.store import EventStore

__all__ = ["EventStore", "SingleWriter", "create_database"]
