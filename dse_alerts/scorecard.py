"""Scorecard: what happened after the alerts this installation actually sent.

For every recorded alert, the close 5 and 20 sessions after the alerted session is looked
up, together with the DSEX move over the same sessions. Alerts too recent to have 20
sessions behind them are shown as pending. Per rule, the realised outcomes sit next to the
backtest expectation, so a user can see whether the rules behave live the way the
14-year history said they would. It grades the alerts, not the user, and it never turns
an outcome into a recommendation.
"""

from __future__ import annotations

import json

import pandas as pd

from dse_alerts.appdb import AppDB
from dse_alerts.config import Settings
from dse_alerts.data.store import MarketStore

OUTCOME_SQL = """
WITH idx AS (
    SELECT date, dsex,
           lead(dsex, 5)  OVER (ORDER BY date) / dsex - 1 AS dsex_fwd_5,
           lead(dsex, 20) OVER (ORDER BY date) / dsex - 1 AS dsex_fwd_20
    FROM index_daily
)
SELECT a.symbol, a.session_date, f.close, f.fwd_5, f.fwd_20, i.dsex_fwd_5, i.dsex_fwd_20
FROM alerts_in a
LEFT JOIN features f ON f.symbol = a.symbol AND f.date = CAST(a.session_date AS DATE)
LEFT JOIN idx i ON i.date = CAST(a.session_date AS DATE)
"""


def outcomes(store: MarketStore, appdb: AppDB, user_id: int) -> pd.DataFrame:
    """One row per alert with realised 5/20-session outcomes (NaN while pending)."""
    rows = appdb.alert_history(user_id, limit=5000)
    if not rows:
        return pd.DataFrame(columns=["symbol", "rule_id", "severity", "session_date", "headline", "close",
                                     "fwd_5", "fwd_20", "dsex_fwd_5", "dsex_fwd_20", "excess_20", "status"])
    alerts = pd.DataFrame([dict(r) for r in rows])
    keys = alerts[["symbol", "session_date"]].drop_duplicates()
    with store.connect(read_only=True) as con:
        con.register("alerts_in", keys)
        out = con.execute(OUTCOME_SQL).df()
        con.unregister("alerts_in")
    out["session_date"] = out["session_date"].astype(str)
    df = alerts.merge(out, on=["symbol", "session_date"], how="left")
    df["excess_20"] = df["fwd_20"] - df["dsex_fwd_20"]
    df["status"] = df["fwd_20"].apply(lambda v: "scored" if pd.notna(v) else "pending")
    cols = ["symbol", "rule_id", "severity", "session_date", "headline", "close", "fwd_5", "fwd_20",
            "dsex_fwd_5", "dsex_fwd_20", "excess_20", "status"]
    return df[cols].sort_values(["session_date", "symbol"], ascending=[False, True]).reset_index(drop=True)


def by_rule(df: pd.DataFrame, expectation: dict[str, dict] | None = None) -> pd.DataFrame:
    """Aggregate per rule: counts, realised medians, share positive, excess over DSEX, and
    the backtest's median 20-session move for comparison."""
    if df.empty:
        return pd.DataFrame(columns=["rule_id", "alerts", "scored", "fwd5_median", "fwd20_median",
                                     "fwd20_positive_share", "excess20_median", "backtest_fwd20_median"])
    g = df.groupby("rule_id")
    agg = pd.DataFrame({
        "alerts": g.size(),
        "scored": g["fwd_20"].count(),
        "fwd5_median": g["fwd_5"].median(),
        "fwd20_median": g["fwd_20"].median(),
        "fwd20_positive_share": g["fwd_20"].apply(lambda s: (s.dropna() > 0).mean() if s.notna().any() else float("nan")),
        "excess20_median": g["excess_20"].median(),
    }).reset_index()
    exp = expectation or {}
    agg["backtest_fwd20_median"] = agg["rule_id"].map(lambda r: (exp.get(r) or {}).get("fwd20_median"))
    return agg.sort_values("alerts", ascending=False).reset_index(drop=True)


def load_expectation(settings: Settings) -> dict[str, dict]:
    p = settings.dse_data_dir / "calibration.json"
    if not p.exists():
        return {}
    try:
        return {e["event"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("event_stats", [])}
    except (ValueError, KeyError, TypeError):
        return {}
