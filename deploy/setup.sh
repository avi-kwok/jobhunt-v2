#!/usr/bin/env bash
set -euo pipefail

# jobhunt Linux bootstrap.
# Run from inside the jobhunt directory:  cd ~/jobhunt && bash deploy/setup.sh
# Idempotent: safe to re-run.

# Python floor from pyproject.toml requires-python (>=3.11).
PY_MAJOR=3
PY_MINOR=11

JOBHUNT_DIR="$(pwd)"

log()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\n\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\n\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# --- Detect package manager ------------------------------------------------
PM=""
if command -v apt-get >/dev/null 2>&1; then
  PM="apt"
elif command -v dnf >/dev/null 2>&1; then
  PM="dnf"
else
  err "No supported package manager found (need apt-get or dnf)."
  exit 1
fi
log "Package manager: $PM"

pm_update_done=0
pm_install() {
  # pm_install <pkg> [pkg...]
  if [ "$PM" = "apt" ]; then
    if [ "$pm_update_done" -eq 0 ]; then
      sudo apt-get update -y
      pm_update_done=1
    fi
    sudo apt-get install -y "$@"
  else
    sudo dnf install -y "$@"
  fi
}

# --- Base tools: git, curl, flock, timeout ---------------------------------
log "Ensuring base tools (git, curl, flock, timeout)..."
command -v git     >/dev/null 2>&1 || pm_install git
command -v curl    >/dev/null 2>&1 || pm_install curl
command -v flock   >/dev/null 2>&1 || pm_install util-linux
command -v timeout >/dev/null 2>&1 || pm_install coreutils

# --- Find or install a Python >= 3.11 --------------------------------------
# Returns success if the given interpreter meets the floor.
py_ok() {
  local py="$1"
  command -v "$py" >/dev/null 2>&1 || return 1
  "$py" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($PY_MAJOR, $PY_MINOR) else 1)" >/dev/null 2>&1
}

# Confirm venv + pip usable for the chosen interpreter.
py_has_venv() {
  local py="$1"
  "$py" -c "import venv, ensurepip" >/dev/null 2>&1
}

PYTHON=""

log "Looking for a Python >= ${PY_MAJOR}.${PY_MINOR} interpreter..."
for cand in python3.13 python3.12 python3.11 python3; do
  if py_ok "$cand"; then
    PYTHON="$(command -v "$cand")"
    log "Found compatible interpreter: $PYTHON"
    break
  fi
done

if [ -z "$PYTHON" ]; then
  log "No compatible Python found; attempting to install 3.11..."
  if [ "$PM" = "apt" ]; then
    if ! pm_install python3.11 python3.11-venv; then
      warn "Direct install failed; adding deadsnakes PPA and retrying..."
      pm_install software-properties-common
      sudo add-apt-repository -y ppa:deadsnakes/ppa
      sudo apt-get update -y
      pm_install python3.11 python3.11-venv
    fi
  else
    pm_install python3.11
  fi
  if py_ok python3.11; then
    PYTHON="$(command -v python3.11)"
  fi
fi

if [ -z "$PYTHON" ] || ! py_ok "$PYTHON"; then
  err "Could not find or install a Python >= ${PY_MAJOR}.${PY_MINOR}. Install it manually and re-run."
  exit 1
fi

# Ensure venv + pip support for the chosen interpreter.
if ! py_has_venv "$PYTHON"; then
  log "Installing venv/pip support for $PYTHON..."
  if [ "$PM" = "apt" ]; then
    # Try version-specific venv package (e.g. python3.11-venv), then generic.
    pyver="$("$PYTHON" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    pm_install "python${pyver}-venv" || pm_install python3-venv
  else
    pm_install python3-pip
  fi
fi

if ! py_has_venv "$PYTHON"; then
  err "$PYTHON is missing venv/ensurepip support and it could not be installed. Fix this and re-run."
  exit 1
fi

log "Using Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# --- Create venv and install project ---------------------------------------
if [ ! -d ".venv" ]; then
  log "Creating virtualenv at .venv..."
  "$PYTHON" -m venv .venv
else
  log "Reusing existing .venv"
fi

log "Upgrading pip and installing jobhunt (editable)..."
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .

mkdir -p logs

# --- Secrets check ----------------------------------------------------------
if [ -f "./.env" ]; then
  chmod 600 .env
  if ! grep -q 'JOBHUNT_SECRET_KEY' .env || ! grep -q 'ADMIN_WEBHOOK_URL' .env; then
    warn ".env is missing JOBHUNT_SECRET_KEY and/or ADMIN_WEBHOOK_URL. Scans may fail or not notify."
  fi
