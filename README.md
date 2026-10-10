# dse-alerts

**Self-hosted stock alerts for the Dhaka Stock Exchange.** Add the companies you follow,
pick the conditions you care about, and get an email when they happen. Rules are tuned to
how the DSE actually behaves (circuit-breaker slabs, A/B/Z categories, price-sensitive
news, record dates) and calibrated on 25 years of daily data, so an alert means something
instead of firing every other day.

> Status: under construction. Phases 1-2 (data layer, calibration) are done; alert rules,
> email, web UI and the AI digest are in progress. See the plan below.

## Principles

* **Your data stays yours.** Everything runs on your machine or your own server. No
  telemetry, no accounts anywhere, no third party sees your watchlist. Market data is
  fetched by you, from DSE, into a local DuckDB file.
* **Alerts are facts, not advice.** "GP closed at its lower circuit, volume 3.4x its 20-day
  average, no price-sensitive news filed" is an alert. "Buy GP" is not, and the tool never
  says it.
* **Optional extras stay optional.** Email needs an SMTP login; the AI digest needs an API
  key (Claude, or any OpenAI-compatible endpoint such as Groq or a local Ollama). Without
  either, alerts still run and land in a local outbox.

## Phase 1: data layer (done)

```bash
pip install -e .
dse data bootstrap     # downloads the 1999-2025 archive (77 MB) from Mendeley, cleans it, loads DuckDB
dse data refresh       # pulls everything newer from dse.com.bd (daily prices, PSI news, dividends)
dse data instruments   # company profiles, categories, sectors, fundamentals
dse data validate      # proves the archive matches the live site where they overlap
dse data status
```

What that gives you today (checked 2026-10-10):

| | |
|---|---|
| Daily OHLCV rows | 1.28 million, 2012-10-01 to the last session, 716 instruments |
| Currently tradeable | 395 (equities + mutual funds), 394 with sector, category, P/E, EPS, free float, 52-week range |
| Archive vs live agreement | 100% of closes on 7,062 overlapping rows, 10 stocks |
| Index history | DSEX / DSES / DS30 per session since 2012-10 |

Two archives exist for the years the DSE site no longer serves. One claims 25 years but its
pre-2023 rows are misdated by months (its DSEX reads 7,356 in March 2021; the real index was
~5,500), which a circuit-breaker consistency test exposed; it is used from 2023 only, as a
fallback. The other, curated at two Dhaka universities, passes every test from October 2012
and is the one you should download (one click; the README of `dse data bootstrap` says
where). The audit is in [docs/data-quality.md](docs/data-quality.md).

## Phase 2: calibration (done)

```bash
dse analysis features    # per-session returns, volume ratios, circuit-breaker hits, streaks, 250-day ranges
dse analysis calibrate   # how often each candidate alert fires, and what followed -> docs/calibration.md
```

From 977k sessions across 395 stocks (2013 to 2026, floor-price period excluded): a fixed rule
like "down 10% in 10 days" fires about 19 times a year *per stock* on the DSE, so thresholds
have to be relative to each stock's own distribution, which `symbol_stats` now holds. Three
sessions in a row at the lower circuit is rare (once in 50 stock-years) and is followed by a
median -6% over the next 20 sessions; three in a row at the upper circuit has a +21% *mean*
but a +1% median, i.e. a few runaway pumps and many fades. Full tables and the forward-return
evidence for every candidate rule: [docs/calibration.md](docs/calibration.md).

## Plan

| Phase | What | Status |
|---|---|---|
| 1 | Data layer: archive + live ingestion, validation, instrument profiles | done |
| 2 | Calibration study: what "unusual" means per stock, measured on clean history | done |
| 3 | Alert rules + engine + backtest of each rule | next |
| 4 | Email delivery, daily scheduled run (GitHub Actions or cron) | |
| 5 | Web UI: login, watchlists, alert settings, history | |
| 6 | AI digest (optional), Docker, docs, public release | |

## Licence

MIT. Market data belongs to its sources; see [docs/data-quality.md](docs/data-quality.md).
This tool is for information only and is not investment advice.
