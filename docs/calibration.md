# Calibration: what "unusual" means on the DSE

Generated 2026-10-11 from 977,329 sessions of 395 tradeable instruments, 2013-01-01 to 2026-10-08. Sessions in masked regimes are excluded: the 2020 exchange closure and the floor-price period (2022-07-28 to 2024-01-18), when most stocks could not fall. Categories are the instruments' current ones; a stock that moved between A, B and Z over the years is counted under today's letter. Regenerate with `dse analysis calibrate`.

## By category

| Category | Symbols | Sessions | Median daily move | 90th pct daily move | 99th pct daily move | 10-day move, 5th pct | 10-day move, 95th pct | Volume ratio p90 | Upper-circuit hits / 250 | Lower-circuit hits / 250 | Zero-volume sessions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 195 | 498,967 | 1.14% | 3.8% | 9.3% | -10.8% | +14.1% | 2.3x | 1.9 | 0.5 | 0.3% |
| B | 73 | 158,277 | 1.44% | 4.6% | 9.8% | -13.0% | +17.2% | 2.3x | 2.9 | 0.8 | 0.2% |
| NA | 1 | 2,826 | 1.64% | 5.4% | 9.7% | -12.6% | +21.7% | 2.4x | 3.0 | 0.9 | 0.2% |
| Z | 126 | 317,259 | 1.40% | 4.9% | 9.8% | -13.5% | +17.6% | 2.3x | 2.8 | 0.9 | 0.8% |

## Candidate alert conditions: how often they fire and what followed

Baseline over all sessions: median 20-session forward return -0.8%, mean +0.6%, positive 43% of the time.

| Condition | Fires per 250 sessions (per stock) | Events | Fwd 5 median | Fwd 20 median | Fwd 20 mean | Fwd 20 positive |
|---|---|---|---|---|---|---|
| `drop_10d_ge_10pct` | 19.47 | 76,111 | +0.0% | -0.3% | +1.0% | 47% |
| `drop_10d_ge_15pct` | 6.80 | 26,596 | +0.8% | +0.2% | +2.5% | 50% |
| `rise_10d_ge_15pct` | 13.69 | 53,517 | -1.4% | -2.7% | +1.7% | 42% |
| `upper_circuit` | 2.36 | 9,225 | +1.4% | -0.7% | +6.6% | 47% |
| `upper_circuit_streak_ge_3` | 0.18 | 716 | +8.2% | +1.2% | +21.0% | 52% |
| `lower_circuit` | 0.69 | 2,700 | -2.0% | -2.6% | +0.3% | 42% |
| `lower_circuit_streak_ge_3` | 0.02 | 94 | -7.4% | -5.9% | -0.4% | 36% |
| `volume_3x_and_up_5pct` | 4.53 | 17,724 | -0.7% | -2.1% | +2.7% | 43% |
| `volume_3x_and_down_5pct` | 0.59 | 2,301 | -0.8% | -0.4% | +2.0% | 46% |
| `volume_5x` | 5.15 | 20,122 | -0.3% | -0.6% | +2.4% | 45% |
| `new_250d_high` | 2.11 | 8,250 | +0.2% | -1.0% | +2.5% | 47% |
| `new_250d_low` | 3.67 | 14,334 | +0.0% | +1.0% | +2.4% | 53% |
| `beats_index_by_5pct` | 11.99 | 46,866 | -0.9% | -2.2% | +2.5% | 43% |
| `lags_index_by_5pct` | 6.21 | 24,283 | +0.0% | -0.5% | +1.8% | 46% |

"Fires per 250 sessions" is per stock per trading year: 0.5 means a typical watchlist of 10 stocks produces about 5 of these alerts a year. Forward returns are what happened next on average, not a prediction; they are shown so that each rule can be judged on evidence.

## Per-symbol baselines

Stored in the `symbol_stats` table: for every tradeable instrument, the standard deviation and 95th percentile of daily moves, the 5th/95th percentiles of 10- and 20-session moves, the 95th percentile of the volume ratio, median volume, and circuit-hit rates. Rules use these so that a move counts as unusual relative to the stock's own history.
