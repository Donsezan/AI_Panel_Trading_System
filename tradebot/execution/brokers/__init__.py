"""Broker adapters: one per venue account, all held to one contract suite.

DESIGN [L11] — *budget most engineering effort here*. Practitioner incident reports agree: most
production failures originate in the venue integration layer, not in strategy code.

```
sim.py       SimBroker          deterministic fills; simulation, backtest, primary paper soak
binance.py   BinanceSpotBroker  Binance spot over a signed ccxt transport, OCO groups
alpaca.py    AlpacaBroker       US equities over plain httpx, brackets, calendar, announcements
```

`tests/contract/test_broker_contract.py` runs the *same* suite against all three. An adapter whose
semantics diverge — on partial fills, cancel races, `SUBMIT_UNKNOWN` recovery, rejections,
precision or minimums — fails CI. That identity is what makes a paper result predictive of live
behaviour rather than a result from a parallel implementation (DESIGN §5).
"""
