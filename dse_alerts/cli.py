"""Command-line entry point: ``dse <group> <command>``."""

from __future__ import annotations

import datetime as dt
import logging

import typer
from rich import print as rprint
from rich.table import Table

from dse_alerts.config import get_settings
from dse_alerts.data import ingest
from dse_alerts.data.store import MarketStore

app = typer.Typer(no_args_is_help=True, help="Self-hosted alerts for the Dhaka Stock Exchange.")
data_app = typer.Typer(no_args_is_help=True, help="Market data: bootstrap, refresh, validate, status.")
app.add_typer(data_app, name="data")


def _store() -> MarketStore:
    return MarketStore(get_settings().market_db)


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v")):
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


@data_app.command()
def bootstrap():
    """Download (if needed) and load the 1999-2025 archive. Safe to re-run."""
    s = get_settings()
    stats = ingest.bootstrap(_store(), s.raw_dir)
    rprint(stats)


@data_app.command()
def refresh(end: str = typer.Option(None, help="YYYY-MM-DD, default today")):
    """Pull recent daily prices, PSI news and dividend declarations from dse.com.bd."""
    e = dt.date.fromisoformat(end) if end else None
    rprint(ingest.refresh(_store(), e))


@data_app.command()
def instruments():
    """Refresh the tradeable symbol list, company profiles and current fundamentals (~2 min)."""
    r = ingest.sync_instruments(_store())
    rprint(f"instruments updated: {r['instruments']}, fundamentals: {r['fundamentals']}, "
           f"no company page: {len(r['no_company_page'])}")


@data_app.command()
def validate(
    symbols: str = typer.Option("GP,SQURPHARMA,BRACBANK,BEXIMCO,ROBI,BATBC,ISLAMIBANK,RENATA"),
    start: str = typer.Option("2024-11-01"),
    end: str = typer.Option("2025-04-08"),
):
    """Compare archive rows with a fresh live pull on the overlapping window."""
    df = ingest.validate_overlap(
        get_settings().raw_dir, [x.strip() for x in symbols.split(",") if x.strip()],
        dt.date.fromisoformat(start), dt.date.fromisoformat(end),
    )
    t = Table("symbol", "overlap days", "close match", "volume match")
    for r in df.itertuples():
        t.add_row(r.symbol, str(r.overlap_days),
                  "-" if r.close_match is None else f"{r.close_match:.1%}",
                  "-" if r.volume_match is None else f"{r.volume_match:.1%}")
    rprint(t)


@data_app.command()
def status():
    """What is in the market database."""
    s = _store().summary()
    rprint(f"rows={s['rows']:,} symbols={s['symbols']} range={s['first']} -> {s['last']}")
    for src, n, a, b in s["by_source"]:
        rprint(f"  {src:8s} {n:>10,} rows  {a} -> {b}")


if __name__ == "__main__":
    app()
