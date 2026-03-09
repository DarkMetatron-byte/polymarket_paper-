---
name: polymarket-risk-check
description: Prft die Risikoregeln des Polymarket Paper Traders anhand von paper_state.json.
---

# Polymarket Risk Check

## Zweck
Dieser Skill bewertet, ob neue Paper-Trades erlaubt sind (Circuit Breaker).

## Projektpfad (im OpenClaw-Container)
`/data/.openclaw/workspace/polymarket_paper`

## Relevante Datei
- `paper_state.json`

## Aktuelle Risikoregeln
- Stop nach **3 Verlusten in Folge** (`consecutive_losses >= 3`)
- Maximale Tradegre: **10 USD**
- Nur Paper-Trading (keine echten Orders)

## Prfschritte
1. Lies `paper_state.json`
2. Prfe `consecutive_losses`
3. Wenn `consecutive_losses >= 3`  BLOCK
4. Andernfalls  ALLOW

## Ausfhrung (manuell)
```bash
cd /data/.openclaw/workspace/polymarket_paper
cat paper_state.json
```

## Regeln
- Keine Trades ausfhren
- `paper_state.json` nicht berschreiben
- Nur Status prfen

## Ergebnis
Entscheidung: **ALLOW** oder **BLOCK**
