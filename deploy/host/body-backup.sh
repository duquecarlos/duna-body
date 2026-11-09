#!/usr/bin/env bash
# body-backup.sh -- Body's off-host backup (host-deploy-v1-amendment-sensitive-data rule 3,
# BODY-HOSTING-PLAN-1 step 8b). Called by duna-deploy-body backup [--dry-run]; runs as root, the
# snapshot itself as duna-body via runuser. Encrypts to the configured age public key, uploads to
# R2 bucket duna-respaldo under body/, then deletes every local plaintext and encrypted copy.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "body-backup.sh: refused -- must run as root (EUID 0)" >&2
  exit 1
fi

PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

REPO_DIR="/srv/duna/repos/duna-body"
ENV_FILE="/srv/duna/env/body.env"
DATA_USER="duna-body"
BODY_HOME_DIR="/srv/duna/data/body"
TMP_DIR="$BODY_HOME_DIR/backup-tmp"

DRY_RUN=""
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN="1"
  shift
fi

if [[ $# -ne 0 ]]; then
  echo "body-backup.sh: unknown argument: $1" >&2
  exit 2
fi

run() {
  if [[ -n "$DRY_RUN" ]]; then
    printf '%s\n' "$*"
  else
    "$@"
  fi
}

if ! command -v age >/dev/null 2>&1; then
  echo "body-backup.sh: age is missing: run once on the host: sudo apt install age" >&2
  exit 1
fi
if ! command -v curl >/dev/null 2>&1; then
  echo "body-backup.sh: curl is missing: run once on the host: sudo apt install curl" >&2
  exit 1
fi

# Reads exactly these five names from $ENV_FILE by parsing KEY=value lines -- never `source`s or
# `cat`s the file, and never prints a value, so a secret never reaches stdout/stderr or any log.
read_env_var() {
  local name="$1"
  local line value
  line="$(grep -E "^${name}=" "$ENV_FILE" 2>/dev/null | tail -n1)"
  value="${line#*=}"
  if [[ "$value" == \"*\" ]]; then
    value="${value#\"}"
    value="${value%\"}"
  elif [[ "$value" == \'*\' ]]; then
    value="${value#\'}"
    value="${value%\'}"
  fi
  printf '%s' "$value"
}

recipient="$(read_env_var BODY_BACKUP_AGE_RECIPIENT)"
access_key="$(read_env_var BODY_R2_ACCESS_KEY_ID)"
secret_key="$(read_env_var BODY_R2_SECRET_ACCESS_KEY)"
endpoint="$(read_env_var BODY_R2_ENDPOINT)"
bucket="$(read_env_var BODY_R2_BUCKET)"

if [[ -z "$recipient" ]]; then
  echo "body-backup.sh: BODY_BACKUP_AGE_RECIPIENT is missing from $ENV_FILE" >&2
  exit 1
fi
if [[ -z "$access_key" || -z "$secret_key" || -z "$endpoint" || -z "$bucket" ]]; then
  echo "body-backup.sh: one or more BODY_R2_* variables are missing from $ENV_FILE" >&2
  exit 1
fi

run mkdir -p "$TMP_DIR"
run chown "$DATA_USER:$DATA_USER" "$TMP_DIR"
run chmod 0700 "$TMP_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
plain_file="$TMP_DIR/body-$timestamp.sqlite"
enc_file="$plain_file.age"
object_key="body/body-$timestamp.sqlite.age"
url="$endpoint/$bucket/$object_key"

# Always runs on exit, success or failure, so the plaintext copy never survives this script --
# the "deletes the plaintext immediately after encrypting" rule, enforced even on a crash.
cleanup_plain() {
  rm -f "$plain_file" "$enc_file"
}
trap cleanup_plain EXIT

run runuser -u "$DATA_USER" -- env -C "$REPO_DIR" "BODY_HOME=$BODY_HOME_DIR" \
  "$REPO_DIR/.venv/bin/python" -m body.backup snapshot --out "$plain_file"

run age -r "$recipient" -o "$enc_file" "$plain_file"
run rm -f "$plain_file"

# The secret never becomes an argv string: curl reads it from stdin via -K -, fed by a bash
# builtin (printf), so it never appears in this or any other process's command line.
if [[ -n "$DRY_RUN" ]]; then
  echo "printf 'user = \"<access-key>:<secret-key>\"\\n' | curl -sS --fail --aws-sigv4 \"aws:amz:auto:s3\" -K - -T \"$enc_file\" \"$url\""
  object_size="<dry-run>"
else
  printf 'user = "%s:%s"\n' "$access_key" "$secret_key" \
    | curl -sS --fail --aws-sigv4 "aws:amz:auto:s3" -K - -T "$enc_file" "$url"
  object_size="$(stat -c %s "$enc_file")"
fi

run rm -f "$enc_file"

echo "$object_key size=$object_size"
