"""Calibration study: what counts as "unusual" on the DSE, measured rather than guessed.

Reads the ``features`` table (see ``features.py``) for the tradeable universe over the
clean, normal-regime history and produces:

* ``category_stats``  percentiles of daily/10-day moves and volume ratios per category
* ``event_stats``     how often each candidate alert condition fires and what followed
* ``symbol_stats``    per-symbol baselines the rule engine uses for stock-specific thresholds
                      (also materialised as a DuckDB table of the same name)

The markdown report in docs/calibration.md is generated from these numbers.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from dse_alerts.data.store import MarketStore

# Full clean history. The floor-price period, the 2020 closure and the 2010-11 crash are
# masked by regime, so what remains is comparable across years.
DEFAULT_SINCE = dt.date(2013, 1, 1)

# Candidate alert conditions evaluated in the event study. Each is a SQL predicate on features.
EVENTS = {
    "drop_10d_ge_10pct": "ret_10 <= -0.10",
    "drop_10d_ge_15pct": "ret_10 <= -0.15",
    "rise_10d_ge_15pct": "ret_10 >= 0.15",
    "upper_circuit": "hit_upper",
    "upper_circuit_streak_ge_3": "upper_streak >= 3",
    "lower_circuit": "hit_lower",
    "lower_circuit_streak_ge_3": "lower_streak >= 3",
    "volume_3x_and_up_5pct": "vol_ratio_20 >= 3 AND ret_1 >= 0.05",
    "volume_3x_and_down_5pct": "vol_ratio_20 >= 3 AND ret_1 <= -0.05",
    "volume_5x": "vol_ratio_20 >= 5",
    "new_250d_high": "close > high_250",
    "new_250d_low": "close < low_250",
    "beats_index_by_5pct": "ret_1 - dsex_ret_1 >= 0.05",
    "lags_index_by_5pct": "ret_1 - dsex_ret_1 <= -0.05",
}

_UNIVERSE = """
    SELECT f.*, coalesce(i.category, 'NA') AS category, i.sector
    FROM features f
    JOIN instruments i USING (symbol)
    WHERE i.tradeable AND f.regime = 'normal' AND f.date >= ? AND f.ret_1 IS NOT NULL
