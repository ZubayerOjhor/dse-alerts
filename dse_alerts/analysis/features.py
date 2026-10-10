"""Per-session features for every instrument, computed in DuckDB from ``prices``.

Everything is defined on the *sequence of sessions* per symbol (``lag``/``lead`` over rows),
not on calendar days, because the pre-2023 archive has sessions whose dates are one day
off (see docs/data-quality.md). The result is materialised as table ``features``.

Columns (one row per symbol, session):

* ``ret_1 / ret_5 / ret_10 / ret_20``   close vs the close 1/5/10/20 sessions earlier
* ``fwd_5 / fwd_20``                     close 5/20 sessions later vs today (for backtests only)
* ``vol_ratio_20``                       volume / mean volume of the previous 20 sessions
* ``slab_limit``                         daily price-move limit implied by the previous close
* ``hit_upper / hit_lower``              closed at (or within a tick of) the upper / lower limit
* ``upper_streak / lower_streak``        consecutive sessions at that limit, including today
* ``high_250 / low_250``                 highest high / lowest low of the previous 250 sessions
* ``dsex_ret_1``                         DSEX move on the same date, where an index series exists
* ``regime``                             'normal' | 'bubble_2010' | 'covid_closure' | 'floor_price'
* ``gap_days``                           calendar days since the previous session
* ``date_quality``                       carried from prices
"""

from __future__ import annotations

from dse_alerts.data.store import MarketStore

# BSEC slab-based daily price limits (percent of previous close), by previous close in Taka.
# Verified 2026-10-10. Applied to the whole history as an approximation; earlier regimes
# differed (flat 10%, temporary 2%/5% lower limits in 2021-22). Hits are detected with a
# tolerance of 0.3 percentage points to absorb tick rounding.
SLABS = [(200, 0.10), (500, 0.0875), (1000, 0.075), (2000, 0.0625), (5000, 0.05), (float("inf"), 0.0375)]
HIT_TOLERANCE = 0.003

REGIMES = {
    "bubble_2010": ("2010-10-01", "2011-12-31"),   # run-up, 2010-12-05 peak, crash
    "covid_closure": ("2020-03-26", "2020-05-30"),  # DSE closed for 66 days
    "floor_price": ("2022-07-28", "2024-01-18"),    # regulator-imposed minimum prices
}

_SLAB_SQL = (
    "CASE "
    + " ".join(f"WHEN prev_close <= {hi} THEN {pct}" for hi, pct in SLABS[:-1])
    + f" ELSE {SLABS[-1][1]} END"
)
_REGIME_SQL = (
    "CASE "
    + " ".join(f"WHEN g.date BETWEEN DATE '{a}' AND DATE '{b}' THEN '{name}'" for name, (a, b) in REGIMES.items())
    + " ELSE 'normal' END"
)

FEATURES_SQL = f"""
CREATE OR REPLACE TABLE index_daily AS
    SELECT date, close AS dsex FROM prices WHERE symbol = 'DSEX' AND date_quality <> 'unreliable'
    UNION ALL
    SELECT date, dsex FROM market_summary
    WHERE dsex IS NOT NULL AND date NOT IN (SELECT date FROM prices WHERE symbol = 'DSEX');

CREATE OR REPLACE TABLE features AS
WITH base AS (
    SELECT p.symbol, p.date, p.open, p.high, p.low, p.close, p.volume, p.source, p.date_quality,
           lag(close)      OVER w AS prev_close,
           lag(close, 5)   OVER w AS close_5,
           lag(close, 10)  OVER w AS close_10,
           lag(close, 20)  OVER w AS close_20,
           lead(close, 5)  OVER w AS close_f5,
           lead(close, 20) OVER w AS close_f20,
           avg(volume)  OVER (w ROWS BETWEEN 20 PRECEDING AND 1 PRECEDING) AS avg_vol_20,
           max(high)    OVER (w ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) AS high_250,
           min(low)     OVER (w ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) AS low_250,
           count(*)     OVER (w ROWS BETWEEN 250 PRECEDING AND 1 PRECEDING) AS n_prev_250,
           date - lag(date) OVER w AS gap_days
    FROM prices p
    WHERE p.symbol NOT IN ('DSEX', 'DS30', 'DSES') AND p.date_quality <> 'unreliable'
    WINDOW w AS (PARTITION BY symbol ORDER BY date)
),
lim AS (
    SELECT *,
           {_SLAB_SQL} AS slab_limit,
           close / prev_close - 1 AS ret_1
    FROM base
),
hits AS (
    SELECT *,
           ret_1 >=  slab_limit - {HIT_TOLERANCE} AS hit_upper,
           ret_1 <= -slab_limit + {HIT_TOLERANCE} AS hit_lower
    FROM lim
),
grp AS (
    SELECT *,
           sum(CASE WHEN coalesce(hit_upper, false) THEN 0 ELSE 1 END) OVER w AS up_grp,
           sum(CASE WHEN coalesce(hit_lower, false) THEN 0 ELSE 1 END) OVER w AS dn_grp
    FROM hits
    WINDOW w AS (PARTITION BY symbol ORDER BY date ROWS UNBOUNDED PRECEDING)
)
SELECT g.symbol, g.date, g.open, g.high, g.low, g.close, g.volume, g.source, g.date_quality,
       g.prev_close, g.ret_1,
       g.close / g.close_5  - 1 AS ret_5,
       g.close / g.close_10 - 1 AS ret_10,
       g.close / g.close_20 - 1 AS ret_20,
       g.close_f5  / g.close - 1 AS fwd_5,
       g.close_f20 / g.close - 1 AS fwd_20,
       CASE WHEN g.avg_vol_20 > 0 THEN g.volume / g.avg_vol_20 END AS vol_ratio_20,
       g.slab_limit,
       coalesce(g.hit_upper, false) AS hit_upper,
       coalesce(g.hit_lower, false) AS hit_lower,
       CASE WHEN coalesce(g.hit_upper, false)
            THEN count(*) OVER (PARTITION BY g.symbol, g.up_grp ORDER BY g.date ROWS UNBOUNDED PRECEDING) - 1
            ELSE 0 END AS upper_streak,
       CASE WHEN coalesce(g.hit_lower, false)
            THEN count(*) OVER (PARTITION BY g.symbol, g.dn_grp ORDER BY g.date ROWS UNBOUNDED PRECEDING) - 1
            ELSE 0 END AS lower_streak,
       CASE WHEN g.n_prev_250 >= 200 THEN g.high_250 END AS high_250,
       CASE WHEN g.n_prev_250 >= 200 THEN g.low_250  END AS low_250,
       i.dsex / lag(i.dsex) OVER (ORDER BY i.date) - 1 AS dsex_ret_1,
       {_REGIME_SQL} AS regime,
       g.gap_days
FROM grp g
LEFT JOIN index_daily i ON i.date = g.date
ORDER BY g.symbol, g.date;
"""


def build(store: MarketStore) -> dict:
    with store.connect() as con:
        con.execute(FEATURES_SQL)
        n, syms, a, b = con.execute("SELECT count(*), count(DISTINCT symbol), min(date), max(date) FROM features").fetchone()
        idx = con.execute("SELECT count(*), min(date), max(date) FROM index_daily").fetchone()
    return {"rows": n, "symbols": syms, "first": a, "last": b, "index_sessions": idx[0], "index_range": (idx[1], idx[2])}
