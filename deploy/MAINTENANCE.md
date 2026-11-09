# jobhunt — VM Maintenance Command List

Deployment: Oracle Cloud Always-Free ARM VM (`instance-20260126-1433`, Toronto),
Ubuntu 22.04. jobhunt runs via **cron** from `~/jobhunt` on the box.

> **Golden rules**
> - Always run `jobhunt` **from `~/jobhunt`** (it reads `config.yaml` from the current dir). Cron does this for you.
> - The box's `jobhunt.db` / `.env` / `.jobhunt_usage.db` are the **live state** — never overwrite them from your Mac.
> - **Never Stop the instance** in the OCI console (it just sits dead + risks idle reclamation).

---

## 1. Connect
```bash
ssh -i ~/PATH_TO_KEY.key USER@BOX_IP
```
If SSH times out, the public IP may have changed — grab the current one from **OCI Console → Compute → Instances**. (Only happens if the instance was stopped/started.)

## 2. Is it healthy? (run on the box)
```bash
crontab -l                                   # should show BOTH: # jobhunt-scan  and  # jobhunt-heartbeat
tail -n 40 ~/jobhunt/logs/cron.log           # recent scan output
tail -f  ~/jobhunt/logs/cron.log             # live-watch a scan (Ctrl-C to stop)
pgrep -fl "jobhunt scan"                      # is a scan running right now?
cd ~/jobhunt && .venv/bin/jobhunt heartbeat --no-notify   # status snapshot (🟢/🔴) without posting
cd ~/jobhunt && .venv/bin/jobhunt budget                  # API spend vs caps
cd ~/jobhunt && .venv/bin/jobhunt security-check          # key/.env protections
```

## 3. Run things manually (on the box — `cd ~/jobhunt` first)
```bash
cd ~/jobhunt
.venv/bin/jobhunt scan --force               # scan now, ignoring quiet hours (~15-20 min, sends alerts)
.venv/bin/jobhunt scan --force --no-notify   # same, but no Discord (safe test)
.venv/bin/jobhunt heartbeat                   # post a heartbeat to Discord right now
.venv/bin/jobhunt list-matches                # show matches recorded in the seen-store
.venv/bin/jobhunt discover "Company" --url <careers-url>   # resolve/add a company
```

## 4. Update the code (careful — protect the box's live state)
**On your Mac:**
```bash
rsync -av -e "ssh -i ~/PATH_TO_KEY.key" \
  --exclude .venv --exclude __pycache__ --exclude '*.pyc' \
  --exclude jobhunt.db --exclude .jobhunt_usage.db --exclude .env --exclude logs \
  ~/jobhunt/ USER@BOX_IP:~/jobhunt/
```
**Then on the box:**
```bash
cd ~/jobhunt && bash deploy/setup.sh          # idempotent: reinstalls deps, adds any new cron lines
```
(Editable install = new `src/` code is live immediately; `setup.sh` is only needed to pick up new cron entries.)

## 5. OS / VM upkeep (on the box, occasionally)
```bash
sudo apt update && sudo apt upgrade -y        # security patches (do ~monthly)
sudo reboot                                    # apply kernel updates; cron auto-resumes on boot
df -h /                                         # disk usage (plenty of room at ~45 GB)
du -sh ~/jobhunt/logs/*                          # log sizes (trim if ever large)
systemctl status cron                            # confirm cron daemon is running
```

## 6. Cron schedule
```bash
crontab -l          # view
crontab -e          # edit (e.g. change heartbeat cadence)
```
Current lines:
- `0,30 * * * *  ... jobhunt scan ...  # jobhunt-scan`         → scan every :00 and :30
- `0 16 * * *    ... jobhunt heartbeat ... # jobhunt-heartbeat` → daily status ~9am Pacific (16:00 UTC)

## 7. Troubleshooting
| Symptom | Fix |
|---|---|
| Heartbeat shows 🔴 / stops arriving | Box or cron is down. `systemctl status cron`; check instance is **Running** in OCI console; `crontab -l`. |
| A scan seems stuck | `pgrep -fl "jobhunt scan"` → check age `ps -o etime= -p <pid>` → `kill <pid>` if hours old. (flock+timeout 1800 should auto-prevent this.) |
| Scans never fire | Confirm `crontab -l` has the scan line; `tail logs/cron.log`; instance Running. |
| Stale lock blocks scans | Only if NO scan is running: `rm -f /tmp/jobhunt.lock` |
| No Discord alerts | Check `.env` has `JOBHUNT_SECRET_KEY` and `jobhunt subscriber list` shows active subscribers (disabled ones have a reason); run `jobhunt heartbeat` to test `ADMIN_WEBHOOK_URL`; grep `logs/cron.log` for notify errors. |
| "No such command" | The box is on old code — re-run the §4 rsync + setup.sh. |
| `config.yaml not found` | You're not in `~/jobhunt` — `cd ~/jobhunt` first. |

## 8. Oracle console (browser)
- **Compute → Instances** → the instance should read **Running**. Don't Stop it.
- After any stop/start, copy the **new public IP** for SSH.
- Optional safety net: **Billing → Budgets** → set a **$1 alert** (Always-Free should never bill; the alert catches an accidental paid resource).

## Quick facts
- Scan: every :00/:30 · Heartbeat: daily 16:00 UTC (~9am PT).
- Quiet hours: 21:00–05:00 **Pacific** (scans self-skip; `--force` overrides). App is timezone-aware; the box runs UTC.
- API spend is hard-capped: **$0.50/day, $5/month** (self-stops).
- Seen-store (`jobhunt.db`) = the box's memory; it prevents re-alerting. Guard it.
