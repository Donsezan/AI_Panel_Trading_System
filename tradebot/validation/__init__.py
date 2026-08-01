"""The validation ladder (DESIGN §9, PLAN Phase 7).

Three readers and one driver, all of them over the append-only event log:

```
Evidence          folds the log into the facts a report is made of
  PromotionReport  the gates a paper soak must pass before a human may consider live
  BacktestReport   a replayed period, banner-stamped as plumbing validation only
BacktestHarness    drives the real loop over recorded history, stepping the clock itself
```

Nothing here decides anything or trades. A report is an argument put to a human, and the one
gate that never passes automatically is the human's.
"""
