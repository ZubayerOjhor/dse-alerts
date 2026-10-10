import pandas as pd

from dse_alerts.analysis import features
from dse_alerts.data.store import MarketStore


def test_no_trade_sessions_do_not_poison_the_250_session_range(tmp_path):
    store = MarketStore(tmp_path / "m.duckdb")
    dates = pd.bdate_range("2024-01-01", periods=260).date
    closes = [100.0] * 260
    highs, lows, opens, vols = [101.0] * 260, [99.0] * 260, [100.0] * 260, [1000] * 260
    # one session with no trades: DSE publishes open/high/low = 0 and the previous close
    opens[100] = highs[100] = lows[100] = 0.0
    vols[100] = 0
    store.upsert_prices(pd.DataFrame({"symbol": "X", "date": dates, "open": opens, "high": highs, "low": lows,
                                      "close": closes, "volume": vols}), source="live")
    features.build(store)
    with store.connect(read_only=True) as con:
        hi, lo, zero10 = con.execute("SELECT high_250, low_250, zero_vol_10 FROM features WHERE symbol='X' ORDER BY date DESC LIMIT 1").fetchone()
    assert hi == 101.0 and lo == 99.0 and zero10 == 0
