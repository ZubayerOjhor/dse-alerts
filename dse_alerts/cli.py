"""Command-line entry point: ``dse <group> <command>``."""

from __future__ import annotations

import datetime as dt
import logging
import sys
from pathlib import Path

import typer
from rich import print as rprint
from rich.table import Table

from dse_alerts.config import get_settings
from dse_alerts.data import ingest
from dse_alerts.data.store import MarketStore
from dse_alerts.notify.context import build as build_context

# Windows consoles default to a legacy code page; Bangla text and the AI note would crash print().
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # closed or non-file stream
            pass

app = typer.Typer(no_args_is_help=True, help="Self-hosted alerts for the Dhaka Stock Exchange.")
data_app = typer.Typer(no_args_is_help=True, help="Market data: bootstrap, refresh, validate, status.")
analysis_app = typer.Typer(no_args_is_help=True, help="Feature table and calibration study.")
alerts_app = typer.Typer(no_args_is_help=True, help="Watchlists, rules and alert runs.")
web_app = typer.Typer(no_args_is_help=True, help="The web UI.")
portfolio_app = typer.Typer(no_args_is_help=True, help="Holdings: import, value, targets.")
demo_app = typer.Typer(no_args_is_help=True, help="The shared read-only demo account for a public instance (APP_DEMO=1).")
app.add_typer(data_app, name="data")
app.add_typer(analysis_app, name="analysis")
app.add_typer(alerts_app, name="alerts")
app.add_typer(web_app, name="web")
app.add_typer(portfolio_app, name="portfolio")
app.add_typer(demo_app, name="demo")

# A sample list for the demo account: liquid, well-known A-category names across sectors.
# Symbols missing from the local instrument table are skipped, so this works on any data set.
DEMO_WATCH = ["GP", "BRACBANK", "SQURPHARMA", "BXPHARMA", "CITYBANK", "RENATA", "BATBC", "OLYMPIC", "WALTONHIL", "PUBALIBANK"]
DEMO_TARGETS = ["ORIONINFU", "IPDC", "APEXFOODS"]
DEMO_HOLDINGS = {
    "Demo BO 1": [("GP", 200), ("BRACBANK", 1000), ("SQURPHARMA", 300), ("BXPHARMA", 500), ("CITYBANK", 1500)],
    "Demo BO 2": [("RENATA", 50), ("BATBC", 100), ("OLYMPIC", 400), ("WALTONHIL", 60), ("PUBALIBANK", 1200)],
}


