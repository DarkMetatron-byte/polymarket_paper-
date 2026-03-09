---
name: polymarket-market-scan
description: Scannt Polymarket (Gamma API) nach relevanten Crypto Up/Down Markets und aktualisiert markets_cache.json im Projekt polymarket_paper.
---

# Polymarket Market Scan

## Zweck
Dieser Skill aktualisiert die Marktdaten fr den Polymarket Paper Trader (nur Discovery, keine Trades).

## Projektpfad (im OpenClaw-Container)
`/data/.openclaw/workspace/polymarket_paper`

## Relevante Dateien
- `discover_markets.py`
- `markets_cache.json`

## Aufgabe
Fhre den Marktscan aus und aktualisiere die lokale Marktbersicht.

## Ablauf
1. Wechsel in das Projektverzeichnis.
2. Fhre `discover_markets.py` mit `python3` aus (keine venv vorausgesetzt).
3. Prfe, dass `markets_cache.json` aktualisiert wurde.

## Ausfhrung
```bash
cd /data/.openclaw/workspace/polymarket_paper
python3 discover_markets.py
```

## Regeln
- Keine Trades ausfhren
- Keine nderungen an `paper_state.json`
- Nur Marktdaten aktualisieren

## Ergebnis
`markets_cache.json` enthlt aktuelle Marktdaten fr die Trading-Logik.
