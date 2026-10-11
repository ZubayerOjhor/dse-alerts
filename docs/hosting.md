# Hosting a public instance

Everything in `README.md` assumes you run dse-alerts for yourself. This page is for the
other case: one instance on the internet that anyone can open, try with the demo account,
and sign up on. Nothing about the code changes; it is the same container with two switches:

| Setting | Public instance | Why |
|---|---|---|
| `APP_DEMO=1` | on | shows **Try the demo** on the login page, which signs the visitor into a shared, read-only account seeded by `dse demo seed` (sample watchlist, sample portfolio, 40 sessions of alert history). Registration stays open, so a visitor who wants their own list creates an account in ten seconds. |
| `APP_PUBLIC_URL` | `https://<ip>.sslip.io` or your domain | the link in the emails. |
| `APP_SECRET_KEY` | a long random string | signs the session cookies. |

The demo account can never write: adding symbols, changing rules, saving settings and
renaming accounts all return a note pointing at registration. Its portfolio is synthetic
(round lots bought at the price of a year ago), not anyone's real holdings.

**What a public instance costs you.** The market data belongs to the exchange and is
fetched by the server; each visitor sees the same local copy. Alert emails go out only to
people who registered with their own address. The AI note is optional and, with Groq's free
tier, costs nothing. The machine itself is the only bill.

## Option A: Azure for Students (US$0, no card; needs the GitHub Student Developer Pack)

The pack includes *Azure for Students*: US$100 of credit, 750 hours a month of a small
machine and 12 months of free storage, verified with a code from GitHub instead of a card.
750 hours is a whole month, so one `B2ats v2` machine (2 CPUs, 1 GB plus a swap file the
setup script adds) runs dse-alerts around the clock for nothing. Click by click:
[docs/azure.md](azure.md). Renewable yearly while you are a student.

## Option B: Oracle Cloud Always Free (US$0, if its card check lets you in)

Oracle's free ARM machine fits well (persistent disk, always on, 12 GB), but its sign-up
rejects many cards from outside the US and Europe, and there is no appeal beyond support
chat. The guide, including what to do when the card is refused, is
[docs/oracle-cloud.md](oracle-cloud.md). On any fresh Ubuntu machine, including any other
VPS, the server side is one command:

```bash
curl -fsSL https://raw.githubusercontent.com/ZubayerOjhor/dse-alerts/main/scripts/vm-setup.sh | bash
```

It installs Docker, opens ports 80/443, clones the repository, writes `.env` (random
secret, demo mode on, an `<ip>.sslip.io` hostname so HTTPS works without a domain) and
starts `docker compose --profile https up -d`: web UI, scheduler and a Caddy proxy that
fetches the certificate. Add `SMTP_*` and `GROQ_API_KEY` to `.env` afterwards.

## Option C: Fly.io (about US$6 a month)

One small machine in Singapore with a 3 GB volume. The web UI and the daily scheduler run
in the same container (`scripts/start.sh`), so there is one machine, one disk, one bill.

1. Install `flyctl` ([fly.io/docs/flyctl/install](https://fly.io/docs/flyctl/install/)) and
   sign up: `fly auth signup`. A card is required, as with any host.
2. In the repository, pick a name and create the app without deploying yet:

   ```bash
   fly launch --no-deploy --copy-config --name <your-app-name> --region sin
   ```

   Answer "no" to Postgres/Redis; `fly.toml` already has the settings.
3. Set the secrets (never put these in `fly.toml`):

   ```bash
   fly secrets set APP_SECRET_KEY="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
                   APP_PUBLIC_URL="https://<your-app-name>.fly.dev" \
                   SMTP_USER="you@gmail.com" SMTP_PASSWORD="xxxx xxxx xxxx xxxx" \
                   GROQ_API_KEY="gsk_..."           # optional: AI note
   ```

   Telegram (`TELEGRAM_BOT_TOKEN`) is optional too; users paste their own chat id in Settings.
4. Deploy: `fly deploy`. The first boot takes 10-20 minutes: the scheduler downloads the
   fallback archive (2023 onward), pulls the latest sessions from dse.com.bd, builds the
   feature table and seeds the demo account. Watch it with `fly logs`. The web UI answers
   as soon as the container starts and fills in as the data lands.
5. Open `https://<your-app-name>.fly.dev`. Create the first account (that is you), then
   test the demo button from a private window.

**History before 2023.** The curated archive cannot be downloaded by a script. Fetch it on
your machine as described in `README.md`, then copy the resulting DuckDB file onto the
volume once: `fly ssh sftp shell`, `put data/market.duckdb /data/market.duckdb`, and
`fly apps restart <your-app-name>`. The scheduler keeps it current from there.

**Memory.** `fly.toml` asks for 1 GB plus 1 GB swap. If `fly logs` shows the feature build
being killed, change `memory = "2gb"` and `fly deploy` again (about US$11 a month).

**Updates.** `git pull && fly deploy`. The volume (data, accounts, alert history) survives.

## Option D: Render / Railway

Both build the `Dockerfile` as-is. Both need a **persistent disk** mounted at `/data`
(Render: paid plans only; Railway: volumes on any plan) and the start command
`sh scripts/start.sh`. Set the same environment variables as above. Free tiers that sleep
the service will miss the 16:30 run, so use a plan that keeps it awake.

## After it is up

* Put the URL in the README's **Try it** line so the repository links to the live demo.
* `sudo docker compose exec web dse alerts run --dry-run` (Fly: `fly ssh console -C "dse alerts run --dry-run"`) shows what today's run would send.
* `dse demo seed --reset` rebuilds the demo account if a visitor somehow changed it, and
  runs harmlessly every container start (the scheduler calls it).
