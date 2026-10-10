"""Application database (SQLite): users, watchlists, alert history, delivery log.

Separate from the market data (DuckDB) so the market file can be rebuilt freely and the
app file stays small and easy to back up. Plain ``sqlite3``; no ORM.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT,
    created_at TEXT NOT NULL,
    alert_email TEXT,              -- where to send; defaults to email
    digest_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS watchlists (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, name)
);
CREATE TABLE IF NOT EXISTS watchlist_items (
    id INTEGER PRIMARY KEY,
    watchlist_id INTEGER NOT NULL REFERENCES watchlists(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    rules_json TEXT NOT NULL DEFAULT '{}',   -- {rule_id: {param: value} | false}
    note TEXT,
    added_at TEXT NOT NULL,
    UNIQUE (watchlist_id, symbol)
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    symbol TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    severity TEXT NOT NULL,
    headline TEXT NOT NULL,
    detail TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE (user_id, symbol, rule_id, session_date)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    session_date TEXT,
    users INTEGER,
    symbols INTEGER,
    alerts_new INTEGER,
    alerts_sent INTEGER,
    status TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS alerts_user_date ON alerts(user_id, session_date);
"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class AppDB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- users ------------------------------------------------------------------------

    def ensure_user(self, email: str, password_hash: str | None = None, alert_email: str | None = None) -> int:
        with self.connect() as con:
            row = con.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                if password_hash:
                    con.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, row["id"]))
                return row["id"]
            cur = con.execute(
                "INSERT INTO users (email, password_hash, created_at, alert_email) VALUES (?, ?, ?, ?)",
                (email, password_hash, _now(), alert_email or email),
            )
            return cur.lastrowid

    def user(self, email: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    def user_by_id(self, user_id: int) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def users(self) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute("SELECT * FROM users ORDER BY id").fetchall()

    def update_user(self, user_id: int, **fields: Any) -> None:
        allowed = {"alert_email", "digest_enabled", "password_hash"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        with self.connect() as con:
            con.execute(
                f"UPDATE users SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                (*sets.values(), user_id),
            )

    # ---- watchlists -------------------------------------------------------------------

    def ensure_watchlist(self, user_id: int, name: str = "My watchlist") -> int:
        with self.connect() as con:
            row = con.execute("SELECT id FROM watchlists WHERE user_id = ? AND name = ?", (user_id, name)).fetchone()
            if row:
                return row["id"]
            return con.execute(
                "INSERT INTO watchlists (user_id, name, created_at) VALUES (?, ?, ?)", (user_id, name, _now())
            ).lastrowid

    def add_item(self, watchlist_id: int, symbol: str, rules: dict | None = None, note: str | None = None) -> None:
        with self.connect() as con:
            con.execute(
                """
                INSERT INTO watchlist_items (watchlist_id, symbol, rules_json, note, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (watchlist_id, symbol) DO UPDATE SET
                    rules_json = excluded.rules_json, note = coalesce(excluded.note, watchlist_items.note)
                """,
                (watchlist_id, symbol.upper().strip(), json.dumps(rules or {}), note, _now()),
            )

    def remove_item(self, watchlist_id: int, symbol: str) -> None:
        with self.connect() as con:
            con.execute("DELETE FROM watchlist_items WHERE watchlist_id = ? AND symbol = ?", (watchlist_id, symbol))

    def items(self, user_id: int) -> list[dict]:
        """Every watched symbol of a user with its rule overrides, across all their watchlists."""
        with self.connect() as con:
            rows = con.execute(
                """
                SELECT wi.symbol, wi.rules_json, wi.note, w.name AS watchlist
                FROM watchlist_items wi JOIN watchlists w ON w.id = wi.watchlist_id
                WHERE w.user_id = ? ORDER BY w.name, wi.symbol
                """,
                (user_id,),
            ).fetchall()
        return [{"symbol": r["symbol"], "rules": json.loads(r["rules_json"]), "note": r["note"], "watchlist": r["watchlist"]} for r in rows]

    # ---- alerts -----------------------------------------------------------------------

    def record_alerts(self, user_id: int, alerts: list) -> list:
        """Insert alerts not seen before for this user. Returns the ones that were new."""
        new = []
        with self.connect() as con:
            for a in alerts:
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO alerts
                        (user_id, symbol, rule_id, session_date, severity, headline, detail, evidence_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, a.symbol, a.rule_id, a.date.isoformat(), a.severity, a.headline, a.detail,
                     json.dumps(a.evidence, default=str), _now()),
                )
                if cur.rowcount:
                    new.append(a)
        return new

    def mark_delivered(self, user_id: int, alerts: list) -> None:
        if not alerts:
            return
        with self.connect() as con:
            con.executemany(
                "UPDATE alerts SET delivered_at = ? WHERE user_id = ? AND symbol = ? AND rule_id = ? AND session_date = ?",
                [(_now(), user_id, a.symbol, a.rule_id, a.date.isoformat()) for a in alerts],
            )

    def alert_history(self, user_id: int, limit: int = 200) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM alerts WHERE user_id = ? ORDER BY session_date DESC, severity DESC, id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

    # ---- runs -------------------------------------------------------------------------

    def start_run(self) -> int:
        with self.connect() as con:
            return con.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (_now(),)).lastrowid

    def finish_run(self, run_id: int, **fields: Any) -> None:
        cols = {k: v for k, v in fields.items() if k in {"session_date", "users", "symbols", "alerts_new", "alerts_sent", "status", "detail"}}
        cols["finished_at"] = _now()
        with self.connect() as con:
            con.execute(f"UPDATE runs SET {', '.join(f'{k} = ?' for k in cols)} WHERE id = ?", (*cols.values(), run_id))

    def last_runs(self, n: int = 10) -> list[sqlite3.Row]:
        with self.connect() as con:
            return con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (n,)).fetchall()