@demo_app.command()
def seed(backfill: int = typer.Option(40, help="Sessions of alert history to compute, so the scorecard has rows"),
         reset: bool = typer.Option(False, help="Drop the demo account's lists and holdings first")):
    """Create or refresh the demo account: sample watchlist, targets, a synthetic portfolio, alert history.

    The portfolio is round lots priced at the close of about a year ago; it is nobody's real
    holdings. The account has no password: only the "Try the demo" button logs into it.
    """
    from dse_alerts import engine
    from dse_alerts.appdb import DEMO_EMAIL

    db = _appdb()
    store = _store()
    known = set(store.instruments()["symbol"])
    uid = db.ensure_user(DEMO_EMAIL)
    if reset:
        with db.connect() as con:
            con.execute("DELETE FROM holdings WHERE account_id IN (SELECT id FROM accounts WHERE user_id = ?)", (uid,))
            con.execute("DELETE FROM accounts WHERE user_id = ?", (uid,))
            con.execute("DELETE FROM watchlist_items WHERE watchlist_id IN (SELECT id FROM watchlists WHERE user_id = ?)", (uid,))
            con.execute("DELETE FROM alerts WHERE user_id = ?", (uid,))
            con.execute("DELETE FROM digests WHERE user_id = ?", (uid,))
    db.update_user(uid, alert_email=DEMO_EMAIL, digest_enabled=1, telegram_chat_id=None)
    wl = db.ensure_watchlist(uid)
    for sym in DEMO_WATCH:
        if sym in known:
            db.add_item(wl, sym, {}, kind="watch")
    for sym in DEMO_TARGETS:
        if sym in known:
            db.add_item(wl, sym, {}, kind="target")
    # a year-old close as the purchase price gives a realistic spread of gains and losses
    with store.connect(read_only=True) as con:
        costs = {r[0]: float(r[1]) for r in con.execute(
            """
            SELECT symbol, close FROM (
                SELECT symbol, close, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn FROM features WHERE close > 0
            ) WHERE rn = 250
            """).fetchall()}
        latest = {r[0]: float(r[1]) for r in con.execute(
            "SELECT symbol, close FROM features WHERE close > 0 QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY date DESC) = 1").fetchall()}
    n = 0
    for account, lots in DEMO_HOLDINGS.items():
        acc = db.ensure_account(uid, account)
        for sym, qty in lots:
            price = costs.get(sym) or latest.get(sym)
            if sym in known and price:
                db.upsert_holding(acc, sym, qty, round(price, 1), note="demo")
                n += 1
    rprint(f"demo account #{uid}: {len(db.items(uid))} symbols, {n} holdings")
    if backfill > 0:
        with store.connect(read_only=True) as con:
            dates = [r[0] for r in con.execute("SELECT DISTINCT date FROM features ORDER BY date DESC LIMIT ?", [backfill]).fetchall()]
        total = 0
        for d in reversed(dates):
            _, results = engine.run(store, db, as_of=d, record=True, user_ids=[uid])
            total += sum(len(r.new_alerts) for r in results)
        rprint(f"backfilled {len(dates)} sessions: {total} alerts recorded")


@portfolio_app.command(name="import")
def portfolio_import(path: str = typer.Argument("data/holdings.csv"), email: str = typer.Option(...),
                     watch: bool = typer.Option(True, help="Also add every held symbol to the watchlist")):
    """Load holdings from CSV (account,symbol,quantity,avg_cost[,statement_price,statement_date,margin,note])."""
    from dse_alerts import portfolio as pf

    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}; run `dse alerts init --email {email}` first")
    r = pf.import_csv(db, u["id"], Path(path))
    if watch:
        wl = db.ensure_watchlist(u["id"])
        for h in db.holdings(u["id"]):
            db.add_item(wl, h["symbol"])
    rprint(f"{r['rows']} holdings across {len(r['accounts'])} account(s): {', '.join(r['accounts'])}")


@portfolio_app.command(name="show")
def portfolio_show(email: str = typer.Option(...)):
    """Value the holdings at the latest session."""
    from dse_alerts import portfolio as pf

    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    v = pf.valuation(_store(), db, u["id"])
    if v["positions"].empty:
        rprint("No holdings. Import with `dse portfolio import data/holdings.csv --email ...`")
        return
    t = Table("account", "symbol", "qty", "avg cost", "close", "cost", "value", "P&L", "P&L %", "day")
    for p in v["positions"].itertuples():
        t.add_row(p.account, p.symbol, f"{p.quantity:,.0f}", f"{p.avg_cost:,.2f}", f"{p.close:,.1f}", f"{p.cost:,.0f}",
                  f"{p.value:,.0f}", f"{p.pnl:+,.0f}", f"{100*p.pnl_pct:+.1f}%", f"{p.day_change:+,.0f}")
    rprint(t)
    tt = v["total"]
    rprint(f"as of {v['as_of']}: cost Tk {tt['cost']:,.0f}, value Tk {tt['value']:,.0f}, P&L Tk {tt['pnl']:+,.0f} "
           f"({100*tt['pnl_pct']:+.1f}%), today {tt['day_change']:+,.0f}")
    for a in v["accounts"].itertuples():
        rprint(f"  {a.account}: {a.positions} positions, value {a.value:,.0f}, P&L {a.pnl:+,.0f} ({100*a.pnl_pct:+.1f}%)")


