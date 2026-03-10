# Roadmap

## Current Prototype

- paper-trading loop with scanner + trader
- dashboard snapshot generation
- status + Telegram reporting
- aligned service loop for operations
- adaptive setup table for paper selection weighting

## Next Improvements

- better edge model calibration and diagnostics
- clearer market labels in dashboard (`observed_markets`, `trading_universe`, `tradeable_markets`)
- explicit blocked-reason transparency in reports/dashboard
- richer analytics on setup performance and decay over time
- stronger docs-to-code traceability for filter stages

## Later / Optional

- safer operations tooling (log rotation checks, watchdog summaries)
- configurable market-label extensions for additional assets
- automated report snapshots for daily/weekly performance
- regression test coverage for filter and scoring paths
- optional sandbox/backtest harness with deterministic fixtures

## Guardrails

- keep paper-trading only unless intentionally redesigned
- prefer explicit documentation over implicit behavior
- avoid hidden automation that changes strategy without visibility
