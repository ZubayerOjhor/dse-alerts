import pandas as pd

from dse_alerts.data import archive


def test_find_curated_locates_extracted_zip(tmp_path):
    assert archive.find_curated(tmp_path) is None
    deep = tmp_path / archive.CURATED_DIRNAME / "Some Folder Name" / "Full Raw Data"
    deep.mkdir(parents=True)
    (deep / archive.CURATED_FILE).write_text("Date,Ticker,Open,High,Low,Close,Volume\n")
    assert archive.find_curated(tmp_path) == deep / archive.CURATED_FILE


def test_load_curated_maps_indexes_and_cleans(tmp_path):
    p = tmp_path / "UnAdjusted.csv"
    pd.DataFrame(
        [
            ("2021-03-01", "00DSEX", 5400, 5450, 5390, 5426.8, 78353504),
            ("2021-03-01", "00DSMEX", 1000, 1010, 990, 1005.0, 1),
            ("2021-03-01", "GP", 348.0, 365.0, 344.0, 358.4, 436617),
            ("2021-03-01", "GP", 348.0, 365.0, 344.0, 358.4, 436617),   # duplicate -> one row
            ("2021-03-02", "GP", 369.7, 379.0, 369.1, 0.0, 10),         # zero close -> dropped
            ("2021-03-02", "ROBI", 30.0, 29.0, 31.0, 30.5, 10),         # high < low -> dropped
            ("2021-03-02", "ROBI", 30.0, 31.0, 29.0, 30.5, 10),
        ],
        columns=["Date", "Ticker", "Open", "High", "Low", "Close", "Volume"],
    ).to_csv(p, index=False)

    df, stats = archive.load_curated(p)

    assert stats["raw_rows"] == 7 and stats["rows"] == 4 and stats["dropped"] == 3
    assert sorted(df["symbol"].unique()) == ["DSEX", "DSMEX", "GP", "ROBI"]
    assert df["date_quality"].unique().tolist() == ["exact"]
    assert df.loc[df["symbol"] == "DSEX", "close"].item() == 5426.8
