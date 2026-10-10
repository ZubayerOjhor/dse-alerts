"""Alert rules.

A rule is a small object with an id, a description, default parameters, a SQL predicate
over the ``features`` table (used for backtests and the calibration report) and an
``evaluate`` method that looks at one symbol's latest session and returns an ``Alert`` or
``None``. Rules state facts about prices, volumes and filings; none of them says buy or sell.
"""

from dse_alerts.rules.base import Alert, Rule, RuleContext
from dse_alerts.rules.catalog import RULES, get_rule

__all__ = ["RULES", "Alert", "Rule", "RuleContext", "get_rule"]
