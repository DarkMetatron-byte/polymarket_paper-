---
name: polymarket-paper-trade
description: Fhrt die Paper-Trading-Strategie aus und aktualisiert paper_state.json.
---

# Polymarket Paper Trade

## Zweck
Dieser Skill fhrt die bestehende Paper-Trading-Logik des Projekts aus (keine echten Orders).

## Projektpfad (im OpenClaw-Container)
`/data/.openclaw/workspace/polymarket_paper`

## Relevante Dateien
- `trader.py`
- `markets_cache.json`
- `paper_state.json`

## Strategie (aktuell implementiert)
Mean Reversion auf **Up** (YES) mit Midprice (bestBid/bestAsk):
- **BUY** wenn `mid_yes_up < 0.40` und Uptrend aktiv
- **SELL** wenn `mid_yes_up > 0.60`

Zustzliche Regeln:
- Uptrend: aktuell **Preis > SMA(8)** aus eigener `price_history` (bentigt mind. 8 Punkte)
- Maximale Tradegre: **10 USD**
- Stop nach **3 Verlusten in Folge** (keine neuen Opens, aber weiterhin Closes)

## Ablauf
1. Lese `markets_cache.json` (Fallback: Live-Discovery, falls Cache fehlt)
2. Aktualisiere `price_history`
3. Prfe Trading-Signale
4. Aktualisiere `paper_state.json`
5. Aktualisiere `dashboard.html`

## Ausfhrung
```bash
cd /data/.openclaw/workspace/polymarket_paper
python3 trader.py
```

## Erwartete State-Felder
- `positions`
- `trades`
- `realized_pnl`
- `consecutive_losses`
- `price_history`

## Regeln
- Keine echten Orders senden
- Nur Paper-State aktualisieren
