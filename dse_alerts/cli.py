"""Command-line entry point: ``dse <group> <command>``."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from dse_alerts.config import get_settings
from dse_alerts.data import ingest
from dse_alerts.data.store import MarketStore

app = typer.Typer(no_args_is_help=True, help="Self-hosted alerts for the Dhaka Stock Exchange.")
data_app = typer.Typer(no_args_is_help=True, help="Market data: bootstrap, refresh, validate, status.")
analysis_app = typer.Typer(no_args_is_help=True, help="Feature table and calibration study.")
alerts_app = typer.Typer(no_args_is_help=True, help="Watchlists, rules and alert runs.")
app.add_typer(data_app, name="data")
app.add_typer(analysis_app, name="analysis")
app.add_typer(alerts_app, name="alerts")


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


@analysis_app.command()
def features():
    """Rebuild the per-session feature table from prices (a few seconds)."""
    from dse_alerts.analysis import features as feats

    rprint(feats.build(_store()))


@analysis_app.command()
def calibrate(since: str = typer.Option("2013-01-01", help="Start of the calibration window; masked regimes are excluded")):
    """Measure how often each candidate alert fires and what followed; writes docs/calibration.md."""
    from dse_alerts.analysis import calibrate as cal

    s = get_settings()
    res = cal.run(_store(), dt.date.fromisoformat(since))
    cal.write_report(res, Path("docs/calibration.md"), s.dse_data_dir / "calibration.json")
    rprint(f"{res['sessions']:,} sessions, {res['symbols']} symbols, {res['first']} -> {res['last']}")
    rprint(res["event_stats"][["event", "per_250_sessions", "n", "fwd20_median", "fwd20_positive_share"]].to_string(index=False))


def _appdb():
    from dse_alerts.appdb import AppDB

    return AppDB(get_settings().app_db)


@alerts_app.command()
def rules():
    """List the rules, their defaults and which ones are opt-in."""
    from dse_alerts.rules import RULES
    from dse_alerts.rules.catalog import OPT_IN

    t = Table("id", "name", "defaults", "opt-in", "what it means")
    for r in RULES:
        t.add_row(r.id, r.name, str({k: v for k, v in r.params.items()}), "yes" if r.id in OPT_IN else "", r.description)
    rprint(t)


@alerts_app.command()
def init(email: str = typer.Option(..., help="Your email; also the default alert recipient"),
         alert_to: str = typer.Option(None, help="Send alerts here instead")):
    """Create (or update) a user and their default watchlist."""
    db = _appdb()
    uid = db.ensure_user(email, alert_email=alert_to)
    if alert_to:
        db.update_user(uid, alert_email=alert_to)
    db.ensure_watchlist(uid)
    rprint(f"user #{uid} {email} -> alerts to {alert_to or email}")


@alerts_app.command()
def add(symbols: list[str] = typer.Argument(..., help="Trading codes, e.g. GP SQURPHARMA"),
        email: str = typer.Option(..., help="Whose watchlist"),
        rules: str = typer.Option(None, help='JSON overrides, e.g. {"big_drop": {"days": 10, "pct": 0.05}, "pe_level": {"max": 10}}')):
    """Add symbols to the user's default watchlist (re-adding updates the overrides)."""
    import json

    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}; run `dse alerts init --email {email}` first")
    wl = db.ensure_watchlist(u["id"])
    cfg = json.loads(rules) if rules else {}
    known = {i.symbol for i in _store().instruments().itertuples()}
    for s in symbols:
        s = s.upper().strip()
        if s not in known:
            rprint(f"[yellow]warning:[/yellow] {s} is not in the instrument table (typo, or run `dse data refresh`)")
        db.add_item(wl, s, cfg)
    rprint(f"watching {len(db.items(u['id']))} symbols for {email}")


@alerts_app.command()
def remove(symbols: list[str], email: str = typer.Option(...)):
    """Remove symbols from the user's default watchlist."""
    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    wl = db.ensure_watchlist(u["id"])
    for s in symbols:
        db.remove_item(wl, s.upper().strip())
    rprint(f"watching {len(db.items(u['id']))} symbols for {email}")


@alerts_app.command(name="list")
def list_items(email: str = typer.Option(...)):
    """Show a user's watchlist and overrides."""
    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    t = Table("symbol", "watchlist", "rule overrides", "note")
    for it in db.items(u["id"]):
        t.add_row(it["symbol"], it["watchlist"], str(it["rules"]) if it["rules"] else "", it["note"] or "")
    rprint(t)


@alerts_app.command()
def run(as_of: str = typer.Option(None, help="Evaluate the last session on or before this date (YYYY-MM-DD)"),
        email: bool = typer.Option(True, help="Send email when SMTP is configured"),
        dry_run: bool = typer.Option(False, help="Evaluate and print, but record nothing and send nothing")):
    """Evaluate every user's watchlist on the latest session; print, write the outbox, email."""
    from dse_alerts import engine
    from dse_alerts.notify import deliver

    s = get_settings()
    db = _appdb()
    run_id = None if dry_run else db.start_run()
    session, results = engine.run(_store(), db, dt.date.fromisoformat(as_of) if as_of else None, record=not dry_run)
    if session is None:
        rprint("[red]no feature data; run `dse data refresh` and `dse analysis features` first[/red]")
        raise typer.Exit(1)
    sent = 0
    for r in results:
        deliver.to_console(session, r.email, r.new_alerts, r.symbols)
        if r.missing_symbols:
            rprint(f"[yellow]no data for:[/yellow] {', '.join(r.missing_symbols)}")
        if dry_run:
            continue
        p = deliver.to_outbox(s.outbox_dir, session, r.email, r.new_alerts, r.symbols)
        rprint(f"outbox: {p}")
        if email and r.new_alerts and s.email_enabled and deliver.send_email(s, r.alert_email, session, r.new_alerts, r.symbols):
            db.mark_delivered(r.user_id, r.new_alerts)
            sent += 1
            rprint(f"emailed {r.alert_email}")
    if run_id:
        db.finish_run(run_id, session_date=session.isoformat(), users=len(results),
                      symbols=sum(len(r.symbols) for r in results), alerts_new=sum(len(r.new_alerts) for r in results),
                      alerts_sent=sent, status="ok")


@alerts_app.command()
def backtest(since: str = typer.Option("2013-01-01")):
    """How often each rule (with defaults) fired per stock-year on history, and what followed."""
    from dse_alerts.analysis import calibrate as cal
    from dse_alerts.rules import RULES

    res = cal.run_events(_store(), {r.id: r.predicate_sql for r in RULES if r.predicate_sql}, dt.date.fromisoformat(since))
    t = Table("rule", "fires / stock-year", "events", "fwd 5 median", "fwd 20 median", "fwd 20 positive")
    for r in res.itertuples():
        t.add_row(r.event, f"{r.per_250_sessions:.2f}", f"{r.n:,}", f"{100*r.fwd5_median:+.1f}%", f"{100*r.fwd20_median:+.1f}%", f"{100*r.fwd20_positive_share:.0f}%")
    rprint(t)


if __name__ == "__main__":
    app()
