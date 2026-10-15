"""Evaluate every rule for every watched symbol of every user, on the latest session."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field

import pandas as pd

from dse_alerts.appdb import AppDB
from dse_alerts.data.store import MarketStore
from dse_alerts.rules import RULES, Alert, RuleContext
from dse_alerts.rules.catalog import OPT_IN

log = logging.getLogger(__name__)

RECENT_SESSIONS = 60


@dataclass
class UserResult:
    user_id: int
    email: str
    alert_email: str
    symbols: list[str]
    alerts: list[Alert]              # everything that fired this run
    new_alerts: list[Alert]          # not previously recorded for this user
    missing_symbols: list[str] = field(default_factory=list)


@dataclass
class MarketSnapshot:
    """Everything the rules need, loaded once per run for the union of watched symbols."""
    as_of: dt.date
    recent: pd.DataFrame        # features rows, last RECENT_SESSIONS per symbol
    stats: pd.DataFrame
    instruments: pd.DataFrame
    fundamentals: pd.DataFrame  # latest two snapshots per symbol, with prev_pe
    news: pd.DataFrame
    dividends: pd.DataFrame


def latest_session(store: MarketStore, as_of: dt.date | None = None) -> dt.date | None:
    with store.connect(read_only=True) as con:
        q = "SELECT max(date) FROM features" + (" WHERE date <= ?" if as_of else "")
        row = con.execute(q, [as_of] if as_of else []).fetchone()
    return row[0] if row and row[0] else None


def load_snapshot(store: MarketStore, symbols: list[str], as_of: dt.date) -> MarketSnapshot:
    syms = sorted(set(symbols))
    ph = ",".join("?" * len(syms)) if syms else "''"
    with store.connect(read_only=True) as con:
        recent = con.execute(
            f"""
            SELECT * FROM (
                SELECT *, row_number() OVER (PARTITION BY symbol ORDER BY date DESC) AS rn
                FROM features WHERE symbol IN ({ph}) AND date <= ?
            ) WHERE rn <= {RECENT_SESSIONS} ORDER BY symbol, date
            """,
            [*syms, as_of],
        ).df()
        stats = con.execute(f"SELECT * FROM symbol_stats WHERE symbol IN ({ph})", syms).df() \
            if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'symbol_stats'").fetchone()[0] \
            else pd.DataFrame(columns=["symbol"])
        instruments = con.execute(f"SELECT * FROM instruments WHERE symbol IN ({ph})", syms).df()
        fundamentals = con.execute(
            f"""
            SELECT f.*, lag(f.pe) OVER (PARTITION BY f.symbol ORDER BY f.as_of) AS prev_pe,
                   row_number() OVER (PARTITION BY f.symbol ORDER BY f.as_of DESC) AS rn
            FROM fundamentals f WHERE f.symbol IN ({ph}) AND f.as_of <= ?
            QUALIFY rn = 1
            """,
            [*syms, as_of],
        ).df()
        news = con.execute(
            f"SELECT * FROM news WHERE symbol IN ({ph}) AND date BETWEEN ? AND ? ORDER BY date DESC",
            [*syms, as_of - dt.timedelta(days=14), as_of],
        ).df()
        dividends = con.execute(
            f"SELECT * FROM dividends WHERE symbol IN ({ph}) AND (record_date >= ? OR declared_on >= ?) ORDER BY declared_on DESC",
            [*syms, as_of - dt.timedelta(days=1), as_of - dt.timedelta(days=30)],
        ).df()
    return MarketSnapshot(as_of, recent, stats, instruments, fundamentals, news, dividends)


def _one(df: pd.DataFrame, symbol: str) -> pd.Series | None:
    sub = df[df["symbol"] == symbol]
    return None if sub.empty else sub.iloc[0]


def evaluate_symbol(snap: MarketSnapshot, symbol: str, overrides: dict) -> list[Alert]:
    rows = snap.recent[snap.recent["symbol"] == symbol]
    if rows.empty:
        return []
    latest = rows.iloc[-1]
    if latest["date"] != pd.Timestamp(snap.as_of) and latest["date"].date() != snap.as_of:
        # the symbol did not trade on the session being evaluated; nothing to say today
        return []
    ctx = RuleContext(
        row=latest, stats=_one(snap.stats, symbol), instrument=_one(snap.instruments, symbol),
        fundamentals=_one(snap.fundamentals, symbol),
        news=snap.news[snap.news["symbol"] == symbol], dividends=snap.dividends[snap.dividends["symbol"] == symbol],
        recent=rows, as_of=snap.as_of,
    )
    out = []
    for rule in RULES:
        cfg = overrides.get(rule.id)
        if cfg is False or (rule.id in OPT_IN and not isinstance(cfg, dict)):
            continue
        try:
            a = rule.run(ctx, cfg if isinstance(cfg, dict) else None)
        except Exception:
            log.exception("rule %s failed for %s", rule.id, symbol)
            continue
        if a:
            out.append(a)
    return out


def run(store: MarketStore, appdb: AppDB, as_of: dt.date | None = None, record: bool = True,
        user_ids: list[int] | None = None) -> tuple[dt.date | None, list[UserResult]]:
    session = latest_session(store, as_of)
    if session is None:
        return None, []
    users = [u for u in appdb.users() if user_ids is None or u["id"] in user_ids]
    items_by_user = {u["id"]: appdb.items(u["id"]) for u in users}
    all_symbols = sorted({it["symbol"] for its in items_by_user.values() for it in its})
    snap = load_snapshot(store, all_symbols, session)
    known = set(snap.recent["symbol"].unique())
    results = []
    for u in users:
        items = items_by_user[u["id"]]
        alerts: list[Alert] = []
        for it in items:
            alerts.extend(evaluate_symbol(snap, it["symbol"], it["rules"] or {}))
        new = appdb.record_alerts(u["id"], alerts) if record else list(alerts)
        if record:
            appdb.record_check(u["id"], session, len(items), len(alerts))
        results.append(UserResult(
            user_id=u["id"], email=u["email"], alert_email=u["alert_email"] or u["email"],
            symbols=[it["symbol"] for it in items], alerts=alerts, new_alerts=new,
            missing_symbols=[it["symbol"] for it in items if it["symbol"] not in known],
        ))
    return session, results
