# Data sources and data quality

Everything the alert engine computes rests on daily OHLCV rows. This page records where
they come from, what was checked, and what was done about the problems found. All checks
were run on 2026-10-10 and can be repeated with `dse data validate`.

## Sources

| Source | Coverage | Used for | Licence |
|---|---|---|---|
| **dse.com.bd** (live site, via [bdshare](https://github.com/rochi88/bdshare)) | daily history from ~2024-10-14; live quotes; PSI news; dividend declarations; 30-day index summary | the daily refresh and every alert | site data, fetched by each user for their own use |
| **Mendeley: DSE Historical Data 1999-2025** ([doi:10.17632/5mww8rb9td.1](https://data.mendeley.com/datasets/5mww8rb9td/1)) | 1999-01-02 to 2025-04-08, 534 instruments, 1.52M rows | calibrating thresholds and backtesting alerts | CC BY 4.0 |

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

### 3. The calendar problem (1999-2022)

DSE does not trade on Fridays or Saturdays, yet the archive has sessions dated on them:

| Year | Sun-Thu days with no data | Fri/Sat days with data |
|---|---|---|
| 2016 | 54 | 35 |
| 2021 | 50 | 30 |
| 2022 | 44 | 14 |
| 2023 | 18 | 0 |
| 2024 | 27 | 0 |

A clean year has about 20 weekday holidays and almost no weekend sessions, which is exactly
what 2023-2025 show. (Genuine Saturday sessions do exist: the government occasionally
declares a Saturday a working day to compensate for long Eid holidays, and DSE trades. The
live data has three such Saturdays, 2025-05-17, 2025-05-24 and 2026-05-23, each with the
full market trading. The pre-2023 archive has 25-60 weekend dates a year, which is a
different thing.) Before 2023, roughly 25-35 sessions a year carry a date one day off (for
example GP's session of Thursday 2021-03-04 is dated Friday 2021-03-05), and a similar
number of weekdays have no row at all, often the day the shifted session should have had.

What this means for the analysis:

* The **order** of sessions per instrument is intact, so returns and volume baselines built
  on consecutive sessions are reliable throughout.
* **Calendar-dependent** features (day-of-week effects, exact event-day alignment with news
  or record dates) are only trusted from 2023-01-01 onward.
* Each row carries a `date_quality` flag: `exact` (2023+ and all live rows), `approx`
  (pre-2023 weekday) or `shifted` (pre-2023 Fri/Sat).

### 4. Other row-level problems

* 66 rows with a close of 0 (all 2023) and 5 rows with high < low (IBBLPBOND, DSES). Dropped.
* 25,554 rows with zero volume. Kept: a listed instrument with no trades that day is real
  information, and most of these fall in the floor-price period.

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

* A second, academically curated archive (Mendeley [doi:10.17632/23553sm4tn.4](https://data.mendeley.com/datasets/23553sm4tn/4),
  2012-2026, with an adjusted-price variant and coverage metadata) could replace the
  2012-2022 span with a clean calendar. Its file listing is not exposed through the public
  API; it needs a manual download. Planned as a follow-up comparison.
* Corporate-action adjustment (bonus shares, splits, rights) is not applied. Bonus issues on
  DSE typically produce a one-day price drop on the record date; rules that look at
  multi-day moves need to know about record dates, which the dividend feed provides.
