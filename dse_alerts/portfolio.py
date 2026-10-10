"""Portfolio valuation: holdings (what the user owns, per account) priced with the latest
session, with cost, profit/loss, the day's move, and the value history of the whole book.

Numbers only. Nothing here says what to do with a position.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from dse_alerts.appdb import AppDB
from dse_alerts.data.store import MarketStore


def import_csv(appdb: AppDB, user_id: int, path: Path) -> dict:
    """Load holdings from a CSV with columns account,symbol,quantity,avg_cost[,statement_price,
    statement_date,margin,note]. Re-importing updates quantities and costs in place."""
    df = pd.read_csv(path, dtype=str).fillna("")
    required = {"account", "symbol", "quantity", "avg_cost"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"holdings file is missing columns: {sorted(missing)}")
    n, accounts = 0, set()
    for r in df.itertuples():
        acc = appdb.ensure_account(user_id, r.account.strip() or "Default")
        accounts.add(r.account.strip() or "Default")
        appdb.upsert_holding(
            acc, r.symbol, float(str(r.quantity).replace(",", "")), float(r.avg_cost),
            float(r.statement_price) if getattr(r, "statement_price", "") else None,
            getattr(r, "statement_date", "") or None, getattr(r, "margin", "") or None, getattr(r, "note", "") or None,
        )
        n += 1
    return {"rows": n, "accounts": sorted(accounts)}


def _latest(store: MarketStore, symbols: list[str]) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame(columns=["symbol", "date", "close", "prev_close", "ret_1", "ret_20", "ret_5", "sma_50", "high_250", "low_250", "name", "category", "sector"])
    ph = ",".join("?" * len(symbols))
    with store.connect(read_only=True) as con:
        return con.execute(
            f"""
            SELECT f.symbol, f.date, f.close, f.prev_close, f.ret_1, f.ret_5, f.ret_20, f.sma_50, f.high_250, f.low_250,
                   i.name, i.category, i.sector
            FROM features f JOIN instruments i USING (symbol)
            WHERE f.symbol IN ({ph})
            QUALIFY row_number() OVER (PARTITION BY f.symbol ORDER BY f.date DESC) = 1
            """,
            symbols,
        ).df()


def valuation(store: MarketStore, appdb: AppDB, user_id: int) -> dict:
    """Per-holding rows plus per-account and total summaries."""
    rows = appdb.holdings(user_id)
    if not rows:
        return {"positions": pd.DataFrame(), "accounts": pd.DataFrame(), "total": {}, "as_of": None}
    h = pd.DataFrame(rows)
    px = _latest(store, sorted(h["symbol"].unique()))
    df = h.merge(px, on="symbol", how="left")
    df["cost"] = df["quantity"] * df["avg_cost"]
    df["value"] = df["quantity"] * df["close"]
    df["pnl"] = df["value"] - df["cost"]
    df["pnl_pct"] = df["pnl"] / df["cost"]
    df["day_change"] = df["quantity"] * (df["close"] - df["prev_close"])
    df["statement_value"] = df["quantity"] * df["statement_price"]
    df["since_statement_pct"] = df["close"] / df["statement_price"] - 1
    df["weight"] = df["value"] / df["value"].sum()
    accounts = (
        df.groupby("account")
        .agg(positions=("symbol", "count"), cost=("cost", "sum"), value=("value", "sum"), pnl=("pnl", "sum"),
             day_change=("day_change", "sum"), statement_value=("statement_value", "sum"))
        .reset_index()
    )
    accounts["pnl_pct"] = accounts["pnl"] / accounts["cost"]
    total = {
        "positions": len(df), "symbols": int(df["symbol"].nunique()),
        "cost": float(df["cost"].sum()), "value": float(df["value"].sum()), "pnl": float(df["pnl"].sum()),
        "pnl_pct": float(df["pnl"].sum() / df["cost"].sum()) if df["cost"].sum() else None,
        "day_change": float(df["day_change"].sum()),
        "day_change_pct": float(df["day_change"].sum() / (df["value"].sum() - df["day_change"].sum())) if df["value"].sum() else None,
        "statement_value": float(df["statement_value"].sum()) if df["statement_value"].notna().any() else None,
    }
    as_of = df["date"].max()
    return {"positions": df.sort_values(["account", "value"], ascending=[True, False]).reset_index(drop=True),
            "accounts": accounts, "total": total, "as_of": as_of.date() if hasattr(as_of, "date") else as_of}


def by_group(positions: pd.DataFrame, key: str) -> pd.DataFrame:
    """Value and P&L by sector / category / account, for allocation views."""
    if positions.empty:
        return pd.DataFrame(columns=[key, "value", "cost", "pnl", "weight"])
    g = positions.groupby(key, dropna=False).agg(value=("value", "sum"), cost=("cost", "sum"), pnl=("pnl", "sum")).reset_index()
    g["weight"] = g["value"] / g["value"].sum()
    return g.sort_values("value", ascending=False).reset_index(drop=True)


def value_history(store: MarketStore, appdb: AppDB, user_id: int, sessions: int = 250) -> pd.DataFrame:
    """Book value per session over the last N sessions at today's quantities (positions are
    held constant, so this is 'what this book was worth', not realised performance)."""
    rows = appdb.holdings(user_id)
    if not rows:
        return pd.DataFrame(columns=["date", "value", "cost"])
    h = pd.DataFrame(rows).groupby("symbol").agg(quantity=("quantity", "sum"), cost=("avg_cost", lambda s: 0)).reset_index()
    cost = float((pd.DataFrame(rows)["quantity"] * pd.DataFrame(rows)["avg_cost"]).sum())
    with store.connect(read_only=True) as con:
        con.register("q", h[["symbol", "quantity"]])
        hist = con.execute(
            """
            WITH sess AS (SELECT DISTINCT date FROM features WHERE symbol IN (SELECT symbol FROM q)
                          ORDER BY date DESC LIMIT ?),
                 px AS (SELECT f.symbol, f.date, f.close FROM features f WHERE f.symbol IN (SELECT symbol FROM q)
                        AND f.date >= (SELECT min(date) FROM sess)),
                 grid AS (SELECT s.date, q.symbol, q.quantity FROM sess s CROSS JOIN q),
                 filled AS (
                   SELECT g.date, g.symbol, g.quantity,
                          (SELECT p.close FROM px p WHERE p.symbol = g.symbol AND p.date <= g.date ORDER BY p.date DESC LIMIT 1) AS close
                   FROM grid g)
            SELECT date, sum(quantity * close) AS value FROM filled GROUP BY date ORDER BY date
            """,
            [sessions],
        ).df()
        con.unregister("q")
    hist["cost"] = cost
    return hist


def to_dict_rows(df: pd.DataFrame) -> list[dict]:
    return [] if df is None or df.empty else df.where(pd.notna(df), None).to_dict("records")


def as_of_str(d: dt.date | None) -> str:
    return d.isoformat() if d else "–"
