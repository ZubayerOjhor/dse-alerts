# A free public instance on Azure (GitHub Student Developer Pack)

If you have the GitHub Student Developer Pack, this is the path with **no card at all**.
Microsoft's *Azure for Students* offer (part of the pack) gives you US$100 of credit, 750
hours a month of a small virtual machine, and 12 months of free storage, with student
verification instead of a credit card. 750 hours covers a machine that runs all month, so
the web UI, the 16:30 run and the demo account all stay up for US$0. Allow 30 minutes,
most of it waiting.

What you get: `https://<ip>.sslip.io` (or a domain; the pack also includes a free one) with
the **Try the demo** button, open registration, daily alerts and HTTPS.

**The one thing to know up front.** The free machine, `B2ats v2`, has 2 CPUs but only 1 GB
of memory. dse-alerts runs on it with a swap file, which the setup script adds, and the
nightly feature build simply takes longer than on your PC. The web UI is unaffected.

## 1. Redeem Azure for Students (5 minutes)

1. Open [education.github.com/pack](https://education.github.com/pack), find **Microsoft
   Azure** and press *Get access*. It takes you to the Azure for Students sign-up and shows
   you a verification code; copy it.
2. Sign in with a Microsoft account (any; your Gmail address works as one), fill in the
   form, and at the verification step choose **Verification code** and paste the code from
   GitHub. No card is asked for.
3. You land in the Azure portal with a subscription called *Azure for Students*.

## 2. Create the machine (10 minutes)

[portal.azure.com](https://portal.azure.com) > **Virtual machines** > **Create** > *Azure
virtual machine*. Go through the tabs; everything not mentioned stays at its default.

**Basics**

* Resource group: *Create new* > `dse-alerts`.
* Virtual machine name: `dse-alerts`.
* Region: student subscriptions are limited to a short list of regions (ours was UAE North,
  Malaysia West, South Central India, Indonesia Central and Japan West; Singapore was
  refused with "disallowed by Azure"). Pick the closest one the dropdown offers; the list
  is in the portal under *Policy > Assignments > Allowed resource deployment regions*.
* Availability options: *No infrastructure redundancy required*.
* Image: **Ubuntu Server 24.04 LTS - x64 Gen2**.
* Size: *See all sizes* > search `B2ats` > **B2ats_v2** (2 vCPUs, 1 GiB; carries the
  *Free services eligible* label).
* Authentication type: **SSH public key**. Username: `azureuser`. SSH public key source:
  *Generate new key pair*, key pair name `dse-alerts`.
* Public inbound ports: *Allow selected ports* > tick **SSH (22)**, **HTTP (80)** and
  **HTTPS (443)**.

**Disks**: the default 30 GB *Standard SSD* is fine.

**Networking**: defaults (a new virtual network and a public IP are created for you).

**Management**: make sure **Enable auto-shutdown is off**. Azure likes to switch this on,
and a machine that is shut down at 7 pm cannot send alerts.

**Review + create** > **Create**. A dialog offers the private key: **Download private key
and create resource**. Keep the downloaded `dse-alerts.pem`; it is the only way in.

When the deployment finishes, *Go to resource* and copy the **Public IP address**.

## 3. Connect and run the setup script (15 minutes, mostly waiting)

From PowerShell on your PC (replace the IP):

```powershell
ssh -i "$HOME\Downloads\dse-alerts.pem" azureuser@203.0.113.10
```

Windows may refuse the key because its permissions are too open. If so:

```powershell
icacls "$HOME\Downloads\dse-alerts.pem" /inheritance:r /grant:r "$env:USERNAME`:R"
```

On the machine, one command does the rest (Docker, swap, firewall, clone, `.env`, start):

```bash
curl -fsSL https://raw.githubusercontent.com/ZubayerOjhor/dse-alerts/main/scripts/vm-setup.sh | bash
```


The script prints the address it chose, `https://<ip-with-dashes>.sslip.io`. sslip.io is a
public DNS service that maps that name to your IP, so HTTPS works without a domain. The
first boot downloads the fallback archive and builds the feature table; on this machine
that is 20-40 minutes. Follow it with `sudo docker compose logs -f scheduler` and wait for
`demo account #... holdings`.

## 4. Finish

1. Open the address. **Create the first account**; that one is yours.
2. Open a private window and press **Try the demo** to see what visitors get.
3. Email and the AI note: `nano ~/dse-alerts/.env`, fill `SMTP_USER`, `SMTP_PASSWORD`
   (a Gmail app password) and `GROQ_API_KEY`, then
   `cd ~/dse-alerts && sudo docker compose --profile https up -d`.
4. Put the address in the README's **Try it** line.

## What it costs, honestly

| Item | Charge |
|---|---|
| B2ats v2 machine, 24 x 7 | 744 hours a month, inside the 750 free hours |
| 30 GB disk | about US$2.5 a month, from the US$100 credit |
| Static public IP | about US$3.7 a month, from the US$100 credit |

So roughly US$75 of the US$100 credit over the year, and nothing from your pocket. Azure
for Students can be **renewed every 12 months** while you are a student, which resets the
credit. If it is not renewed, the subscription is disabled rather than billed: nothing is
charged without you adding a card, but the machine stops, so move the data out first
(see Backups below).

## Later

* **Update to a new version:** `cd ~/dse-alerts && git pull && sudo docker compose --profile https up -d --build`.
* **A real domain for free:** the pack includes a one-year `.me` from Namecheap (and others
  from Name.com and .TECH). Point an A record at the public IP, set
  `DOMAIN=alerts.yourname.me` and `APP_PUBLIC_URL=https://alerts.yourname.me` in `.env`,
  restart as above. Caddy fetches the certificate.
* **History before 2023:** download the curated archive on your PC (README), then copy the
  finished database up once:
  `scp -i <key> data/market.duckdb azureuser@<ip>:/tmp/ && ssh -i <key> azureuser@<ip> "sudo docker cp /tmp/market.duckdb dse-alerts-web-1:/data/market.duckdb && cd dse-alerts && sudo docker compose --profile https restart"`.
* **Memory.** Left alone, DuckDB takes 80% of the machine's memory and the feature build
  peaks near 2 GB. The setup script therefore sets `DSE_DUCKDB_MEMORY=300MB` and
  `DSE_DUCKDB_THREADS=2` in `.env` on machines under 3 GB, so DuckDB spills to disk
  instead (measured: the build then peaks at about 450 MB resident, under 20 seconds). If the
  scheduler log ever shows `Killed`, add swap (`sudo fallocate -l 4G /swapfile2 && sudo chmod 600 /swapfile2 && sudo mkswap /swapfile2 && sudo swapon /swapfile2`) and restart. Do not go below `300MB`; DuckDB cannot finish the build under that.
* **Backups:** everything lives in the `dse-alerts_dse-data` Docker volume. Copy `app.sqlite`
  out of it occasionally: `sudo docker cp dse-alerts-web-1:/data/app.sqlite ./app-backup.sqlite`.
