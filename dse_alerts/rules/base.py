from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pandas as pd


@dataclass
class Alert:
    symbol: str
    rule_id: str
    date: dt.date                 # session the alert refers to
    headline: str                 # one line, plain language, a fact
    detail: str                   # a few lines of context (numbers, baselines, filings)
    severity: str = "info"        # info | notable | high
    evidence: dict[str, Any] = field(default_factory=dict)   # numbers used, for the UI and the AI digest

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.symbol, self.rule_id, self.date.isoformat())


@dataclass
class RuleContext:
    """Everything a rule may look at for one symbol."""
    row: pd.Series                      # latest features row for the symbol
    stats: pd.Series | None             # symbol_stats row, if the symbol has history
    instrument: pd.Series | None        # instruments row
    fundamentals: pd.Series | None      # latest fundamentals snapshot
    news: pd.DataFrame                  # PSI/corporate news for the symbol, newest first
    dividends: pd.DataFrame             # dividend declarations for the symbol, newest first
    recent: pd.DataFrame                # last ~60 feature rows for the symbol, oldest first
    as_of: dt.date                      # the session being evaluated

    def stat(self, name: str, default: float | None = None) -> float | None:
        if self.stats is None or name not in self.stats or pd.isna(self.stats[name]):
            return default
        return float(self.stats[name])

    def val(self, name: str) -> float | None:
        v = self.row.get(name)
        return None if v is None or pd.isna(v) else float(v)


@dataclass
class Rule:
    id: str
    name: str
    description: str                 # what the alert means, for the UI
    params: dict[str, Any]           # defaults; a watchlist entry may override any of them
    predicate_sql: str | None        # feature-table predicate with defaults baked in, for backtests
    evaluate: Callable[[RuleContext, dict[str, Any]], Alert | None]
    severity: str = "info"

    def run(self, ctx: RuleContext, overrides: dict[str, Any] | None = None) -> Alert | None:
        params = {**self.params, **(overrides or {})}
        return self.evaluate(ctx, params)


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:+.{digits}f}%"
