import datetime as dt

import pandas as pd

from dse_alerts.data.ingest import _month_chunks


def test_month_chunks_cover_the_range_without_gaps():
    chunks = list(_month_chunks(dt.date(2025, 11, 20), dt.date(2026, 2, 3)))
    assert chunks[0] == (dt.date(2025, 11, 20), dt.date(2025, 11, 30))
    assert chunks[-1] == (dt.date(2026, 2, 1), dt.date(2026, 2, 3))
    assert len(chunks) == 4
    for (_, b), (a2, _) in zip(chunks, chunks[1:]):
        assert a2 == b + dt.timedelta(days=1)


def test_month_chunks_accept_pandas_timestamps():
    # the cached market summary hands back Timestamps; the CLI passes dates. Both must work.
    chunks = list(_month_chunks(pd.Timestamp("2026-01-28"), dt.date(2026, 2, 2)))
    assert chunks == [(dt.date(2026, 1, 28), dt.date(2026, 1, 31)), (dt.date(2026, 2, 1), dt.date(2026, 2, 2))]
