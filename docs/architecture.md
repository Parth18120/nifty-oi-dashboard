# Architecture

The project is a Flask backend that turns a live Dhan option chain into a single JSON payload, plus a
single-page HTML dashboard that renders it. A rule-based engine paper-trades on top of the same data.
**No real orders are ever placed**; the Dhan API is used for market data only.

## Request / refresh flow

```
Dhan API ──► feeds/dhan_feed.py ──► market/snapshot.py ──► api/market.py ──► web/dashboard.html
                                        │
                                        ├─ analytics/*   PCR, max pain, OI build-up, IV skew,
                                        │                realised vol, fair value and OVR%, Greeks
                                        ├─ trading/*     entry rules, strategies, risk, exits
                                        └─ core/state    shared runtime state (one place, no globals)
```

`market/snapshot.py::get_fresh_data()` is called by `/api/data` and `/api/refresh`. It is cached for
a few seconds (see `market/session.py`) so several browser tabs do not hammer the broker API.

## Packages

| Package | Responsibility |
|---|---|
| `oi_dashboard/core` | `state` (all shared mutable state), `constants`, `paths`, small utilities |
| `oi_dashboard/feeds` | Dhan option chain, spot, VIX and Sensex feeds; historical spot updaters; NSE bhavcopy fetch |
| `oi_dashboard/analytics` | OI analysis, realised vol (Yang-Zhang), IV surface, Greeks, signal classifiers, spike detector |
| `oi_dashboard/market` | Builds the dashboard payload, market-hours helpers, demo/mock data |
| `oi_dashboard/trading` | Entry rules, auto-entry orchestration, per-strategy modules, accounting, risk engine, backtester |
| `oi_dashboard/storage` | SQLite persistence (`db`) and session/JSON stores |
| `oi_dashboard/services` | Background threads (open-leg price tracking, Sensex refresh) |
| `oi_dashboard/api` | Flask blueprints: market, trades, carry-forward, journal, backtest |
| `oi_dashboard/terminal` | Rich-based terminal view (`main.py`) |

## Strategies (paper)

All live under `oi_dashboard/trading/strategies/`:

- `iron_condor`: four-leg iron condor (both sides or nothing) on non-directional days
- `gap_spread`: directional credit spread (put spread after a gap-up, call spread after a gap-down)
- `strangle`: naked strangle in range-bound markets
- `deep_otm`: hedged far-OTM spread that harvests inflated implied volatility
- `panic_buy`: detects a 150-point drop in 10 minutes with a 100%+ premium spike and buys the option for the bounce

Entries are gated by `trading/rules.py` (sweet-zone strikes, minimum premium, feasibility), the risk engine
(VIX / gap / event vetoes) and the PE/CE activity classifier. Exits use DTE-scaled profit targets, stop
multiples, trailing stops, IV-crush and spike-aware rules (`trading/accounting.py`, strategy `update_*` functions).

## Shared state

Earlier versions kept ~70 module-level globals in one file. They now live in `core/state.py` and are
accessed as `state.<name>`, so any module can read or update them without circular imports.

## Data

| Path | Contents | Tracked in git |
|---|---|---|
| `data/spot_all.csv`, `data/sensex_spot_all.csv` | Daily index OHLC used for realised vol | yes |
| `data/events.json` | RBI / FOMC / CPI / expiry calendar | yes |
| `data/dashboard.db` | SQLite: chain snapshots, trades, EOD stats | no |
| `Paper Trading Data/`, `Intraday Data/`, `logs/` | Session exports and logs | no |
