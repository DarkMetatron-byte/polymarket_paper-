# Strategy

See `app/trader.py` docstring for the authoritative description.

In short:

- Mean reversion on the **Up** outcome in Up/Down markets.
- Buy when midprice is low (below threshold) and an uptrend proxy is satisfied.
- Sell when midprice is high (above threshold).
- Circuit breaker after a number of consecutive realized losses.
