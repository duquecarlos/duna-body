#!/usr/bin/env bash
# body-deploy.sh -- Body's product deploy script, execed by duna-deploy-body after the pull.
# Runs as root (inherited from the entry script); every git/pip/venv call runs as the checkout
# owner through runuser, never as root.
set -Eeuo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "body-deploy.sh: refused -- must run as root (EUID 0)" >&2
  exit 1
fi

PATH="/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

REPO_DIR="/srv/duna/repos/duna-body"
UNIT_SRC_DIR="$REPO_DIR/deploy/host"
UNIT_DST_DIR="/etc/systemd/system"
CADDY_SRC="$REPO_DIR/deploy/host/Caddyfile.body"
CADDY_DST="/etc/caddy/sites/body.caddy"
HEALTH_URL="http://127.0.0.1:8770/healthz"
DATA_USER="duna-body"
# /srv/duna itself is duna:duna mode 750 (BODY-DEPLOY-HOST-1-INV), so duna-body cannot even
# traverse into it -- walked up from REPO_DIR, never a second literal.
PARENT_DIR="$(dirname "$(dirname "$REPO_DIR")")"
UNITS=(body-web.service body-pull.service body-pull.timer body-backfill.service body-backfill.timer body-backup.service body-backup.timer)
TIMERS=(body-pull.timer body-backfill.timer body-backup.timer)
RETIRED_UNITS=(body-vida.service body-vida.timer)

# duna-deploy-body sources this file to read UNITS/TIMERS as the single list of Body's live
# units (BODY-DEPLOY-HOST-1); stop here so sourcing it never runs the deploy itself.
if [[ "${BASH_SOURCE[0]}" != "${0}" ]]; then
  return 0
fi

DRY_RUN=""

run() {
  if [[ -n "$DRY_RUN" ]]; then
    printf '%s\n' "$*"
  else
    "$@"
  fi
}

before_sha=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --before-sha)
      before_sha="${2:-}"
      shift 2
      ;;
    *)
      echo "body-deploy.sh: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ ! "$before_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "body-deploy.sh: --before-sha must be 40 hex characters, got: $before_sha" >&2
  exit 2
fi

if [[ ! -d "$REPO_DIR" ]]; then
  echo "body-deploy.sh: repo not found at $REPO_DIR" >&2
  exit 1
fi
owner="$(stat -c %U "$REPO_DIR")"

fail_step() {
  echo "body-deploy.sh: FAILED at step: $1 (service restarted: $2)" >&2
  exit 1
}

install_units() {
  for unit in "${UNITS[@]}"; do
    run install -o root -g root -m 644 "$UNIT_SRC_DIR/$unit" "$UNIT_DST_DIR/$unit"
  done
}

remove_retired_units() {
  # BODY-DISPOSITIVO-1: the weekly vida-v1 export moved from a host timer to the per-person
  # API /api/vida/semana -- a leftover unit from before this deploy is stopped, disabled and
  # removed, never left running against the wrong person.
  for unit in "${RETIRED_UNITS[@]}"; do
    if [[ -f "$UNIT_DST_DIR/$unit" ]]; then
      run systemctl stop "$unit"
      run systemctl disable "$unit"
      run rm -f "$UNIT_DST_DIR/$unit"
    fi
  done
}

CADDY_BAK="${CADDY_DST}.bak"

install_caddy_site_if_changed() {
  if [[ -f "$CADDY_DST" ]] && cmp -s "$CADDY_SRC" "$CADDY_DST"; then
    return 0
  fi

  local had_backup="no"
  if [[ -f "$CADDY_DST" ]]; then
    run cp "$CADDY_DST" "$CADDY_BAK"
    had_backup="yes"
  fi

  run install -o root -g root -m 644 "$CADDY_SRC" "$CADDY_DST"

  if ! run caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile; then
    if [[ "$had_backup" == "yes" ]]; then
      run mv "$CADDY_BAK" "$CADDY_DST"
    else
      run rm -f "$CADDY_DST"
    fi
    fail_step "caddy validate" "no"
  fi

  run systemctl reload caddy
}

ensure_traverse_acl() {
  if runuser -u "$DATA_USER" -- test -x "$PARENT_DIR"; then
    return 0
  fi
  if ! command -v setfacl >/dev/null 2>&1; then
    echo "body-deploy.sh: $DATA_USER cannot traverse $PARENT_DIR and setfacl is not installed -- run once on the host: setfacl -m u:$DATA_USER:x $PARENT_DIR" >&2
    exit 1
  fi
  run setfacl -m "u:$DATA_USER:x" "$PARENT_DIR"
  if ! runuser -u "$DATA_USER" -- test -x "$PARENT_DIR"; then
    echo "body-deploy.sh: $DATA_USER still cannot traverse $PARENT_DIR -- run by hand: setfacl -m u:$DATA_USER:x $PARENT_DIR" >&2
    exit 1
  fi
}

rollback_and_fail() {
  local step="$1"
  local deps_ok="yes"
  runuser -u "$owner" -- git -C "$REPO_DIR" reset --hard "$before_sha"
  if ! runuser -u "$owner" -- "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"; then
    deps_ok="no"
  fi
  ensure_traverse_acl
  install_units
  install_caddy_site_if_changed
  run systemctl daemon-reload
  run systemctl restart body-web.service
  run systemctl restart "${TIMERS[@]}"
  fail_step "$step (rollback dependencies reinstalled: $deps_ok)" "yes"
}

if ! run runuser -u "$owner" -- python3 -c "import ensurepip, venv"; then
  echo "body-deploy.sh: python3-venv is missing: run once on the host: sudo apt install python3-venv" >&2
  exit 1
fi

runuser -u "$owner" -- python3 -m venv "$REPO_DIR/.venv"
runuser -u "$owner" -- "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt" \
  || rollback_and_fail "pip install"

ensure_traverse_acl

install_units
remove_retired_units
install_caddy_site_if_changed

run systemctl daemon-reload
run systemctl enable body-web.service
run systemctl enable --now "${TIMERS[@]}"
run systemctl restart body-web.service

health_ok=""
for attempt in $(seq 1 20); do
  code="$(curl -fsS -o /dev/null -w '%{http_code}' "$HEALTH_URL" 2>/dev/null || true)"
  if [[ "$code" == "200" ]]; then
    health_ok="1"
    break
  fi
  sleep 1
done
if [[ -z "$health_ok" ]]; then
  rollback_and_fail "health check"
fi

echo "body-deploy.sh: deployed $(runuser -u "$owner" -- git -C "$REPO_DIR" rev-parse HEAD)"
exit 0
