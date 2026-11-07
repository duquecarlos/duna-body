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
UNITS=(body-web.service body-pull.service body-pull.timer body-vida.service body-vida.timer)

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

install_caddy_site() {
  run install -o root -g root -m 644 "$CADDY_SRC" "$CADDY_DST"
}

rollback_and_fail() {
  local step="$1"
  runuser -u "$owner" -- git -C "$REPO_DIR" reset --hard "$before_sha"
  install_units
  install_caddy_site
  run systemctl daemon-reload
  run systemctl restart body-web.service
  fail_step "$step" "yes"
}

runuser -u "$owner" -- python3 -m venv "$REPO_DIR/.venv"
runuser -u "$owner" -- "$REPO_DIR/.venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt" \
  || rollback_and_fail "pip install"

install_units
install_caddy_site

run systemctl daemon-reload
run systemctl enable body-web.service body-pull.timer body-vida.timer
run systemctl restart body-web.service
run systemctl reload caddy

sleep 1
if ! curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
  rollback_and_fail "health check"
fi

echo "body-deploy.sh: deployed $(runuser -u "$owner" -- git -C "$REPO_DIR" rev-parse HEAD)"
exit 0