@portfolio_app.command(name="target")
def portfolio_target(symbols: list[str], email: str = typer.Option(...), remove: bool = typer.Option(False)):
    """Mark symbols as targets (companies you are considering buying); they are watched too."""
    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    wl = db.ensure_watchlist(u["id"])
    for s in symbols:
        db.add_item(wl, s, kind="watch" if remove else "target")
    rprint(f"targets: {', '.join(i['symbol'] for i in db.items(u['id']) if i['kind'] == 'target') or 'none'}")


@web_app.command()
def serve(host: str = typer.Option(None), port: int = typer.Option(None), reload: bool = typer.Option(False)):
    """Start the web UI (default http://127.0.0.1:8000)."""
    import uvicorn

    s = get_settings()
    if s.app_secret_key == "change-me":
        rprint("[yellow]APP_SECRET_KEY is the default; set a long random value in .env before exposing this beyond localhost[/yellow]")
    uvicorn.run("dse_alerts.web.app:create_app", factory=True, host=host or s.app_host, port=port or s.app_port, reload=reload)


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


@analysis_app.command(name="export-stats")
def export_stats(path: str = typer.Option("calibration/symbol_stats.csv")):
    """Write per-symbol baselines to CSV (committed, so CI needs no long history)."""
    from dse_alerts.analysis import calibrate as cal

    rprint(f"{cal.export_stats(_store(), Path(path))} symbols -> {path}")


@analysis_app.command(name="import-stats")
def import_stats(path: str = typer.Option("calibration/symbol_stats.csv"), force: bool = typer.Option(False)):
    """Load per-symbol baselines from CSV when the store has none (or --force)."""
    from dse_alerts.analysis import calibrate as cal

    n = cal.import_stats(_store(), Path(path), force)
    rprint(f"{n} symbols loaded" if n else "symbol_stats already present; use --force to replace")


def _appdb():
    from dse_alerts.appdb import AppDB

    return AppDB(get_settings().app_db)


@alerts_app.command(name="import")
def import_watchlist(path: str = typer.Argument("watchlist.json")):
    """Create or update users and watchlists from a JSON file (see watchlist.example.json)."""
    import json

    db = _appdb()
    spec = json.loads(Path(path).read_text(encoding="utf-8-sig"))   # -sig: tolerate a BOM from Windows tools
    users = spec if isinstance(spec, list) else [spec]
    for u in users:
        uid = db.ensure_user(u["email"], alert_email=u.get("alert_to"))
        if u.get("alert_to"):
            db.update_user(uid, alert_email=u["alert_to"])
        db.update_user(uid, email_confirmed=u.get("alert_to") or u["email"])   # the operator vouches for it
        wl = db.ensure_watchlist(uid, u.get("watchlist", "My watchlist"))
        symbols = u.get("symbols", {})
        if isinstance(symbols, list):
            symbols = {s: {} for s in symbols}
        for sym, cfg in symbols.items():
            db.add_item(wl, sym, cfg or {})
        rprint(f"{u['email']}: {len(db.items(uid))} symbols")


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
    db.update_user(uid, email_confirmed=alert_to or email)   # the operator vouches for it
    db.ensure_watchlist(uid)
    rprint(f"user #{uid} {email} -> alerts to {alert_to or email}")


@alerts_app.command(name="set-password")
def set_password(email: str = typer.Argument(..., help="An existing user"),
                 password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True)):
    """Set or reset the web password of a user created here (the web sign-up refuses existing emails)."""
    from dse_alerts.web.auth import PASSWORD_MAX_LENGTH, hash_password

    db = _appdb()
    u = db.user(email.strip().lower())
    if not u:
        rprint(f"[red]no user {email}[/red]")
        raise typer.Exit(1)
    if not 8 <= len(password) <= PASSWORD_MAX_LENGTH:
        rprint("[red]use 8 to 128 characters[/red]")
        raise typer.Exit(1)
    db.update_user(u["id"], password_hash=hash_password(password))
    rprint(f"password set for {u['email']}; other sessions are signed out")


