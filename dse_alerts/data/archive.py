"""Historical archives for years the live DSE site no longer serves.

Supported archive: *Dhaka Stock Exchange Historical Data (1999-2025)*, Mendeley Data,
doi:10.17632/5mww8rb9td.1, CC BY 4.0 (Sunny, Nafis, Khan). One CSV, 1.52M rows,
534 instruments, 1999-01-02 to 2025-04-08.

What the loader does to it (see docs/data-quality.md for the evidence behind each rule):

* trims trading codes (``'OLYMPIC '`` -> ``'OLYMPIC'``);
* drops rows with a non-positive close or high < low (71 rows);
* drops exact duplicate rows (63,914);
* where the same (symbol, date) still has conflicting values (4,825 pairs, all in 2022),
  keeps the row with the largest volume, i.e. the most complete record of the session;
* keeps rows dated on Fridays/Saturdays. DSE does not trade on those days, so these are
  sessions whose date is off by one in the source; the *sequence* of sessions is intact,
  which is what return and volume baselines need. They are flagged in ``date_quality``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

ARCHIVE_URL = "https://data.mendeley.com/datasets/5mww8rb9td/1"
ARCHIVE_FILES = {
    "DSE_Data.csv": "https://data.mendeley.com/public-files/datasets/5mww8rb9td/files/29a65ede-b47a-4c44-abb6-ed237c00880c/file_downloaded",
    "Instruments.txt": "https://data.mendeley.com/public-files/datasets/5mww8rb9td/files/8e9b4e99-7d22-425c-8a4d-6049745ee222/file_downloaded",
}
# From this date on the archive's calendar is clean (no Fri/Sat sessions, ~20 holidays/year).
CLEAN_CALENDAR_FROM = "2023-01-01"


def download(dest_dir: Path) -> Path:
    """Fetch the archive files if missing. Returns the CSV path."""
    import urllib.request

    dest_dir.mkdir(parents=True, exist_ok=True)
    for name, url in ARCHIVE_FILES.items():
        target = dest_dir / name
        if not target.exists():
            urllib.request.urlretrieve(url, target)
    return dest_dir / "DSE_Data.csv"


def load(csv_path: Path) -> tuple[pd.DataFrame, dict]:
    """Return (clean prices frame, stats). Columns: symbol, date, open, high, low, close, volume, date_quality."""
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE raw AS
        SELECT trim(Trading_Code) AS symbol, CAST("Date" AS DATE) AS date,
               Open AS open, High AS high, Low AS low, Close AS close, CAST(Volume AS BIGINT) AS volume
        FROM read_csv('{csv_path.as_posix()}', header=true)
        """
    )
    stats = {"raw_rows": con.execute("SELECT count(*) FROM raw").fetchone()[0]}
    con.execute("CREATE TABLE ok AS SELECT * FROM raw WHERE close > 0 AND high >= low AND symbol <> ''")
    stats["dropped_bad"] = stats["raw_rows"] - con.execute("SELECT count(*) FROM ok").fetchone()[0]
    con.execute("CREATE TABLE dedup AS SELECT DISTINCT * FROM ok")
    stats["dropped_exact_dups"] = (
        con.execute("SELECT count(*) FROM ok").fetchone()[0]
        - con.execute("SELECT count(*) FROM dedup").fetchone()[0]
    )
    con.execute(
        """
        CREATE TABLE clean AS
        SELECT symbol, date, open, high, low, close, volume,
               CASE WHEN date >= DATE '2023-01-01' THEN 'exact'
                    WHEN dayofweek(date) IN (5, 6) THEN 'shifted'
                    ELSE 'approx' END AS date_quality
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
        shifted_rows=int((df["date_quality"] == "shifted").sum()),
    )
    con.close()
    return df, stats