"""


def run(store: MarketStore, since: dt.date = DEFAULT_SINCE) -> dict:
    with store.connect() as con:
        con.execute(f"CREATE OR REPLACE TEMP TABLE u AS {_UNIVERSE}", [since])
        n, syms, a, b = con.execute("SELECT count(*), count(DISTINCT symbol), min(date), max(date) FROM u").fetchone()

        category = con.execute(
            """
            SELECT category, count(*) AS sessions, count(DISTINCT symbol) AS symbols,
                   quantile_cont(abs(ret_1), 0.5)  AS abs_ret1_p50,
                   quantile_cont(abs(ret_1), 0.9)  AS abs_ret1_p90,
                   quantile_cont(abs(ret_1), 0.99) AS abs_ret1_p99,
                   quantile_cont(ret_10, 0.05) AS ret10_p05,
                   quantile_cont(ret_10, 0.95) AS ret10_p95,
                   quantile_cont(vol_ratio_20, 0.9)  AS vol_ratio_p90,
                   quantile_cont(vol_ratio_20, 0.99) AS vol_ratio_p99,
                   250.0 * avg(CASE WHEN hit_upper THEN 1 ELSE 0 END) AS upper_hits_per_250,
                   250.0 * avg(CASE WHEN hit_lower THEN 1 ELSE 0 END) AS lower_hits_per_250,
                   avg(CASE WHEN volume = 0 THEN 1 ELSE 0 END) AS zero_volume_share
            FROM u GROUP BY 1 ORDER BY 1
            """
        ).df()

        rows = []
        for name, pred in EVENTS.items():
            r = con.execute(
                f"""
                SELECT count(*) AS n, count(DISTINCT symbol) AS symbols,
                       250.0 * count(*) / (SELECT count(*) FROM u) AS per_250_sessions,
                       quantile_cont(fwd_5, 0.5) AS fwd5_median, avg(fwd_5) AS fwd5_mean,
                       quantile_cont(fwd_20, 0.5) AS fwd20_median, avg(fwd_20) AS fwd20_mean,
                       avg(CASE WHEN fwd_20 > 0 THEN 1.0 ELSE 0.0 END) AS fwd20_positive_share
                FROM u WHERE ({pred}) AND fwd_20 IS NOT NULL
                """
            ).fetchone()
            base = con.execute("SELECT quantile_cont(fwd_20, 0.5), avg(fwd_20), avg(CASE WHEN fwd_20 > 0 THEN 1.0 ELSE 0.0 END) FROM u WHERE fwd_20 IS NOT NULL").fetchone()
            rows.append({"event": name, "predicate": pred, "n": r[0], "symbols": r[1], "per_250_sessions": r[2],
                         "fwd5_median": r[3], "fwd5_mean": r[4], "fwd20_median": r[5], "fwd20_mean": r[6],
                         "fwd20_positive_share": r[7],
                         "base_fwd20_median": base[0], "base_fwd20_mean": base[1], "base_fwd20_positive_share": base[2]})
        events = pd.DataFrame(rows)

        con.execute(
            """
            CREATE OR REPLACE TABLE symbol_stats AS
            SELECT symbol, category, sector, count(*) AS sessions, min(date) AS first_date, max(date) AS last_date,
                   stddev_samp(ret_1) AS ret1_sd,
                   quantile_cont(abs(ret_1), 0.95) AS abs_ret1_p95,
                   quantile_cont(ret_10, 0.05) AS ret10_p05,
                   quantile_cont(ret_10, 0.95) AS ret10_p95,
                   quantile_cont(ret_20, 0.05) AS ret20_p05,
                   quantile_cont(ret_20, 0.95) AS ret20_p95,
                   quantile_cont(vol_ratio_20, 0.95) AS vol_ratio_p95,
                   median(volume) AS median_volume,
                   250.0 * avg(CASE WHEN hit_upper THEN 1 ELSE 0 END) AS upper_hits_per_250,
                   250.0 * avg(CASE WHEN hit_lower THEN 1 ELSE 0 END) AS lower_hits_per_250,
                   avg(CASE WHEN volume = 0 THEN 1 ELSE 0 END) AS zero_volume_share,
                   now() AS computed_at
            FROM u GROUP BY 1, 2, 3
            """
        )
        symbol_stats = con.execute("SELECT * FROM symbol_stats ORDER BY symbol").df()

    return {
        "since": since, "sessions": n, "symbols": syms, "first": a, "last": b,
        "category_stats": category, "event_stats": events, "symbol_stats": symbol_stats,
    }


def run_events(store: MarketStore, predicates: dict[str, str], since: dt.date = DEFAULT_SINCE) -> pd.DataFrame:
    """Event study for arbitrary predicates over features joined with symbol_stats (so rule
    predicates can refer to per-symbol percentiles). Requires ``run`` to have been executed
    once, which creates ``symbol_stats``."""
    with store.connect() as con:
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE u AS
            SELECT f.*, s.ret1_sd, s.abs_ret1_p95, s.ret10_p05, s.ret10_p95, s.ret20_p05, s.ret20_p95, s.vol_ratio_p95
            FROM ({_UNIVERSE}) f LEFT JOIN symbol_stats s USING (symbol)
            """,
            [since],
        )
        base = con.execute(
            "SELECT quantile_cont(fwd_20, 0.5), avg(fwd_20), avg(CASE WHEN fwd_20 > 0 THEN 1.0 ELSE 0.0 END) FROM u WHERE fwd_20 IS NOT NULL"
        ).fetchone()
        total = con.execute("SELECT count(*) FROM u").fetchone()[0]
        rows = []
        for name, pred in predicates.items():
            r = con.execute(
                f"""
                SELECT count(*), count(DISTINCT symbol),
                       quantile_cont(fwd_5, 0.5), avg(fwd_5), quantile_cont(fwd_20, 0.5), avg(fwd_20),
                       avg(CASE WHEN fwd_20 > 0 THEN 1.0 ELSE 0.0 END)
                FROM u WHERE ({pred}) AND fwd_20 IS NOT NULL
                """
            ).fetchone()
            rows.append({"event": name, "predicate": pred, "n": r[0], "symbols": r[1],
                         "per_250_sessions": 250.0 * r[0] / total if total else 0.0,
                         "fwd5_median": r[2], "fwd5_mean": r[3], "fwd20_median": r[4], "fwd20_mean": r[5],
                         "fwd20_positive_share": r[6],
                         "base_fwd20_median": base[0], "base_fwd20_mean": base[1], "base_fwd20_positive_share": base[2]})
    return pd.DataFrame(rows)


STATS_COLUMNS = ["symbol", "category", "sector", "sessions", "first_date", "last_date", "ret1_sd", "abs_ret1_p95",
                 "ret10_p05", "ret10_p95", "ret20_p05", "ret20_p95", "vol_ratio_p95", "median_volume",
                 "upper_hits_per_250", "lower_hits_per_250", "zero_volume_share", "computed_at"]


