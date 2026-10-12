# Calibration: what "unusual" means on the DSE

Generated 2026-10-13 from 976,943 sessions of 395 tradeable instruments, 2013-01-01 to 2026-10-12. Sessions in masked regimes are excluded: the 2020 exchange closure and the floor-price period (2022-07-28 to 2024-01-18), when most stocks could not fall. Categories are the instruments' current ones; a stock that moved between A, B and Z over the years is counted under today's letter. Regenerate with `dse analysis calibrate`.

## By category

| Category | Symbols | Sessions | Median daily move | 90th pct daily move | 99th pct daily move | 10-day move, 5th pct | 10-day move, 95th pct | Volume ratio p90 | Upper-circuit hits / 250 | Lower-circuit hits / 250 | Zero-volume sessions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 195 | 498,791 | 1.14% | 3.8% | 9.1% | -10.5% | +14.3% | 2.3x | 1.8 | 0.3 | 0.3% |
| B | 73 | 158,226 | 1.44% | 4.6% | 9.8% | -12.9% | +17.4% | 2.3x | 2.8 | 0.6 | 0.2% |
| NA | 1 | 2,825 | 1.64% | 5.4% | 9.7% | -12.5% | +21.7% | 2.3x | 2.9 | 0.7 | 0.2% |
| Z | 126 | 317,101 | 1.40% | 4.8% | 9.8% | -13.2% | +17.8% | 2.3x | 2.7 | 0.7 | 0.8% |

## Candidate alert conditions: how often they fire and what followed

Baseline over all sessions: median 20-session forward return -0.7%, mean +0.9%, positive 44% of the time.

| Condition | Fires per 250 sessions (per stock) | Events | Fwd 5 median | Fwd 20 median | Fwd 20 mean | Fwd 20 positive |
|---|---|---|---|---|---|---|
| `drop_10d_ge_10pct` | 18.62 | 72,782 | +0.0% | +0.0% | +1.8% | 47% |
| `drop_10d_ge_15pct` | 6.24 | 24,383 | +1.2% | +0.8% | +4.0% | 51% |
| `rise_10d_ge_15pct` | 13.91 | 54,364 | -1.4% | -2.4% | +3.1% | 42% |
| `upper_circuit` | 2.24 | 8,739 | +1.8% | -0.1% | +7.4% | 49% |
| `upper_circuit_streak_ge_3` | 0.18 | 693 | +8.0% | +1.2% | +20.3% | 51% |
| `lower_circuit` | 0.48 | 1,867 | -3.2% | -3.6% | -0.1% | 40% |
| `lower_circuit_streak_ge_3` | 0.02 | 89 | -8.0% | -4.9% | +0.8% | 37% |
| `volume_3x_and_up_5pct` | 4.53 | 17,697 | -0.7% | -1.9% | +2.9% | 44% |
| `volume_3x_and_down_5pct` | 0.56 | 2,180 | -0.7% | +0.0% | +7.5% | 47% |
| `volume_5x` | 5.11 | 19,970 | -0.3% | -0.4% | +2.9% | 46% |
| `new_250d_high` | 2.31 | 9,035 | +0.2% | -0.7% | +2.8% | 48% |
| `new_250d_low` | 3.49 | 13,633 | +0.0% | +1.1% | +2.6% | 53% |
| `beats_index_by_5pct` | 12.03 | 47,030 | -0.9% | -2.0% | +3.0% | 43% |
| `lags_index_by_5pct` | 6.07 | 23,736 | +0.0% | -0.1% | +4.2% | 47% |

"Fires per 250 sessions" is per stock per trading year: 0.5 means a typical watchlist of 10 stocks produces about 5 of these alerts a year. Forward returns are what happened next on average, not a prediction; they are shown so that each rule can be judged on evidence.

## The rules as shipped

The same study for each rule's own test with its default parameters, which is what the Rules page and the AI note quote. Rules that fire once per episode are counted once per episode here too; stock-specific rules use each stock's own percentiles.

| Rule | Fires per 250 sessions (per stock) | Events | Fwd 5 median | Fwd 20 median | Fwd 20 mean | Fwd 20 positive |
|---|---|---|---|---|---|---|
| `big_drop` | 4.21 | 16,463 | +0.1% | +0.0% | +1.6% | 47% |
| `big_rise` | 3.41 | 13,306 | -1.0% | -2.0% | +1.5% | 42% |
| `upper_circuit` | 2.24 | 8,739 | +1.8% | -0.1% | +7.4% | 49% |
| `lower_circuit` | 0.48 | 1,867 | -3.2% | -3.6% | -0.1% | 40% |
| `volume_spike` | 6.98 | 27,286 | -0.8% | -1.5% | +2.4% | 44% |
| `new_250d_high` | 1.39 | 5,447 | +0.0% | -0.8% | +2.4% | 48% |
| `new_250d_low` | 2.06 | 8,038 | +0.0% | +0.5% | +1.6% | 51% |
| `vs_index` | 18.11 | 70,766 | -0.5% | -1.4% | +3.4% | 44% |
| `ma_cross` | 18.72 | 73,169 | -0.4% | -1.0% | +0.8% | 43% |

## Per-symbol baselines

Stored in the `symbol_stats` table: for every tradeable instrument, the standard deviation and 95th percentile of daily moves, the 5th/95th percentiles of 10- and 20-session moves, the 95th percentile of the volume ratio, median volume, and circuit-hit rates. Rules use these so that a move counts as unusual relative to the stock's own history.
