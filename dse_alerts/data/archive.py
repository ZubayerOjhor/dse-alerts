"""Historical archives for years the live DSE site no longer serves.

Two archives are supported; both are CC BY 4.0 on Mendeley Data.

**Curated (preferred): "DSE End-of-Day Financial Dataset with Coverage Metadata"**,
doi:10.17632/23553sm4tn.4 (Muhammad, Islam, Alam; Southeast University / AUST).
2012-10-01 to 2026-01-22, 486 instruments incl. the DSEX/DS30/DSES indexes, unadjusted and
adjusted variants. Its files are only served to a browser, so the user downloads the zip
and extracts it under ``data/raw/mendeley_23553sm4tn/``; ``find_curated`` locates it.
Verified 2026-10-10: no duplicates, no weekend sessions, circuit-breaker violations
0.05-0.5% per year, index levels correct, 100% agreement with the live site on 7,062
overlapping rows. The unadjusted series is what is stored and shown (it is what the exchange
printed and what the live site serves); the adjusted one, which differs after bonus shares
and rights issues, is loaded alongside it as a per-row factor so returns, ranges and
thresholds are computed on comparable prices.

**Fallback (auto-download): "Dhaka Stock Exchange Historical Data (1999-2025)"**,
doi:10.17632/5mww8rb9td.1 (Sunny, Nafis, Khan). One CSV, 1.52M rows, 534 instruments.
What the loader does to it (see docs/data-quality.md for the evidence behind each rule):

* **keeps only rows from 2023-01-01 on.** Before that, the archive's dates are wrong by
  days to months (its DSEX series jumps from 5,618 to 7,356 inside one week of March 2021,
  and 20% of daily moves exceed the circuit-breaker limit, which cannot happen). From 2023
  the series is consistent with the exchange's rules and matches the live site exactly;
* trims trading codes (``'OLYMPIC '`` -> ``'OLYMPIC'``);
* drops rows with a non-positive close or high < low;
* drops exact duplicate rows and, where the same (symbol, date) still has conflicting
  values, keeps the row with the largest volume (the most complete record of the session).

``RELIABLE_FROM`` can be moved earlier for experiments; ``date_quality`` is then
``'unreliable'`` on those rows and the feature builder ignores them.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from dse_alerts.data.store import open_duckdb

ARCHIVE_URL = "https://data.mendeley.com/datasets/5mww8rb9td/1"
ARCHIVE_FILES = {
    "DSE_Data.csv": "https://data.mendeley.com/public-files/datasets/5mww8rb9td/files/29a65ede-b47a-4c44-abb6-ed237c00880c/file_downloaded",
    "Instruments.txt": "https://data.mendeley.com/public-files/datasets/5mww8rb9td/files/8e9b4e99-7d22-425c-8a4d-6049745ee222/file_downloaded",
}
# From this date on the fallback archive is consistent with reality; earlier rows are misdated.
RELIABLE_FROM = "2023-01-01"

CURATED_URL = "https://data.mendeley.com/datasets/23553sm4tn/4"
CURATED_DIRNAME = "mendeley_23553sm4tn"
CURATED_FILE = "UnAdjusted.csv"
ADJUSTED_FILE = "Adjusted.csv"      # same layout, next to the unadjusted file
# The curated file carries the indexes as tickers prefixed with 00; we store them under the usual names.
INDEX_TICKERS = {"00DSEX": "DSEX", "00DS30": "DS30", "00DSES": "DSES", "00DSMEX": "DSMEX"}


def find_curated(raw_dir: Path) -> Path | None:
    """Locate the curated archive's UnAdjusted.csv wherever the zip was extracted under raw_dir."""
    root = raw_dir / CURATED_DIRNAME
    if not root.exists():
        return None
    hits = sorted(root.rglob(CURATED_FILE))
    return hits[0] if hits else None


