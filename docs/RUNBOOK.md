# jobhunt v2 runbook: first deploy and operations

v2 shares nothing with v1: not its VM, not its database, not its Discord webhook. Do not copy
v1's `.env`, `jobhunt.db` or webhook into any step below. Total running cost: $0.

You need: a GitHub-connected Vercel Hobby account, a Supabase account (free), an Oracle Cloud
account (Always Free), a private Discord channel of your own for admin heartbeats, and this repo
checked out on your Mac with `.venv` set up (`pip install -e '.[dev]'`).

## 1. Generate the secret key (Mac)

```
cd ~/jobhunt-v2 && .venv/bin/jobhunt keygen
```

The bare `jobhunt` command is not on PATH; either use `.venv/bin/jobhunt` as above or run
`source .venv/bin/activate` first.

Save the output in your password manager. This one value goes into Vercel AND the worker `.env`.
Both must hold the same key, or stored webhooks cannot be decrypted. Never commit it.

## 2. Supabase (database)

1. Create a free project. Region: Canada (Central), the closest to Toronto. Set a database
   password; if it has reserved characters (`& # ? @ /` or a space), percent-encode them in URLs.
2. Click **Connect** at the top of the dashboard and copy two strings:
   - **Transaction pooler** (port 6543) for Vercel. Username looks like `postgres.PROJECT_REF`.
   - **Session pooler** (port 5432) for the worker. It supports IPv4; the direct connection
     (`db.PROJECT_REF.supabase.co`) is IPv6-only on the free plan and the Oracle VM is IPv4.
3. Create the schema once, from the Mac, with the session pooler URL:

```
read -s "PW?DB password: "    # zsh (Mac); the password stays out of history and this transcript
export DATABASE_URL="postgresql://postgres.PROJECT_REF:${PW}@POOLER_HOST:5432/postgres"
.venv/bin/jobhunt db init; unset PW DATABASE_URL
```

Replace `PROJECT_REF` and `POOLER_HOST` with your values, and remove any `[YOUR-PASSWORD]` brackets
Supabase shows: type only the password at the prompt. Percent-encode reserved characters first.

Expect `Schema ready: backend=postgres, version=2`. Re-running is harmless. A free Supabase
project pauses after 7 days of inactivity; the worker scans every 30 minutes, so it stays awake.

## 3. Vercel (website)

1. Import the GitHub repo `avi-kwok/jobhunt-v2` as a new project (framework preset: Other). Vercel
   detects `app.py`.
2. Environment variables (Production):
   - `DATABASE_URL` = the transaction pooler URL (6543)
   - `JOBHUNT_SECRET_KEY` = the key from step 1
   - `PUBLIC_BASE_URL` = `https://placeholder.vercel.app` (fixed in the next sub-step). Set its
     type to **Config**, not Secret: Vercel rejects Secret for `PUBLIC_`-prefixed names.
3. Deploy. Note the assigned `https://<name>.vercel.app` URL.
4. Set `PUBLIC_BASE_URL` to that URL (no trailing slash) and **redeploy**. Env var changes only
   apply to new deployments.
5. Check `https://<name>.vercel.app/healthz` returns `ok`.

## 4. Oracle (worker)

1. Create a **new** Always Free instance: shape VM.Standard.A1.Flex, 1 OCPU, 6 GB RAM, image
   Ubuntu 24.04 (or 22.04), region near Toronto, a public IP. Keep it isolated from any other
   instance (e.g. v1's):
   - Networking: **Create new virtual cloud network** and **Create new public subnet** (Always
     Free allows 2 VCNs). Sharing an existing VCN shares its security list, so tightening SSH
     here would change the other instance too.
   - SSH keys: **Generate a key pair for me**, download the private key, then on the Mac:
     `mv ~/Downloads/ssh-key-YYYY-MM-DD.key ~/.ssh/jobhunt-v2.key && chmod 600 ~/.ssh/jobhunt-v2.key`.
     Add a passphrase (`ssh-keygen -p -f ~/.ssh/jobhunt-v2.key`) and store it in the Keychain
     (`ssh-add --apple-use-keychain ~/.ssh/jobhunt-v2.key`). Use a new key, not another box's.

   If creation fails with **Out of host capacity**, retry later (different hours, or another
   availability domain). It is a free-tier capacity limit, not a config error.
2. Harden before first login (the VM holds DATABASE_URL and JOBHUNT_SECRET_KEY, which decrypt
   every subscriber's webhook):
   - **Instance metadata service: Version 2 only** (Details or Management tab, or Actions → Edit →
     advanced options). Blocks a redirect-based SSRF from reaching the metadata endpoint.
   - **SSH from your IP only:** Networking → subnet → Security → the new VCN's security list →
     edit the port 22 ingress rule's source from `0.0.0.0/0` to `<your IP>/32` (`curl ifconfig.me`).
     Add no other ingress rules; the worker only makes outbound requests. If your IP changes
     (new network), update this rule from the console.
   - **Verify the host key on first connect** (matters on untrusted networks): Actions → Console
     history → Capture, find `BEGIN SSH HOST KEY FINGERPRINTS`, and type `yes` at the SSH prompt
     only if its `SHA256:` fingerprint is in that list.
3. `ssh -i ~/.ssh/jobhunt-v2.key ubuntu@<public-ip>`, then confirm password logins are off
   (`sudo sshd -T | grep -i passwordauthentication` → `passwordauthentication no`), enable
   automatic security updates and reboots, and keep Ubuntu's default iptables rules (do not flush
   them or run anything that listens on a port):

```
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
echo 'Unattended-Upgrade::Automatic-Reboot "true";' | sudo tee /etc/apt/apt.conf.d/52jobhunt-reboot
```

   Then run the setup script:

```
curl -fsSL https://raw.githubusercontent.com/avi-kwok/jobhunt-v2/main/deploy/v2/worker-setup.sh | bash
```

   It installs Python 3.11+, the PGDG `postgresql-client`, clones the repo to `~/jobhunt-v2`,
   builds `.venv`, creates `~/jobhunt-v2/.env` (mode 600) if missing, and installs the crontab.
   Re-run it any time to update code; it never overwrites `.env`.
4. Fill `~/jobhunt-v2/.env` (`nano`):
   - `DATABASE_URL` = the **session pooler** URL (5432)
   - `JOBHUNT_SECRET_KEY` = the key from step 1, **byte-identical** to Vercel's (no stray
     newline or space). A mismatch aborts the scan; run `jobhunt doctor` (next step) first.
   - `ADMIN_WEBHOOK_URL` = a webhook for your private admin channel
