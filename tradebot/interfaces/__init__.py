"""The plugin surface: protocols only, no implementations.

Core packages depend on these interfaces; concrete adapters are wired in `app.py` and nowhere
else. Freezing this surface early is what keeps venue assumptions out of core (PLAN Phase 1).
"""

from tradebot.interfaces.broker import BrokerAdapter, BrokerCapabilities, OrderAck, OrderRef
from tradebot.interfaces.debate import DebateProtocol
from tradebot.interfaces.llm import CompletionRequest, CompletionResult, LLMProvider
from tradebot.interfaces.market_data import DataCapabilities, MarketDataProvider
from tradebot.interfaces.news import NewsSource, RawNewsItem, RelevanceFilter
from tradebot.interfaces.risk import RiskProposal, RiskRule
from tradebot.interfaces.vectorstore import VectorStore

__all__ = [
    "BrokerAdapter",
    "BrokerCapabilities",
    "CompletionRequest",
    "CompletionResult",
    "DataCapabilities",
    "DebateProtocol",
    "LLMProvider",
    "MarketDataProvider",
    "NewsSource",
    "OrderAck",
    "OrderRef",
    "RawNewsItem",
    "RelevanceFilter",
    "RiskProposal",
    "RiskRule",
    "VectorStore",
]
