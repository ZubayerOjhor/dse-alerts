"""DuckDB market-data store.

One file (``market.duckdb``) holds everything the alert engine reads:

* ``prices``      daily OHLCV per instrument, one row per (symbol, date), with the source it came from
* ``instruments`` the symbol list with category/sector when known
* ``news``        price-sensitive information and corporate announcements
* ``dividends``   dividend declarations with record dates
* ``market_days`` the trading calendar derived from the data itself
* ``ingest_log``  one row per ingestion run (what, when, how many rows)
"""

from __future__ import annotations

import datetime as dt
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol  VARCHAR NOT NULL,
    date    DATE    NOT NULL,
    open    DOUBLE,
    high    DOUBLE,
    low     DOUBLE,
    close   DOUBLE,
    volume  BIGINT,
    source  VARCHAR NOT NULL,          -- 'archive' | 'live'
    date_quality VARCHAR NOT NULL DEFAULT 'exact',  -- 'exact' | 'approx' | 'shifted' (archive only)
    PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS instruments (
    symbol      VARCHAR PRIMARY KEY,
    name        VARCHAR,
    category    VARCHAR,               -- A / B / N / Z / G when known
    sector      VARCHAR,
    first_date  DATE,
    last_date   DATE,
    updated_at  TIMESTAMP
);
CREATE TABLE IF NOT EXISTS news (
    symbol     VARCHAR,
    date       DATE,
    kind       VARCHAR,                -- 'psi' | 'corporate' | 'other'
    title      VARCHAR,
    body       VARCHAR,
    PRIMARY KEY (symbol, date, title)
);
CREATE TABLE IF NOT EXISTS dividends (
    symbol       VARCHAR,
    declared_on  DATE,
    year_end     VARCHAR,
    dividend     VARCHAR,
    agm_date     DATE,
    record_date  DATE,
    PRIMARY KEY (symbol, declared_on, dividend)
);
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS instrument_type VARCHAR;  -- Equity | Mutual Fund | Bond | ...
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS listing_year INTEGER;
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS tradeable BOOLEAN;      -- in DSE's current trading-code list
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol          VARCHAR,
    as_of           DATE,
    price           DOUBLE,
    pe              DOUBLE,
    eps             DOUBLE,
    nav             DOUBLE,
    dividend_yield  DOUBLE,
    free_float_pct  DOUBLE,
    market_cap      DOUBLE,
    paid_up_capital DOUBLE,
    face_value      DOUBLE,
    week_high_52    DOUBLE,
    week_low_52     DOUBLE,
    PRIMARY KEY (symbol, as_of)
);
CREATE TABLE IF NOT EXISTS market_summary (
    date            DATE PRIMARY KEY,
    dsex            DOUBLE,
    dses            DOUBLE,
    ds30            DOUBLE,
    total_trade     BIGINT,
    total_volume    BIGINT,
    total_value_mn  DOUBLE,
    market_cap_mn   DOUBLE
);
-- Cumulative adjustment for bonus shares and rights, from the curated archive's adjusted
-- series (1 where unknown or after the archive). close * adj_factor is comparable across time.
ALTER TABLE prices ADD COLUMN IF NOT EXISTS adj_factor DOUBLE DEFAULT 1;
CREATE TABLE IF NOT EXISTS ingest_log (
    run_at      TIMESTAMP,
    source      VARCHAR,
    detail      VARCHAR,
    rows_in     BIGINT,
    rows_upsert BIGINT,
    date_min    DATE,
    date_max    DATE
);
"""


LOCK_WAIT_SECONDS = 20.0


def open_duckdb(path: Path | str | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection (file, or in-memory when ``path`` is None) with the resource caps
    from settings applied. Spill files go under the data directory."""
    from dse_alerts.config import get_settings

    target = str(path) if path else ":memory:"
    # Another process (the scheduler during its daily run, or the web UI) may hold the file
    # lock for a moment; DuckDB allows one writing process at a time. Wait briefly instead
    # of failing the request.
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            con = duckdb.connect(target, read_only=read_only)
            break
        except duckdb.IOException as e:
            if "lock" not in str(e).lower() or time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
    s = get_settings()
    if s.dse_duckdb_memory:
        tmp = s.dse_data_dir / "duckdb_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        con.execute(f"SET memory_limit = '{s.dse_duckdb_memory}'")
        con.execute(f"SET temp_directory = '{tmp.as_posix()}'")
    if s.dse_duckdb_threads > 0:
        con.execute(f"SET threads = {s.dse_duckdb_threads}")
    return con


