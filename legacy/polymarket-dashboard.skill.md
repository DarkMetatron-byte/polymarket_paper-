---
name: polymarket-dashboard
description: Aktualisiert dashboard.html anhand des aktuellen Paper-Trading-States.
---

# Polymarket Dashboard

## Zweck
Erstellt oder aktualisiert das Dashboard fr den Paper Trader.

## Projektpfad (im OpenClaw-Container)
`/data/.openclaw/workspace/polymarket_paper`

## Datenquellen
- `paper_state.json`
- `markets_cache.json` (optional)

## Anzeigen
- Realized P/L
- Win Rate
- Anzahl Trades
- Offene Positionen
- Letzte Trades

## Aufgabe
Generiere eine aktualisierte Version von `dashboard.html`.

## Ausfhrung
Aktuell wird das Dashboard von `trader.py` automatisch geschrieben.

```bash
cd /data/.openclaw/workspace/polymarket_paper
python3 trader.py
```

## Regeln
- Keine Trades ausfhren (auer dem, was `trader.py` als Paper-Run ohnehin macht)
- Keine Strategie ndern
- Nur Dashboard generieren/aktualisieren

## Ergebnis
`dashboard.html` zeigt den aktuellen Zustand des Paper Traders.
