"""Everything a delivery (email, Telegram, outbox) may show for one user and one session."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from dse_alerts import market, portfolio
from dse_alerts.appdb import AppDB
from dse_alerts.data.store import MarketStore
from dse_alerts.rules import Alert


@dataclass
class DeliveryContext:
    session: dt.date
    alerts: list[Alert]
    symbols: list[str]
    digest: str | None = None
    index: dict = field(default_factory=dict)        # close, ret_1, ret_20
    watch_rows: list[dict] = field(default_factory=list)   # symbol, name, kind, close, ret_1, ret_20
    portfolio: dict | None = None                    # total dict + movers
    base_url: str = ""
    lang: str = "en"                                 # en | bn; the user's saved preference


def build(store: MarketStore, appdb: AppDB, user_id: int, session: dt.date, alerts: list[Alert],
          digest: str | None = None, base_url: str = "", lang: str | None = None) -> DeliveryContext:
    if lang is None:
        u = appdb.user_by_id(user_id)
        lang = (dict(u).get("lang") if u else None) or "en"
    items = appdb.items(user_id)
    kinds = {i["symbol"]: i["kind"] for i in items}
    symbols = sorted(kinds)
    rows = market.latest_rows(store, symbols, spark_sessions=2) if symbols else None
    watch_rows = []
    if rows is not None and not rows.empty:
        for r in rows.itertuples():
            watch_rows.append({"symbol": r.symbol, "name": r.name, "kind": kinds.get(r.symbol, "watch"),
                               "close": r.close, "ret_1": r.ret_1, "ret_20": r.ret_20})
        watch_rows.sort(key=lambda x: (x["ret_1"] if x["ret_1"] == x["ret_1"] and x["ret_1"] is not None else 0), reverse=True)
    pf = None
    try:
        v = portfolio.valuation(store, appdb, user_id)
        if v["total"]:
            pos = v["positions"].dropna(subset=["day_change"])
            # one line per company, across accounts
            movers = pos.groupby("symbol").agg(day_change=("day_change", "sum"), ret_1=("ret_1", "first")).reset_index().sort_values("day_change")
            movers = movers[movers["day_change"] != 0]
            pf = {**v["total"], "as_of": v["as_of"],
                  "best": [{"symbol": r.symbol, "day_change": r.day_change, "ret_1": r.ret_1} for r in movers.tail(3).iloc[::-1].itertuples() if r.day_change > 0],
                  "worst": [{"symbol": r.symbol, "day_change": r.day_change, "ret_1": r.ret_1} for r in movers.head(3).itertuples() if r.day_change < 0]}
    except Exception:  # noqa: BLE001 - a valuation problem must not block the alerts
        pf = None
    return DeliveryContext(session=session, alerts=alerts, symbols=symbols, digest=digest,
                           index=market.index_last(store), watch_rows=watch_rows, portfolio=pf, base_url=base_url, lang=lang)