SNAPSHOT_SUFFIX = ".snapshot.duckdb"


class MarketStore:
    """The market database. Writers (the daily chain) use ``path``. A reader that passes
    ``prefer_snapshot`` (the web UI) reads the last published copy instead, so pages keep
    working while the chain holds the write lock for a minute; ``snapshot()`` publishes."""

    def __init__(self, path: Path, prefer_snapshot: bool = False):
        self.path = Path(path)
        self.snapshot_path = self.path.with_name(self.path.stem + SNAPSHOT_SUFFIX)
        self.prefer_snapshot = prefer_snapshot
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.execute(SCHEMA)

    @contextmanager
    def connect(self, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
        target = self.path
        if read_only and self.prefer_snapshot and self.snapshot_path.exists():
            target = self.snapshot_path
        con = open_duckdb(target, read_only=read_only)
        try:
            yield con
        finally:
            con.close()

    def snapshot(self) -> Path:
        """Publish a consistent copy for readers: checkpoint, copy to a temp file next to it,
        then rename over the previous snapshot (atomic on the same filesystem)."""
        import shutil

        with self.connect() as con:
            con.execute("CHECKPOINT")
        tmp = self.snapshot_path.with_suffix(".tmp")
        shutil.copyfile(self.path, tmp)
        os.replace(tmp, self.snapshot_path)
        wal = self.snapshot_path.with_suffix(self.snapshot_path.suffix + ".wal")
        if wal.exists():
            wal.unlink()
        return self.snapshot_path

    # ---- writes -------------------------------------------------------------------------

    def upsert_prices(self, df: pd.DataFrame, source: str) -> int:
        """Insert or replace rows. ``df`` needs symbol, date, open, high, low, close, volume.
        Live data wins over archive data for the same (symbol, date)."""
        if df.empty:
            return 0
        frame = df[["symbol", "date", "open", "high", "low", "close", "volume"]].copy()
        frame["date"] = pd.to_datetime(frame["date"]).dt.date
        frame["source"] = source
        frame["date_quality"] = df["date_quality"].values if "date_quality" in df.columns else "exact"
        has_adj = "adj_factor" in df.columns
        frame["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0).values if has_adj else 1.0
        cols = "symbol, date, open, high, low, close, CAST(volume AS BIGINT), source, date_quality, adj_factor"
        with self.connect() as con:
            con.register("incoming", frame)
            before = con.execute("SELECT count(*) FROM prices").fetchone()[0]
            if source == "live":
                # a live row replaces the archive's prices but keeps the adjustment the archive knew
                con.execute(
                    "INSERT OR REPLACE INTO prices "
                    "SELECT i.symbol, i.date, i.open, i.high, i.low, i.close, CAST(i.volume AS BIGINT), i.source, "
                    "i.date_quality, coalesce(p.adj_factor, 1) FROM incoming i "
                    "LEFT JOIN prices p ON p.symbol = i.symbol AND p.date = i.date"
                )
            else:  # archive never overwrites live prices, but it does bring the adjustment factors
                cols_i = ", ".join(
                    f"i.{c}" if not c.startswith("CAST") else "CAST(i.volume AS BIGINT)"
                    for c in cols.split(", ")
                )
                con.execute(
                    f"INSERT INTO prices SELECT {cols_i} FROM incoming i "
                    "LEFT JOIN prices p ON p.symbol = i.symbol AND p.date = i.date "
                    "WHERE p.symbol IS NULL"
                )
                if has_adj:
                    con.execute(
                        "UPDATE prices SET adj_factor = i.adj_factor FROM incoming i "
                        "WHERE prices.symbol = i.symbol AND prices.date = i.date AND i.adj_factor IS NOT NULL"
                    )
            # live: every incoming row is written (insert or replace); archive: only new keys
            n = len(frame) if source == "live" else con.execute("SELECT count(*) FROM prices").fetchone()[0] - before
            con.execute(
                "INSERT INTO ingest_log VALUES (?, ?, ?, ?, ?, ?, ?)",
                [dt.datetime.now(), source, "prices", len(frame), n,
                 frame["date"].min(), frame["date"].max()],
            )
            con.unregister("incoming")
        return int(n)

    def upsert_news(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        frame = df[["symbol", "date", "kind", "title", "body"]].dropna(subset=["symbol", "date", "title"])
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute("INSERT OR IGNORE INTO news SELECT * FROM incoming")
            con.unregister("incoming")
        return len(frame)

    def upsert_dividends(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        cols = ["symbol", "declared_on", "year_end", "dividend", "agm_date", "record_date"]
        frame = df[cols].dropna(subset=["symbol", "declared_on", "dividend"])
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute("INSERT OR IGNORE INTO dividends SELECT * FROM incoming")
            con.unregister("incoming")
        return len(frame)

    def upsert_instrument_details(self, df: pd.DataFrame) -> int:
        """Name, category, sector, type, listing year and tradeable flag per symbol."""
        if df.empty:
            return 0
        cols = ["symbol", "name", "category", "sector", "instrument_type", "listing_year", "tradeable"]
        frame = df[cols].dropna(subset=["symbol"])
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute(
                """
                INSERT INTO instruments (symbol, name, category, sector, instrument_type, listing_year, tradeable, updated_at)
                SELECT symbol, name, category, sector, instrument_type, listing_year, tradeable, now() FROM incoming
                ON CONFLICT (symbol) DO UPDATE SET
                    name = coalesce(excluded.name, instruments.name),
                    category = coalesce(excluded.category, instruments.category),
                    sector = coalesce(excluded.sector, instruments.sector),
                    instrument_type = coalesce(excluded.instrument_type, instruments.instrument_type),
                    listing_year = coalesce(excluded.listing_year, instruments.listing_year),
                    tradeable = coalesce(excluded.tradeable, instruments.tradeable),
                    updated_at = now()
                """
            )
            con.unregister("incoming")
        return len(frame)

    def upsert_fundamentals(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        cols = ["symbol", "as_of", "price", "pe", "eps", "nav", "dividend_yield", "free_float_pct",
                "market_cap", "paid_up_capital", "face_value", "week_high_52", "week_low_52"]
        frame = df[cols].dropna(subset=["symbol", "as_of"])
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute("INSERT OR REPLACE INTO fundamentals SELECT * FROM incoming")
            con.unregister("incoming")
        return len(frame)

    def upsert_market_summary(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        cols = ["date", "dsex", "dses", "ds30", "total_trade", "total_volume", "total_value_mn", "market_cap_mn"]
        frame = df.reindex(columns=cols).dropna(subset=["date"])
        with self.connect() as con:
            con.register("incoming", frame)
            con.execute("INSERT OR REPLACE INTO market_summary SELECT * FROM incoming")
            con.unregister("incoming")
        return len(frame)

    def market_summary(self) -> pd.DataFrame:
        with self.connect(read_only=True) as con:
            return con.execute("SELECT * FROM market_summary ORDER BY date").df()

    def instruments(self) -> pd.DataFrame:
        with self.connect(read_only=True) as con:
            return con.execute("SELECT * FROM instruments ORDER BY symbol").df()

    def refresh_instruments(self) -> int:
        """Derive first/last trading date per symbol from prices; keep names/categories if set."""
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO instruments (symbol, first_date, last_date, updated_at)
                SELECT symbol, min(date), max(date), now() FROM prices GROUP BY symbol
                ON CONFLICT (symbol) DO UPDATE SET
                    first_date = excluded.first_date,
                    last_date  = excluded.last_date,
                    updated_at = excluded.updated_at
                """
            )
            return con.execute("SELECT count(*) FROM instruments").fetchone()[0]

    # ---- reads --------------------------------------------------------------------------

    def prices(self, symbols: list[str] | None = None, start: dt.date | None = None,
               end: dt.date | None = None) -> pd.DataFrame:
        q = "SELECT symbol, date, open, high, low, close, volume, source FROM prices WHERE 1=1"
        params: list = []
        if symbols:
            q += f" AND symbol IN ({','.join('?' * len(symbols))})"
            params += symbols
        if start:
            q += " AND date >= ?"
            params.append(start)
        if end:
            q += " AND date <= ?"
            params.append(end)
        q += " ORDER BY symbol, date"
        with self.connect(read_only=True) as con:
            return con.execute(q, params).df()

    def last_date(self, source: str | None = None) -> dt.date | None:
        q = "SELECT max(date) FROM prices" + (" WHERE source = ?" if source else "")
        with self.connect(read_only=True) as con:
            row = con.execute(q, [source] if source else []).fetchone()
        return row[0] if row else None

    def summary(self) -> dict:
        with self.connect(read_only=True) as con:
            r = con.execute(
                "SELECT count(*), count(DISTINCT symbol), min(date), max(date) FROM prices"
            ).fetchone()
            by_source = con.execute(
                "SELECT source, count(*), min(date), max(date) FROM prices GROUP BY 1 ORDER BY 1"
            ).fetchall()
        return {"rows": r[0], "symbols": r[1], "first": r[2], "last": r[3], "by_source": by_source}
