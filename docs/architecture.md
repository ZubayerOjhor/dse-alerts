# Architecture, tech stack and design decisions

How dse-alerts is put together: every layer, the daily chain that runs it, and why each choice
was made. The short version is in the [README](../README.md#architecture).

![Architecture](architecture.svg)

## 1. Tech stack by layer

| Layer | Technology | What it does in dse-alerts | Why this choice |
|---|---|---|---|
| Language | Python 3.11+ (3.12 in Docker and CI) | Everything: data, analysis, rules, web, delivery | One language for the data work and the product |
| Packaging | pyproject + setuptools, extras `web`, `ai`, `dev` | Install only what a run needs: CI skips the web stack, the server adds it | Small installs on GitHub Actions |
| Command line | Typer + Rich | The `dse` command: `data`, `analysis`, `alerts`, `portfolio`, `web`, `demo` groups (about 30 commands) | Every job is scriptable, so cron, Docker and CI all call the same commands |
| Configuration | pydantic-settings reading `.env` | SMTP, Telegram, AI keys, data paths, demo switch, memory caps; strips inline comments | Secrets stay in `.env` or GitHub secrets, never in code |
| Live data | bdshare 1.2.8 (dse.com.bd) | Daily prices, live quotes, price-sensitive news, dividend declarations, index, company profiles | The only maintained DSE client; a browser user-agent and 45 s timeout added after DSE started refusing its default |
| History | Mendeley DSE dataset 23553sm4tn v4 (Oct 2012 on) | 13 years of daily prices and an adjustment ratio for bonus shares and rights | Live source only goes back to late 2024; this archive passed every quality test |
| Market store | DuckDB | 1.28 M price rows, 716 symbols; features and baselines computed in SQL | Fast column store in one file, no database server to run |
| Website copy | DuckDB snapshot file | The website reads a published copy, so pages stay up while the daily job writes | DuckDB allows one writer; this removes lock errors |
| Raw files | Parquet / pyarrow | Raw drops from the archive | Compact and typed |
| App database | SQLite | Users, watchlists, custom rules, holdings and BO accounts, alerts sent, AI notes, confirmations | Zero setup, one file, easy to back up |
| Analysis | pandas + DuckDB SQL | Returns over 1/5/20/60/250 sessions, volume ratios, 50-session average, 250-session range, circuit hits, adjusted prices | Vectorised, reproducible, tested |
| Calibration | Own code (`analysis/calibrate.py`), output `calibration.json` + `symbol_stats.csv` | Each stock's 5th/95th percentile moves, volume percentiles, backtest of the 9 price rules (40% to 51% vs 44% for any session) | "Unusual" must mean unusual for that stock; the result is published, not hidden |
| Alert engine | Own rule catalog (`rules/catalog.py`) | 13 rules: big drop, big rise, upper circuit, lower circuit, volume spike, 250-session high, 250-session low, diverged from DSEX, 50-session average cross, price-sensitive news, record date, P/E range, no trading. Each alert carries its evidence and fires once per episode | Facts with numbers, never buy/sell advice (BSEC adviser rules) |
| Scorecard | Own code (`scorecard.py`) | Groups live alerts into episodes and grades what followed against the backtest; hides live figures under 30 episodes | Keeps the system honest about itself |
| Portfolio | Own code (`portfolio.py`) | Several BO accounts, holdings by hand or CSV template, value, P&L, allocation, bonus shares added after the statement date | The daily email shows the whole position |
| Email | smtplib over Gmail SMTP (465 SSL, 587 STARTTLS, one fallback) | HTML and plain-text alert email with portfolio; signed confirmation link before any mail is sent | Free, reliable, works from GitHub Actions too |
| Telegram | Telegram Bot API over httpx | Same alerts in English or Bangla; chat confirmed with a code sent to the bot | For people who run their own copy |
| AI note (optional) | Groq (gpt-oss-120b), then OpenRouter (Gemma), over their OpenAI-compatible API with httpx; then Anthropic through its official SDK | A short plain-language summary of the day's alerts | Free tiers first; a fallback chain so one outage does not stop it |
| AI guardrails | Own checker (`ai/digest.py`) | Every number must appear in the input; predictive or advice wording rejected; one retry, then no note; only on days with notable alerts | An AI must never invent figures or give advice in a finance product |
| Web framework | FastAPI + Starlette, uvicorn | 13 pages (landing, overview, alerts, portfolio, targets, find stocks, symbol pages, rules, scorecard, learn, settings, login, register) and about 20 form actions, plus a small JSON API for charts | Fast, typed, small |
| Pages | Jinja2 server-rendered templates, plain JavaScript | No front-end build step; sortable tables, flash messages with undo, chart range switch | Simple to host and to read |
| Charts | lightweight-charts (vendored) | Candles, averages, portfolio value, index comparison | Light, no CDN dependency |
| Languages | Own i18n catalogs (`en.json`, `bn.json`) with a parity test | Every page, email, Telegram message and AI note in English or Bangla, Bangla dates and alert sentences | Built for Bangladeshi investors |
| Themes and layout | Plain CSS, four colour themes | Works from phone to desktop; columns hide on small screens | No framework weight |
| Auth | argon2id (argon2-cffi), itsdangerous signed cookies | Password hashes, sessions carrying a CSRF token and a password fingerprint, so a password change signs other sessions out | Standard, well-reviewed building blocks |
| Web security | Own middleware + Caddy | CSRF and Origin checks, login and register rate limits, CSP and hardening headers, HSTS, 256 KB body cap, no Server header, confirm pages for destructive actions | Public site with a login form |
| Container | Docker (python:3.12-slim), non-root user uid 1000 | One image for the website and the scheduler | Same build locally and in production |
| Services | Docker Compose: `web`, `scheduler`, `caddy` | Website, daily job and HTTPS proxy sharing one data volume | One command to run the whole system |
| HTTPS | Caddy 2 | Automatic Let's Encrypt certificate and renewal | No manual certificate work |
| Cloud | Azure for Students VM (B2ats v2, 2 vCPU, 1 GB RAM, Ubuntu 24.04, 3 GB swap), Azure DNS label | Hosts the live site | Free with the GitHub Student Pack; DuckDB capped at 300 MB to fit |
| Server setup | `scripts/vm-setup.sh` | Installs Docker, adds swap, sets memory caps, fixes volume ownership, starts the stack | One command for anyone to host their own |
| Free mode | GitHub Actions (`daily.yml`) | Daily run at 16:30 Dhaka time, Sun to Thu, for anyone who forks the repo; market data kept in the Actions cache | Alerts with no server at all |
| Testing | pytest (98 tests), ruff | Rules, features, archive checks, i18n parity, rendering, web flows, security, AI checker; live keys blanked in tests | Every bug fix got a test |
| UI checks | Playwright | Screenshots for the README and width checks from 360 to 1920 px | Catches layout breaks |
| Repo tooling | git, gh CLI, git filter-repo, Azure CLI | Version control, public repo, history clean-up, server management | Standard tools |
| Licence | MIT | Anyone can use, change and host it | Open source |

## 2. Orchestration: the daily chain

The DSE closes at 14:30 Dhaka time. Trading days are Sunday to Thursday.

| Step | Command | What happens |
|---|---|---|
| 0. First start only | `dse data bootstrap`, `refresh`, `instruments`, `analysis features`, `import-stats` | Loads the archive, live data, company profiles, features and each stock's baselines into an empty volume |
| 0b. Demo | `dse demo seed` | Re-creates the read-only demo account on every start (when `APP_DEMO=1`) |
| 0c. Snapshot | `dse data snapshot` | Publishes a copy of the market store for the website |
| 1. 16:30 | `dse data refresh` | Pulls today's prices, news, dividends and index |
| 2. Check | `dse data latest --expect-today` | Stops if DSE has not published today's session yet |
| 3. Features | `dse analysis features` | Recomputes returns, volume ratios, ranges, circuit hits, adjusted prices |
| 4. Alerts | `dse alerts run` | Every user's stocks against all rules, custom rules applied, alerts stored, optional AI note, email and Telegram sent to confirmed recipients, outbox written |
| 5. Sundays | `dse data instruments` | Weekly refresh of company profiles and fundamentals (P/E) |
| 6. Publish | `dse data snapshot` | Website switches to the new data |
| Retry | built into `scheduler.sh` | If a step fails or the session is late, try again every 30 minutes until 21:30, then give the day up (holiday or outage) |
| Restart safety | `latest --expect-today` at start | A restarted scheduler does not resend a day already done |
| On demand | "Check alerts now" button | One user's stocks checked against the latest session, limited to 3 checks per 5 minutes |

The same chain runs in two places:
* **Live site:** the `scheduler` container (`scripts/scheduler.sh`, TZ=Asia/Dhaka, a 60-second loop).
* **Free mode:** GitHub Actions cron `30 10 * * 0-4` (UTC), with the market data restored from and saved to the Actions cache, the outbox kept as a run artifact, and a manual dry-run button.

## 3. System design decisions

| Topic | Decision | Why |
|---|---|---|
| Two databases | DuckDB for market data, SQLite for app data | Analytics and many small writes have different needs |
| One writer | The scheduler writes, the website reads a snapshot | DuckDB allows a single writer process |
| Per-stock thresholds | Each stock's own 5th/95th percentile, not a fixed % | A fixed 10% drop rule fires 18.6 times a year per stock; per-stock lines fire 4.2 |
| Adjusted prices | All returns use `close × adj_factor` | Bonus shares otherwise look like crashes |
| Once per episode | New highs, lows, big moves and record dates fire once | No repeated alerts for the same event |
| Data audit first | Circuit-breaker test, duplicates, closures excluded | 16 to 20% of pre-2023 sessions in one archive were impossible moves; 63,914 duplicate rows dropped |
| Facts, not advice | Alerts state what happened with numbers; AI wording checked | Legal (BSEC) and trust |
| Confirmed delivery | Email by signed link, Telegram by code | Nobody can send alerts to someone else's inbox |
| Privacy | Self-hostable, your data stays in your copy; the demo is read-only | Portfolios are personal |
| Low cost | Free Azure student VM, free GitHub Actions, free AI tiers | Runs for nothing |
| Bilingual | Every user-facing string in both catalogs, tested | The audience reads Bangla |

## 4. How it was built

| Item | Figure |
|---|---|
| Time | 16 days (10 to 25 Oct 2026) |
| Commits | 47 in that period |
| Code | about 10k lines (6.2k Python in the package, plus templates, CSS/JS and tests) |
| Tests | 98 |
| Reviews | 3 rounds by other AI models (another Claude model and ChatGPT) testing the live site and the code |
| Split | Md. Zubayer: the problem, the rules, what it must never do, the data audit, checking numbers, every trade-off. Claude: most of the code, the tests and the deploy scripts. |

More detail: [calibration and backtest](calibration.md), [data audit](data-quality.md), [running it daily](deploy.md), [hosting](hosting.md).
