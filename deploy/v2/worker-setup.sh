#!/usr/bin/env bash
# jobhunt v2 worker bootstrap for a NEW Oracle Always Free VM.Standard.A1.Flex
# (Ubuntu 22.04 or 24.04, aarch64). Run as the normal login user (needs sudo):
#
#   curl -fsSL https://raw.githubusercontent.com/avi-kwok/jobhunt-v2/main/deploy/v2/worker-setup.sh | bash
#   (or, after cloning:  bash ~/jobhunt-v2/deploy/v2/worker-setup.sh)
#
# Idempotent: re-running pulls the latest code, re-installs deps and replaces the
# jobhunt-v2 crontab block. It never overwrites an existing ~/jobhunt-v2/.env.
# Optional env: PG_CLIENT_VERSION (default 17), REPO_URL, JOBHUNT_HOME.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/avi-kwok/jobhunt-v2}"
JOBHUNT_HOME="${JOBHUNT_HOME:-$HOME/jobhunt-v2}"
PG_CLIENT_VERSION="${PG_CLIENT_VERSION:-17}"
CRON_BEGIN="# BEGIN jobhunt-v2 (managed by deploy/v2/worker-setup.sh; edits here are overwritten)"
CRON_END="# END jobhunt-v2"

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n[warn] %s\n' "$*" >&2; }
die()  { printf '\n[error] %s\n' "$*" >&2; exit 1; }

# --- crontab rendering (pure functions; tested from tests/test_worker_setup.py) -------------

# render_cron_block HOME_DIR -> the managed block, markers included.
render_cron_block() {
  local home="$1"
  printf '%s\n' "$CRON_BEGIN"
  printf '%s\n' "*/30 * * * * cd $home && flock -n logs/scan.lock timeout 1800 .venv/bin/jobhunt scan >> logs/worker.log 2>&1"
  printf '%s\n' "0 16 * * * cd $home && .venv/bin/jobhunt heartbeat >> logs/heartbeat.log 2>&1"
  printf '%s\n' "0 9 * * * cd $home && bash deploy/v2/backup.sh >> logs/backup.log 2>&1"
  printf '%s\n' "$CRON_END"
}

# merge_cron HOME_DIR < existing_crontab > new_crontab
# Drops any previous managed block (and nothing else), then appends a fresh one.
merge_cron() {
  local home="$1"
  awk -v b="$CRON_BEGIN" -v e="$CRON_END" '
    $0 == b { skip = 1; next }
    skip && $0 == e { skip = 0; next }
    !skip { print }
  '
  render_cron_block "$home"
}

# --- steps ----------------------------------------------------------------------------------

find_python() {  # prints the first interpreter that is >= 3.11
  local c
  for c in python3.13 python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

install_packages() {
  log "Base packages"
  sudo apt-get update -y
  sudo apt-get install -y git curl ca-certificates gnupg lsb-release cron util-linux coreutils \
    software-properties-common postgresql-common
  sudo systemctl enable --now cron >/dev/null 2>&1 || warn "could not enable cron service"

  log "Python >= 3.11"
  if ! find_python >/dev/null; then
    # Ubuntu 22.04 ships 3.10: use deadsnakes. 24.04 ships 3.12 and never gets here.
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt-get update -y
    sudo apt-get install -y python3.11 python3.11-venv
  fi
  PY="$(find_python)" || die "no Python >= 3.11 available"
  local ver
  ver="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  sudo apt-get install -y "python${ver}-venv" || sudo apt-get install -y python3-venv
  echo "using $PY ($ver)"

  log "PostgreSQL client $PG_CLIENT_VERSION from the PGDG apt repo"
  # Official PGDG helper: adds the apt.postgresql.org repo and key. Safe to re-run.
  sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
  sudo apt-get install -y "postgresql-client-${PG_CLIENT_VERSION}"
}

ensure_utc() {
  if [ "$(timedatectl show -p Timezone --value 2>/dev/null || echo UTC)" != "UTC" ]; then
    log "Setting system timezone to UTC (cron times below are UTC)"
    sudo timedatectl set-timezone UTC
  fi
}

sync_repo() {
  log "Code in $JOBHUNT_HOME"
  if [ -d "$JOBHUNT_HOME/.git" ]; then
    git -C "$JOBHUNT_HOME" pull --ff-only
  elif [ -e "$JOBHUNT_HOME" ] && [ -n "$(ls -A "$JOBHUNT_HOME" 2>/dev/null)" ]; then
    die "$JOBHUNT_HOME exists and is not a git checkout"
  else
    git clone "$REPO_URL" "$JOBHUNT_HOME"
  fi
}

install_venv() {
  log "Virtualenv + dependencies"
  cd "$JOBHUNT_HOME"
  [ -x .venv/bin/python ] || "$PY" -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -e .
}

ensure_env_file() {
  log ".env"
  cd "$JOBHUNT_HOME"
  mkdir -p logs "$HOME/backups"
  chmod 700 logs "$HOME/backups"  # private even if they already existed
  if [ -e .env ]; then
    chmod 600 .env
    echo ".env exists: left untouched (permissions set to 600)"
    return
  fi
  ( umask 077
    cat > .env <<'ENVEOF'
# jobhunt v2 worker. Fill every value, then run: .venv/bin/jobhunt doctor
# Supabase SESSION pooler URL (port 5432). The direct host can be IPv6-only; this VM is IPv4.
DATABASE_URL=
# Must equal the key the Vercel web app uses. Generate once with: .venv/bin/jobhunt keygen
JOBHUNT_SECRET_KEY=
# Operator-only Discord channel for the daily heartbeat. Never a subscriber webhook.
ADMIN_WEBHOOK_URL=
# Optional: contact address sent in the scraper User-Agent.
JOBHUNT_CONTACT=
ENVEOF
  )
  chmod 600 .env
  echo "created .env from template: fill it in"
}

install_cron() {
  log "Crontab (UTC)"
  local current
  current="$(crontab -l 2>/dev/null || true)"
  { [ -z "$current" ] || printf '%s\n' "$current"; } | merge_cron "$JOBHUNT_HOME" | crontab -
  crontab -l | sed -n "/^# BEGIN jobhunt-v2/,/^# END jobhunt-v2/p"
}

main() {
  [ "$(id -u)" -ne 0 ] || die "run as a normal user with sudo, not root"
  [ "$(uname -s)" = "Linux" ] || die "Linux only"
  [ "$(uname -m)" = "aarch64" ] || warn "expected aarch64 (A1.Flex), found $(uname -m)"
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${VERSION_ID:-}" in 22.04|24.04) ;; *) warn "tested on Ubuntu 22.04/24.04, found ${PRETTY_NAME:-unknown}" ;; esac

  install_packages
  ensure_utc
  sync_repo
  install_venv
  ensure_env_file
  install_cron

  cat <<EOF2

Done. Next:
  1. nano $JOBHUNT_HOME/.env     (fill all values)
  2. cd $JOBHUNT_HOME && .venv/bin/jobhunt doctor
  3. .venv/bin/jobhunt scan --seed       (once, before real subscribers)
  4. Check the DB server version, then confirm the client is not older:
       psql "\$DATABASE_URL" -c 'SHOW server_version;'
       pg_dump --version
     If the server major is above $PG_CLIENT_VERSION, re-run with PG_CLIENT_VERSION=<that major>.
EOF2
}

if [ "${BASH_SOURCE[0]}" = "$0" ] || [ -z "${BASH_SOURCE[0]:-}" ]; then
  main "$@"
fi
