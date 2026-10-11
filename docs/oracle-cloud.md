# A free public instance on Oracle Cloud

Oracle's *Always Free* tier gives a permanent ARM virtual machine (2 CPUs, 12 GB RAM as of
mid-2026) that never bills. That is more than dse-alerts needs, and unlike every other free
host it has a real disk and stays awake, so the 16:30 run happens and accounts survive.
Allow about 30 minutes, most of it waiting.

What you get at the end: `https://<ip>.sslip.io` (or your own domain) with the **Try the
demo** button, open registration, daily alerts, and HTTPS, for US$0 a month.

## 1. Oracle account (10 minutes)

1. Go to [cloud.oracle.com](https://cloud.oracle.com) → *Sign up*. Pick the **home region**
   carefully; it cannot be changed. For Bangladesh, **Singapore (ap-singapore-1)** or
   **Mumbai (ap-mumbai-1)** are closest. If one is "out of capacity" later, Frankfurt and
   Phoenix usually have room.
2. A card is required for identity verification. Nothing is charged for Always Free
   resources, and the account stays in "Free Tier" mode unless you explicitly upgrade.
3. Wait for the "your account is ready" email, then sign in.

### If the card is refused

Oracle's signup is the strictest of any cloud host and rejects many perfectly good cards.
The check is a US$1 authorisation without the bank's OTP step, so a card that works in
shops and on Amazon can still fail here. In rough order of how often each one helps:

1. **Stop after two or three attempts.** Every failure raises an internal risk score on the
   email, phone and card; after that nothing passes for a day or two. Wait 24-48 hours and
   use a fresh email address and a different browser profile on the next try.
2. **No VPN, no ad-blocker, no autofill.** Plain Chrome or Edge, normal window, extensions off.
3. **Type the details exactly as the bank has them:** the cardholder name as printed, the
   billing address the bank holds (Latin spelling, with postcode), and a phone number in the
   same country as the card. Country of residence must equal the card's country.
4. **Ask the bank** to enable "international / e-commerce transactions" on the card and to
   lift any per-transaction block on USD card-not-present payments. Dual-currency cards
   from Bangladeshi banks often ship with this off.
5. **Credit beats debit, and no prepaid or virtual cards.** Oracle refuses prepaid, virtual
   and single-use numbers outright.
6. **Pick "Pay As You Go" instead of "Free Tier"** on the plan page. Always Free resources
   are included either way, and the verification sometimes behaves differently.
7. **Oracle chat support** can finish the signup by hand when the automated check keeps
   failing; it is the slowest route but it does work.

If none of that works in an evening, stop. With the GitHub Student Developer Pack, Azure
for Students needs no card at all ([docs/azure.md](azure.md)); otherwise Fly.io or any VPS
takes the card through a normal 3-D Secure payment. The server side is the same script
either way ([docs/hosting.md](hosting.md)).

## 2. Open ports 80 and 443 in the network (3 minutes)

Oracle blocks everything except SSH by default, in two places. This is the network one.

1. Menu ☰ → **Networking → Virtual cloud networks**. If none exists, *Start VCN Wizard →
   Create VCN with Internet Connectivity*, accept the defaults, *Create*.
2. Open the VCN → **Security Lists** → *Default Security List* → **Add Ingress Rules**:
   * Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port range `80`
   * Source CIDR `0.0.0.0/0`, IP protocol TCP, destination port range `443`

## 3. Create the machine (5 minutes)

1. Menu ☰ → **Compute → Instances → Create instance**.
2. Name it `dse-alerts`.
3. **Image and shape → Edit**: Image **Canonical Ubuntu 24.04** (not Oracle Linux; the
   setup script assumes Ubuntu). Shape: *Ampere* → **VM.Standard.A1.Flex**, 2 OCPUs, 12 GB
   memory (both show the *Always Free-eligible* label).
4. **Networking**: the VCN from step 2, public subnet, *Assign a public IPv4 address*.
5. **Add SSH keys → Generate a key pair for me → Save private key**. Keep the downloaded
   file; it is the only way in.
6. Boot volume: the default 50 GB is fine (200 GB is free in total).
7. *Create*. If you see **Out of host capacity**, the region is full of A1 machines right
   now; retry in a few hours, pick another availability domain, or try at night.
8. When the instance shows **Running**, copy its **Public IP address**.

## 4. Connect and run the setup script (15 minutes, mostly waiting)

From PowerShell on your PC (replace the key path and IP):

```powershell
ssh -i "$HOME\Downloads\ssh-key-2026-10-11.key" ubuntu@203.0.113.10
```

Windows may refuse the key because its permissions are too open. If so:

```powershell
icacls "$HOME\Downloads\ssh-key-2026-10-11.key" /inheritance:r /grant:r "$env:USERNAME`:R"
```

On the machine, one command does the rest (Docker, OS firewall, clone, `.env`, start):

```bash
curl -fsSL https://raw.githubusercontent.com/ZubayerOjhor/dse-alerts/main/scripts/vm-setup.sh | bash
```

> The repository must be public for that URL to work. While it is private, do this instead:
> `sudo apt-get install -y gh git && gh auth login && gh repo clone ZubayerOjhor/dse-alerts && bash dse-alerts/scripts/vm-setup.sh`

The script prints the address it chose, `https://<ip-with-dashes>.sslip.io`. sslip.io is a
public DNS service that maps that name to your IP, so HTTPS works without buying a domain.
The first boot downloads the fallback archive and builds the feature table; follow it with
`sudo docker compose logs -f scheduler` and wait for `demo account #... holdings`.

## 5. Finish

1. Open the address. **Create the first account**; that one is yours.
2. Open a private window and press **Try the demo** to see what visitors get.
3. Email and the AI note: `nano ~/dse-alerts/.env`, fill `SMTP_USER`, `SMTP_PASSWORD`
   (a Gmail app password) and `GROQ_API_KEY`, then
   `cd ~/dse-alerts && sudo docker compose --profile https up -d`.
4. Put the address in the README's **Try it** line.

## Later

* **Update to a new version:** `cd ~/dse-alerts && git pull && sudo docker compose --profile https up -d --build`.
* **Own domain:** point an A record at the IP, set `DOMAIN=alerts.yourdomain.com` and
  `APP_PUBLIC_URL=https://alerts.yourdomain.com` in `.env`, restart as above.
* **History before 2023:** download the curated archive on your PC (README), then copy the
  finished database up once:
  `scp -i <key> data/market.duckdb ubuntu@<ip>:/tmp/ && ssh -i <key> ubuntu@<ip> "sudo docker cp /tmp/market.duckdb dse-alerts-web-1:/data/market.duckdb && cd dse-alerts && sudo docker compose --profile https restart"`.
* **Oracle reclaims idle free machines** (CPU under 20% for 7 days). The scheduler's daily
  build keeps this one busy enough; if you ever get a reclamation notice, open the UI once.
* **Backups:** everything lives in the `dse-alerts_dse-data` Docker volume. Copy `app.sqlite`
  out of it occasionally: `sudo docker cp dse-alerts-web-1:/data/app.sqlite ./app-backup.sqlite`.
