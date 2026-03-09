# Polymarket paper trader (Up/Down crypto)

## What this does

- Discovers active Polymarket markets via Gamma API (`https://gamma-api.polymarket.com`).
- Filters for crypto **"Up or Down"** style markets.
- Paper trades only (local JSON state), no real orders.
- Simple mean reversion on the **Up** outcome, using midprice (bestBid/bestAsk):
  - Buy when price < 0.40 (and uptrend proxy)
  - Sell when price > 0.60
- Circuit breaker: stop opening new trades after 3 consecutive losses.
- Writes an HTML dashboard.

## Files

- `polymarket_client.py` - Gamma client + discovery helpers
- `discover_markets.py` - discovery CLI + writes `markets_cache.json`
- `trader.py` - paper trading runner (run every 15 minutes)
- `paper_state.json` - paper trading state + trade log (auto-created)
- `dashboard.html` - simple dashboard (auto-created)

## Run manually

```bash
cd /data/.openclaw/workspace/polymarket_paper
python3 discover_markets.py
python3 trader.py
```

## Cron (every 15 minutes)

Inside the OpenClaw container:

```bash
crontab -e
```

Add:

```cron
*/15 * * * * cd /data/.openclaw/workspace/polymarket_paper && /usr/bin/python3 trader.py >> trader.log 2>&1
```

If `/usr/bin/python3` differs, use `which python3` inside the container.
