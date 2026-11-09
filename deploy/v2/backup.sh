#!/usr/bin/env bash
# Nightly Postgres dump to ~/backups, keeping the 7 newest. Run from the repo root by cron.
# The dump holds subscriber webhooks ENCRYPTED (useless without JOBHUNT_SECRET_KEY), but treat it as private.
set -euo pipefail
umask 077

DEST="${BACKUP_DIR:-$HOME/backups}"
KEEP=7
mkdir -p "$DEST"

# Read DATABASE_URL from .env without sourcing it (values may contain shell metacharacters).
# Prints the URL with the password removed, then "P" + the decoded password: the password goes to
# pg_dump via PGPASSWORD (environment), never argv, where `ps` would show it to other local users.
PARTS="$(.venv/bin/python -c "
from urllib.parse import unquote, urlsplit
from dotenv import dotenv_values
url = dotenv_values('.env').get('DATABASE_URL') or ''
if url:
    u = urlsplit(url)
    userinfo, _, host = u.netloc.rpartition('@')
    print(u._replace(netloc=(userinfo.partition(':')[0] + '@' if userinfo else '') + host).geturl())
    print('P' + unquote(u.password or ''))
")"
URL="${PARTS%%$'\n'*}"
[ -n "$URL" ] || { echo "$(date -u +%FT%TZ) DATABASE_URL missing in .env" >&2; exit 1; }
PGPASSWORD="${PARTS#*$'\n'P}"
export PGPASSWORD

STAMP="$(date -u +%Y%m%d-%H%M%S)"
OUT="$DEST/jobhunt-$STAMP.dump"
# pg_dump refuses to run against a newer server than itself, so a version mismatch fails loudly here.
if ! pg_dump --format=custom --no-owner --no-privileges --file="$OUT.tmp" "$URL" || [ ! -s "$OUT.tmp" ]; then
  rm -f "$OUT.tmp"
  echo "$(date -u +%FT%TZ) backup FAILED" >&2
  exit 1
fi
mv "$OUT.tmp" "$OUT"

# shellcheck disable=SC2012  # names are ours: jobhunt-<stamp>.dump
ls -1t "$DEST"/jobhunt-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --
echo "$(date -u +%FT%TZ) backup ok: $(basename "$OUT")"
