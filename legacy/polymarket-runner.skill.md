---
name: polymarket-runner
description: Fhrt einen vollstndigen Zyklus des Polymarket Paper Traders aus (Discovery  Paper Run  Dashboard).
---

# Polymarket Runner

## Zweck
Dieser Skill orchestriert einen vollstndigen Paper-Trading-Zyklus.

## Projektpfad (im OpenClaw-Container)
`/data/.openclaw/workspace/polymarket_paper`

## Pipeline
1. Markt-Scan (Discovery) ausfhren  aktualisiert `markets_cache.json`
2. Risiko prfen (Circuit Breaker) anhand `paper_state.json`
3. Paper-Trading ausfhren (aktualisiert `paper_state.json`)
4. Dashboard aktualisieren (wird durch `trader.py` geschrieben)

## Reihenfolge
1) Market Scan
2) Risk Check
3) Paper Trade
4) Dashboard Update

## Ausfhrung
```bash
cd /data/.openclaw/workspace/polymarket_paper
python3 discover_markets.py
python3 trader.py
```

## Hinweise
- `trader.py` nutzt standardmig `markets_cache.json` und fllt nur bei fehlendem Cache auf Live-Discovery zurck.
- Circuit Breaker: Bei `consecutive_losses >= 3`  keine neuen Opens, aber weiterhin Closes.

## Regeln
- Arbeite nur im Projektordner
- Keine Live-Trades (keine echten Orders)
- Bestehende Dateien nicht unntig berschreiben

## Ergebnis
Aktualisiert:
- `markets_cache.json`
- `paper_state.json`
- `dashboard.html`