@alerts_app.command(name="confirm-email")
def confirm_email(email: str = typer.Argument(..., help="An existing user")):
    """Mark a user's alert address as confirmed without the emailed link."""
    db = _appdb()
    u = db.user(email.strip().lower())
    if not u:
        rprint(f"[red]no user {email}[/red]")
        raise typer.Exit(1)
    db.update_user(u["id"], email_confirmed=u["alert_email"] or u["email"])
    rprint(f"{u['alert_email'] or u['email']} confirmed")


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
    from dse_alerts.ai import digest as ai

    sent = 0
    evidence = ai.load_rule_evidence(s) if s.ai_enabled else {}
    for r in results:
        u = db.user_by_id(r.user_id)
        lang = (dict(u).get("lang") if u else None) or "en"
        summary = None
        if r.new_alerts and s.ai_enabled and u and u["digest_enabled"]:
            summary, provider = ai.write_digest_detailed(s, session, r.new_alerts, r.symbols, evidence, lang=lang)
            rprint("[green]AI summary written[/green]" if summary else "[yellow]AI summary unavailable; sending alerts without it[/yellow]")
            if summary and not dry_run:
                db.save_digest(r.user_id, session, summary, provider, lang)   # shown on the overview page
        ctx = build_context(_store(), db, r.user_id, session, r.new_alerts, summary, base_url=s.base_url, lang=lang)
        deliver.to_console(r.email, ctx)
        if r.missing_symbols:
            rprint(f"[yellow]no data for:[/yellow] {', '.join(r.missing_symbols)}")
        if dry_run:
            continue
        p = deliver.to_outbox(s.outbox_dir, r.email, ctx)
        rprint(f"outbox: {p}")
        delivered = False
        # only confirmed destinations: a web sign-up cannot point the operator's mail or bot at a stranger
        confirmed = u is not None and dict(u).get("email_confirmed") == r.alert_email
        if email and r.new_alerts and s.email_enabled and not confirmed:
            rprint(f"[yellow]{r.alert_email} is not confirmed; not emailed[/yellow]")
        if email and r.new_alerts and s.email_enabled and confirmed and deliver.send_email(s, r.alert_email, ctx):
            delivered = True
            rprint(f"emailed {r.alert_email}")
        chat = (dict(u).get("telegram_confirmed") if u else None) or (s.telegram_chat_id if u and u["id"] == db.operator_id() else None)
        if r.new_alerts and s.telegram_enabled and chat and deliver.send_telegram(s, chat, deliver.telegram_text(session, r.new_alerts, summary, ctx)):
            delivered = True
            rprint(f"telegram -> chat {chat}")
        if delivered:
            db.mark_delivered(r.user_id, r.new_alerts)
            sent += 1
    if run_id:
        db.finish_run(run_id, session_date=session.isoformat(), users=len(results),
                      symbols=sum(len(r.symbols) for r in results), alerts_new=sum(len(r.new_alerts) for r in results),
                      alerts_sent=sent, status="ok")


@alerts_app.command(name="test-email")
def test_email(email: str = typer.Option(..., help="User whose watchlist and address to use")):
    """Send this user whatever fired on the latest session (or a 'nothing fired' note) to verify SMTP."""
    from dse_alerts import engine
    from dse_alerts.notify import deliver

    s = get_settings()
    if not s.email_enabled:
        rprint("[red]SMTP is not configured in .env (SMTP_USER / SMTP_PASSWORD)[/red]")
        raise typer.Exit(1)
    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    session, results = engine.run(_store(), db, record=False, user_ids=[u["id"]])
    if session is None or not results:
        rprint("[red]no data to evaluate[/red]")
        raise typer.Exit(1)
    r = results[0]
    ctx = build_context(_store(), db, u["id"], session, r.alerts, None, base_url=s.base_url)
    deliver.to_outbox(s.outbox_dir, r.email, ctx)
    ok = deliver.send_email(s, r.alert_email, ctx)
    rprint(f"{'sent' if ok else '[red]failed[/red]'}: {len(r.alerts)} alert(s) for session {session} -> {r.alert_email}")
    if not ok:
        raise typer.Exit(1)


