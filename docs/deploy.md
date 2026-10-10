# Running it every day

Three ways, from zero-maintenance to fully private. All of them run the same command,
`dse alerts run`, after the DSE closes (14:30 Bangladesh time, Sunday to Thursday).

## 1. GitHub Actions (free, nothing to host)

1. Fork this repository (private forks are fine).
2. In your fork: **Settings → Secrets and variables → Actions → New repository secret**:
   * `SMTP_USER`: your Gmail address
   * `SMTP_PASSWORD`: a Gmail **App password** (Google Account → Security → 2-Step
     Verification → App passwords). Your normal password will not work and should never
     be put anywhere.
   * Optional: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` for Telegram delivery (create a
     bot with @BotFather, message it once, then `dse alerts telegram-id` locally shows the id).
   * Optional: `WATCHLIST_JSON` with the contents of your watchlist file, if you would rather
     not commit your email address to the repository.
3. Copy `watchlist.example.json` to `watchlist.json`, edit it, commit (or use the secret above).
4. **Actions → daily-alerts → Run workflow** once, with *dry run* ticked, and read the log.
   Then run it again without dry run to get the first email.

From then on it runs at 16:30 Bangladesh time on trading days. Market data is cached
between runs, so a run takes two to three minutes. The rendered alerts are also attached to
every run as an artifact (`outbox-<run id>`), which is handy when email is not set up.

What GitHub sees: your watchlist file and the market data cache. What it does not see:
your password beyond the encrypted secret store, and nothing is sent anywhere except the
SMTP server you configured.

## 2. Your own machine (Windows Task Scheduler / cron)

```bash
git clone https://github.com/ZubayerOjhor/dse-alerts && cd dse-alerts
python -m venv .venv && .venv/Scripts/activate      # or source .venv/bin/activate
pip install -e .
copy .env.example .env                               # fill in SMTP_* if you want email
dse data bootstrap && dse data refresh && dse data instruments
dse analysis features && dse analysis import-stats   # or `dse analysis calibrate` to recompute
dse alerts init --email you@example.com
dse alerts add GP SQURPHARMA --email you@example.com
dse alerts run --dry-run
```

Then schedule `dse alerts run` (preceded by `dse data refresh` and `dse analysis features`)
daily at 16:30. A wrapper script keeps it to one line:

```bash
# run_daily.sh / run_daily.cmd
dse data refresh && dse analysis features && dse alerts run
```

* **Windows:** Task Scheduler → Create Basic Task → Daily 16:30 → Start a program →
  `D:\path\to\dse-alerts\.venv\Scripts\dse.exe` is not enough on its own; point it at
  `cmd /c "cd /d D:\path\to\dse-alerts && .venv\Scripts\activate && run_daily.cmd"`.
  Weekends are skipped automatically because there is no new session.
* **Linux/macOS cron:** `30 16 * * 0-4 cd /path/to/dse-alerts && .venv/bin/dse data refresh && .venv/bin/dse analysis features && .venv/bin/dse alerts run >> logs/daily.log 2>&1`

## 3. Docker

```bash
docker compose up -d
```

The compose file (phase 6) runs the web UI and a scheduler container that executes the
same daily chain. Data lives in a named volume.

## Timing

DSE publishes day-end data on its site shortly after the close; 16:00-17:00 Bangladesh
time is safe. `dse data refresh` re-pulls the last five sessions every time, so a run that
happens before the day-end data is published simply catches up the next day.
