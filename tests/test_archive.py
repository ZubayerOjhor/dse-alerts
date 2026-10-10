import pandas as pd

from dse_alerts.data import archive


def _csv(tmp_path, rows):
    p = tmp_path / "DSE_Data.csv"
    pd.DataFrame(rows, columns=["Trading_Code", "Date", "Open", "High", "Low", "Close", "Volume"]).to_csv(p, index=False)
    return p


ROWS = [
    ("GP ", "2021-03-04", 10, 11, 9, 10.5, 100),        # before RELIABLE_FROM -> dropped by default
    ("GP", "2022-08-02", 6.3, 6.4, 6.2, 6.3, 439037),   # before RELIABLE_FROM -> dropped by default
    ("GP", "2022-08-02", 6.4, 6.4, 6.3, 6.4, 163971),
    ("GP", "2023-05-02", 10, 11, 9, 10.5, 100),         # kept
    ("GP", "2023-05-02", 10, 11, 9, 10.5, 100),         # exact duplicate -> dropped
    ("GP", "2023-05-03", 10, 11, 9, 0.0, 100),          # zero close -> dropped
    ("GP", "2023-05-04", 10, 9, 11, 10.0, 100),         # high < low -> dropped
    ("GP", "2023-05-07", 6.3, 6.4, 6.2, 6.3, 439037),   # conflict pair: keep max volume
    ("GP", "2023-05-07", 6.4, 6.4, 6.3, 6.4, 163971),
]


def test_load_keeps_reliable_range_and_cleans(tmp_path):
    df, stats = archive.load(_csv(tmp_path, ROWS))

    assert stats["dropped_unreliable_dates"] == 3
    assert stats["dropped_bad"] == 2
    assert stats["dropped_exact_dups"] == 1
    assert stats["resolved_conflicts"] == 1
    assert df["symbol"].unique().tolist() == ["GP"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2023-05-02", "2023-05-07"]
    assert df["date_quality"].unique().tolist() == ["exact"]

    kept = df[df["date"] == "2023-05-07"].iloc[0]
    assert kept["volume"] == 439037 and kept["close"] == 6.3


def test_load_can_include_unreliable_rows_flagged(tmp_path):
    df, stats = archive.load(_csv(tmp_path, ROWS), reliable_from="2000-01-01")

    assert stats["dropped_unreliable_dates"] == 0
    assert df["symbol"].tolist() == ["GP"] * 4
    q = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["date_quality"]))
    assert q["2021-03-04"] == "unreliable" and q["2023-05-02"] == "exact"