def load_curated(csv_path: Path) -> tuple[pd.DataFrame, dict]:
    """Return (prices frame, stats) for the curated archive. Same columns as ``load``."""
    con = open_duckdb()
    con.execute(
        f"""
        CREATE TABLE raw AS
        SELECT trim(Ticker) AS symbol, CAST("Date" AS DATE) AS date,
               Open AS open, High AS high, Low AS low, Close AS close, CAST(Volume AS BIGINT) AS volume
        FROM read_csv('{csv_path.as_posix()}', header=true)
        """
    )
    stats = {"raw_rows": con.execute("SELECT count(*) FROM raw").fetchone()[0]}
    index_case = "CASE " + " ".join(f"WHEN symbol = '{k}' THEN '{v}'" for k, v in INDEX_TICKERS.items()) + " ELSE symbol END"
    con.execute(
        f"""
        CREATE TABLE clean AS
        SELECT {index_case} AS symbol, date, open, high, low, close, volume, 'exact' AS date_quality
        FROM raw
        WHERE close > 0 AND high >= low AND symbol <> ''
          AND (symbol NOT LIKE '00%' OR symbol IN ({", ".join(f"'{k}'" for k in INDEX_TICKERS)}))
        QUALIFY row_number() OVER (PARTITION BY symbol, date ORDER BY volume DESC, close DESC) = 1
        """
    )
    adjusted = csv_path.parent / ADJUSTED_FILE
    if adjusted.exists():
        con.execute(
            f"""
            CREATE TABLE adj AS
            SELECT {index_case} AS symbol, CAST("Date" AS DATE) AS date, Close AS adj_close
            FROM (SELECT trim(Ticker) AS symbol, "Date", Close FROM read_csv('{adjusted.as_posix()}', header=true))
            QUALIFY row_number() OVER (PARTITION BY symbol, date ORDER BY adj_close DESC) = 1
            """
        )
        con.execute(
            """
            CREATE TABLE joined AS
            SELECT c.*, CASE WHEN a.adj_close > 0 AND c.close > 0 THEN a.adj_close / c.close END AS adj_factor
            FROM clean c LEFT JOIN adj a USING (symbol, date)
            """
        )
        stats["adjusted_rows"] = con.execute("SELECT count(adj_factor) FROM joined").fetchone()[0]
    else:
        con.execute("CREATE TABLE joined AS SELECT *, NULL::DOUBLE AS adj_factor FROM clean")
        stats["adjusted_rows"] = 0
    df = con.execute("SELECT * FROM joined ORDER BY symbol, date").df()
    stats.update(
        dropped=stats["raw_rows"] - len(df),
        rows=len(df),
        symbols=df["symbol"].nunique(),
        first=df["date"].min(),
        last=df["date"].max(),
    )
    con.close()
    return df, stats


def download(dest_dir: Path) -> Path:
    """Fetch the archive files if missing. Returns the CSV path."""
    import urllib.request

    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, url in ARCHIVE_FILES.items():
        target = dest_dir / name
        if not target.exists():
            urllib.request.urlretrieve(url, target)
    return dest_dir / "DSE_Data.csv"


def load(csv_path: Path, reliable_from: str = RELIABLE_FROM) -> tuple[pd.DataFrame, dict]:
    """Return (clean prices frame, stats). Columns: symbol, date, open, high, low, close, volume, date_quality."""
    con = open_duckdb()
    con.execute(
        f"""
        CREATE TABLE raw AS
        SELECT trim(Trading_Code) AS symbol, CAST("Date" AS DATE) AS date,
               Open AS open, High AS high, Low AS low, Close AS close, CAST(Volume AS BIGINT) AS volume
        FROM read_csv('{csv_path.as_posix()}', header=true)
        """
    )
    stats = {"raw_rows": con.execute("SELECT count(*) FROM raw").fetchone()[0]}
    con.execute(f"CREATE TABLE dated AS SELECT * FROM raw WHERE date >= DATE '{reliable_from}'")
    stats["dropped_unreliable_dates"] = stats["raw_rows"] - con.execute("SELECT count(*) FROM dated").fetchone()[0]
    con.execute("CREATE TABLE ok AS SELECT * FROM dated WHERE close > 0 AND high >= low AND symbol <> ''")
    stats["dropped_bad"] = con.execute("SELECT count(*) FROM dated").fetchone()[0] - con.execute("SELECT count(*) FROM ok").fetchone()[0]
    con.execute("CREATE TABLE dedup AS SELECT DISTINCT * FROM ok")
    stats["dropped_exact_dups"] = (
        con.execute("SELECT count(*) FROM ok").fetchone()[0]
        - con.execute("SELECT count(*) FROM dedup").fetchone()[0]
    )
    con.execute(
        f"""
        CREATE TABLE clean AS
        SELECT symbol, date, open, high, low, close, volume,
               CASE WHEN date >= DATE '{RELIABLE_FROM}' THEN 'exact' ELSE 'unreliable' END AS date_quality
        FROM dedup
        QUALIFY row_number() OVER (PARTITION BY symbol, date ORDER BY volume DESC, close DESC) = 1
        """
    )
    stats["resolved_conflicts"] = (
        con.execute("SELECT count(*) FROM dedup").fetchone()[0]
        - con.execute("SELECT count(*) FROM clean").fetchone()[0]
    )
    df = con.execute("SELECT * FROM clean ORDER BY symbol, date").df()
    stats.update(
        rows=len(df),
        symbols=df["symbol"].nunique(),
        first=df["date"].min(),
        last=df["date"].max(),
    )
    con.close()
    return df, stats
