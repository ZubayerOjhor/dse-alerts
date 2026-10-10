"""Ingestion flows: bootstrap from the archive, refresh from the live site, validate the overlap."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pandas as pd

from dse_alerts.data import archive, live
from dse_alerts.data.store import MarketStore

log = logging.getLogger(__name__)

# The live site serves daily history back to roughly this date (checked 2026-10-10).
LIVE_HISTORY_FLOOR = dt.date(2024, 10, 14)
# Re-pull this many days before the last stored live date on every refresh, so late
# corrections on the DSE side are picked up.
REFRESH_OVERLAP_DAYS = 5


def bootstrap(store: MarketStore, raw_dir: Path) -> dict:
    """Load the long-run archive into an empty (or archive-less) store."""
    csv = archive.download(raw_dir / "mendeley_5mww8rb9td")
    df, stats = archive.load(csv)
    n = store.upsert_prices(df, source="archive")
    store.refresh_instruments()
    stats["upserted"] = n
    log.info("archive bootstrap: %s", stats)
    return stats


def _month_chunks(start: dt.date, end: dt.date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - dt.timedelta(days=1))
        cur = nxt


def refresh(store: MarketStore, end: dt.date | None = None) -> dict:
    """Pull daily prices for all instruments from the live site, from the last live date
    (minus an overlap) to ``end``. Also refreshes PSI news and dividend declarations."""
    end = end or dt.date.today()
    last_live = store.last_date(source="live")
    if last_live:
        start = last_live - dt.timedelta(days=REFRESH_OVERLAP_DAYS)
    else:
        start = LIVE_HISTORY_FLOOR
    total = 0
    for a, b in _month_chunks(start, end):
        df = live.daily_prices(a, b)
        n = store.upsert_prices(df, source="live")
        log.info("live %s..%s: %d rows", a, b, n)
        total += n
    idx_total = 0
    ms = store.market_summary()
    idx_start = (ms["date"].max() - dt.timedelta(days=REFRESH_OVERLAP_DAYS)) if len(ms) else LIVE_HISTORY_FLOOR
    for a, b in _month_chunks(idx_start, end):
        idx_total += store.upsert_market_summary(live.market_summary(a, b))
    news_n = store.upsert_news(live.psi_news())
    div_n = store.upsert_dividends(live.dividend_declarations())
    store.refresh_instruments()
    return {"start": start, "end": end, "price_rows": total, "index_rows": idx_total,
            "news_rows": news_n, "dividend_rows": div_n}


def sync_instruments(store: MarketStore, symbols: list[str] | None = None) -> dict:
    """Refresh the tradeable list and, for each symbol with a company page, its profile and
    current fundamentals. ~400 requests; bdshare rate-limits to 5/s."""
    codes = live.trading_codes()
    targets = symbols or codes
    rows, funds, missing = [], [], []
    today = dt.date.today()
    for sym in targets:
        d = live.company_details(sym)
        if d is None:
            missing.append(sym)
            rows.append({"symbol": sym, "name": None, "category": None, "sector": None,
                         "instrument_type": None, "listing_year": None, "tradeable": sym in codes})
            continue
        rows.append({k: d[k] for k in ("symbol", "name", "category", "sector", "instrument_type", "listing_year")}
                    | {"tradeable": sym in codes})
        funds.append({"as_of": today} | {k: d[k] for k in (
            "symbol", "price", "pe", "eps", "nav", "dividend_yield", "free_float_pct",
            "market_cap", "paid_up_capital", "face_value", "week_high_52", "week_low_52")})
    n_i = store.upsert_instrument_details(pd.DataFrame(rows))
    n_f = store.upsert_fundamentals(pd.DataFrame(funds))
    # anything in prices but not in the current trading list is not tradeable (delisted, bond, ...)
    with store.connect() as con:
        con.execute("UPDATE instruments SET tradeable = false WHERE tradeable IS NULL")
    log.info("instruments: %d updated, %d fundamentals, %d without company page", n_i, n_f, len(missing))
    return {"instruments": n_i, "fundamentals": n_f, "no_company_page": missing}


def validate_overlap(raw_dir: Path, symbols: list[str], start: dt.date, end: dt.date) -> pd.DataFrame:
    """Compare the archive file with a fresh live pull, per symbol, on [start, end].
    Reads the archive CSV directly so the check is meaningful even after live rows have
    replaced archive rows in the store. Returns one row per symbol: overlap days, close
    match %, volume match %."""
    csv = archive.download(raw_dir / "mendeley_5mww8rb9td")
    arc_all, _ = archive.load(csv)
    arc_all["date"] = pd.to_datetime(arc_all["date"])
    out = []
    for sym in symbols:
        lv = live.daily_prices(start, end, sym)
        ar = arc_all[(arc_all["symbol"] == sym) & (arc_all["date"] >= pd.Timestamp(start)) & (arc_all["date"] <= pd.Timestamp(end))]
        m = lv.merge(ar, on="date", suffixes=("_live", "_arc"))
        if m.empty:
            out.append({"symbol": sym, "overlap_days": 0, "close_match": None, "volume_match": None})
            continue
        out.append(
            {
                "symbol": sym,
                "overlap_days": len(m),
                "close_match": round(float((abs(m["close_live"] - m["close_arc"]) < 0.051).mean()), 4),
                "volume_match": round(float((m["volume_live"] == m["volume_arc"]).mean()), 4),
            }
        )
    return pd.DataFrame(out)