5. Check:

```
cd ~/jobhunt-v2 && .venv/bin/jobhunt doctor
```

   All rows must say PASS (exit code 0). The `company endpoints` row confirms the tracked
   `companies.resolved.yaml` (ATS slugs for every company) came down with the clone; without it
   the scan fetches nothing.
6. Make pg_dump compatible. The client major version must be >= the server's:

```
psql "$(grep '^DATABASE_URL=' .env | cut -d= -f2-)" -c 'SHOW server_version;'
pg_dump --version
```

   If the server major is higher, re-run the setup script with `PG_CLIENT_VERSION=<server major>`.
7. Record today's postings without alerting anyone (so no backlog is sent to subscribers):

```
.venv/bin/jobhunt scan --seed --no-jitter
```

8. Confirm cron: `crontab -l` shows a block between `# BEGIN jobhunt-v2` and `# END jobhunt-v2`:
   scan every 30 min, heartbeat daily 16:00 UTC, backup daily 09:00 UTC.
   After 30 minutes `tail ~/jobhunt-v2/logs/worker.log` shows a scan.

## 5. Subscribe your own channel

Open the site, paste a webhook for a channel you control (not the admin one), submit. A message
with a private manage link appears in that channel. Open it and press **Activate**.

## 6. Smoke checks

- `https://<name>.vercel.app/healthz` is `ok`.
- The manage link only works from the channel message; resubmitting the same webhook issues a new
  link and kills the old one.
- On the worker: `.venv/bin/jobhunt subscriber list` shows your subscriber as `active` (masked id
  only, never the URL).
- Within a day or two of new postings you receive alerts; `.venv/bin/jobhunt heartbeat --no-notify`
  prints counts and a green status.
- The 16:00 UTC heartbeat arrives in the admin channel. Silence means the worker is down.
- `bash deploy/v2/backup.sh` (from `~/jobhunt-v2`) creates a file in `~/backups`.

## 7. Operations

- **Logs** (on the worker, in `~/jobhunt-v2/logs/`): `worker.log` (scans), `heartbeat.log`,
  `backup.log`. They are gitignored and redacted, but treat them as private. Web logs: Vercel
  dashboard. Manage tokens appear in `GET /manage?t=` request logs, so treat those as sensitive.
- **Pause the worker**: `crontab -l | sed '/^# BEGIN jobhunt-v2/,/^# END jobhunt-v2/{/^[*0-9]/s/^/#PAUSED /;}' | crontab -`
  comments the three job lines (markers stay). Re-running `worker-setup.sh` restores them. A running scan finishes
  (or is killed by `timeout 1800`).
- **Retention**: each scan deletes postings first seen and last seen more than 90 days ago, and
  `pending` subscribers (never activated) created more than 7 days ago. Active, paused and
  disabled subscribers are never expired.
- **Update code**: re-run `worker-setup.sh` on the worker; Vercel redeploys on every push to `main`.
- **Rotate `JOBHUNT_SECRET_KEY`**: generate a new one, set it in Vercel (redeploy) and the worker
  `.env`. Stored webhooks are encrypted with the old key and become unreadable: the worker disables
  those subscribers, and every subscriber must resubscribe on the site. Only rotate if the key leaked.
- **Scan exits 3 on every run with one subscriber**: a wrong key and a lone corrupt row look the
  same, and delivery stays blocked. Run `.venv/bin/jobhunt doctor`; if the key is fine, run
  `.venv/bin/jobhunt subscriber remove ID` for the bad row (see `subscriber list`).
- **Remove a subscriber**: `.venv/bin/jobhunt subscriber remove ID`.
- **Restore from backup** (`~/backups/jobhunt-<UTC stamp>.dump`, 7 newest kept). Use the session
  pooler URL, from a client whose major version is >= the server's:

```
read -rsp 'DB password: ' PW; echo    # bash (VM); keeps the password out of history and `ps`
PGPASSWORD="$PW" pg_restore --clean --if-exists --no-owner --no-privileges -d "postgresql://postgres.PROJECT_REF@POOLER_HOST:5432/postgres" ~/backups/jobhunt-YYYYMMDD-HHMMSS.dump
unset PW
.venv/bin/jobhunt doctor
```

  A restore needs the same `JOBHUNT_SECRET_KEY` that was in use when the dump was taken, or the
  restored webhooks are unreadable. Copy a dump off the VM occasionally (`scp`); the VM holds the only copies.
