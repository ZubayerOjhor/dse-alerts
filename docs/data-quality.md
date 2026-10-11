# Data sources and data quality

Everything the alert engine computes rests on daily OHLCV rows. This page records where
they come from, what was checked, and what was done about the problems found. All checks
were run on 2026-10-10 and can be repeated with `dse data validate`.

## Sources

| Source | Coverage | Used for | Licence |
|---|---|---|---|
| **dse.com.bd** (live site, via [bdshare](https://github.com/rochi88/bdshare)) | daily history from ~2024-10-14; live quotes; PSI news; dividend declarations; 30-day index summary | the daily refresh and every alert | site data, fetched by each user for their own use |
| **Mendeley: DSE End-of-Day Dataset with Coverage Metadata** ([doi:10.17632/23553sm4tn.4](https://data.mendeley.com/datasets/23553sm4tn/4)), curated at Southeast University / AUST | 2012-10-01 to 2026-01-22, 486 instruments incl. DSEX/DS30/DSES, 1.07M rows; unadjusted and adjusted variants | all history before the live site's floor; **preferred archive**. Browser download only (see §5) | CC BY 4.0 |
| **Mendeley: DSE Historical Data 1999-2025** ([doi:10.17632/5mww8rb9td.1](https://data.mendeley.com/datasets/5mww8rb9td/1)) | file: 1999-01-02 to 2025-04-08, 534 instruments, 1.52M rows. **Used: 2023-01-01 onward only** (see §3) | automatic fallback when the curated archive is absent | CC BY 4.0 |

The live site is the ground truth where the two overlap. Nothing from either source is
redistributed in this repository; `dse data bootstrap` downloads the archive from Mendeley
and `dse data refresh` pulls from DSE, on the user's own machine.

## Why an archive at all

The DSE website was rebuilt in 2026 and its day-end archive now only serves roughly one
year of history. Requests for January 2024 and earlier return nothing. Calibrating what
"unusual" means for a stock needs much more than one year, so an archive is unavoidable.

## What was checked

### 1. Does the archive agree with the live site?

For every date where both exist (2024-11-01 to 2025-04-08, 104 sessions) and for eight
liquid stocks across sectors, close prices and volumes were compared row by row.

| Symbol | Overlap sessions | Close match | Volume match |
|---|---|---|---|
| GP, SQURPHARMA, BRACBANK, BEXIMCO, ROBI | 104 each | 100% | 100% |

Result: the recent part of the archive is an exact copy of what DSE publishes.

### 2. Duplicates

* 63,914 rows were exact duplicates of another row. Dropped.
* 4,825 (symbol, date) pairs, **all in 2022**, had two rows with different values. In
  4,824 of them one row has strictly lower volume than the other. The higher-volume row
  is kept as the more complete record of the session. 2022 falls inside the floor-price
  period and is excluded from volatility calibration anyway (see below).

### 3. The dates before 2023 are wrong, and not by a little

The first warning sign: DSE does not trade on Fridays or Saturdays, yet the archive has
sessions dated on them, 25-60 weekend dates a year before 2023 and none from 2023 on.
(Genuine Saturday sessions exist when the government declares a working Saturday around
Eid; the live data has three, 2025-05-17, 2025-05-24 and 2026-05-23, with the full market
trading. That is a different thing.)

| Year | Sun-Thu days with no data | Fri/Sat days with data |
|---|---|---|
| 2016 | 54 | 35 |
| 2021 | 50 | 30 |
| 2022 | 44 | 14 |
| 2023 | 18 | 0 |
| 2024 | 27 | 0 |

The decisive test uses the exchange's own rules. Daily moves are capped by slab-based
circuit breakers (10% for shares under Tk 200, down to 3.75% above Tk 5,000), so a
close-to-close move beyond the cap plus a 1-point tolerance is impossible unless the
sequence of sessions is broken. Share of sessions beyond the cap:

| Year | 2015 | 2018 | 2021 | 2022 | **2023** | **2024** | **2025** | **2026** |
|---|---|---|---|---|---|---|---|---|
| Beyond cap | 17% | 16% | 20% | 9% | **0.03%** | **0.3%** | **0.04%** | **0.02%** |

The archive's own DSEX series settles it. For the week of 2021-03-01 it reads 5,619 ->
5,582 -> 5,488 -> 5,511 -> **6,053 -> 6,536 -> 7,356** -> 6,898. The real DSEX was around
5,500 that week and first reached 7,300 in October 2021. Rows from different months have
been assigned to the same week; the pre-2023 part of the file is not a time series.

**Decision: the loader keeps only rows dated 2023-01-01 or later** (1,236,288 rows dropped).
From 2023 the archive passes the circuit-breaker test and matches the live site exactly
where they overlap. The 2024 figure (0.3%) is the floor-price exit in January 2024, when
stocks released from their floors moved more than the cap on their first free session.

Rows carry a `date_quality` flag, `exact` for everything kept; loading with an earlier
`reliable_from` marks older rows `unreliable` and the feature builder ignores them.
### 4. Other row-level problems

* 66 rows with a close of 0 (all 2023) and 5 rows with high < low (IBBLPBOND, DSES). Dropped.
* 25,554 rows with zero volume. Kept: a listed instrument with no trades that day is real
  information, and most of these fall in the floor-price period.

### 5. The curated archive passes the same tests

The second archive had to be downloaded by hand (its file list is not exposed through
Mendeley's API). Before loading a single row it was put through the same battery:

| Test | Result |
|---|---|
| Duplicate (symbol, date) rows | 0 |
| Sessions dated Friday/Saturday | 4 in 14 years (2 in 2016, 2 in 2025; the 2025 ones are real working Saturdays) |
| Daily moves beyond the circuit-breaker cap, per year | 0.05% to 0.5% (bonus-share record dates, floor exits) |
| DSEX, week of 2021-03-01 | 5,427 to 5,604, correct; 2021 peak 7,368 on 2021-10-10, correct |
| Agreement with the live site + fallback archive, 10 stocks, 2023-2026 | 7,062 rows, 100% of closes, 99.6% of volumes (live-side revisions) |

It is loaded first; the fallback archive only fills what it lacks (sessions after
2026-01-22 come from the live site anyway). The unadjusted variant is used because it is
what the exchange printed and what the live feed serves. The adjusted variant differs only
around corporate actions and is kept on disk for later use.

## Market regimes that must be masked

Thresholds are calibrated on "normal" sessions. Two periods are excluded from calibration
and flagged in backtests:

* **2010-11 bubble and crash.** DGEN peaked on 2010-12-05 and lost about half its value
  over the following year. Daily moves in that window are not representative of today's
  slab-based circuit breakers.
* **Floor-price period, 2022-07-28 to 2024-01-18.** The regulator fixed a minimum price per
  stock; most instruments could not fall and many did not trade for months. Volatility and
  volume measured here are artificially low. A handful of instruments stayed under a floor
  until 2024-08.

## Open items

* History before October 2012 has no clean source yet. The 1999-2012 span of the fallback
  archive is misdated (§3) and the live site serves nothing that old.
* Corporate-action adjustment (bonus shares, splits, rights) is not applied. Bonus issues on
  DSE typically produce a one-day price drop on the record date; rules that look at
  multi-day moves need to know about record dates, which the dividend feed provides.
