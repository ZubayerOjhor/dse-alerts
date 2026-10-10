# Calibration: what "unusual" means on the DSE

Generated 2026-10-10 from 253,452 sessions of 395 tradeable instruments, 2024-01-21 to 2026-10-08 (normal regime only, i.e. after the floor-price period ended on 2024-01-18). Regenerate with `dse analysis calibrate`.

## By category

| Category | Symbols | Sessions | Median daily move | 90th pct daily move | 99th pct daily move | 10-day move, 5th pct | 10-day move, 95th pct | Volume ratio p90 | Upper-circuit hits / 250 | Lower-circuit hits / 250 | Zero-volume sessions |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A | 195 | 124,964 | 1.16% | 3.7% | 8.9% | -11.2% | +14.0% | 2.3x | 1.5 | 0.5 | 1.1% |
| B | 73 | 46,529 | 1.60% | 4.4% | 9.8% | -13.9% | +16.5% | 2.2x | 2.3 | 0.6 | 0.7% |
| NA | 1 | 646 | 1.60% | 4.2% | 8.7% | -10.5% | +17.8% | 2.1x | 2.3 | 0.8 | 0.5% |
| Z | 126 | 81,313 | 1.61% | 5.4% | 9.9% | -15.3% | +18.3% | 2.3x | 3.1 | 1.0 | 3.1% |

## Candidate alert conditions: how often they fire and what followed

Baseline over all sessions: median 20-session forward return -1.3%, mean -0.0%, positive 41% of the time.

| Condition | Fires per 250 sessions (per stock) | Events | Fwd 5 median | Fwd 20 median | Fwd 20 mean | Fwd 20 positive |
|---|---|---|---|---|---|---|
| `drop_10d_ge_10pct` | 22.95 | 23,266 | +0.2% | -1.2% | +0.3% | 44% |
| `drop_10d_ge_15pct` | 8.51 | 8,630 | +1.6% | +0.0% | +2.2% | 49% |
| `rise_10d_ge_15pct` | 13.52 | 13,704 | -2.0% | -3.9% | +0.9% | 38% |
| `upper_circuit` | 2.13 | 2,160 | +1.9% | -0.6% | +7.1% | 48% |
| `upper_circuit_streak_ge_3` | 0.13 | 136 | +8.3% | +1.8% | +23.8% | 53% |
| `lower_circuit` | 0.65 | 658 | -5.7% | -6.1% | +0.3% | 36% |
| `lower_circuit_streak_ge_3` | 0.05 | 55 | -9.8% | -5.7% | +3.1% | 38% |
| `volume_3x_and_up_5pct` | 4.70 | 4,764 | -1.1% | -2.9% | +1.2% | 40% |
| `volume_3x_and_down_5pct` | 0.95 | 962 | -2.6% | -1.2% | +1.8% | 45% |
| `volume_5x` | 5.06 | 5,134 | -1.4% | -2.0% | +0.6% | 40% |
| `new_250d_high` | 2.89 | 2,929 | +0.0% | +0.0% | +1.6% | 30% |
| `new_250d_low` | 0.27 | 276 | +0.0% | +4.0% | +6.8% | 58% |
| `beats_index_by_5pct` | 8.59 | 8,709 | -0.6% | -1.5% | +4.6% | 44% |
| `lags_index_by_5pct` | 4.95 | 5,018 | +0.0% | +0.0% | +3.4% | 48% |

"Fires per 250 sessions" is per stock per trading year: 0.5 means a typical watchlist of 10 stocks produces about 5 of these alerts a year. Forward returns are what happened next on average, not a prediction; they are shown so that each rule can be judged on evidence.

## Per-symbol baselines

Stored in the `symbol_stats` table: for every tradeable instrument, the standard deviation and 95th percentile of daily moves, the 5th/95th percentiles of 10- and 20-session moves, the 95th percentile of the volume ratio, median volume, and circuit-hit rates. Rules use these so that a move counts as unusual relative to the stock's own history.