def export_stats(store: MarketStore, path: Path) -> int:
    """Write symbol_stats to CSV so a machine without the long history (e.g. CI) can use it."""
    with store.connect(read_only=True) as con:
        df = con.execute("SELECT * FROM symbol_stats ORDER BY symbol").df()
    path.parent.mkdir(parents=True, exist_ok=True)
    df[STATS_COLUMNS].to_csv(path, index=False)
    return len(df)


def import_stats(store: MarketStore, path: Path, force: bool = False) -> int:
    """Load symbol_stats from CSV unless the table already has rows (or force=True)."""
    df = pd.read_csv(path, parse_dates=["first_date", "last_date", "computed_at"])
    with store.connect() as con:
        exists = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'symbol_stats'").fetchone()[0]
        if exists and not force and con.execute("SELECT count(*) FROM symbol_stats").fetchone()[0]:
            return 0
        con.register("incoming", df)
        con.execute("CREATE OR REPLACE TABLE symbol_stats AS SELECT * FROM incoming")
        con.unregister("incoming")
    return len(df)


def _pct(x: float | None, digits: int = 1) -> str:
    return "-" if x is None or pd.isna(x) else f"{100 * x:+.{digits}f}%"


def write_report(result: dict, path: Path, json_path: Path | None = None) -> None:
    cat, ev = result["category_stats"], result["event_stats"]
    lines = [
        "# Calibration: what \"unusual\" means on the DSE",
        "",
        (f"Generated {dt.date.today()} from {result['sessions']:,} sessions of {result['symbols']} tradeable "
         f"instruments, {result['first']} to {result['last']}. Sessions in masked regimes are excluded: the "
         "2020 exchange closure and the floor-price period (2022-07-28 to 2024-01-18), when most stocks could "
         "not fall. Categories are the instruments' current ones; a stock that moved between A, B and Z over "
         "the years is counted under today's letter. Regenerate with `dse analysis calibrate`."),
        "",
        "## By category",
        "",
        "| Category | Symbols | Sessions | Median daily move | 90th pct daily move | 99th pct daily move | 10-day move, 5th pct | 10-day move, 95th pct | Volume ratio p90 | Upper-circuit hits / 250 | Lower-circuit hits / 250 | Zero-volume sessions |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in cat.itertuples():
        lines.append(
            f"| {r.category} | {r.symbols} | {r.sessions:,} | {100*r.abs_ret1_p50:.2f}% | {100*r.abs_ret1_p90:.1f}% | "
            f"{100*r.abs_ret1_p99:.1f}% | {_pct(r.ret10_p05)} | {_pct(r.ret10_p95)} | {r.vol_ratio_p90:.1f}x | "
            f"{r.upper_hits_per_250:.1f} | {r.lower_hits_per_250:.1f} | {100*r.zero_volume_share:.1f}% |"
        )
    base = ev.iloc[0]
    lines += [
        "",
        "## Candidate alert conditions: how often they fire and what followed",
        "",
        (f"Baseline over all sessions: median 20-session forward return {_pct(base.base_fwd20_median)}, "
        f"mean {_pct(base.base_fwd20_mean)}, positive {100*base.base_fwd20_positive_share:.0f}% of the time."),
        "",
        "| Condition | Fires per 250 sessions (per stock) | Events | Fwd 5 median | Fwd 20 median | Fwd 20 mean | Fwd 20 positive |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in ev.itertuples():
        lines.append(
            f"| `{r.event}` | {r.per_250_sessions:.2f} | {r.n:,} | {_pct(r.fwd5_median)} | {_pct(r.fwd20_median)} | "
            f"{_pct(r.fwd20_mean)} | {100*r.fwd20_positive_share:.0f}% |"
        )
    lines += [
        "",
        ("\"Fires per 250 sessions\" is per stock per trading year: 0.5 means a typical watchlist of 10 stocks "
        "produces about 5 of these alerts a year. Forward returns are what happened next on average, not a "
        "prediction; they are shown so that each rule can be judged on evidence."),
        "",
        "## Per-symbol baselines",
        "",
        ("Stored in the `symbol_stats` table: for every tradeable instrument, the standard deviation and 95th "
        "percentile of daily moves, the 5th/95th percentiles of 10- and 20-session moves, the 95th percentile "
        "of the volume ratio, median volume, and circuit-hit rates. Rules use these so that a move counts as "
        "unusual relative to the stock's own history."),
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if json_path:
        payload = {
            "since": str(result["since"]), "sessions": result["sessions"], "symbols": result["symbols"],
            "first": str(result["first"]), "last": str(result["last"]),
            "category_stats": json.loads(cat.to_json(orient="records")),
            "event_stats": json.loads(ev.to_json(orient="records")),
        }
        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
