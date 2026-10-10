import pandas as pd

from dse_alerts.data import archive


def _csv(tmp_path, rows):
    p = tmp_path / "DSE_Data.csv"
    pd.DataFrame(rows, columns=["Trading_Code", "Date", "Open", "High", "Low", "Close", "Volume"]).to_csv(p, index=False)
    return p


def test_load_cleans_trims_dedups_and_flags(tmp_path):
    rows = [
        ("GP ", "2021-03-04", 10, 11, 9, 10.5, 100),        # Thursday, pre-2023 -> approx
        ("GP", "2021-03-05", 10, 11, 9, 10.5, 100),         # Friday, pre-2023 -> shifted
        ("GP", "2021-03-05", 10, 11, 9, 10.5, 100),         # exact duplicate -> dropped
        ("GP", "2022-08-02", 6.3, 6.4, 6.2, 6.3, 439037),   # conflict pair: keep max volume
        ("GP", "2022-08-02", 6.4, 6.4, 6.3, 6.4, 163971),
        ("GP", "2023-05-02", 10, 11, 9, 10.5, 100),         # 2023+ -> exact
        ("GP", "2023-05-03", 10, 11, 9, 0.0, 100),          # zero close -> dropped
        ("GP", "2023-05-04", 10, 9, 11, 10.0, 100),         # high < low -> dropped
    ]
    df, stats = archive.load(_csv(tmp_path, rows))

    assert stats["dropped_bad"] == 2
    assert stats["dropped_exact_dups"] == 1
    assert stats["resolved_conflicts"] == 1
    assert df["symbol"].unique().tolist() == ["GP"]
    assert len(df) == 4

    q = dict(zip(df["date"].dt.strftime("%Y-%m-%d"), df["date_quality"]))
    assert q == {"2021-03-04": "approx", "2021-03-05": "shifted", "2022-08-02": "approx", "2023-05-02": "exact"}

    kept = df[df["date"] == "2022-08-02"].iloc[0]
    assert kept["volume"] == 439037 and kept["close"] == 6.3
