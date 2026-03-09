# polymarket-engine

Paper-trading engine for Polymarket crypto *Up/Down* markets.
No real orders are ever placed — all trades are simulated and tracked in `paper_state.json`.

---

## Architecture

| File | Role |
|---|---|
| `app/main.py` | Entry point: runs scanner then trader |
| `app/scanner.py` | Discovers active crypto Up/Down markets via Gamma API, writes `markets_cache.json` |
| `app/trader.py` | Paper-trading loop (run every 15 min via systemd timer) |
| `app/polymarket_client.py` | Gamma API client (read-only market data, no auth) |
| `app/binance_client.py` | Binance Spot API client (public endpoints, no auth) |
| `app/signal_engine.py` | External signal: BTC/ETH/SOL momentum blended with internal model |
| `app/market_intelligence.py` | Structural pre-filter: classifies markets HIGH\_QUALITY / NORMAL / LOW\_QUALITY / AVOID |
| `app/mispricing_engine.py` | Internal p̂ model: SMA + momentum (estimate\_probability\_v1) |
| `app/market_scan.py` | Broader market scan for dashboard |
| `app/config.py` | Path constants: STATE\_PATH, DASHBOARD\_PATH, CACHE\_PATH |

---

## Strategy

1. **Signal**: internal SMA/momentum model produces `p_hat` (implied probability for *Up*).
   Blended 30/70 with a Binance spot momentum signal (`p_binance = 0.5 + 0.5·tanh(return/vol)`).
2. **Entry**: buy YES (*Up*) or NO (*Down*) when edge at ask price ≥ 10 % and market passes all filters.
3. **Exit**: close when edge drops below threshold, adverse edge, or 6-hour time-stop.
4. **Circuit breaker**: halts new entries on 3 consecutive losses, total drawdown > 5× trade size, or rolling 5-trade loss > 2× trade size. Existing positions are always allowed to close.

---

## Filters & risk controls

| Parameter | Value |
|---|---|
| Max USD per trade | $10 |
| Max open positions | 3 |
| Max positions per asset (BTC/ETH/SOL) | 1 |
| Max new entries per cycle | 2 |
| Max spread | 3 % |
| Entry edge threshold | 10 % |
| Post-exit cooldown | 60 min |
| Max hold time | 6 h |

---

## Run

```bash
# One-shot cycle (scanner + trader)
python app/main.py

# Individual steps
python app/scanner.py   # refresh markets_cache.json
python app/trader.py    # run one trading cycle
```

---

## VPS deployment (systemd)

```bash
# Copy service + timer
sudo cp scripts/polymarket.service /etc/systemd/system/
sudo cp scripts/polymarket.timer   /etc/systemd/system/

# Enable (runs every 15 min)
sudo systemctl daemon-reload
sudo systemctl enable --now polymarket.timer

# Logs
journalctl -u polymarket.service -f
```

---

## Output files

| File | Contents |
|---|---|
| `paper_state.json` | Full state: open positions, trade log, P/L, price history, CB status |
| `dashboard.html` | HTML dashboard: KPIs, Binance spot prices, open positions, trade history |
| `markets_cache.json` | Cached market list from last scanner run |

---

## Dependencies

Standard library only — no pip packages required (`urllib`, `json`, `math`, `dataclasses`).

See `requirements.txt` for optional extras (linting only).

---

## Transparency

- Export inventory: `INVENTORY.md`
- Original files preserved under: `legacy/polymarket_paper/`