else
  warn "No .env file found. Create one with JOBHUNT_SECRET_KEY and ADMIN_WEBHOOK_URL before scanning."
fi

# --- SAFETY GUARD: refuse to install cron without a seen-store --------------
if [ ! -f "./jobhunt.db" ]; then
  printf '\n'
  printf '\033[1;31m############################################################\033[0m\n'
  printf '\033[1;31m# SEEN-STORE MISSING — CRON NOT INSTALLED                  #\033[0m\n'
  printf '\033[1;31m############################################################\033[0m\n'
  warn "./jobhunt.db was not found in this directory."
  cat <<'EOF'

The seen-store (jobhunt.db) tracks which postings have already been seen.
Without it, the FIRST scan would treat EVERY current posting as brand new and
flood your Discord channel with hundreds of alerts.

The venv is set up, but cron was NOT installed. To proceed safely, do ONE of:

  1. Copy your existing jobhunt.db into this directory (recommended if you
     already ran jobhunt on another machine):
        scp your-mac:~/jobhunt/jobhunt.db ~/jobhunt/

  OR

  2. Seed the store on this box (records current postings as seen, sends NO
     alerts):
        .venv/bin/jobhunt scan --seed

Then re-run this script:
        cd ~/jobhunt && bash deploy/setup.sh
EOF
  exit 0
fi

# --- Install cron entry (idempotent) ---------------------------------------
CRON_MARKER="# jobhunt-scan"
CRON_LINE="0,30 * * * * cd ${JOBHUNT_DIR} && flock -n /tmp/jobhunt.lock timeout 1800 ${JOBHUNT_DIR}/.venv/bin/jobhunt scan >> ${JOBHUNT_DIR}/logs/cron.log 2>&1  ${CRON_MARKER}"

existing_cron="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$existing_cron" | grep -qF "$CRON_MARKER"; then
  log "Cron entry already present; leaving it unchanged."
else
  log "Installing cron entry (runs every :00 and :30)..."
  {
    printf '%s\n' "$existing_cron" | sed '/^$/d'
    printf '%s\n' "$CRON_LINE"
  } | crontab -
  log "Cron entry installed."
fi

# --- Install heartbeat cron entry (idempotent) -----------------------------
# Once daily at 16:00 UTC (~9am Pacific). Posts a liveness/status summary to the
# same Discord webhook so that SILENCE becomes meaningful. Preserve all existing
# crontab lines (incl. the scan line above); only add ours if it's not present.
HEARTBEAT_MARKER="# jobhunt-heartbeat"
HEARTBEAT_LINE="0 16 * * * cd ${JOBHUNT_DIR} && ${JOBHUNT_DIR}/.venv/bin/jobhunt heartbeat >> ${JOBHUNT_DIR}/logs/heartbeat.log 2>&1  ${HEARTBEAT_MARKER}"

existing_cron="$(crontab -l 2>/dev/null || true)"
if printf '%s\n' "$existing_cron" | grep -qF "$HEARTBEAT_MARKER"; then
  log "Heartbeat cron entry already present; leaving it unchanged."
else
  log "Installing heartbeat cron entry (daily at 16:00 UTC ~ 9am Pacific)..."
  {
    printf '%s\n' "$existing_cron" | sed '/^$/d'
    printf '%s\n' "$HEARTBEAT_LINE"
  } | crontab -
  log "Heartbeat cron entry installed."
fi

# --- Done -------------------------------------------------------------------
cat <<EOF

============================================================
Done — next steps
============================================================
  * Confirm the cron entry:
        crontab -l

  * Watch scans as they run (after the next :00 / :30):
        tail -f ${JOBHUNT_DIR}/logs/cron.log

  * Run a one-off test WITHOUT sending any alerts:
        ${JOBHUNT_DIR}/.venv/bin/jobhunt scan --force --no-notify

  * Heartbeat: a daily 16:00 UTC (~9am Pacific) liveness/status post to Discord.
    If it stops arriving, the scanner is broken (not merely "no new jobs").
        Preview it (console only, no post):
            ${JOBHUNT_DIR}/.venv/bin/jobhunt heartbeat --no-notify
        Post one right now:
            ${JOBHUNT_DIR}/.venv/bin/jobhunt heartbeat

  * REMINDER: disable the old macOS launchd agent on your Mac so the two
    machines don't double-scan the same boards. Find its real label first:
        ls ~/Library/LaunchAgents | grep jobhunt
        launchctl bootout gui/\$(id -u)/<THAT_LABEL>
============================================================
EOF