@alerts_app.command(name="test-telegram")
def test_telegram(email: str = typer.Option(..., help="User whose watchlist to use"),
                  chat_id: str = typer.Option(None, help="Override the chat id from .env / user settings")):
    """Send this user whatever fired on the latest session to Telegram, to verify the bot."""
    from dse_alerts import engine
    from dse_alerts.notify import deliver

    s = get_settings()
    if not s.telegram_enabled:
        rprint("[red]TELEGRAM_BOT_TOKEN is not set in .env[/red]")
        raise typer.Exit(1)
    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    chat = chat_id or dict(u).get("telegram_chat_id") or s.telegram_chat_id
    if not chat:
        rprint("[red]no chat id: set TELEGRAM_CHAT_ID in .env or pass --chat-id (see `dse alerts telegram-id`)[/red]")
        raise typer.Exit(1)
    session, results = engine.run(_store(), db, record=False, user_ids=[u["id"]])
    if session is None or not results:
        rprint("[red]no data to evaluate[/red]")
        raise typer.Exit(1)
    r = results[0]
    ctx = build_context(_store(), db, u["id"], session, r.alerts, None)
    ok = deliver.send_telegram(s, chat, deliver.telegram_text(session, r.alerts, None, ctx))
    rprint(f"{'sent' if ok else '[red]failed[/red]'}: {len(r.alerts)} alert(s) for session {session} -> chat {chat}")
    if not ok:
        raise typer.Exit(1)


@alerts_app.command(name="telegram-id")
def telegram_id():
    """Show the chat ids of people who have messaged the bot (set TELEGRAM_BOT_TOKEN first)."""
    from dse_alerts.notify import deliver

    s = get_settings()
    if not s.telegram_enabled:
        rprint("[red]TELEGRAM_BOT_TOKEN is not set in .env[/red]")
        raise typer.Exit(1)
    ups = deliver.telegram_updates(s)
    if not ups:
        rprint("No messages yet. Open your bot in Telegram, send it any message, then run this again.")
        return
    t = Table("chat id", "who", "last message")
    for u in ups:
        t.add_row(u["chat_id"], u["name"], u["text"])
    rprint(t)
    rprint("Put the chat id in .env as TELEGRAM_CHAT_ID, or per user under Settings in the web UI.")


@alerts_app.command()
def scorecard(email: str = typer.Option(...)):
    """What happened 5 and 20 sessions after the alerts sent to this user, per rule."""
    from dse_alerts import scorecard as sc

    db = _appdb()
    u = db.user(email)
    if not u:
        raise typer.BadParameter(f"no user {email}")
    df = sc.outcomes(_store(), db, u["id"])
    agg = sc.by_rule(df, sc.load_expectation(get_settings()))
    if agg.empty:
        rprint("No alerts recorded yet.")
        return
    import math

    def f(v, digits=1, sign="+"):
        return "-" if v is None or (isinstance(v, float) and math.isnan(v)) else f"{100 * v:{sign}.{digits}f}%"

    t = Table("rule", "alerts", "scored", "fwd 5 median", "fwd 20 median", "positive", "vs DSEX (20)", "backtest fwd 20")
    for r in agg.itertuples():
        t.add_row(r.rule_id, str(r.alerts), str(r.scored), f(r.fwd5_median), f(r.fwd20_median),
                  f(r.fwd20_positive_share, 0, ""), f(r.excess20_median), f(r.backtest_fwd20_median))
    rprint(t)
    pend = int((df["status"] == "pending").sum())
    rprint(f"{len(df)} alerts, {pend} still pending (fewer than 20 sessions since).")


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
